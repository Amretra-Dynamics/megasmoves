from __future__ import annotations

import ctypes
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .model import MCPServer
from .protocol import _summarize_prompt, _summarize_resource, _summarize_tool
from .store import MCPStore


MAX_RPC_BYTES = 4 * 1024 * 1024


def _runtime_key(identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"server-{digest}"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_process_tree(pid: int, *, timeout: float = 5.0) -> None:
    if pid <= 0 or not _pid_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return

    deadline = time.monotonic() + timeout
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


class MCPRuntime:
    """Local supervisor for persistent MCP stdio sessions.

    Runtime files are keyed by stable server identity. A compatibility lookup for
    the pre-v2 name-keyed runtime files keeps already-running local sessions
    controllable until their next stop/restart.
    """

    def __init__(self, store: MCPStore | None = None) -> None:
        self.store = store or MCPStore()

    def _state_path(self, server: MCPServer) -> Path:
        return self.store.runtime_dir / f"{_runtime_key(server.id)}.json"

    def _legacy_state_path(self, server: MCPServer) -> Path:
        return self.store.runtime_dir / f"{_runtime_key(server.name)}.json"

    def _log_path(self, server: MCPServer) -> Path:
        return self.store.logs_dir / f"{_runtime_key(server.id)}.log"

    def _legacy_log_path(self, server: MCPServer) -> Path:
        return self.store.logs_dir / f"{_runtime_key(server.name)}.log"

    def _read_state(self, server: MCPServer) -> dict[str, Any] | None:
        for path in (self._state_path(server), self._legacy_state_path(server)):
            if not path.exists():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                state_server_id = str(value.get("server_id") or "")
                state_server_name = str(value.get("server") or "")
                if state_server_id and state_server_id != server.id:
                    continue
                if not state_server_id and state_server_name and state_server_name != server.name:
                    continue
                return value
        return None

    def _remove_state(self, server: MCPServer) -> None:
        for path in (self._state_path(server), self._legacy_state_path(server)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _rotate_logs(self, server: MCPServer, *, max_bytes: int = 1_000_000, keep: int = 3) -> None:
        path = self._log_path(server)
        try:
            if not path.exists() or path.stat().st_size <= max_bytes:
                return
        except OSError:
            return

        for index in range(keep, 0, -1):
            source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
            target = path.with_name(f"{path.name}.{index}")
            if not source.exists():
                continue
            try:
                if target.exists():
                    target.unlink()
                source.replace(target)
            except OSError:
                pass

    def _configuration_problems(self, server: MCPServer) -> tuple[list[str], str | None]:
        problems: list[str] = []
        resolved: str | None = None

        if server.transport == "stdio":
            assert server.command is not None
            candidate = Path(server.command).expanduser()
            if candidate.is_absolute() or os.sep in server.command or (
                os.altsep and os.altsep in server.command
            ):
                resolved = str(candidate)
                if not candidate.exists():
                    problems.append(f"command does not exist: {candidate}")
            else:
                resolved = shutil.which(server.command)
                if not resolved:
                    problems.append(f"command not found on PATH: {server.command}")

            if server.cwd and not Path(server.cwd).expanduser().is_dir():
                problems.append(f"cwd does not exist: {server.cwd}")

            missing_refs = [
                f"{key}->{source}"
                for key, source in server.env_refs.items()
                if source not in os.environ
            ]
            if missing_refs:
                problems.append("missing environment references: " + ", ".join(missing_refs))
        elif not (server.url or "").startswith(("http://", "https://")):
            problems.append(f"{server.transport} transport requires an http:// or https:// URL")

        return problems, resolved

    @staticmethod
    def _recv_rpc(sock: socket.socket) -> dict[str, Any]:
        data = bytearray()
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_RPC_BYTES:
                raise RuntimeError("runtime response exceeds maximum size")
        line = bytes(data).split(b"\n", 1)[0]
        if not line:
            raise RuntimeError("runtime worker returned an empty response")
        value = json.loads(line.decode("utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("runtime worker returned a malformed response")
        return value

    def _rpc_state(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        host = str(state.get("host") or "")
        port = int(state.get("port") or 0)
        token = str(state.get("token") or "")
        if host != "127.0.0.1" or not (1 <= port <= 65535) or not token:
            raise RuntimeError("runtime state is invalid")

        message = {"version": 1, "token": token, **payload}
        encoded = json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
        if len(encoded) > MAX_RPC_BYTES:
            raise ValueError("runtime request exceeds maximum size")

        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(encoded)
            response = self._recv_rpc(sock)

        if not response.get("ok"):
            error = str(response.get("error") or "RuntimeError")
            message_text = str(response.get("message") or "runtime request failed")
            if error == "TimeoutError":
                raise TimeoutError(message_text)
            if error == "ValueError":
                raise ValueError(message_text)
            if error == "PermissionError":
                raise PermissionError(message_text)
            raise RuntimeError(message_text)

        result = response.get("result")
        if not isinstance(result, dict):
            return {"value": result}
        return result

    def _ping_state(self, state: dict[str, Any], *, timeout: float = 1.0) -> dict[str, Any]:
        return self._rpc_state(state, {"action": "ping"}, timeout=timeout)

    def status(self, server: MCPServer) -> dict[str, Any]:
        if server.transport != "stdio":
            return {
                "id": server.id,
                "name": server.name,
                "transport": server.transport,
                "enabled": server.enabled,
                "state": "configured" if server.enabled else "disabled",
                "target": server.url,
                "healthy": True,
            }

        state = self._read_state(server)
        if state is None:
            return {
                "id": server.id,
                "name": server.name,
                "transport": "stdio",
                "enabled": server.enabled,
                "state": "stopped" if server.enabled else "disabled",
                "target": server.command,
                "pid": None,
                "child_pid": None,
                "log": str(self._log_path(server)),
                "healthy": True,
            }

        pid = int(state.get("pid") or 0)
        try:
            ping = self._ping_state(state)
        except (OSError, RuntimeError, TimeoutError, ValueError, PermissionError):
            if not _pid_alive(pid):
                self._remove_state(server)
                return {
                    "id": server.id,
                    "name": server.name,
                    "transport": "stdio",
                    "enabled": server.enabled,
                    "state": "stopped" if server.enabled else "disabled",
                    "target": server.command,
                    "pid": None,
                    "child_pid": None,
                    "log": str(self._log_path(server)),
                    "healthy": True,
                }
            return {
                "id": server.id,
                "name": server.name,
                "transport": "stdio",
                "enabled": server.enabled,
                "state": "degraded",
                "target": server.command,
                "pid": pid,
                "child_pid": state.get("child_pid"),
                "log": str(self._log_path(server)),
                "healthy": False,
                "problem": "runtime worker is alive but not responding",
            }

        child_running = bool(ping.get("child_running"))
        return {
            "id": server.id,
            "name": server.name,
            "transport": "stdio",
            "enabled": server.enabled,
            "state": "running" if child_running else "degraded",
            "target": server.command,
            "pid": ping.get("pid"),
            "child_pid": ping.get("child_pid"),
            "log": str(self._log_path(server)),
            "healthy": child_running,
            "protocol_version": ping.get("protocol_version"),
            "server_info": ping.get("server_info") or {},
            "instructions": str(ping.get("instructions") or ""),
            "crash_restarts": int(ping.get("crash_restarts") or 0),
        }

    def doctor(self, server: MCPServer) -> dict[str, Any]:
        problems, resolved = self._configuration_problems(server)
        runtime = self.status(server)
        if runtime.get("state") == "degraded" and runtime.get("problem"):
            problems.append(str(runtime["problem"]))
        return {
            **runtime,
            "resolved_command": resolved,
            "healthy": not problems and bool(runtime.get("healthy", True)),
            "problems": problems,
        }

    def _worker_command(self, server: MCPServer) -> list[str]:
        if getattr(sys, "frozen", False):
            return [
                sys.executable,
                "--runtime-worker",
                server.id,
                "--home",
                str(self.store.home),
            ]
        return [
            sys.executable,
            "-m",
            "megas.mcp.runtime_worker",
            server.id,
            "--home",
            str(self.store.home),
        ]

    def start(self, server: MCPServer, *, timeout: float = 8.0) -> dict[str, Any]:
        if not server.enabled:
            raise RuntimeError(f"{server.name} is disabled")
        if server.transport != "stdio":
            raise RuntimeError(f"remote {server.transport} MCP servers are configured, not locally started")

        current = self.status(server)
        if current["state"] == "running":
            return {**current, "changed": False}
        if current["state"] == "degraded" and current.get("pid"):
            _terminate_process_tree(int(current["pid"]))
            self._remove_state(server)

        problems, _resolved = self._configuration_problems(server)
        if problems:
            raise RuntimeError("; ".join(problems))

        self._rotate_logs(server)
        log_path = self._log_path(server)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        env = os.environ.copy()
        env["MEGAS_RUNTIME_TOKEN"] = token

        log_handle = log_path.open("ab", buffering=0)
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "env": env,
            "close_fds": os.name != "nt",
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(self._worker_command(server), **kwargs)
        finally:
            log_handle.close()

        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            state = self._read_state(server)
            if state is not None:
                try:
                    ping = self._ping_state(state, timeout=min(1.0, timeout))
                    if ping.get("child_running"):
                        self.store.record(
                            "runtime.server.started",
                            server_id=server.id,
                            server=server.name,
                            pid=ping.get("pid"),
                            child_pid=ping.get("child_pid"),
                        )
                        return {**self.status(server), "changed": True}
                except Exception as exc:
                    last_error = exc
            time.sleep(0.05)

        _terminate_process_tree(proc.pid)
        self._remove_state(server)
        tail = self.tail_logs(server, lines=20)
        detail = tail[-1] if tail else str(last_error or "runtime worker did not become ready")
        raise RuntimeError(f"{server.name} failed to start: {detail}")

    def stop(self, server: MCPServer, *, timeout: float = 5.0) -> dict[str, Any]:
        if server.transport != "stdio":
            raise RuntimeError(f"remote {server.transport} MCP servers are not locally stopped")

        state = self._read_state(server)
        if state is None:
            return {**self.status(server), "changed": False}

        pid = int(state.get("pid") or 0)
        changed = _pid_alive(pid)
        try:
            self._rpc_state(state, {"action": "shutdown"}, timeout=min(timeout, 2.0))
        except Exception:
            pass

        deadline = time.monotonic() + timeout
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _pid_alive(pid):
            _terminate_process_tree(pid, timeout=timeout)

        self._remove_state(server)
        if changed:
            self.store.record(
                "runtime.server.stopped",
                server_id=server.id,
                server=server.name,
                pid=pid,
            )
        return {**self.status(server), "changed": changed}

    def restart(self, server: MCPServer) -> dict[str, Any]:
        self.stop(server)
        result = self.start(server)
        self.store.record(
            "runtime.server.restarted",
            server_id=server.id,
            server=server.name,
        )
        return result

    def tail_logs(self, server: MCPServer, lines: int = 100) -> list[str]:
        path = self._log_path(server)
        if not path.exists():
            legacy = self._legacy_log_path(server)
            if legacy.exists():
                path = legacy
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, lines):]

    def request(
        self,
        server: MCPServer,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if server.transport != "stdio":
            raise RuntimeError("managed runtime requests require a local stdio MCP server")
        state = self._read_state(server)
        if state is None:
            raise RuntimeError(f"{server.name} is not running")
        return self._rpc_state(
            state,
            {
                "action": "request",
                "method": method,
                "params": params or {},
                "timeout": timeout,
            },
            timeout=max(1.0, timeout + 1.0),
        )

    def probe(self, server: MCPServer, *, timeout: float = 5.0) -> dict[str, Any]:
        state = self._read_state(server)
        if state is None:
            raise RuntimeError(f"{server.name} is not running")
        ping = self._ping_state(state, timeout=min(1.0, timeout))
        collected: dict[str, Any] = {}
        for key, method, value_key, summarizer in (
            ("tools", "tools/list", "tools", _summarize_tool),
            ("resources", "resources/list", "resources", _summarize_resource),
            ("prompts", "prompts/list", "prompts", _summarize_prompt),
        ):
            try:
                result = self.request(server, method, {}, timeout=timeout)
                items = [item for item in (result.get(value_key) or []) if isinstance(item, dict)]
                collected[key] = {
                    "supported": True,
                    "count": len(items),
                    "items": [summarizer(item) for item in items],
                }
            except (RuntimeError, TimeoutError) as exc:
                collected[key] = {
                    "supported": False,
                    "count": 0,
                    "items": [],
                    "error": str(exc),
                }
        return {
            "protocol_version": ping.get("protocol_version"),
            "server_info": ping.get("server_info") or {},
            "capabilities": ping.get("capabilities") or {},
            "instructions": str(ping.get("instructions") or ""),
            **collected,
        }

    def call_tool(
        self,
        server: MCPServer,
        tool: str,
        arguments: dict[str, Any],
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if not tool:
            raise ValueError("tool name must be non-empty")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be a JSON object")
        listed = self.request(server, "tools/list", {}, timeout=timeout)
        tools = [item for item in (listed.get("tools") or []) if isinstance(item, dict)]
        known = {str(item.get("name")) for item in tools if item.get("name")}
        if tool not in known:
            raise ValueError(f"MCP server {server.name} does not report tool: {tool}")
        result = self.request(
            server,
            "tools/call",
            {"name": tool, "arguments": arguments},
            timeout=timeout,
        )
        return {
            "server_id": server.id,
            "server": server.name,
            "tool": tool,
            "is_error": bool(result.get("isError", False)),
            "content": result.get("content") or [],
            "structured_content": result.get("structuredContent"),
        }
