from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Sequence


MODERN_PROTOCOL = "2026-07-28"
PROTOCOL_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
MAX_MCP_LINE_BYTES = 4 * 1024 * 1024


def _meta() -> dict[str, Any]:
    return {
        PROTOCOL_META_KEY: MODERN_PROTOCOL,
        CLIENT_CAPABILITIES_META_KEY: {},
        CLIENT_INFO_META_KEY: {"name": "Megas self-check", "version": "0.5.0"},
    }


def _reader(stream: Any, inbox: "queue.Queue[dict[str, Any] | Exception | None]") -> None:
    try:
        while True:
            raw = stream.readline(MAX_MCP_LINE_BYTES + 1)
            if not raw:
                return
            if len(raw) > MAX_MCP_LINE_BYTES:
                inbox.put(RuntimeError("Megas MCP self-check response exceeded the 4 MiB limit"))
                return
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                inbox.put(value)
    except Exception as exc:  # pragma: no cover - defensive reader boundary
        inbox.put(exc)
    finally:
        inbox.put(None)


def _send(
    proc: subprocess.Popen[bytes],
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> None:
    if proc.stdin is None:
        raise RuntimeError("Megas MCP self-check stdin is unavailable")
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }
    raw = (json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    if len(raw) > MAX_MCP_LINE_BYTES:
        raise ValueError("Megas MCP self-check request exceeded the 4 MiB limit")
    proc.stdin.write(raw)
    proc.stdin.flush()


def _wait(
    proc: subprocess.Popen[bytes],
    inbox: "queue.Queue[dict[str, Any] | Exception | None]",
    request_id: int,
    *,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        try:
            item = inbox.get(timeout=remaining)
        except queue.Empty:
            break
        if isinstance(item, Exception):
            raise RuntimeError(f"Megas MCP self-check reader failed: {type(item).__name__}") from item
        if item is None:
            raise RuntimeError(f"Megas MCP self-check process exited before replying (code={proc.poll()})")
        if item.get("id") != request_id:
            continue
        if "error" in item:
            error = item.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "MCP request failed")
            else:
                message = "MCP request failed"
            raise RuntimeError(f"Megas MCP modern self-check failed: {message}")
        result = item.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Megas MCP self-check returned a non-object result")
        return result
    raise TimeoutError(f"Megas MCP self-check request {request_id} timed out")


def _request(
    proc: subprocess.Popen[bytes],
    inbox: "queue.Queue[dict[str, Any] | Exception | None]",
    request_id: int,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    _send(proc, request_id, method, params)
    return _wait(proc, inbox, request_id, timeout=timeout)


def probe_embedded_operator_modern(
    launcher: str | os.PathLike[str] | Path,
    *,
    timeout: float = 20.0,
    serve_args: Sequence[str] = ("serve",),
) -> dict[str, Any]:
    """Prove the installed MegasMoves executable speaks current stateless MCP over stdio.

    This is intentionally a self-probe of the embedded Megas operator rather than a
    general MCP client implementation. It validates the exact flow that current
    standards-compatible coding agents need: server/discover followed by typed list
    requests carrying per-request protocol/client metadata.

    ``serve_args`` exists so the contract can be exercised with an isolated test
    process. Production callers use the default ``("serve",)`` and therefore probe
    the same installed ``MegasMoves.exe`` that an MCP host launches.
    """

    executable = Path(launcher).expanduser().resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"Megas executable does not exist: {executable}")
    actual_timeout = max(1.0, min(float(timeout), 60.0))
    argv = [str(executable), *[str(value) for value in serve_args]]
    env = os.environ.copy()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        shell=False,
        env=env,
        bufsize=0,
    )
    if proc.stdout is None:
        proc.kill()
        raise RuntimeError("Megas MCP self-check stdout is unavailable")

    inbox: "queue.Queue[dict[str, Any] | Exception | None]" = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, inbox), daemon=True).start()
    meta = _meta()
    try:
        discover = _request(
            proc,
            inbox,
            1,
            "server/discover",
            {"_meta": dict(meta)},
            timeout=actual_timeout,
        )
        versions = [str(value) for value in discover.get("supportedVersions", [])]
        if MODERN_PROTOCOL not in versions:
            raise RuntimeError(f"Megas MCP did not advertise {MODERN_PROTOCOL}")
        if discover.get("resultType") != "complete":
            raise RuntimeError("Megas MCP discovery did not return a complete result")

        tools = _request(
            proc,
            inbox,
            2,
            "tools/list",
            {"_meta": dict(meta)},
            timeout=actual_timeout,
        )
        resources = _request(
            proc,
            inbox,
            3,
            "resources/list",
            {"_meta": dict(meta)},
            timeout=actual_timeout,
        )
        tool_names = {
            str(row.get("name") or "")
            for row in tools.get("tools", [])
            if isinstance(row, dict)
        }
        resource_uris = {
            str(row.get("uri") or "")
            for row in resources.get("resources", [])
            if isinstance(row, dict)
        }
        required_tools = {"operator_status", "workspace_projects", "workspace_check"}
        required_resources = {"megas://operator/status", "megas://workspace/projects"}
        missing_tools = sorted(required_tools - tool_names)
        missing_resources = sorted(required_resources - resource_uris)
        if missing_tools or missing_resources:
            details: list[str] = []
            if missing_tools:
                details.append("tools " + ", ".join(missing_tools))
            if missing_resources:
                details.append("resources " + ", ".join(missing_resources))
            raise RuntimeError("Megas MCP modern self-check is missing " + "; ".join(details))

        result_meta = discover.get("_meta") if isinstance(discover.get("_meta"), dict) else {}
        server_info = result_meta.get(SERVER_INFO_META_KEY)
        return {
            "protocol_version": MODERN_PROTOCOL,
            "discovered": True,
            "server_info": dict(server_info) if isinstance(server_info, dict) else {},
            "tool_count": len(tool_names),
            "resource_count": len(resource_uris),
            "required_tools_present": True,
            "required_resources_present": True,
            "stateless": True,
        }
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass


__all__ = [
    "CLIENT_CAPABILITIES_META_KEY",
    "CLIENT_INFO_META_KEY",
    "MAX_MCP_LINE_BYTES",
    "MODERN_PROTOCOL",
    "PROTOCOL_META_KEY",
    "SERVER_INFO_META_KEY",
    "probe_embedded_operator_modern",
]
