from __future__ import annotations

import asyncio
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import AsyncIterator

from megas.adapters.base import (
    WorkerEvent,
    BaseAgentAdapter,
    prefer_console_free_tool_path,
    resolve_cli_argv,
    windows_creationflags,
    windows_subprocess_kwargs,
)
from megas.adapters.headless_agent import _copy_working_state
from megas.adapters.codex_app_server import stream_codex_app_server
from megas.context import write_megas_context
from megas.evidence.git import (
    collect_changed_files,
    collect_git_diff,
    collect_git_snapshot,
    generate_patch,
    get_head_commit,
    validate_changed_paths,
    get_repo_state,
)
from megas.policy import redact, resolve_allowed_workdir
from megas.evidence.verification import (
    is_docs_only,
    build_verification_plan,
    run_verification_pipeline,
)


_CODEX_STREAM_LIMIT_BYTES = 8 * 1024 * 1024
_CODEX_STREAM_CHUNK_BYTES = 64 * 1024
_CODEX_EVENT_TEXT_LIMIT = 128 * 1024
_CODEX_EVENT_VALUE_ITEMS = 512


def _bounded_codex_text(text: str, limit: int = _CODEX_EVENT_TEXT_LIMIT) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n[TRUNCATED - EVENT TEXT LIMIT EXCEEDED]"
    if len(marker) >= limit:
        return marker[-limit:]
    return text[: max(0, limit - len(marker))] + marker


def _bounded_codex_value(
    value: object,
    *,
    redact_text,
    budget: dict[str, int] | None = None,
    depth: int = 0,
) -> object:
    """Redact and bound structured Codex fields before event transport.

    File-change and plan payloads are structured rather than plain text. A
    recursive shared budget prevents those fields from bypassing the same
    secret-redaction and memory limits applied to terminal/tool text.
    """
    if budget is None:
        budget = {"chars": _CODEX_EVENT_TEXT_LIMIT, "items": _CODEX_EVENT_VALUE_ITEMS}
    if depth > 6 or budget["items"] <= 0 or budget["chars"] <= 0:
        return "[TRUNCATED - EVENT VALUE LIMIT EXCEEDED]"
    budget["items"] -= 1
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        redacted = str(redact_text(value))
        limit = min(16 * 1024, budget["chars"])
        bounded = _bounded_codex_text(redacted, limit)
        budget["chars"] -= min(len(redacted), limit)
        return bounded
    if isinstance(value, list):
        result: list[object] = []
        for item in value:
            if budget["items"] <= 0 or budget["chars"] <= 0:
                result.append("[TRUNCATED - EVENT VALUE LIMIT EXCEEDED]")
                break
            result.append(
                _bounded_codex_value(
                    item,
                    redact_text=redact_text,
                    budget=budget,
                    depth=depth + 1,
                )
            )
        return result
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for raw_key, item in value.items():
            if budget["items"] <= 0 or budget["chars"] <= 0:
                result["truncated"] = "[TRUNCATED - EVENT VALUE LIMIT EXCEEDED]"
                break
            key = _bounded_codex_text(str(redact_text(str(raw_key))), 256)
            result[key] = _bounded_codex_value(
                item,
                redact_text=redact_text,
                budget=budget,
                depth=depth + 1,
            )
        return result
    return _bounded_codex_value(
        str(value),
        redact_text=redact_text,
        budget=budget,
        depth=depth + 1,
    )


async def _bounded_stream_lines(
    reader: asyncio.StreamReader,
    *,
    max_bytes: int = _CODEX_STREAM_LIMIT_BYTES,
) -> AsyncIterator[bytes | None]:
    """Yield newline frames without ``StreamReader.readline`` size failures.

    Codex command-completion JSON can contain a large ``aggregated_output`` in a
    single JSONL record. ``readline`` inherits asyncio's roughly 64 KiB line
    limit and raises ``LimitOverrunError`` for that valid record. Read fixed-size
    chunks instead and bound each frame independently. If one frame is too large,
    drain through its newline and resume parsing later lifecycle/final messages.

    ``None`` is a one-time truncation marker. A partial over-limit JSON frame is
    intentionally discarded rather than parsed as misleading terminal state.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    pending = bytearray()
    discarding_frame = False
    while True:
        chunk = await reader.read(_CODEX_STREAM_CHUNK_BYTES)
        if not chunk:
            break
        offset = 0
        while offset < len(chunk):
            if discarding_frame:
                newline = chunk.find(b"\n", offset)
                if newline < 0:
                    # Draining is essential: stopping reads can fill the OS pipe
                    # and deadlock Codex before process.wait().
                    break
                discarding_frame = False
                offset = newline + 1
                continue

            newline = chunk.find(b"\n", offset)
            end = len(chunk) if newline < 0 else newline + 1
            part = chunk[offset:end]
            if len(pending) + len(part) > max_bytes:
                pending.clear()
                yield None
                if newline < 0:
                    discarding_frame = True
                    break
                offset = end
                continue
            pending.extend(part)
            offset = end
            if newline >= 0:
                yield bytes(pending)
                pending.clear()
    if pending and not discarding_frame:
        yield bytes(pending)


def _codex_text(value: object) -> str:
    """Return useful text from the small set of Codex JSONL item shapes."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_codex_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "summary", "content", "message", "query"):
            text = _codex_text(value.get(key))
            if text:
                return text
    return ""


def codex_json_line_to_worker_events(
    line: str,
    *,
    redact_text=lambda value: value,
) -> list[WorkerEvent]:
    """Translate ``codex exec --json`` JSONL into stable Megas activity events.

    The Codex JSONL protocol is richer than a terminal transcript. Preserving its
    item lifecycle lets Android/web render narration, command cards, plans, file
    changes, and failures as first-class chat activity while the raw terminal
    remains an optional fallback.
    """
    try:
        message = json.loads(line)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(message, dict):
        return []

    event_type = str(message.get("type") or "").strip()
    common = {
        "source": "codex_jsonl",
        "codex_event_type": _bounded_codex_text(redact_text(event_type), 256),
    }
    if event_type == "thread.started":
        return [
            WorkerEvent(
                "agent_thread_started",
                {
                    **common,
                    "thread_id": _bounded_codex_text(
                        redact_text(str(message.get("thread_id") or "")), 4096
                    ),
                },
            )
        ]
    if event_type == "turn.started":
        return [WorkerEvent("agent_turn_started", common)]
    if event_type == "turn.completed":
        usage = (
            _bounded_codex_value(message.get("usage"), redact_text=redact_text)
            if isinstance(message.get("usage"), dict)
            else {}
        )
        return [WorkerEvent("agent_turn_completed", {**common, "usage": usage})]
    if event_type == "turn.failed":
        detail = _codex_text(message.get("error")) or _codex_text(message)
        return [
            WorkerEvent(
                "agent_error",
                {
                    **common,
                    "message": _bounded_codex_text(
                        redact_text(detail or "Codex turn failed.")
                    ),
                    "fatal": True,
                },
            )
        ]
    if event_type == "error":
        detail = _codex_text(message.get("error")) or _codex_text(message)
        return [
            WorkerEvent(
                "agent_error",
                {
                    **common,
                    "message": _bounded_codex_text(
                        redact_text(detail or "Codex reported an error.")
                    ),
                    "fatal": False,
                },
            )
        ]
    if not event_type.startswith("item."):
        return []

    item = message.get("item") if isinstance(message.get("item"), dict) else {}
    item_type = _bounded_codex_text(
        redact_text(str(item.get("type") or "activity").strip()), 256
    )
    item_id = _bounded_codex_text(redact_text(str(item.get("id") or "")), 4096)
    status = _bounded_codex_text(
        redact_text(str(item.get("status") or event_type.removeprefix("item."))), 256
    )
    payload = {
        **common,
        "item_id": item_id,
        "item_type": item_type,
        "status": status,
    }

    if item_type == "agent_message":
        text = _bounded_codex_text(
            redact_text(_codex_text(item.get("text") or item.get("content"))),
            256 * 1024,
        )
        if not text:
            return []
        return [
            WorkerEvent(
                "assistant_delta" if event_type == "item.updated" else "assistant_message",
                {**payload, "text": text, "final": event_type == "item.completed"},
            )
        ]

    if item_type == "reasoning":
        text = _bounded_codex_text(
            redact_text(
                _codex_text(item.get("text") or item.get("summary") or item.get("content"))
            )
        )
        if not text:
            return []
        return [WorkerEvent("agent_reasoning", {**payload, "text": text})]

    if item_type == "command_execution":
        command = _bounded_codex_text(
            redact_text(str(item.get("command") or "")), 32 * 1024
        )
        output = _bounded_codex_text(
            redact_text(str(item.get("aggregated_output") or item.get("output") or ""))
        )
        tool_payload = {
            **payload,
            "tool_type": "command",
            "title": "Command execution",
            "command": command,
            "output": output,
            "exit_code": item.get("exit_code"),
        }
        if event_type == "item.started":
            return [WorkerEvent("tool_started", tool_payload)]
        if event_type == "item.updated":
            return [WorkerEvent("tool_output", tool_payload)] if output else []
        return [WorkerEvent("tool_finished", tool_payload)]

    if item_type in {"mcp_tool_call", "web_search"}:
        title = str(item.get("tool") or item.get("name") or "").strip()
        if not title:
            title = "Web search" if item_type == "web_search" else "Tool call"
        tool_payload = {
            **payload,
            "tool_type": item_type,
            "title": _bounded_codex_text(redact_text(title), 4096),
            "input": _bounded_codex_text(
                redact_text(_codex_text(item.get("arguments") or item.get("query")))
            ),
            "output": _bounded_codex_text(
                redact_text(_codex_text(item.get("result") or item.get("output")))
            ),
        }
        if event_type == "item.started":
            return [WorkerEvent("tool_started", tool_payload)]
        if event_type == "item.updated":
            return [WorkerEvent("tool_output", tool_payload)]
        return [WorkerEvent("tool_finished", tool_payload)]

    if item_type in {"file_change", "file_changes"}:
        return [
            WorkerEvent(
                "file_change",
                {
                    **payload,
                    "changes": _bounded_codex_value(
                        item.get("changes") or item.get("files") or [],
                        redact_text=redact_text,
                    ),
                    "summary": _bounded_codex_text(
                        redact_text(_codex_text(item.get("summary") or item.get("text")))
                    ),
                },
            )
        ]

    if item_type in {"plan", "plan_update"}:
        return [
            WorkerEvent(
                "plan_update",
                {
                    **payload,
                    "text": _bounded_codex_text(
                        redact_text(_codex_text(item.get("text") or item.get("plan") or item))
                    ),
                    "steps": _bounded_codex_value(
                        item.get("steps") or [],
                        redact_text=redact_text,
                    ),
                },
            )
        ]

    if item_type == "todo_list":
        return [
            WorkerEvent(
                "plan_update",
                {
                    **payload,
                    "title": "To-do list",
                    "steps": _bounded_codex_value(
                        item.get("items") or [],
                        redact_text=redact_text,
                    ),
                },
            )
        ]

    summary = _bounded_codex_text(redact_text(_codex_text(item)))
    return [WorkerEvent("agent_activity", {**payload, "text": summary})]


async def _hidden_exec(*args: str, cwd: str | Path) -> asyncio.subprocess.Process:
    """Run a short git/tool process without a Windows console popup."""
    return await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=prefer_console_free_tool_path(),
        **windows_subprocess_kwargs(new_group=False),
    )


async def find_git_repo_info(workdir: Path) -> tuple[Path | None, Path | None]:
    curr = workdir.resolve()
    while True:
        if (curr / ".git").exists():
            try:
                rel = workdir.resolve().relative_to(curr)
                return curr, rel
            except ValueError:
                return curr, Path(".")
        if curr.parent == curr:
            break
        curr = curr.parent
    return None, None


def save_patch_proposal_locally(
    patch_id: str, session_id: str, patch_content: str, sha256: str, base_commit: str, expires_at: str, workspace_path: str
) -> None:
    patch_dir = Path.home() / ".megas" / "patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    
    # Save unredacted raw patch file
    patch_file = patch_dir / f"{patch_id}.patch"
    patch_file.write_text(patch_content, encoding="utf-8")
    
    # Save metadata JSON file
    import json
    metadata = {
        "patch_id": patch_id,
        "session_id": session_id,
        "sha256": sha256,
        "base_commit": base_commit,
        "expires_at": expires_at,
        "workspace_path": workspace_path
    }
    metadata_file = patch_dir / f"{patch_id}.json"
    metadata_file.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def delete_patch_proposal_locally(patch_id: str) -> None:
    patch_dir = Path.home() / ".megas" / "patches"
    patch_file = patch_dir / f"{patch_id}.patch"
    metadata_file = patch_dir / f"{patch_id}.json"
    try:
        if patch_file.exists():
            patch_file.unlink()
    except Exception:
        pass
    try:
        if metadata_file.exists():
            metadata_file.unlink()
    except Exception:
        pass


class CodexCliAdapter(BaseAgentAdapter):
    name = "codex_cli"
    adapter_id = "codex_cli"
    contract_version = "1.0"
    status = "discovered"
    supported_range = ">=0.1.0"
    binary_env = "MEGAS_CODEX_BINARY"
    default_binary = "codex"

    def __init__(self, allowed_folders: list[str]) -> None:
        super().__init__(allowed_folders)

    def is_authenticated(self) -> bool:
        if os.getenv("OPENAI_API_KEY") or os.getenv("CODEX_API_KEY"):
            return True
        home = os.path.expanduser("~")
        config_paths = [
            os.path.join(home, ".codex"),
            os.path.join(home, ".config", "codex"),
            os.path.join(home, ".codex.json"),
        ]
        return any(os.path.exists(p) for p in config_paths)

    def supports(self, command: dict) -> bool:
        context_pack = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        agent_type = str(context_pack.get("agent_type") or command.get("agent_type") or "")
        
        base_agent = agent_type
        for suffix in ["_w-", "_w_", "_worker_"]:
            if suffix in agent_type:
                base_agent = agent_type.split(suffix)[0]
                break

        return base_agent == self.name

    def prepare_context(self, context_pack: dict, workspace: Path, command: dict | None = None, repo_state: dict | None = None) -> Path:
        payload = dict(command or {})
        payload["context_pack"] = context_pack
        return write_megas_context(workspace, payload, repo_state)

    async def launch(self, command: dict) -> AsyncIterator[WorkerEvent]:
        session_id = str(command.get("session_id") or "")
        command_id = str(command.get("command_id") or "")
        command_name = command.get("command", {}).get("name", "")

        if command_name == "apply_patch":
            context_pack = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
            workdir = resolve_allowed_workdir(command.get("workdir") or context_pack.get("workdir"), self.allowed_folders)
            repo_root, _ = await find_git_repo_info(workdir)
            if not repo_root:
                raise PermissionError("Apply patch requires a git repository.")
            async for event in self._launch_apply_patch(command, workdir, repo_root, session_id, command_id):
                yield event
            return

        if command_name == "discard_patch":
            async for event in self._launch_discard_patch(command, session_id, command_id):
                yield event
            return

        context_pack = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        workdir = resolve_allowed_workdir(command.get("workdir") or context_pack.get("workdir"), self.allowed_folders)
        write_mode = bool(context_pack.get("write_mode", False))
        cmd_obj = command.get("command") if isinstance(command.get("command"), dict) else {}
        cmd_args_list = [str(a) for a in (cmd_obj.get("args") or [])]
        light_chat = (
            bool(context_pack.get("megas_light_chat"))
            or str(context_pack.get("chat_mode") or "").lower()
            in {"conversational", "agent_chat", "chat"}
        )

        # Enforce destructive keyword checks
        instruction_raw = str(command.get("instruction") or context_pack.get("instruction") or "")
        instruction = instruction_raw.lower()
        write_blocked = {"delete", "rm", "push", "commit", "deploy", "destroy"}
        readonly_blocked = write_blocked | {"remove", "revert"}
        blocked = write_blocked if write_mode else readonly_blocked
        if any(kw in instruction for kw in blocked):
            raise PermissionError(f"Destructive actions are blocked in {'write' if write_mode else 'read-only'} mode.")

        # Agent chat (including "hi"): pass message to codex like CLI — no forced repo inspect.
        if light_chat and not write_mode:
            async for event in self._launch_chat(
                command, context_pack, workdir, session_id, command_id
            ):
                yield event
            return

        # Edit / heavy coding paths still need a git repo.
        repo_root, relative_path = await find_git_repo_info(workdir)
        if not repo_root:
            raise PermissionError("Codex requires a git repo with at least one commit.")
        proc_check = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--verify", "HEAD",
            cwd=str(repo_root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=prefer_console_free_tool_path(),
            **windows_subprocess_kwargs(new_group=False),
        )
        await proc_check.communicate()
        if proc_check.returncode != 0:
            raise PermissionError("Codex requires a git repo with at least one commit.")

        if write_mode:
            async for event in self._launch_write_mode(
                command, context_pack, workdir, repo_root, relative_path, session_id, command_id, instruction
            ):
                yield event
        else:
            async for event in self._launch_readonly(
                command, context_pack, workdir, repo_root, relative_path, session_id, command_id
            ):
                yield event

    async def _launch_chat(
        self,
        command: dict,
        context_pack: dict,
        workdir: Path,
        session_id: str,
        command_id: str,
    ) -> AsyncIterator[WorkerEvent]:
        """Normal agent chat: user text → codex exec (like terminal), no forced inspect."""
        instruction = str(
            command.get("instruction") or context_pack.get("instruction") or ""
        ).strip() or "Hello"
        prompt = instruction  # CLI-like: no "summarize the repo" wrapper
        started = time.time()
        yield WorkerEvent(
            "worker_context_written",
            {"path": "", "workdir": str(workdir), "adapter": self.name, "chat_mode": True},
        )
        yield WorkerEvent(
            "command_started",
            {
                "command_id": command_id,
                "command": ["codex", "chat"],
                "workdir": str(workdir),
                "session_id": session_id,
                "adapter": self.name,
                "agent_type": self.name,
            },
        )
        yield WorkerEvent(
            "agent_turn_queued",
            {
                "instruction_preview": instruction[:240],
                "adapter": self.name,
                "agent_type": self.name,
                "phase": "chat",
            },
        )
        args = self._build_process_args(
            prompt,
            workdir,
            write_mode=False,
            agent_type=self.name,
            chat_mode=True,
            context_pack=context_pack,
        )
        timeout_sec = float(os.getenv("MEGAS_CODEX_CHAT_TIMEOUT_SEC", "90") or "90")
        # sanitize_environment also applies the console-free Git/Node PATH.
        environment = self.sanitize_environment(dict(os.environ))
        environment["NO_COLOR"] = "1"
        if os.getenv("MEGAS_CODEX_DRY_RUN", "").lower() not in {"1", "true", "yes"}:
            binary = args[0]
            model = str(context_pack.get("model") or "").strip()[:200]
            effort = str(context_pack.get("reasoning_effort") or "").strip()
            if effort not in {"minimal", "low", "medium", "high", "xhigh"}:
                effort = ""
            provider_session_id = str(context_pack.get("provider_session_id") or "").strip()[:200]
            assistant_parts: list[str] = []
            try:
                async for event in stream_codex_app_server(
                    binary=binary,
                    prompt=prompt,
                    cwd=workdir,
                    environment=environment,
                    provider_session_id=provider_session_id,
                    write_mode=False,
                    model=model,
                    effort=effort,
                    timeout_sec=float(os.getenv("MEGAS_CODEX_CHAT_TIMEOUT_SEC", "900") or "900"),
                    redact_text=self.redact_output,
                ):
                    payload = {
                        **event.payload,
                        "adapter": self.name,
                        "agent_type": self.name,
                        "phase": "chat",
                    }
                    if event.event_type in {"assistant_delta", "assistant_message"}:
                        text = str(payload.get("text") or "")
                        if text:
                            assistant_parts.append(text)
                    yield WorkerEvent(event.event_type, payload)
            except Exception as exc:
                yield WorkerEvent(
                    "agent_activity",
                    {
                        "adapter": self.name,
                        "agent_type": self.name,
                        "phase": "chat",
                        "source": "codex_exec_json_fallback",
                        "message": "Native app-server unavailable; using structured Codex JSONL fallback.",
                        "detail": self.redact_output(str(exc)),
                    },
                )
            else:
                duration_ms = int((time.time() - started) * 1000)
                answer = "".join(assistant_parts).strip()
                yield WorkerEvent(
                    "command_finished",
                    {
                        "command_id": command_id,
                        "adapter": self.name,
                        "exit_code": 0,
                        "duration_ms": duration_ms,
                        "stream_protocol": "codex_app_server_v2",
                        **({"assistant_answer": answer[:8000]} if answer else {}),
                    },
                )
                return
        print(f"[SPAWN] codex chat argv0={args[0] if args else '?'} workdir={workdir}")
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(workdir),
            # Close stdin so `codex exec` does not hang on "Reading additional input…".
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            **windows_subprocess_kwargs(new_group=True),
        )
        assert process.stdout is not None and process.stderr is not None
        output_tail: list[dict[str, str]] = []
        assistant_messages: list[str] = []
        queue: asyncio.Queue[WorkerEvent | None] = asyncio.Queue(maxsize=512)

        async def pump(name: str, reader: asyncio.StreamReader) -> None:
            try:
                async for line in _bounded_stream_lines(reader):
                    if line is None:
                        text = "[TRUNCATED - OUTPUT LIMIT EXCEEDED]\n"
                        output_tail.append({"stream": name, "line": text})
                        del output_tail[:-200]
                        await queue.put(
                            WorkerEvent(
                                "terminal_stdout" if name == "stdout" else "terminal_stderr",
                                {
                                    "text": text,
                                    "adapter": self.name,
                                    "agent_type": self.name,
                                    "phase": "chat",
                                    "truncated": True,
                                },
                            )
                        )
                        continue
                    decoded = line.decode(errors="replace")
                    text = self.redact_output(decoded)
                    if not text.endswith("\n"):
                        text += "\n"
                    output_tail.append({"stream": name, "line": text[-4096:]})
                    del output_tail[:-200]

                    structured = (
                        codex_json_line_to_worker_events(
                            decoded,
                            redact_text=self.redact_output,
                        )
                        if name == "stdout"
                        else []
                    )
                    if structured:
                        for event in structured:
                            payload = {
                                **event.payload,
                                "adapter": self.name,
                                "agent_type": self.name,
                                "phase": "chat",
                            }
                            normalized = WorkerEvent(event.event_type, payload)
                            if normalized.event_type in {"assistant_message", "assistant_delta"}:
                                answer_text = str(normalized.payload.get("text") or "").strip()
                                if answer_text:
                                    assistant_messages.append(answer_text)
                            await queue.put(normalized)
                        continue

                    # Older Codex versions and stderr remain available as an
                    # expandable raw-terminal fallback.
                    await queue.put(
                        WorkerEvent(
                            "terminal_stdout" if name == "stdout" else "terminal_stderr",
                            {
                                "text": text,
                                "adapter": self.name,
                                "agent_type": self.name,
                                "phase": "chat",
                            },
                        )
                    )
            finally:
                await queue.put(None)

        readers = [
            asyncio.create_task(pump("stdout", process.stdout)),
            asyncio.create_task(pump("stderr", process.stderr)),
        ]
        try:
            closed = 0
            process_exit_seen_at: float | None = None
            provider_terminal_seen_at: float | None = None
            while closed < len(readers):
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
                    if (
                        provider_terminal_seen_at is not None
                        and process.returncode is None
                        and time.monotonic() - provider_terminal_seen_at >= 2.0
                    ):
                        await self.terminate_process_tree(process)
                    continue
                if event is None:
                    closed += 1
                    continue
                if event.event_type in {"agent_turn_completed", "agent_error"}:
                    provider_terminal_seen_at = provider_terminal_seen_at or time.monotonic()
                yield event
            remaining = max(0.1, timeout_sec - (time.time() - started))
            if process.returncode is None:
                await asyncio.wait_for(process.wait(), timeout=remaining)
            await asyncio.gather(*readers, return_exceptions=True)
        except asyncio.TimeoutError as exc:
            if process.returncode is None:
                await self.terminate_process_tree(process)
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            raise TimeoutError(f"Codex chat timed out after {timeout_sec:g}s.") from exc
        finally:
            if process.returncode is None:
                await self.terminate_process_tree(process)
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

        exit_code = process.returncode if process.returncode is not None else 1
        duration_ms = int((time.time() - started) * 1000)
        from megas.terminal_parse import extract_assistant_answer

        raw_stdout = "".join(
            str(i.get("line") or "") for i in output_tail if i.get("stream") == "stdout"
        )
        answer = (
            assistant_messages[-1]
            if assistant_messages
            else (extract_assistant_answer(raw_stdout) or raw_stdout).strip()
        )
        yield WorkerEvent(
            "command_finished",
            {
                "command_id": command_id,
                "adapter": self.name,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                **({"assistant_answer": answer[:8000]} if answer else {}),
            },
        )
        yield WorkerEvent(
            "evidence",
            {
                "evidence_type": "terminal_result",
                "summary": f"Codex chat exited with code {exit_code}",
                "payload": {
                    "adapter": self.name,
                    "command": "codex chat",
                    "workdir": str(workdir),
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    "output_tail": output_tail,
                    "chat_mode": True,
                    **({"assistant_answer": answer[:8000]} if answer else {}),
                },
            },
        )

    async def _launch_readonly(
        self, command: dict, context_pack: dict, workdir: Path,
        repo_root: Path, relative_path: Path, session_id: str, command_id: str,
    ) -> AsyncIterator[WorkerEvent]:
        temp_dir_obj = tempfile.TemporaryDirectory(prefix="megas_sandbox_")
        temp_dir_path = Path(temp_dir_obj.name).resolve()
        proc_add = await asyncio.create_subprocess_exec(
            "git", "worktree", "add", "--detach", str(temp_dir_path), "HEAD",
            cwd=str(repo_root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=prefer_console_free_tool_path(),
            **windows_subprocess_kwargs(new_group=False),
        )
        await proc_add.communicate()
        if proc_add.returncode != 0:
            try:
                temp_dir_obj.cleanup()
            except Exception:
                pass
            raise PermissionError("Read-only Codex failed to create sandbox worktree.")

        await _copy_working_state(repo_root, temp_dir_path)
        sandbox_workdir = (temp_dir_path / relative_path).resolve()
        sandbox_workdir.mkdir(parents=True, exist_ok=True)

        async for event in self._run_in_sandbox(
            command, context_pack, sandbox_workdir, repo_root, temp_dir_obj, temp_dir_path, session_id, command_id, write_mode=False,
        ):
            yield event

    async def _launch_write_mode(
        self, command: dict, context_pack: dict, workdir: Path,
        repo_root: Path, relative_path: Path, session_id: str, command_id: str, instruction: str,
    ) -> AsyncIterator[WorkerEvent]:
        # Gate: require clean workspace
        dirty_check = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain=v1", "--untracked-files=all",
            cwd=str(repo_root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=prefer_console_free_tool_path(),
            **windows_subprocess_kwargs(new_group=False),
        )
        stdout, _ = await dirty_check.communicate()
        if stdout and stdout.decode(errors="replace").strip():
            repo_state = await get_repo_state(repo_root)
            prepared_payload = {
                "branch": repo_state["branch"],
                "head_commit": repo_state["head_commit"],
                "head_subject": repo_state["head_subject"],
                "dirty": True,
                "modified_files": repo_state["modified_files"],
                "untracked_files": repo_state["untracked_files"],
                "expected_evidence": context_pack.get("expected_evidence") or ["terminal_result"],
                "protected_paths_count": len(context_pack.get("protected_paths") or []),
                "recent_memories_count": len(context_pack.get("recent_memories") or []),
            }
            yield WorkerEvent("context_prepared", prepared_payload)
            raise PermissionError("Aborting: Workspace is dirty in write mode. Controlled edit requires a clean workspace. Commit or stash changes first.")

        # Prepare safe session branch
        from megas.runtime.git_branch import prepare_session_branch
        async for event in prepare_session_branch(repo_root, session_id, working_branch=context_pack.get("working_branch")):
            yield event

        base_commit = await get_head_commit(repo_root)

        # Create disposable worktree for Codex to write in
        temp_dir_obj = tempfile.TemporaryDirectory(prefix="megas_edit_sandbox_")
        temp_dir_path = Path(temp_dir_obj.name).resolve()
        proc_add = await asyncio.create_subprocess_exec(
            "git", "worktree", "add", "--detach", str(temp_dir_path), "HEAD",
            cwd=str(repo_root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=prefer_console_free_tool_path(),
            **windows_subprocess_kwargs(new_group=False),
        )
        await proc_add.communicate()
        if proc_add.returncode != 0:
            try:
                temp_dir_obj.cleanup()
            except Exception:
                pass
            raise PermissionError("Controlled edit failed to create sandbox worktree.")

        sandbox_workdir = (temp_dir_path / relative_path).resolve()
        sandbox_workdir.mkdir(parents=True, exist_ok=True)

        # Run Codex in sandbox and collect output events
        output_events: list[WorkerEvent] = []
        exit_code = -1
        duration_ms = 0
        assistant_answer = ""

        async for event in self._run_in_sandbox(
            command, context_pack, sandbox_workdir, repo_root, temp_dir_obj, temp_dir_path, session_id, command_id, write_mode=True,
        ):
            output_events.append(event)
            if event.event_type == "command_finished":
                exit_code = event.payload.get("exit_code", -1)
                duration_ms = event.payload.get("duration_ms", 0)
                assistant_answer = str(event.payload.get("assistant_answer") or "").strip()
            yield event

        # Capture sandbox evidence BEFORE cleanup
        sandbox_head = await get_head_commit(sandbox_workdir)
        if sandbox_head != base_commit:
            raise PermissionError("Codex committed changes in sandbox. Controlled edits must not commit.")

        sandbox_changed = await collect_changed_files(sandbox_workdir)
        if sandbox_changed:
            validate_changed_paths(sandbox_changed, sandbox_workdir)

        sandbox_diff = await collect_git_diff(sandbox_workdir)
        patch_content = await generate_patch(sandbox_workdir)

        # Clean up sandbox worktree
        try:
            proc_rem = await asyncio.create_subprocess_exec(
                "git", "worktree", "remove", "--force", str(temp_dir_path),
                cwd=str(repo_root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=prefer_console_free_tool_path(),
                **windows_subprocess_kwargs(new_group=False),
            )
            await proc_rem.communicate()
        except Exception:
            pass
        try:
            temp_dir_obj.cleanup()
        except Exception:
            pass

        # Generate proposal if there are changes
        patch_id = ""
        sha256 = ""
        expires_at = ""
        if patch_content.strip() and sandbox_changed:
            import uuid
            import hashlib
            from datetime import datetime, timezone, timedelta
            
            patch_id = f"patch_{uuid.uuid4().hex}"
            sha256 = hashlib.sha256(patch_content.encode("utf-8")).hexdigest()
            now = datetime.now(timezone.utc)
            expires_at = (now + timedelta(seconds=3600)).isoformat()
            
            # Save raw patch and JSON metadata sidecar locally
            save_patch_proposal_locally(
                patch_id=patch_id,
                session_id=session_id,
                patch_content=patch_content,
                sha256=sha256,
                base_commit=base_commit,
                expires_at=expires_at,
                workspace_path=str(workdir)
            )

        if patch_id and sha256:
            # Emit evidence_patch_proposal
            yield WorkerEvent("evidence", {
                "evidence_type": "evidence_patch_proposal",
                "summary": f"Patch proposal proposed: {patch_id}",
                "payload": {
                    "patch_id": patch_id,
                    "sha256": sha256,
                    "base_commit": base_commit,
                    "changed_files": sandbox_changed,
                    "diff_stat": {"diff_stat": sandbox_diff[:200]}, # small redacted summary
                    "validation_result": {"status": "valid", "errors": []},
                    "command_id": command_id,
                    "expires_at": expires_at,
                    "write_mode": True
                }
            })
            yield WorkerEvent("evidence", {
                "evidence_type": "terminal_result",
                "summary": f"Codex CLI controlled edit proposed patch {patch_id}",
                "payload": {
                    "adapter": self.name,
                    "command": "codex",
                    "workdir": str(workdir),
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    "write_mode": True,
                    "patch_applied": False,
                    "base_commit": base_commit,
                    **({"assistant_answer": assistant_answer} if assistant_answer else {}),
                },
            })
        else:
            yield WorkerEvent("evidence", {
                "evidence_type": "terminal_result",
                "summary": f"Codex CLI controlled edit finished with no changes",
                "payload": {
                    "adapter": self.name,
                    "command": "codex",
                    "workdir": str(workdir),
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    "write_mode": True,
                    "patch_applied": False,
                    "base_commit": base_commit,
                    **({"assistant_answer": assistant_answer} if assistant_answer else {}),
                },
            })

    async def _launch_apply_patch(
        self, command: dict, workdir: Path, repo_root: Path, session_id: str, command_id: str
    ) -> AsyncIterator[WorkerEvent]:
        args = command.get("command", {}).get("args", [])
        if len(args) < 4:
            raise PermissionError("Malformed apply_patch arguments: missing identity fields.")
        patch_id = args[0]
        expected_sha256 = args[1]
        expected_base_commit = args[2]
        expected_session_id = args[3]
        
        patch_dir = Path.home() / ".megas" / "patches"
        patch_file = patch_dir / f"{patch_id}.patch"
        metadata_file = patch_dir / f"{patch_id}.json"
        
        if not patch_file.exists() or not metadata_file.exists():
            raise FileNotFoundError(f"Patch proposal files for ID {patch_id} not found on this worker.")
            
        import json
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Failed to load proposal metadata: {exc}")
            
        # Verify identity fields
        if metadata.get("patch_id") != patch_id:
            raise PermissionError("Identity mismatch: patch_id does not match.")
        if metadata.get("sha256") != expected_sha256:
            raise PermissionError("Identity mismatch: sha256 does not match.")
        if metadata.get("base_commit") != expected_base_commit:
            raise PermissionError("Identity mismatch: base_commit does not match.")
        if metadata.get("session_id") != expected_session_id or expected_session_id != session_id:
            raise PermissionError("Identity mismatch: session_id does not match.")
        if metadata.get("workspace_path") and Path(metadata["workspace_path"]).resolve() != workdir.resolve():
            raise PermissionError("Identity mismatch: workspace_path does not match.")
            
        # Check expiry
        try:
            from datetime import datetime, timezone
            expires_at = datetime.fromisoformat(metadata.get("expires_at", ""))
            if datetime.now(timezone.utc) > expires_at:
                delete_patch_proposal_locally(patch_id)
                raise PermissionError("Patch proposal has expired.")
        except Exception as exc:
            if "expired" in str(exc).lower():
                raise
                
        # Check workspace clean
        dirty_check = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain=v1", "--untracked-files=all",
            cwd=str(repo_root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=prefer_console_free_tool_path(),
            **windows_subprocess_kwargs(new_group=False),
        )
        stdout, _ = await dirty_check.communicate()
        if stdout and stdout.decode(errors="replace").strip():
            raise PermissionError("Controlled apply requires a clean workspace. Commit or stash changes first.")

        # Prepare safe session branch
        from megas.runtime.git_branch import prepare_session_branch
        async for event in prepare_session_branch(repo_root, session_id):
            yield event
            
        # Check HEAD == base_commit
        current_head = await get_head_commit(repo_root)
        if current_head != expected_base_commit:
            raise PermissionError(f"HEAD changed since proposal: expected {expected_base_commit}, current {current_head}.")
            
        # Verify SHA256 of patch file matches expected
        patch_content = patch_file.read_text(encoding="utf-8")
        import hashlib
        actual_sha256 = hashlib.sha256(patch_content.encode("utf-8")).hexdigest()
        if actual_sha256 != expected_sha256:
            raise PermissionError(f"Patch file SHA256 mismatch: expected {expected_sha256}, actual {actual_sha256}.")
            
        # Validate paths inside patch (using validate_changed_paths)
        changed_paths = []
        for line in patch_content.splitlines():
            if line.startswith("+++ b/"):
                filepath = line[6:].strip()
                changed_paths.append({"path": filepath, "status": "M"})
        validate_changed_paths(changed_paths, repo_root)
        
        # git apply --check
        check = await asyncio.create_subprocess_exec(
            "git", "apply", "--check", str(patch_file),
            cwd=str(repo_root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=prefer_console_free_tool_path(),
            **windows_subprocess_kwargs(new_group=False),
        )
        _, check_err = await check.communicate()
        if check.returncode != 0:
            raise PermissionError(f"Patch failed dry-run check: {check_err.decode(errors='replace')[:500]}")
            
        # git apply for real
        yield WorkerEvent("command_started", {"command_id": command_id, "adapter": self.name, "workdir": str(workdir), "session_id": session_id})
        started = time.time()
        apply = await asyncio.create_subprocess_exec(
            "git", "apply", str(patch_file),
            cwd=str(repo_root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=prefer_console_free_tool_path(),
            **windows_subprocess_kwargs(new_group=False),
        )
        _, apply_err = await apply.communicate()
        duration_ms = int((time.time() - started) * 1000)
        
        if apply.returncode != 0:
            raise PermissionError(f"Patch apply failed: {apply_err.decode(errors='replace')[:500]}")
            
        # Capture final real-workspace evidence
        real_head = await get_head_commit(repo_root)
        real_changed = await collect_changed_files(repo_root)
        real_snapshot = await collect_git_snapshot(repo_root)
        
        yield WorkerEvent("evidence", {
            "evidence_type": "evidence_git_status",
            "summary": "Git status after controlled edit applied to real workspace.",
            "payload": self._git_status_payload(repo_root, real_snapshot),
        })
        yield WorkerEvent("evidence", {
            "evidence_type": "evidence_git_diff_stat",
            "summary": "Git diff stat after controlled edit.",
            "payload": {"diff_stat": real_snapshot.get("diff_stat", ""), "base_commit": expected_base_commit},
        })
        yield WorkerEvent("evidence", {
            "evidence_type": "evidence_changed_files",
            "summary": f"Controlled edit changed {len(real_changed)} file(s).",
            "payload": {
                "kind": "changed_files",
                "workspace": str(workdir),
                "base_commit": expected_base_commit,
                "head_commit_after": real_head,
                "patch_applied": True,
                "changed_files": real_changed,
                "write_mode": True,
            },
        })
        yield WorkerEvent("evidence", {
            "evidence_type": "evidence_patch_applied",
            "summary": f"Patch {patch_id} was successfully applied.",
            "payload": {"patch_id": patch_id, "status": "applied", "sha256": expected_sha256},
        })
        
        # v1.7 Verification Pipeline execution
        if not is_docs_only(real_changed):
            plan = build_verification_plan(real_changed, repo_root)
            yield WorkerEvent("evidence", {
                "evidence_type": "verification_plan_created",
                "summary": f"Verification plan contains {len(plan['commands'])} commands.",
                "payload": {
                    "changed_files": plan["changed_files"],
                    "required_evidence": plan["required_evidence"],
                    "commands": [
                        {
                            "id": c["id"],
                            "kind": c["kind"],
                            "label": c["label"]
                        }
                        for c in plan["commands"]
                    ]
                }
            })
            async for ev in run_verification_pipeline(plan, repo_root):
                yield ev

        yield WorkerEvent("evidence", {
            "evidence_type": "terminal_result",
            "summary": f"Apply patch exited with code 0",
            "payload": {
                "adapter": self.name,
                "command": "git apply",
                "workdir": str(workdir),
                "exit_code": 0,
                "duration_ms": duration_ms,
                "write_mode": True,
                "patch_applied": True,
                "base_commit": expected_base_commit,
                "head_commit_after": real_head,
            },
        })
        yield WorkerEvent("command_finished", {"command_id": command_id, "exit_code": 0, "duration_ms": duration_ms})
        
        # Clean up local files
        delete_patch_proposal_locally(patch_id)

    async def _launch_discard_patch(
        self, command: dict, session_id: str, command_id: str
    ) -> AsyncIterator[WorkerEvent]:
        args = command.get("command", {}).get("args", [])
        if len(args) < 1:
            raise PermissionError("Malformed discard_patch arguments: missing patch_id.")
        patch_id = args[0]
        
        delete_patch_proposal_locally(patch_id)
        
        yield WorkerEvent("command_started", {"command_id": command_id, "adapter": self.name, "session_id": session_id})
        yield WorkerEvent("evidence", {
            "evidence_type": "evidence_human_review_decision",
            "summary": f"Patch proposal {patch_id} was discarded/rejected.",
            "payload": {"patch_id": patch_id, "decision": "rejected", "status": "discarded"},
        })
        yield WorkerEvent("evidence", {
            "evidence_type": "terminal_result",
            "summary": f"Discard patch complete",
            "payload": {
                "adapter": self.name,
                "command": "discard_patch",
                "exit_code": 0,
                "duration_ms": 0,
            },
        })
        yield WorkerEvent("command_finished", {"command_id": command_id, "exit_code": 0, "duration_ms": 0})

    async def _run_in_sandbox(
        self, command: dict, context_pack: dict, sandbox_workdir: Path,
        repo_root: Path, temp_dir_obj: tempfile.TemporaryDirectory, temp_dir_path: Path,
        session_id: str, command_id: str, *, write_mode: bool,
    ) -> AsyncIterator[WorkerEvent]:
        timeout_sec = float(context_pack.get("timeout_sec") or command.get("timeout_sec") or 300.0)
        repo_state = await get_repo_state(sandbox_workdir)
        prepared_payload = {
            "branch": repo_state["branch"],
            "head_commit": repo_state["head_commit"],
            "head_subject": repo_state["head_subject"],
            "dirty": repo_state["dirty"],
            "modified_files": repo_state["modified_files"],
            "untracked_files": repo_state["untracked_files"],
            "expected_evidence": context_pack.get("expected_evidence") or ["terminal_result"],
            "protected_paths_count": len(context_pack.get("protected_paths") or []),
            "recent_memories_count": len(context_pack.get("recent_memories") or []),
        }
        
        yield WorkerEvent("context_prepared", prepared_payload)
        
        if write_mode and repo_state["dirty"]:
            raise PermissionError("Aborting: Workspace is dirty in write mode. Controlled edit requires a clean workspace. Commit or stash changes first.")
            
        megas_md = self.prepare_context(context_pack, sandbox_workdir, command, repo_state=repo_state)
        prompt = self._prompt(command, context_pack, megas_md, write_mode=write_mode)

        before_git = await collect_git_snapshot(sandbox_workdir)
        yield WorkerEvent("worker_context_written", {"path": str(megas_md), "workdir": str(sandbox_workdir), "adapter": self.name})
        yield WorkerEvent("command_started", {"command_id": command_id, "adapter": self.name, "workdir": str(sandbox_workdir), "session_id": session_id})

        started = time.time()
        try:
            timeout_sec = float(command.get("timeout_sec") or context_pack.get("timeout_sec") or os.getenv("MEGAS_CODEX_TIMEOUT_SEC", "300"))
        except (TypeError, ValueError):
            timeout_sec = 300.0
        timeout_sec = min(max(timeout_sec, 1.0), 3600.0)
        agent_type = str(context_pack.get("agent_type") or command.get("agent_type") or "codex_cli")
        args = self._build_process_args(
            prompt,
            sandbox_workdir,
            write_mode=write_mode,
            agent_type=agent_type,
            context_pack=context_pack,
        )

        # sanitize_environment also applies the console-free Git/Node PATH.
        environment = self.sanitize_environment(os.environ)
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(sandbox_workdir),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
            env=environment,
            **windows_subprocess_kwargs(new_group=True),
        )

        assert process.stdout is not None
        assert process.stderr is not None
        output_tail: list[dict[str, str]] = []
        assistant_messages: list[str] = []
        queue: asyncio.Queue[WorkerEvent | None] = asyncio.Queue(maxsize=512)

        async def stream(name: str, reader: asyncio.StreamReader) -> None:
            try:
                async for line in _bounded_stream_lines(reader):
                    if line is None:
                        text = "[TRUNCATED - OUTPUT LIMIT EXCEEDED]\n"
                        output_tail.append({"stream": name, "line": text})
                        del output_tail[:-120]
                        await queue.put(
                            WorkerEvent(
                                "terminal_stdout" if name == "stdout" else "terminal_stderr",
                                {
                                    "text": text,
                                    "adapter": self.name,
                                    "agent_type": self.name,
                                    "truncated": True,
                                },
                            )
                        )
                        continue
                    decoded = line.decode(errors="replace")
                    text = self.redact_output(decoded)
                    output_tail.append({"stream": name, "line": text[-4096:]})
                    del output_tail[:-120]

                    structured = (
                        codex_json_line_to_worker_events(
                            decoded,
                            redact_text=self.redact_output,
                        )
                        if name == "stdout"
                        else []
                    )
                    if structured:
                        for event in structured:
                            normalized = WorkerEvent(
                                event.event_type,
                                {
                                    **event.payload,
                                    "adapter": self.name,
                                    "agent_type": self.name,
                                },
                            )
                            if normalized.event_type in {"assistant_message", "assistant_delta"}:
                                answer_text = str(normalized.payload.get("text") or "").strip()
                                if answer_text:
                                    assistant_messages.append(answer_text)
                            await queue.put(normalized)
                    else:
                        await queue.put(
                            WorkerEvent(
                                "terminal_stdout" if name == "stdout" else "terminal_stderr",
                                {"text": text, "adapter": self.name, "agent_type": self.name},
                            )
                        )
            except Exception:
                pass
            finally:
                await queue.put(None)

        readers = [
            asyncio.create_task(stream("stdout", process.stdout)),
            asyncio.create_task(stream("stderr", process.stderr)),
        ]

        after_git: dict | None = None
        try:
            closed = 0
            while closed < 2:
                elapsed = time.time() - started
                if elapsed >= timeout_sec:
                    raise asyncio.TimeoutError("Codex CLI execution timed out.")
                remaining = max(0.1, timeout_sec - elapsed)
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise asyncio.TimeoutError("Codex CLI execution timed out.")
                if event is None:
                    closed += 1
                    continue
                yield event
            await asyncio.gather(*readers)
            remaining = max(0.1, timeout_sec - (time.time() - started))
            exit_code = await asyncio.wait_for(process.wait(), timeout=remaining)
            if not write_mode:
                after_git = await collect_git_snapshot(sandbox_workdir)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                await self.terminate_process_tree(process)
            for r in readers:
                r.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            if isinstance(exc, asyncio.TimeoutError):
                raise TimeoutError(f"Codex CLI execution timed out after {timeout_sec:g}s.") from exc
            raise
        finally:
            if process.returncode is None:
                await self.terminate_process_tree(process)
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            if not write_mode:
                # Read-only: clean up worktree immediately
                try:
                    proc_rem = await asyncio.create_subprocess_exec(
                        "git", "worktree", "remove", "--force", str(temp_dir_path),
                        cwd=str(repo_root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        env=prefer_console_free_tool_path(),
                        **windows_subprocess_kwargs(new_group=False),
                    )
                    await proc_rem.communicate()
                except Exception:
                    pass
                try:
                    temp_dir_obj.cleanup()
                except Exception:
                    pass

        duration_ms = int((time.time() - started) * 1000)
        assistant_answer = assistant_messages[-1] if assistant_messages else ""

        if not write_mode:
            # Read-only: emit evidence directly from sandbox snapshot
            if after_git is None:
                raise RuntimeError("Codex CLI completed without a final repository snapshot.")
            yield WorkerEvent(
                "command_finished",
                {
                    "command_id": command_id,
                    "adapter": self.name,
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    **({"assistant_answer": assistant_answer} if assistant_answer else {}),
                },
            )
            yield WorkerEvent("evidence", {"evidence_type": "evidence_git_status", "summary": "Captured git status after Codex CLI run.", "payload": self._git_status_payload(sandbox_workdir, after_git)})
            yield WorkerEvent("evidence", {"evidence_type": "evidence_git_diff_stat", "summary": "Captured git diff stat before and after Codex CLI run.", "payload": {"before": before_git, "after": after_git}})
            yield WorkerEvent("evidence", {
                "evidence_type": "terminal_result",
                "summary": f"Codex CLI command exited with code {exit_code}",
                "payload": {
                    "adapter": self.name, "command": "codex", "workdir": str(sandbox_workdir),
                    "exit_code": exit_code, "duration_ms": duration_ms,
                    "output_tail": output_tail, "git": after_git,
                    **({"assistant_answer": assistant_answer} if assistant_answer else {}),
                },
            })
        else:
            # Write-mode: just yield command_finished, evidence comes from _launch_write_mode
            yield WorkerEvent(
                "command_finished",
                {
                    "command_id": command_id,
                    "adapter": self.name,
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    **({"assistant_answer": assistant_answer} if assistant_answer else {}),
                },
            )

    def _build_process_args(
        self,
        prompt: str,
        workdir: Path | None = None,
        *,
        write_mode: bool = False,
        agent_type: str = "codex_cli",
        chat_mode: bool = False,
        context_pack: dict | None = None,
    ) -> list[str]:
        if os.getenv("MEGAS_CODEX_DRY_RUN", "").lower() in {"1", "true", "yes"}:
            if write_mode:
                edit_file = os.getenv("MEGAS_CODEX_DRY_RUN_EDIT", "")
                edit_text = os.getenv("MEGAS_CODEX_DRY_RUN_TEXT", "Controlled edit test line")
                if edit_file and workdir:
                    script = (
                        "import sys, pathlib; "
                        f"p = pathlib.Path(sys.argv[1]) / '{edit_file}'; "
                        "p.parent.mkdir(parents=True, exist_ok=True); "
                        f"p.open('a', encoding='utf-8').write('{edit_text}\\n'); "
                        "print(f'Dry-run edit: {{p}}')"
                    )
                    return [sys.executable, "-c", script, str(workdir)]
            return [
                sys.executable,
                "-c",
                "import sys; print(sys.argv[1])",
                prompt[:2000] if chat_mode else f"Agent dry-run: {prompt[:400]}",
            ]

        if agent_type != self.name:
            raise PermissionError(f"Codex adapter cannot execute agent type: {agent_type}")
        binary = (os.getenv("MEGAS_CODEX_BINARY") or "").strip()
        if not binary:
            # Prefer standalone CLI (supports exec --json). Avoid OpenAI desktop.
            try:
                from megas.core.agent_health import _resolve_binary

                binary = _resolve_binary("codex_cli") or "codex"
            except Exception:
                binary = "codex"
        options = context_pack if isinstance(context_pack, dict) else {}
        # Agent chat workspaces are frequently not Git repositories (Documents,
        # light-chat sandboxes). Without this flag Codex exits 1 immediately with
        # "Not inside a trusted directory and --skip-git-repo-check was not
        # specified.", which surfaced in chat as "No model reply stored for this
        # turn". Sandbox mode still governs what Codex may write.
        args: list[str] = ["exec", "--json", "--skip-git-repo-check"]
        model = str(options.get("model") or "").strip()
        if model:
            args.extend(["--model", model[:200]])
        args.extend(["--sandbox", "workspace-write" if write_mode else "read-only"])
        reasoning = str(options.get("reasoning_effort") or "").strip()
        if reasoning in {"minimal", "low", "medium", "high", "xhigh"}:
            args.extend(["--config", f'model_reasoning_effort="{reasoning}"'])
        web_search = str(options.get("web_search") or "").strip()
        if web_search in {"disabled", "cached", "live"}:
            args.extend(["--config", f'web_search="{web_search}"'])
        provider_session_id = str(options.get("provider_session_id") or "").strip()
        if provider_session_id:
            args.extend(["resume", provider_session_id[:200]])
        args.append(prompt)
        return resolve_cli_argv(binary, *args)
        # Chat / non-write: same as CLI — user prompt only (no inspect wrapper).

    def _prompt(self, command: dict, context_pack: dict, megas_md: Path, *, write_mode: bool = False) -> str:
        instruction = str(
            command.get("instruction")
            or context_pack.get("instruction")
            or "Hello"
        ).strip()
        # Agent chat: pass user message like the terminal. Do not force repo research.
        if not write_mode:
            return instruction
        return (
            f"{instruction}\n\n"
            f"Read MEGAS.md at {megas_md.name} for context and constraints. "
            "You may modify files as instructed. "
            "Do NOT run git commit, git push, or delete files."
        )

    def _git_status_payload(self, workdir: Path, git_snapshot: dict) -> dict:
        changed_files = []
        for line in str(git_snapshot.get("status_short") or "").splitlines():
            if not line.strip():
                continue
            changed_files.append({"status": line[:2].strip(), "path": line[3:].strip()})
        return {
            "kind": "git_status",
            "workspace": str(workdir),
            "branch": git_snapshot.get("branch", ""),
            "dirty": bool(changed_files),
            "changed_files": changed_files,
        }
