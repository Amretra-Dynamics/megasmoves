"""MegasMCP composition for the standalone MegasMoves distribution.

This module composes the original MegasActions, BrowserOperatorCatalog and
WorkspaceCapabilityCatalog implementations. No external product authority or account transport is mounted.
"""
from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from megas.actions import MegasActions, tools as megas_action_tools
from megas.mcp.browser_operator import BrowserOperatorCatalog
from megas.mcp.workspace_server import WorkspaceCapabilityCatalog

PROTOCOL = "2025-06-18"
MAX_RPC_BYTES = 1_048_576


class MegasMCP:
    def __init__(self) -> None:
        self.actions = MegasActions()
        self.browser = BrowserOperatorCatalog()
        self.workspaces = WorkspaceCapabilityCatalog()
        tool_definitions = [
            *megas_action_tools(),
            *self.browser.tools(),
            *self.workspaces.tools(),
        ]
        self._tools = {tool["name"]: tool for tool in tool_definitions}
        if len(self._tools) != len(tool_definitions):
            raise ValueError("duplicate MCP capability identifiers")
        self._tools["operator_status"] = {
            "name": "operator_status",
            "description": "Inspect this MegasMoves operator's native capabilities",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        }

    def dispatch(self, method: str, params: Any) -> Any:
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL,
                "serverInfo": {"name": "MegasMoves", "version": "0.5.0"},
                "capabilities": {"tools": {"listChanged": False}},
            }
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": list(self._tools.values())}
        if method != "tools/call":
            raise LookupError("method not found")
        if not isinstance(params, dict) or set(params) - {"name", "arguments"}:
            raise ValueError("invalid tool call")
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or name not in self._tools:
            raise LookupError("unknown tool")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        if name == "operator_status":
            if arguments:
                raise ValueError("unexpected arguments")
            status = {
                "product": "MegasMoves",
                "mcp": "local_stdio",
                "capability_count": len(self._tools),
                "browser": self.browser.status(),
            }
            return {"content": [{"type": "text", "text": json.dumps(status, sort_keys=True)}], "isError": False}
        if name.startswith("megas_action_"):
            return self.actions.call(name, arguments)
        if name.startswith("browser_"):
            return self.browser.call(name, arguments)
        if name.startswith("workspace_"):
            return self.workspaces.call(name, arguments)
        raise LookupError("unknown capability")

    def close(self) -> None:
        self.browser.host.shutdown()


def _answer(request_id: Any, *, result: Any = None, error: tuple[int, str] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is None:
        msg["result"] = result
    else:
        msg["error"] = {"code": error[0], "message": error[1]}
    return msg


def serve(*, input_stream: TextIO | None = None, output_stream: TextIO | None = None,
          operator: MegasMCP | None = None) -> None:
    input_stream = input_stream if input_stream is not None else sys.stdin
    output_stream = output_stream if output_stream is not None else sys.stdout
    op = operator if operator is not None else MegasMCP()
    try:
        for line in input_stream:
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > MAX_RPC_BYTES:
                response = _answer(None, error=(-32600, "request exceeds limit"))
            else:
                try:
                    request = json.loads(line)
                except ValueError:
                    response = _answer(None, error=(-32700, "parse error"))
                else:
                    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                        response = _answer(None, error=(-32600, "invalid request"))
                    elif "id" in request and (isinstance(request["id"], bool) or
                          not isinstance(request["id"], (str, int, float, type(None)))):
                        response = _answer(None, error=(-32600, "invalid id"))
                    else:
                        respond = "id" in request
                        request_id = request.get("id")
                        try:
                            payload = op.dispatch(request["method"], request.get("params"))
                            response = _answer(request_id, result=payload) if respond and payload is not None else None
                        except LookupError:
                            response = _answer(request_id, error=(-32601, "method or tool not found")) if respond else None
                        except (ValueError, PermissionError, OSError, RuntimeError):
                            response = _answer(request_id, error=(-32602, "invalid or unauthorized request")) if respond else None
                        except Exception:
                            response = _answer(request_id, error=(-32603, "internal operator error")) if respond else None
            if response is not None:
                output_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
                output_stream.flush()
    finally:
        op.close()


__all__ = ["MegasMCP", "serve"]
