from __future__ import annotations

import argparse
import hmac
import json
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Any

from .protocol import StdioMCPClient
from .runtime import _runtime_key
from .secret_release import SecretReleasePolicy
from .store import MCPStore
from .validation import validate_tool_arguments


MAX_RPC_BYTES = 4 * 1024 * 1024


def _recv_json(conn: socket.socket) -> dict[str, Any]:
    data = bytearray()
    while b"\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_RPC_BYTES:
            raise ValueError("runtime request exceeds maximum size")
    line = bytes(data).split(b"\n", 1)[0]
    if not line:
        raise ValueError("empty runtime request")
    value = json.loads(line.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("runtime request must be a JSON object")
    return value


def _send_json(conn: socket.socket, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    conn.sendall(encoded)


def _state_payload(
    *,
    server_id: str,
    server_name: str,
    token: str,
    listener: socket.socket,
    client: StdioMCPClient,
    started_at: str,
    crash_restarts: int,
) -> dict[str, Any]:
    host, port = listener.getsockname()[:2]
    child_pid = client.proc.pid if client.proc is not None else None
    return {
        "version": 2,
        "server_id": server_id,
        "server": server_name,
        "pid": os.getpid(),
        "child_pid": child_pid,
        "host": host,
        "port": port,
        "token": token,
        "started_at": started_at,
        "protocol_version": client.protocol_version,
        "server_info": client.server_info,
        "capabilities": client.capabilities,
        "instructions": client.instructions,
        "crash_restarts": crash_restarts,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="megas-runtime-worker")
    parser.add_argument("server_id")
    parser.add_argument("--home", required=True)
    return parser


def _validate_runtime_tool_call(
    client: StdioMCPClient,
    params: dict[str, Any],
    *,
    timeout: float,
) -> None:
    tool = str(params.get("name") or "").strip()
    if not tool:
        raise ValueError("tools/call requires a non-empty tool name")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValueError("tools/call arguments must be a JSON object")
    listed = client.request("tools/list", {}, timeout=timeout)
    tools = [item for item in (listed.get("tools") or []) if isinstance(item, dict)]
    selected = next(
        (item for item in tools if str(item.get("name") or "") == tool),
        None,
    )
    if selected is None:
        raise ValueError(f"MCP server does not report tool: {tool}")
    schema = selected.get("inputSchema")
    if not isinstance(schema, dict):
        raise ValueError(f"MCP server reported invalid inputSchema for {tool}")
    validate_tool_arguments(tool, schema, arguments)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    token = os.environ.pop("MEGAS_RUNTIME_TOKEN", "")
    if not token:
        print("runtime worker missing local authentication token", file=sys.stderr, flush=True)
        return 2

    store = MCPStore(Path(args.home))
    try:
        configured_server = store.get_server_by_id(args.server_id)
        server = SecretReleasePolicy(store).prepare_server(configured_server)
    except (KeyError, PermissionError, ValueError) as exc:
        print(f"runtime worker refused server {args.server_id}: {exc}", file=sys.stderr, flush=True)
        return 2
    if not server.enabled:
        print(f"runtime worker refused disabled server: {server.name}", file=sys.stderr, flush=True)
        return 2
    if server.transport != "stdio":
        print("runtime worker only supports local stdio MCP servers", file=sys.stderr, flush=True)
        return 2

    from datetime import UTC, datetime

    started_at = datetime.now(UTC).isoformat()
    state_path = store.runtime_dir / f"{_runtime_key(server.id)}.json"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.5)

    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    try:
        signal.signal(signal.SIGTERM, request_stop)
    except Exception:
        pass
    try:
        signal.signal(signal.SIGINT, request_stop)
    except Exception:
        pass

    client: StdioMCPClient | None = None
    crash_restarts = 0
    max_crash_restarts = 3

    def start_client() -> StdioMCPClient:
        # Re-evaluate grants before every crash restart. A grant revoked while
        # the child was down must not be reused by a replacement process.
        fresh = SecretReleasePolicy(store).prepare_server(store.get_server_by_id(server.id))
        session = StdioMCPClient(fresh, timeout=10.0, stderr=sys.stderr)
        session.__enter__()
        return session

    try:
        client = start_client()
        store._atomic_json(
            state_path,
            _state_payload(
                server_id=server.id,
                server_name=server.name,
                token=token,
                listener=listener,
                client=client,
                started_at=started_at,
                crash_restarts=crash_restarts,
            ),
        )
        print(f"Megas runtime ready for {server.name}", file=sys.stderr, flush=True)

        while not stopping:
            if client.proc is None or client.proc.poll() is not None:
                exit_code = None if client.proc is None else client.proc.poll()
                store.record(
                    "runtime.server.crashed",
                    server_id=server.id,
                    server=server.name,
                    exit_code=exit_code,
                    restart_attempt=crash_restarts + 1,
                )
                client.close()
                if crash_restarts >= max_crash_restarts:
                    print(
                        f"runtime restart budget exhausted for {server.name}",
                        file=sys.stderr,
                        flush=True,
                    )
                    break
                import time

                delay = min(2.0, 0.25 * (2**crash_restarts))
                time.sleep(delay)
                crash_restarts += 1
                try:
                    client = start_client()
                except Exception as exc:
                    print(
                        f"runtime restart {crash_restarts} failed for {server.name}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                store._atomic_json(
                    state_path,
                    _state_payload(
                        server_id=server.id,
                        server_name=server.name,
                        token=token,
                        listener=listener,
                        client=client,
                        started_at=started_at,
                        crash_restarts=crash_restarts,
                    ),
                )
                store.record(
                    "runtime.server.restarted",
                    server_id=server.id,
                    server=server.name,
                    restart_attempt=crash_restarts,
                )

            try:
                conn, _address = listener.accept()
            except socket.timeout:
                continue

            with conn:
                conn.settimeout(10.0)
                try:
                    request = _recv_json(conn)
                    request_token = str(request.get("token") or "")
                    if not hmac.compare_digest(request_token, token):
                        raise PermissionError("runtime authentication failed")
                    if int(request.get("version") or 0) != 1:
                        raise ValueError("unsupported runtime RPC version")

                    action = str(request.get("action") or "")
                    if action == "ping":
                        result = {
                            "server_id": server.id,
                            "server": server.name,
                            "pid": os.getpid(),
                            "child_pid": client.proc.pid if client.proc is not None else None,
                            "child_running": bool(client.proc is not None and client.proc.poll() is None),
                            "protocol_version": client.protocol_version,
                            "server_info": client.server_info,
                            "capabilities": client.capabilities,
                            "instructions": client.instructions,
                            "crash_restarts": crash_restarts,
                        }
                    elif action == "request":
                        method = str(request.get("method") or "").strip()
                        if not method:
                            raise ValueError("runtime MCP method must be non-empty")
                        params = request.get("params")
                        if params is not None and not isinstance(params, dict):
                            raise ValueError("runtime MCP params must be a JSON object")
                        params = params or {}
                        timeout = float(request.get("timeout") or 30.0)
                        if timeout <= 0 or timeout > 300:
                            raise ValueError("runtime MCP timeout must be between 0 and 300 seconds")
                        if method == "tools/call":
                            _validate_runtime_tool_call(client, params, timeout=timeout)
                        result = client.request(method, params, timeout=timeout)
                    elif action == "shutdown":
                        result = {"server_id": server.id, "server": server.name, "shutting_down": True}
                        stopping = True
                    else:
                        raise ValueError(f"unknown runtime action: {action}")

                    _send_json(conn, {"ok": True, "result": result})
                except Exception as exc:
                    error_name = "ValueError" if isinstance(exc, ValueError) else type(exc).__name__
                    _send_json(
                        conn,
                        {
                            "ok": False,
                            "error": error_name,
                            "message": str(exc),
                        },
                    )
    except Exception as exc:
        print(f"runtime worker failed for {server.name}: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        try:
            listener.close()
        except Exception:
            pass
        if client is not None:
            client.close()
        try:
            current = store._load_json(state_path, {})
            if int(current.get("pid") or 0) == os.getpid():
                state_path.unlink(missing_ok=True)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
