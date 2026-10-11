"""Worker-side execution for authenticated remote MCP relay requests.

The cloud backend only routes an MCP envelope to the account-owned Worker. This module
executes it through the exact production Megas operator used by local MCP clients and
posts the JSON-RPC result back with Worker authentication.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from .operator_runtime import MegasOperatorMCPServer
from .operator_server import _error, _response


MAX_REMOTE_MCP_BYTES = 1024 * 1024
CALLBACK_TIMEOUT_SECONDS = 10.0


def _bounded_json_size(value: Any) -> int:
    try:
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("remote MCP envelope is not JSON serializable") from exc
    if len(raw) > MAX_REMOTE_MCP_BYTES:
        raise ValueError("remote MCP envelope exceeds the 1 MiB Worker limit")
    return len(raw)


def _validate_relay_message(envelope: dict[str, Any]) -> tuple[str, bool, dict[str, Any]]:
    relay_id = str(envelope.get("relay_id") or "").strip()
    if not relay_id or len(relay_id) > 64:
        raise ValueError("remote MCP relay id is invalid")
    expect_reply = envelope.get("expect_reply") is True
    message = envelope.get("message")
    if not isinstance(message, dict):
        raise ValueError("remote MCP relay message must be an object")
    _bounded_json_size(message)
    if message.get("jsonrpc") != "2.0":
        raise ValueError("remote MCP request must use JSON-RPC 2.0")
    method = str(message.get("method") or "").strip()
    if not method or len(method) > 128:
        raise ValueError("remote MCP method is invalid")
    params = message.get("params")
    if params is not None and not isinstance(params, dict):
        raise ValueError("remote MCP params must be an object")
    if expect_reply != (message.get("id") is not None):
        raise ValueError("remote MCP reply expectation does not match JSON-RPC id")
    return relay_id, expect_reply, message


class RemoteMCPRelay:
    """Execute remote requests through one long-lived embedded operator instance."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.operator = MegasOperatorMCPServer(transport="remote-streamable-http")

    def _dispatch(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params")
        try:
            result = self.operator.dispatch(method, params)
            if request_id is None or result is None:
                return None
            return _response(request_id, result=result)
        except KeyError as exc:
            if request_id is None:
                return None
            return _response(
                request_id,
                error=_error(
                    -32601,
                    f"method not found or operator capability unavailable: {exc.args[0]}",
                ),
            )
        except (ValueError, RuntimeError, TimeoutError, PermissionError, OSError) as exc:
            if request_id is None:
                return None
            return _response(
                request_id,
                error=_error(-32602, str(exc), {"type": type(exc).__name__}),
            )
        except Exception as exc:
            if request_id is None:
                return None
            # Do not reflect unexpected exception text or local paths over the relay.
            return _response(
                request_id,
                error=_error(-32603, "internal operator error", {"type": type(exc).__name__}),
            )

    async def handle(self, envelope: dict[str, Any]) -> None:
        relay_id, expect_reply, message = _validate_relay_message(envelope)
        response = await asyncio.to_thread(self._dispatch, message)
        if not expect_reply:
            return
        if response is None:
            response = _response(
                message.get("id"),
                error=_error(-32603, "remote operator returned no response"),
            )
        try:
            _bounded_json_size(response)
        except ValueError:
            response = _response(
                message.get("id"),
                error=_error(-32010, "MCP result exceeds the remote relay limit"),
            )

        callback = (
            f"{str(self.config.rest_base_url).rstrip('/')}"
            f"/api/v1/systems/mcp/remote/{relay_id}/result"
        )
        headers = {
            "X-Megas-Worker-Id": str(self.config.worker_id),
            "X-Megas-Worker-Token": str(self.config.worker_token),
        }
        async with httpx.AsyncClient(timeout=CALLBACK_TIMEOUT_SECONDS) as client:
            result = await client.post(callback, json={"response": response}, headers=headers)
            if result.status_code != 200:
                raise RuntimeError(f"remote MCP callback rejected with HTTP {result.status_code}")


__all__ = [
    "CALLBACK_TIMEOUT_SECONDS",
    "MAX_REMOTE_MCP_BYTES",
    "RemoteMCPRelay",
]
