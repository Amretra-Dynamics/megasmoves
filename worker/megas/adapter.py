from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import AsyncIterator

from .adapters.base import WorkerEvent, BaseAgentAdapter
from .context import write_megas_context
from .evidence.git import collect_git_snapshot
from .policy import redact, resolve_allowed_workdir, validate_shell_command


class CustomShellAdapter(BaseAgentAdapter):
    name = "custom_shell"
    adapter_id = "custom_shell"
    contract_version = "1.0"
    status = "discovered"
    supported_range = ">=0.0.0"

    def __init__(self, allowed_folders: list[str]) -> None:
        super().__init__(allowed_folders)

    def supports(self, command: dict) -> bool:
        command_type = str(command.get("command_type") or "")
        context_pack = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        agent_type = str(context_pack.get("agent_type") or "")
        return command_type in {"", "safe_shell_command"} and agent_type in {"", "custom_shell"}

    def prepare_context(self, context_pack: dict, workspace: Path, command: dict | None = None) -> Path:
        payload = dict(command or {})
        payload["context_pack"] = context_pack
        return write_megas_context(workspace, payload)

    async def launch(self, command: dict) -> AsyncIterator[WorkerEvent]:
        instruction = str(command.get("instruction") or "")
        session_id = str(command.get("session_id") or "")
        command_id = str(command.get("command_id") or "")
        workdir = resolve_allowed_workdir(command.get("workdir"), self.allowed_folders)
        command_plan = self._select_command(instruction, command)
        display_command = " ".join(command_plan)
        validate_shell_command(display_command)
        megas_md = write_megas_context(workdir, command)
        context_pack = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        try:
            timeout_sec = float(
                command.get("timeout_sec")
                or context_pack.get("timeout_sec")
                or os.getenv("MEGAS_SHELL_TIMEOUT_SEC", "120")
            )
        except (TypeError, ValueError):
            timeout_sec = 120.0
        timeout_sec = min(max(timeout_sec, 1.0), 3600.0)

        started = time.time()
        yield WorkerEvent("worker_context_written", {"path": str(megas_md), "workdir": str(workdir)})
        yield WorkerEvent("command_started", {"command_id": command_id, "command": command_plan, "workdir": str(workdir), "session_id": session_id})

        if command_plan == ["pwd"]:
            line = str(workdir)
            yield WorkerEvent("terminal_stdout", {"text": line})
            yield WorkerEvent("command_finished", {"command_id": command_id, "exit_code": 0, "duration_ms": 0})
            yield self._terminal_result(display_command, workdir, 0, 0, [{"stream": "stdout", "line": line}], await collect_git_snapshot(workdir))
            return
        if command_plan == ["ls"]:
            line = "\n".join(sorted(item.name for item in workdir.iterdir())[:500])
            yield WorkerEvent("terminal_stdout", {"text": line})
            yield WorkerEvent("command_finished", {"command_id": command_id, "exit_code": 0, "duration_ms": 0})
            yield self._terminal_result(display_command, workdir, 0, 0, [{"stream": "stdout", "line": line}], await collect_git_snapshot(workdir))
            return

        from .adapters.base import windows_subprocess_kwargs

        environment = self.sanitize_environment(os.environ)
        process = await asyncio.create_subprocess_exec(
            *command_plan,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
            env=environment,
            **windows_subprocess_kwargs(new_group=True),
        )

        assert process.stdout is not None
        assert process.stderr is not None
        output_tail: list[dict[str, str]] = []
        queue: asyncio.Queue[WorkerEvent | None] = asyncio.Queue(maxsize=256)

        async def stream(name: str, reader: asyncio.StreamReader) -> None:
            total_bytes = 0
            max_bytes = 1024 * 1024  # 1MB limit
            truncated = False
            try:
                while chunk := await reader.read(4096):
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        if not truncated:
                            truncated = True
                            text = self.redact_output(chunk.decode(errors="replace")) + "\n[TRUNCATED - OUTPUT LIMIT EXCEEDED]\n"
                            output_tail.append({"stream": name, "line": text[-4096:]})
                            del output_tail[:-80]
                            await queue.put(WorkerEvent("terminal_stdout" if name == "stdout" else "terminal_stderr", {"text": text}))
                        break
                    text = self.redact_output(chunk.decode(errors="replace"))
                    output_tail.append({"stream": name, "line": text[-4096:]})
                    del output_tail[:-80]
                    await queue.put(WorkerEvent("terminal_stdout" if name == "stdout" else "terminal_stderr", {"text": text}))
            except Exception:
                pass
            finally:
                await queue.put(None)

        readers = [
            asyncio.create_task(stream("stdout", process.stdout)),
            asyncio.create_task(stream("stderr", process.stderr)),
        ]
        try:
            closed = 0
            while closed < 2:
                elapsed = time.time() - started
                if elapsed >= timeout_sec:
                    raise asyncio.TimeoutError
                event = await asyncio.wait_for(queue.get(), timeout=max(0.1, timeout_sec - elapsed))
                if event is None:
                    closed += 1
                    continue
                yield event
            await asyncio.gather(*readers)
            exit_code = await process.wait()
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"Safe shell command timed out after {timeout_sec:g}s.") from exc
        finally:
            if process.returncode is None:
                await self._terminate_process(process)
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

        duration_ms = int((time.time() - started) * 1000)
        git_snapshot = await collect_git_snapshot(workdir)
        yield WorkerEvent("command_finished", {"command_id": command_id, "exit_code": exit_code, "duration_ms": duration_ms})
        if command_plan[:2] == ["git", "status"]:
            yield WorkerEvent("evidence", {"evidence_type": "evidence_git_status", "summary": "Captured git status.", "payload": self._git_status_payload(workdir, git_snapshot)})
        if command_plan[:2] == ["git", "diff"]:
            yield WorkerEvent("evidence", {"evidence_type": "evidence_git_diff_stat", "summary": "Captured git diff stat.", "payload": git_snapshot})
        yield self._terminal_result(display_command, workdir, exit_code, duration_ms, output_tail, git_snapshot)

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "nt":
                from megas.adapters.base import subprocess_hide_window_kwargs

                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                    **subprocess_hide_window_kwargs(),
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired, asyncio.TimeoutError):
            if process.returncode is None:
                if os.name == "nt":
                    process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
                await process.wait()

    def _select_command(self, instruction: str, command: dict) -> list[str]:
        context_pack = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        explicit = context_pack.get("command") or command.get("command")
        if explicit:
            if isinstance(explicit, dict):
                name = str(explicit.get("name") or "")
                args = explicit.get("args") if isinstance(explicit.get("args"), list) else []
                return self._normalize_allowed_command(" ".join([name, *[str(arg) for arg in args]]))
            return self._normalize_allowed_command(str(explicit))

        # Prefer instruction as an exact allowlisted shell command when it matches.
        try:
            return self._normalize_allowed_command(instruction)
        except PermissionError:
            pass
        lowered = instruction.lower()
        if "diff" in lowered:
            return ["git", "diff", "--stat"]
        if "branch" in lowered:
            return ["git", "branch", "--show-current"]
        if "list" in lowered or "ls" in lowered:
            return ["ls"]
        if "pwd" in lowered or "where" in lowered:
            return ["pwd"]
        if "uname" in lowered:
            return ["uname", "-a"]
        if "hostname" in lowered:
            return ["hostname"]
        if "whoami" in lowered:
            return ["whoami"]
        return ["git", "status", "--short"]

    def _normalize_allowed_command(self, command: str) -> list[str]:
        normalized = " ".join(command.strip().split())
        # Drop shell wrappers that some callers prepend.
        if normalized.startswith("shell "):
            normalized = normalized[6:].strip()
        allowed = {
            "pwd": ["pwd"],
            "git status": ["git", "status"],
            "git status --short": ["git", "status", "--short"],
            "git diff --stat": ["git", "diff", "--stat"],
            "git branch --show-current": ["git", "branch", "--show-current"],
            "ls": ["ls"],
            "dir": ["ls"],
            "uname": ["uname", "-a"],
            "uname -a": ["uname", "-a"],
            "hostname": ["hostname"],
            "whoami": ["whoami"],
            "megas version": ["megas", "version"],
            "megas status": ["megas", "status"],
            "megas update": ["megas", "update"],
        }
        if normalized.startswith("echo"):
            return ["echo"] + normalized.split()[1:]
        if normalized not in allowed:
            raise PermissionError(f"Command is not allowed in Worker Session Spine v1: {normalized}")
        return allowed[normalized]

    def _terminal_result(self, command: str, workdir, exit_code: int, duration_ms: int, output_tail: list[dict[str, str]], git_snapshot: dict) -> WorkerEvent:
        return WorkerEvent(
            "evidence",
            {
                "evidence_type": "terminal_result",
                "summary": f"Command exited with code {exit_code}",
                "payload": {
                    "command": command,
                    "workdir": str(workdir),
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    "output_tail": output_tail,
                    "git": git_snapshot,
                },
            },
        )

    def _git_status_payload(self, workdir, git_snapshot: dict) -> dict:
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
