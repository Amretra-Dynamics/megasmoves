from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .operator_runtime import MegasOperatorMCPServer
from .operator_server import _error, _response


def _decode(line: str) -> tuple[Any, str | None, dict[str, Any] | None, bool, dict[str, Any] | None]:
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return None, None, None, False, _response(None, error=_error(-32700, "parse error"))
    if not isinstance(message, dict):
        return None, None, None, False, _response(None, error=_error(-32600, "invalid request"))

    has_id = "id" in message
    request_id = message.get("id") if has_id else None
    if isinstance(request_id, bool) or (has_id and request_id is not None and not isinstance(request_id, (str, int, float))):
        return None, None, None, False, _response(None, error=_error(-32600, "invalid request"))
    if message.get("jsonrpc") != "2.0":
        return None, None, None, False, _response(request_id, error=_error(-32600, "invalid request"))
    method = message.get("method")
    if not isinstance(method, str) or not method.strip():
        return None, None, None, False, _response(request_id, error=_error(-32600, "invalid request"))
    params = message.get("params")
    if params is not None and not isinstance(params, dict):
        if not has_id:
            return None, None, None, False, None
        return None, None, None, False, _response(request_id, error=_error(-32602, "params must be a JSON object"))
    return request_id, method.strip(), params, has_id, None


def _emit(stream: TextIO, value: dict[str, Any]) -> None:
    stream.write(json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n")
    stream.flush()


def serve_operator_stdio(*, stdin: TextIO | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None) -> None:
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    server = MegasOperatorMCPServer(transport="stdio")
    try:
        for line in input_stream:
            text = line.strip()
            if not text:
                continue
            request_id, method, params, wants_response, framing_error = _decode(text)
            if framing_error is not None:
                _emit(output_stream, framing_error)
                continue
            if method is None:
                continue
            try:
                result = server.dispatch(method, params)
                if not wants_response or result is None:
                    continue
                response = _response(request_id, result=result)
            except KeyError as exc:
                if not wants_response:
                    continue
                response = _response(request_id, error=_error(-32601, f"method not found or operator capability unavailable: {exc.args[0]}"))
            except (ValueError, RuntimeError, TimeoutError, PermissionError, OSError) as exc:
                if not wants_response:
                    print(f"Megas MCP notification error: {type(exc).__name__}", file=error_stream, flush=True)
                    continue
                response = _response(request_id, error=_error(-32602, str(exc), {"type": type(exc).__name__}))
            except Exception as exc:
                if not wants_response:
                    print(f"Megas MCP notification internal error: {type(exc).__name__}", file=error_stream, flush=True)
                    continue
                response = _response(request_id, error=_error(-32603, "internal operator error", {"type": type(exc).__name__}))
            _emit(output_stream, response)
    finally:
        server.record_stdio_disconnect()


__all__ = ["serve_operator_stdio"]
