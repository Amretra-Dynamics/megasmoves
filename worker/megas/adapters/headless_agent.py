from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import AsyncIterator

from .base import (
    WorkerEvent,
    BaseAgentAdapter,
    prefer_console_free_tool_path,
    windows_subprocess_kwargs,
)
from .. import __version__ as WORKER_VERSION
from ..context import write_megas_context
from ..evidence.git import collect_git_snapshot
from ..policy import redact, resolve_allowed_workdir
from ..terminal_parse import (
    StreamLineBuffer,
    detect_completion_in_text,
    extract_assistant_answer,
)


MAX_SNAPSHOT_FILES = 5_000
MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024
# Per-file cap so one accidental untracked installer/binary cannot fail the session.
MAX_UNTRACKED_FILE_BYTES = 2 * 1024 * 1024
_SKIP_UNTRACKED_DIR_PARTS = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        "release",
        "scratch",
        ".megas",
        ".megasmoves",
        "__pycache__",
        ".git",
        "target",
        ".next",
        "coverage",
    }
)
_SKIP_UNTRACKED_SUFFIXES = (
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".apk",
    ".aab",
    ".dmg",
    ".msi",
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".gz",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".mp4",
    ".mov",
    ".wav",
    ".mp3",
    ".pdf",
    ".sqlite3",
    ".db",
)


def _spawn_evidence(argv: list[str], cwd: Path, pid: int | None) -> dict[str, object]:
    """Return safe, verifiable metadata for the exact provider spawn.

    The prompt is deliberately redacted, but flags and their ordering remain
    visible.  This is evidence for runtime acceptance tests, not user-facing
    chat content.
    """
    displayed = [str(item) for item in argv]
    if displayed:
        displayed[-1] = "<redacted-prompt>"
    executable = Path(str(argv[0])).expanduser() if argv else Path("")
    try:
        executable = executable.resolve()
    except OSError:
        pass
    metadata: dict[str, object] = {
        "argv_redacted": displayed,
        "argv0": str(argv[0]) if argv else "",
        "cwd": str(cwd),
        "pid": pid,
    }
    try:
        if executable.is_file():
            metadata["resolved_executable"] = str(executable)
            metadata["binary_size"] = executable.stat().st_size
            digest = hashlib.sha256()
            with executable.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            metadata["binary_sha256"] = digest.hexdigest()
    except OSError:
        # Evidence collection must never prevent the provider from starting.
        pass
    return metadata


def _should_skip_untracked(relative: PurePosixPath, size: int) -> bool:
    """Ignore build/scratch/binary noise when sandboxing a dirty worktree."""
    if size > MAX_UNTRACKED_FILE_BYTES:
        return True
    parts_lower = {part.lower() for part in relative.parts}
    if parts_lower & _SKIP_UNTRACKED_DIR_PARTS:
        return True
    name = relative.name.lower()
    return any(name.endswith(suffix) for suffix in _SKIP_UNTRACKED_SUFFIXES)


async def _capture(command: list[str], workdir: Path, *, timeout: float = 30) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=prefer_console_free_tool_path(),
        **windows_subprocess_kwargs(new_group=False),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise TimeoutError(f"Repository preparation timed out: {' '.join(command[:2])}") from exc
    if process.returncode != 0:
        detail = redact(stderr.decode(errors="replace").strip())[:500]
        raise RuntimeError(detail or f"Repository command failed: {' '.join(command[:2])}")
    return stdout


async def _repo_root(workdir: Path) -> Path | None:
    try:
        output = await _capture(["git", "rev-parse", "--show-toplevel"], workdir, timeout=10)
        root = Path(output.decode("utf-8", errors="strict").strip()).resolve()
        workdir.relative_to(root)
        await _capture(["git", "rev-parse", "--verify", "HEAD"], root, timeout=10)
        return root
    except Exception:
        return None


async def _copy_working_state(repo_root: Path, sandbox_root: Path) -> None:
    patch = await _capture(["git", "diff", "--binary", "HEAD", "--"], repo_root, timeout=30)
    if len(patch) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Repository diff exceeds the 50 MB agent snapshot limit.")
    if patch:
        process = await asyncio.create_subprocess_exec(
            "git",
            "apply",
            "--binary",
            "--whitespace=nowarn",
            cwd=str(sandbox_root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=prefer_console_free_tool_path(),
            **windows_subprocess_kwargs(new_group=False),
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(input=patch), timeout=30)
        _ = stdout
        if process.returncode != 0:
            raise RuntimeError(redact(stderr.decode(errors="replace").strip())[:500] or "Agent snapshot patch failed.")

    untracked = await _capture(["git", "ls-files", "--others", "--exclude-standard", "-z"], repo_root, timeout=20)
    paths = [item for item in untracked.split(b"\x00") if item]
    if len(paths) > MAX_SNAPSHOT_FILES:
        raise ValueError("Repository has too many untracked files for an agent snapshot.")
    total = len(patch)
    for raw_path in paths:
        relative_text = raw_path.decode("utf-8", errors="strict")
        relative = PurePosixPath(relative_text.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or any(part.lower() == ".git" for part in relative.parts):
            raise ValueError("Repository contains an unsafe untracked path.")
        source = (repo_root / Path(*relative.parts)).resolve()
        try:
            source.relative_to(repo_root)
        except ValueError as exc:
            raise ValueError("Repository contains an unsafe untracked path.") from exc
        if source.is_symlink() or not source.is_file():
            continue
        try:
            size = source.stat().st_size
        except OSError:
            continue
        if _should_skip_untracked(relative, size):
            continue
        total += size
        if total > MAX_SNAPSHOT_BYTES:
            raise ValueError("Repository snapshot exceeds the 50 MB limit.")
        target = sandbox_root / Path(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


class ReadOnlyHeadlessAgentAdapter(BaseAgentAdapter):
    name = "headless_agent"
    adapter_id = "headless_agent"
    contract_version = "1.0"
    status = "discovered"
    supported_range = ">=0.0.0"
    binary_env = ""
    default_binary = ""
    timeout_env = "MEGAS_AGENT_TIMEOUT_SEC"

    def __init__(self, allowed_folders: list[str]) -> None:
        super().__init__(allowed_folders)

    def supports(self, command: dict) -> bool:
        context_pack = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        agent_type = str(context_pack.get("agent_type") or command.get("agent_type") or command.get("command_type") or "")
        for suffix in ("_w-", "_w_", "_worker_"):
            if suffix in agent_type:
                agent_type = agent_type.split(suffix, 1)[0]
                break
        return agent_type == self.name

    def prepare_context(self, context_pack: dict, workspace: Path, command: dict | None = None) -> Path:
        payload = dict(command or {})
        payload["context_pack"] = context_pack
        return write_megas_context(workspace, payload)

    def process_args(self, prompt: str, context_pack: dict) -> list[str]:
        raise NotImplementedError

    def stream_event(self, stream: str, line: str) -> WorkerEvent:
        """Map one complete CLI line to the Megas structured event protocol.

        Plain-text agents retain terminal fidelity. JSON-capable adapters
        override this to expose assistant/tool/reasoning lifecycle events.
        """
        return WorkerEvent(
            "terminal_stdout" if stream == "stdout" else "terminal_stderr",
            {"text": line, "adapter": self.name, "agent_type": self.name},
        )

    def instruction_text(self, command: dict, context_pack: dict) -> str:
        return str(
            command.get("instruction")
            or context_pack.get("instruction")
            or "Inspect this repository and summarize its current changes."
        ).strip()

    def is_conversational_ping(self, command: dict, context_pack: dict) -> bool:
        """True for short chat like "hi" — must not force full-repo plan/inspect.

        Interactive ``agy`` answers greetings in seconds. Megas was wrapping "hi"
        in a monorepo worktree + plan-mode + MEGAS.md essay, which made the model
        hang until print-timeout (~3 min). Light path keeps latency honest.
        """
        instruction = self.instruction_text(command, context_pack)
        if not instruction or len(instruction) > 96:
            return False
        lower = instruction.lower().strip()
        # Coding / inspect intent → full sandbox path.
        heavy = (
            "fix", "file", "repo", "implement", "bug", "error", "git", "read ",
            "write", "create", "delete", "refactor", "test", "build", "deploy",
            "megas", "diff", "commit", "pr ", "pull request", "function", "class ",
            "/", "\\", ".py", ".ts", ".kt", ".go", ".rs",
        )
        if any(token in lower for token in heavy):
            return False
        bare = lower.rstrip("!?. ")
        pings = {
            "hi", "hello", "hey", "yo", "sup", "ping", "ok", "okay", "thanks",
            "thank you", "good morning", "good evening", "good afternoon", "hola",
            "howdy", "greetings", "test", "testing",
        }
        if bare in pings:
            return True
        # Very short free text with no code-ish tokens.
        return len(bare.split()) <= 4 and not any(ch.isdigit() for ch in bare)

    def is_agent_remote_chat(self, command: dict, context_pack: dict) -> bool:
        """Connected Agents remote chat (phone/web) — NOT home Moves context.

        Product planes (only two):
          - agent_remote_chat: user ↔ OpenCode/Codex/… on a Worker (native chat)
          - home_context: Moves Context tab orchestration (MEGAS.md / packages)

        There is no product "light chat" vs "hard chat". Legacy wire flag
        ``megas_light_chat`` only means skip home-context inspect scaffolding.
        """
        chat_mode = str(context_pack.get("chat_mode") or "").strip().lower()
        if chat_mode in {"agent_chat", "conversational", "chat", "true", "1"}:
            return True
        # Legacy flag still used on the wire for agent plane.
        if bool(context_pack.get("megas_light_chat")):
            return True
        source = str(
            context_pack.get("source") or command.get("source") or ""
        ).strip().lower()
        if source in {
            "android_agent_chat",
            "web_agent_chat",
            "agent_chat",
            "agent_chat_follow_up",
            "android_thread_continue",
            "web_thread_continue",
            "native_thread_continue",
            "thread_continue",
        }:
            return True
        return self.is_conversational_ping(command, context_pack)

    def prompt(self, command: dict, context_pack: dict) -> str:
        instruction = self.instruction_text(command, context_pack)
        # Agent remote chat: pure user message. Home context: inspect scaffolding.
        if self.is_agent_remote_chat(command, context_pack):
            return instruction
        return (
            f"{instruction}\n\n"
            "Read MEGAS.md for task context and constraints. Work only in this disposable repository snapshot. "
            "Do not modify files, create commits, push, change credentials, or operate outside the repository. "
            "Return a concise evidence-based result."
        )

    async def launch(self, command: dict) -> AsyncIterator[WorkerEvent]:
        context_pack = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        if context_pack.get("write_mode") is True:
            raise PermissionError(f"{self.name} write mode is not certified by Megas.")
        session_id = str(command.get("session_id") or "")
        command_id = str(command.get("command_id") or "")
        workdir = resolve_allowed_workdir(command.get("workdir") or context_pack.get("workdir"), self.allowed_folders)
        # Product plane — not light/hard.
        agent_remote_chat = self.is_agent_remote_chat(command, context_pack)
        # Agent remote chat: no full repo snapshot. Home context: worktree when git.
        repo_root = None if agent_remote_chat else await _repo_root(workdir)
        host_before = await collect_git_snapshot(repo_root) if repo_root else {"is_git_repo": False}
        # Prefer a scratch dir under the allowed host workdir so OpenCode/native
        # session cwd never becomes /tmp/megas-* (that path was synced as thr_
        # project_path and broke every follow-up with "Workspace is not allowed").
        temporary: tempfile.TemporaryDirectory | None = None
        sandbox_root: Path
        host_scratch = (workdir / ".megas" / "agent-chat").resolve()
        try:
            host_scratch.mkdir(parents=True, exist_ok=True)
            sandbox_root = Path(
                tempfile.mkdtemp(prefix=f"megas-{self.name}-", dir=str(host_scratch))
            ).resolve()
        except Exception:
            temporary = tempfile.TemporaryDirectory(prefix=f"megas-{self.name}-")
            sandbox_root = Path(temporary.name).resolve()
        worktree_added = False
        process: asyncio.subprocess.Process | None = None
        readers: list[asyncio.Task] = []
        started = time.time()
        output_tail: list[dict[str, str]] = []
        try:
            if agent_remote_chat or not repo_root:
                sandbox_workdir = sandbox_root
                sandbox_workdir.mkdir(parents=True, exist_ok=True)
                if agent_remote_chat:
                    megas_md = sandbox_workdir / "MEGAS.md"
                    megas_md.write_text(
                        "# Megas agent chat\n"
                        "Connected Agents remote chat — pure user message, no home Context package.\n",
                        encoding="utf-8",
                    )
                else:
                    megas_md = self.prepare_context(context_pack, sandbox_workdir, command)
            else:
                relative_workdir = workdir.relative_to(repo_root)
                try:
                    await _capture(
                        ["git", "worktree", "add", "--detach", str(sandbox_root), "HEAD"],
                        repo_root,
                        timeout=30,
                    )
                    worktree_added = True
                    await _copy_working_state(repo_root, sandbox_root)
                    sandbox_workdir = (sandbox_root / relative_workdir).resolve()
                    megas_md = self.prepare_context(context_pack, sandbox_workdir, command)
                except ValueError as snap_exc:
                    # 50 MB / too-many-untracked — fall back to scratch sandbox.
                    msg = str(snap_exc)
                    print(f"[SNAPSHOT] {self.name}: {msg}; falling back to scratch sandbox", flush=True)
                    if worktree_added:
                        try:
                            await _capture(
                                ["git", "worktree", "remove", "--force", str(sandbox_root)],
                                repo_root,
                                timeout=15,
                            )
                        except Exception:
                            pass
                        worktree_added = False
                    sandbox_workdir = sandbox_root
                    sandbox_workdir.mkdir(parents=True, exist_ok=True)
                    megas_md = self.prepare_context(context_pack, sandbox_workdir, command)
                    yield WorkerEvent(
                        "terminal_stdout",
                        {
                            "text": f"[megas] {msg} Using scratch sandbox (no full repo snapshot).\n",
                            "adapter": self.name,
                            "agent_type": self.name,
                            "phase": "snapshot_fallback",
                        },
                    )
            prompt = self.prompt(command, context_pack)
            # Wire-compat: keep megas_light_chat true for agent plane only as skip-scaffold.
            context_pack = {
                **context_pack,
                "megas_light_chat": agent_remote_chat,
                "chat_mode": (
                    str(context_pack.get("chat_mode") or "").strip()
                    or ("agent_chat" if agent_remote_chat else "home_context")
                ),
            }
            if os.getenv("MEGAS_AGENT_DRY_RUN", "").lower() in {"1", "true", "yes"}:
                args = [sys.executable, "-c", "import sys; print(sys.argv[1])", f"{self.name} dry run"]
            else:
                args = self.process_args(prompt, context_pack)
            # One timeout policy for the coding agent — agent remote chat is NOT
            # a shorter second-class "light" path. Free OpenRouter/NIM need room.
            model_wire = str(
                context_pack.get("model")
                or command.get("model")
                or ""
            ).strip().lower()
            free_remote = (
                "openrouter" in model_wire
                or model_wire.endswith(":free")
                or "/free" in model_wire
                or model_wire.startswith("nvidia/")
            )
            if free_remote:
                default_timeout = "240"
            elif context_pack.get("write_mode") is True:
                default_timeout = "300"
            else:
                default_timeout = "180"
            try:
                timeout_sec = float(
                    command.get("timeout_sec")
                    or context_pack.get("timeout_sec")
                    or os.getenv(self.timeout_env, default_timeout)
                )
            except (TypeError, ValueError):
                timeout_sec = float(default_timeout)
            # Same caps for agent remote chat and home context inspect.
            cap = 300.0 if free_remote else 240.0
            timeout_sec = min(timeout_sec, cap)
            timeout_sec = min(max(timeout_sec, 1.0), 3600.0)

            yield WorkerEvent(
                "worker_context_written",
                {
                    "path": str(megas_md),
                    "workdir": str(sandbox_workdir),
                    "adapter": self.name,
                    "agent_remote_chat": agent_remote_chat,
                    "product_plane": "agent_chat" if agent_remote_chat else "home_context",
                },
            )
            environment = dict(os.environ)
            environment["NO_COLOR"] = "1"
            # Force UTF-8 so child agents can output emoji/non-ASCII without
            # crashing on Windows charmap (cp1252) consoles.
            environment["PYTHONIOENCODING"] = "utf-8"
            environment = self.sanitize_environment(environment)
            # Log spawn surface so residual console flashes can be attributed.
            print(
                f"[SPAWN] no-console adapter={self.name} argv0={args[0] if args else '?'} "
                f"workdir={sandbox_workdir} plane="
                f"{'agent_chat' if agent_remote_chat else 'home_context'}"
            )
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(sandbox_workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name != "nt",
                env=environment,
                **windows_subprocess_kwargs(new_group=True),
            )
            assert process.stdout is not None and process.stderr is not None
            spawn_metadata = _spawn_evidence(args, sandbox_workdir, process.pid)
            yield WorkerEvent(
                "command_started",
                {
                    "command_id": command_id,
                    # Preserve the exact provider argv shape while redacting
                    # only the user prompt. The old placeholder command made
                    # runtime resume verification impossible.
                    "command": spawn_metadata["argv_redacted"],
                    "workdir": str(workdir),
                    "spawn_cwd": str(sandbox_workdir),
                    "session_id": session_id,
                    "adapter": self.name,
                    "agent_type": self.name,
                    "agent_remote_chat": agent_remote_chat,
                    "product_plane": "agent_chat" if agent_remote_chat else "home_context",
                    "provider_session_id_requested": str(
                        context_pack.get("provider_session_id")
                        or context_pack.get("native_thread_id")
                        or ""
                    ).strip(),
                    "worker_version": WORKER_VERSION,
                    **spawn_metadata,
                },
            )
            # Immediate lifecycle signal so phone/web are not silent while the
            # model thinks.  This is not provider output and must never enter
            # terminal_result/chat prose.
            wait_hint = (
                f"[{self.name}] Running… waiting for model."
                if agent_remote_chat
                else (
                    f"[{self.name}] Running… waiting for model "
                    f"(home Context inspect)."
                )
            )
            yield WorkerEvent(
                "agent_activity",
                {
                    "message": wait_hint,
                    "status": "waiting_for_model",
                    "adapter": self.name,
                    "agent_type": self.name,
                    "phase": "waiting_for_model",
                    "agent_remote_chat": agent_remote_chat,
                    "product_plane": "agent_chat" if agent_remote_chat else "home_context",
                },
            )
            queue: asyncio.Queue[WorkerEvent | None] = asyncio.Queue(maxsize=256)
            # Line buffers prevent completion markers from splitting across 4KB reads
            # (Aider/Claude/agy print-mode all emit line-oriented agent text).
            line_buffers = {
                "stdout": StreamLineBuffer(),
                "stderr": StreamLineBuffer(),
            }

            async def stream(name: str, reader: asyncio.StreamReader) -> None:
                total_bytes = 0
                max_bytes = 1024 * 1024
                truncated = False
                buf = line_buffers[name]
                try:
                    while chunk := await reader.read(4096):
                        total_bytes += len(chunk)
                        if total_bytes > max_bytes:
                            if not truncated:
                                truncated = True
                                for line in buf.flush():
                                    output_tail.append({"stream": name, "line": line[-4096:]})
                                    del output_tail[:-80]
                                    await queue.put(self.stream_event(name, line))
                                marker = "[TRUNCATED - OUTPUT LIMIT EXCEEDED]\n"
                                output_tail.append({"stream": name, "line": marker})
                                del output_tail[:-80]
                                await queue.put(self.stream_event(name, marker))
                            # Keep draining. Stopping reads fills the OS pipe and
                            # deadlocks every provider after large output.
                            continue
                        text = self.redact_output(chunk.decode(errors="replace"))
                        for line in buf.feed(text):
                            output_tail.append({"stream": name, "line": line[-4096:]})
                            del output_tail[:-80]
                            await queue.put(self.stream_event(name, line))
                    for line in buf.flush():
                        output_tail.append({"stream": name, "line": line[-4096:]})
                        del output_tail[:-80]
                        await queue.put(self.stream_event(name, line))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                finally:
                    await queue.put(None)

            readers = [
                asyncio.create_task(stream("stdout", process.stdout)),
                asyncio.create_task(stream("stderr", process.stderr)),
            ]
            closed = 0
            announced_exit: int | None = None
            process_exit_seen_at: float | None = None
            expected_provider_session_id = str(
                context_pack.get("provider_session_id")
                or context_pack.get("native_thread_id")
                or ""
            ).strip()
            provider_identity_error: str | None = None
            while closed < 2:
                remaining = timeout_sec - (time.time() - started)
                if remaining <= 0:
                    raise asyncio.TimeoutError
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=min(remaining, 0.5))
                except asyncio.TimeoutError:
                    if process.returncode is not None:
                        if process_exit_seen_at is None:
                            process_exit_seen_at = time.monotonic()
                        elif time.monotonic() - process_exit_seen_at >= 1.0:
                            for reader_task in readers:
                                if not reader_task.done():
                                    reader_task.cancel()
                            break
                    if announced_exit is not None and process.returncode is None:
                        print(
                            f"[SPAWN] {self.name} announced exit={announced_exit} "
                            "but process still open; terminating process tree"
                        )
                        await self._terminate_process_tree(process)
                    continue
                if event is None:
                    closed += 1
                    continue
                text = str(event.payload.get("text") or "") if isinstance(event.payload, dict) else ""
                hit = detect_completion_in_text(text)
                if hit is not None and announced_exit is None:
                    announced_exit = hit.exit_code if hit.exit_code is not None else 0
                yield event
                observed_provider_session_id = str(
                    event.payload.get("provider_session_id")
                    if isinstance(event.payload, dict)
                    else ""
                ).strip()
                if (
                    expected_provider_session_id
                    and observed_provider_session_id
                    and observed_provider_session_id != expected_provider_session_id
                ):
                    provider_identity_error = (
                        f"{self.name} resumed native conversation "
                        f"{observed_provider_session_id!r}, expected "
                        f"{expected_provider_session_id!r}."
                    )
                    yield WorkerEvent(
                        "agent_error",
                        {
                            "adapter": self.name,
                            "agent_type": self.name,
                            "error_code": "provider_session_mismatch",
                            "message": provider_identity_error,
                            "expected_provider_session_id": expected_provider_session_id,
                            "observed_provider_session_id": observed_provider_session_id,
                        },
                    )
                    await self._terminate_process_tree(process)
                    raise RuntimeError(provider_identity_error)
                if announced_exit is not None and process.returncode is None:
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        await self._terminate_process_tree(process)
            await asyncio.gather(*readers, return_exceptions=True)
            exit_code = process.returncode
            if exit_code is None:
                remaining = max(0.1, timeout_sec - (time.time() - started))
                await asyncio.wait_for(process.wait(), timeout=remaining)
                exit_code = process.returncode
            if announced_exit is not None and exit_code not in (0, announced_exit):
                # taskkill often returns 1 after forced kill; prefer announced success.
                if announced_exit == 0:
                    exit_code = 0
            if exit_code is None:
                exit_code = announced_exit if announced_exit is not None else 1
            host_after = await collect_git_snapshot(repo_root) if repo_root else {"is_git_repo": False}
            # Read-only adapters execute in a detached worktree sandbox. The host
            # tree can change concurrently (IDE save, another commit, smoke
            # tests). Failing the whole session for external host noise leaves
            # phone users stuck and used to take the Worker offline mid-run.
            if host_after != host_before:
                before_status = str(host_before.get("status_short") or "")
                after_status = str(host_after.get("status_short") or "")
                before_branch = str(host_before.get("branch") or "")
                after_branch = str(host_after.get("branch") or "")
                print(
                    "[WARN] Host repository snapshot changed during isolated "
                    f"{self.name} session (branch {before_branch!r}->{after_branch!r}); "
                    "continuing because the agent ran in a sandbox worktree."
                )
                if before_status != after_status:
                    print(
                        "[WARN] Host git status_short changed externally; "
                        "sandbox isolation still applies."
                    )
            duration_ms = int((time.time() - started) * 1000)
            changed_files = []
            for line in str(host_after.get("status_short") or "").splitlines():
                if line.strip():
                    changed_files.append({"status": line[:2].strip(), "path": line[3:].strip()})
            raw_stdout = "".join(
                str(item.get("line") or "")
                for item in output_tail
                if item.get("stream") == "stdout"
            )
            raw_stderr = "".join(
                str(item.get("line") or "")
                for item in output_tail
                if item.get("stream") == "stderr"
            )
            assistant_answer = extract_assistant_answer(raw_stdout)
            # Vendor model timeouts (agy: "timeout waiting for response") leave empty stdout.
            # Surface a clear phone/chat answer instead of a silent multi-minute hang.
            if not assistant_answer and (
                "timeout waiting for response" in raw_stderr.lower()
                or "timeout waiting for response" in raw_stdout.lower()
            ):
                assistant_answer = (
                    f"{self.name} model timed out waiting for a response "
                    f"(~{max(1, duration_ms // 1000)}s). This is the coding-agent CLI/model "
                    "latency, not Megas offline. Try again, use Codex CLI for quick replies, "
                    "or a shorter inspect instruction."
                )
                yield WorkerEvent(
                    "terminal_stdout",
                    {
                        "text": assistant_answer,
                        "adapter": self.name,
                        "agent_type": self.name,
                        "phase": "model_timeout",
                    },
                )
            yield WorkerEvent(
                "command_finished",
                {
                    "command_id": command_id,
                    "adapter": self.name,
                    "agent_type": self.name,
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    **({"assistant_answer": assistant_answer} if assistant_answer else {}),
                },
            )
            yield WorkerEvent(
                "evidence",
                {
                    "evidence_type": "evidence_git_status",
                    "summary": "Captured host Git status before and after isolated agent execution.",
                    "payload": {
                        "kind": "git_status",
                        "workspace": str(repo_root),
                        "dirty": bool(changed_files),
                        "changed_files": changed_files,
                        **host_after,
                    },
                },
            )
            yield WorkerEvent(
                "evidence",
                {
                    "evidence_type": "evidence_git_diff_stat",
                    "summary": "Captured host Git diff stat.",
                    "payload": host_after,
                },
            )
            yield WorkerEvent(
                "evidence",
                {
                    "evidence_type": "terminal_result",
                    "summary": f"{self.name} exited with code {exit_code}",
                    "payload": {
                        "adapter": self.name,
                        "command": self.default_binary,
                        "workdir": str(workdir),
                        "exit_code": exit_code,
                        "duration_ms": duration_ms,
                        "output_tail": output_tail,
                        "assistant_answer": assistant_answer,
                        "git": host_after,
                        "isolated_snapshot": True,
                    },
                },
            )
        except asyncio.TimeoutError as exc:
            # If the CLI already reported success, recover instead of hard-failing:
            # hangy tools (agy) often print "completed with exit code 0" then stall.
            recovered_exit: int | None = None
            joined = "".join(str(item.get("line") or "") for item in output_tail)
            hit = detect_completion_in_text(joined)
            if hit is not None and hit.kind in {"completed", "process_exit"}:
                recovered_exit = hit.exit_code if hit.exit_code is not None else 0
            if recovered_exit is None:
                raise TimeoutError(f"{self.name} timed out.") from exc
            print(
                f"[SPAWN] {self.name} timed out after completion line "
                f"(exit={recovered_exit}); emitting finish + evidence"
            )
            if process is not None and process.returncode is None:
                await self._terminate_process_tree(process)
            try:
                host_after = await collect_git_snapshot(repo_root) if repo_root else {"is_git_repo": False}
            except Exception:
                host_after = dict(host_before) if isinstance(host_before, dict) else {}
            duration_ms = int((time.time() - started) * 1000)
            changed_files = []
            for line in str(host_after.get("status_short") or "").splitlines():
                if line.strip():
                    changed_files.append({"status": line[:2].strip(), "path": line[3:].strip()})
            yield WorkerEvent(
                "command_finished",
                {
                    "command_id": command_id,
                    "adapter": self.name,
                    "agent_type": self.name,
                    "exit_code": recovered_exit,
                    "duration_ms": duration_ms,
                    "recovered_from_timeout": True,
                },
            )
            yield WorkerEvent(
                "evidence",
                {
                    "evidence_type": "evidence_git_status",
                    "summary": "Captured host Git status after hung CLI recovery.",
                    "payload": {
                        "kind": "git_status",
                        "workspace": str(repo_root),
                        "dirty": bool(changed_files),
                        "changed_files": changed_files,
                        **host_after,
                    },
                },
            )
            yield WorkerEvent(
                "evidence",
                {
                    "evidence_type": "evidence_git_diff_stat",
                    "summary": "Captured host Git diff stat after hung CLI recovery.",
                    "payload": host_after,
                },
            )
            recovered_answer = extract_assistant_answer(joined)
            yield WorkerEvent(
                "evidence",
                {
                    "evidence_type": "terminal_result",
                    "summary": f"{self.name} recovered after hang (exit {recovered_exit})",
                    "payload": {
                        "adapter": self.name,
                        "command": self.default_binary,
                        "workdir": str(workdir),
                        "exit_code": recovered_exit,
                        "duration_ms": duration_ms,
                        "output_tail": output_tail,
                        "assistant_answer": recovered_answer,
                        "git": host_after,
                        "isolated_snapshot": True,
                        "recovered_from_timeout": True,
                    },
                },
            )
        finally:
            if process is not None and process.returncode is None:
                await self._terminate_process_tree(process)
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)
            if worktree_added:
                try:
                    await _capture(["git", "worktree", "remove", "--force", str(sandbox_root)], repo_root, timeout=30)
                except Exception:
                    pass
                try:
                    await _capture(["git", "worktree", "prune"], repo_root, timeout=10)
                except Exception:
                    pass
            if temporary is not None:
                temporary.cleanup()
            else:
                # Host-scratch light-chat dir under allowed workdir.
                try:
                    import shutil

                    if sandbox_root.is_dir() and "megas-" in sandbox_root.name:
                        shutil.rmtree(sandbox_root, ignore_errors=True)
                except Exception:
                    pass

    async def _terminate_process_tree(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                    **windows_subprocess_kwargs(new_group=False),
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=3)
        except (OSError, ProcessLookupError, asyncio.TimeoutError):
            if process.returncode is None:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
                else:
                    process.kill()
                await process.wait()
