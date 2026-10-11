"""SSH hand: run a command on a remote host *from this Worker* (LAN/VPN).

Product rule: cloud never opens inbound SSH into the customer network.
This adapter uses the OpenSSH client on the Worker host (same model as CUA).
Requires explicit Megas Operate enablement on this machine.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, AsyncIterator

from megas.adapters.base import BaseAgentAdapter, WorkerEvent, windows_subprocess_kwargs
from megas.context import write_megas_context
from megas.operate.gate import is_operate_enabled
from megas.policy import redact, resolve_allowed_workdir


def _terminal_result(
    command: str,
    workdir: Path,
    exit_code: int,
    duration_ms: int,
    output_tail: list[dict[str, str]],
) -> WorkerEvent:
    return WorkerEvent(
        "evidence",
        {
            "evidence_type": "terminal_result",
            "summary": f"SSH command exited with code {exit_code}",
            "payload": {
                "command": command,
                "workdir": str(workdir),
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "output_tail": output_tail,
                "hand": "ssh",
            },
        },
    )


_HOST_RE = re.compile(
    r"^(?:\[?[A-Fa-f0-9:]+\]?|(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?))*)$"
)
_USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class SshExecAdapter(BaseAgentAdapter):
    """Execute ``ssh user@host -- remote-command`` via local OpenSSH client."""

    name = "ssh_exec"
    adapter_id = "ssh_exec"
    contract_version = "1.0"
    status = "discovered"
    supported_range = ">=0.0.0"

    def __init__(self, allowed_folders: list[str]) -> None:
        super().__init__(allowed_folders)

    def supports(self, command: dict) -> bool:
        if not is_operate_enabled():
            return False
        command_type = str(command.get("command_type") or "").strip().lower()
        context = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        agent_type = str(context.get("agent_type") or command.get("agent_type") or "").strip().lower()
        if command_type in {"ssh_exec", "remote_ssh", "ssh"}:
            return True
        if agent_type in {"ssh_exec", "ssh", "remote.ssh", "shell.ssh"}:
            return True
        hand = str(command.get("hand") or context.get("hand") or "").strip().lower()
        return hand in {"ssh", "ssh_exec", "remote.ssh", "shell.ssh"}

    def prepare_context(self, context_pack: dict, workspace: Path, command: dict | None = None) -> Path:
        payload = dict(command or {})
        payload["context_pack"] = context_pack
        return write_megas_context(workspace, payload)

    def _ssh_binary(self) -> str:
        for name in ("ssh", "ssh.exe"):
            found = shutil.which(name)
            if found:
                return found
        raise RuntimeError(
            "SSH hand unavailable: OpenSSH client not found on this Worker PATH."
        )

    def _parse_target(self, command: dict) -> tuple[str, str, list[str], int]:
        context = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        host = str(
            command.get("ssh_host")
            or command.get("host")
            or context.get("ssh_host")
            or context.get("host")
            or ""
        ).strip()
        user = str(
            command.get("ssh_user")
            or command.get("user")
            or context.get("ssh_user")
            or context.get("user")
            or ""
        ).strip()
        port_raw = (
            command.get("ssh_port")
            or command.get("port")
            or context.get("ssh_port")
            or context.get("port")
            or 22
        )
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            port = 22
        if port < 1 or port > 65535:
            raise RuntimeError("SSH port must be between 1 and 65535.")
        if not host or not _HOST_RE.match(host):
            raise RuntimeError(
                "SSH hand requires a valid ssh_host (hostname or IP) on the command."
            )
        if user and not _USER_RE.match(user):
            raise RuntimeError("SSH hand ssh_user contains invalid characters.")
        remote = str(command.get("instruction") or command.get("remote_command") or "").strip()
        if not remote:
            raise RuntimeError("SSH hand requires a non-empty remote command (instruction).")
        target = f"{user}@{host}" if user else host
        return target, remote, [], port

    async def launch(self, command: dict) -> AsyncIterator[WorkerEvent]:
        if not is_operate_enabled():
            raise PermissionError(
                "Megas Operate is disabled on this machine. Run `megas operate enable` locally."
            )
        session_id = str(command.get("session_id") or "")
        command_id = str(command.get("command_id") or "")
        workdir = resolve_allowed_workdir(command.get("workdir"), self.allowed_folders)
        ssh_bin = self._ssh_binary()
        target, remote, _extra, port = self._parse_target(command)

        argv = [
            ssh_bin,
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=20",
            "-p",
            str(port),
            target,
            "--",
            remote,
        ]
        display = " ".join(argv[:-1] + [redact(remote)])
        context_pack = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        try:
            timeout_sec = float(
                command.get("timeout_sec")
                or context_pack.get("timeout_sec")
                or os.getenv("MEGAS_SSH_TIMEOUT_SEC", "120")
            )
        except (TypeError, ValueError):
            timeout_sec = 120.0
        timeout_sec = min(max(timeout_sec, 1.0), 3600.0)

        started = time.time()
        yield WorkerEvent(
            "command_started",
            {
                "command_id": command_id,
                "command": argv,
                "display": display,
                "workdir": str(workdir),
                "session_id": session_id,
                "hand": "ssh",
                "ssh_target": target,
            },
        )

        environment = self.sanitize_environment(os.environ)
        process = await asyncio.create_subprocess_exec(
            *argv,
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
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(), timeout=timeout_sec
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            yield WorkerEvent(
                "command_failed",
                {
                    "command_id": command_id,
                    "error": f"SSH command timed out after {timeout_sec:.0f}s",
                    "session_id": session_id,
                },
            )
            return

        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        if stdout:
            yield WorkerEvent("terminal_stdout", {"text": stdout[:1_000_000]})
            output_tail.append({"stream": "stdout", "line": stdout[:2000]})
        if stderr:
            yield WorkerEvent("terminal_stderr", {"text": stderr[:1_000_000]})
            output_tail.append({"stream": "stderr", "line": stderr[:2000]})

        code = int(process.returncode or 0)
        duration_ms = int((time.time() - started) * 1000)
        yield WorkerEvent(
            "command_finished",
            {
                "command_id": command_id,
                "exit_code": code,
                "duration_ms": duration_ms,
                "session_id": session_id,
                "hand": "ssh",
                "ssh_target": target,
            },
        )
        yield _terminal_result(display, workdir, code, duration_ms, output_tail[-50:])


__all__ = ["SshExecAdapter"]
