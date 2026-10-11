"""Small local JSON-RPC / MCP stdio surface for human-approved workspaces."""
from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from . import __version__
from .core import TrustStore


_PROTOCOL = "2025-06-18"
_TOOLS = [
    {
        "name": "operator_status",
        "description": "Read-only status for the Community preview; no device control",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "workspace_projects",
        "description": "List explicitly trusted local workspaces",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "workspace_check",
        "description": "Inspect one trusted workspace by id; no commands executed",
        "inputSchema": {
            "type": "object",
            "properties": {"workspace_id": {"type": "string", "minLength": 1}},
            "required": ["workspace_id"],
            "additionalProperties": False,
        },
    },
]


def dispatch(method: str, params: Any, store: TrustStore) -> dict[str, Any] | None:
    if method == "initialize":
        return {
            "protocolVersion": _PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "megasmoves-community", "version": __version__},
        }
    if method.startswith("notifications/"):
        return None
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": _TOOLS}
    if method != "tools/call":
        raise KeyError("method not found")
    if not isinstance(params, dict) or set(params) - {"name", "arguments"}:
        raise ValueError("invalid tool call parameters")
    name = params.get("name")
    args = params.get("arguments", {})
    if not isinstance(args, dict):
        raise ValueError("tool arguments must be an object")
    if name == "operator_status" and not args:
        value = {"product": "MegasMoves Community preview", "version": __version__,
                 "mode": "local_read_only", "trusted_workspaces": len(store.list()),
                 "device_control": False, "remote_execution": False}
    elif name == "workspace_projects" and not args:
        value = {"projects": store.list()}
    elif name == "workspace_check" and set(args) == {"workspace_id"} and isinstance(args["workspace_id"], str):
        value = store.check(args["workspace_id"])
    else:
        raise ValueError("unknown tool or invalid arguments")
    return {"content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}], "isError": False}


def _reply(request_id: Any, *, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        value["error"] = error
    else:
        value["result"] = result
    return value


def serve(*, store: TrustStore | None = None, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    store = store if store is not None else TrustStore()
    input_stream, output_stream = stdin or sys.stdin, stdout or sys.stdout
    for line in input_stream:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            resp = _reply(None, error={"code": -32700, "message": "parse error"})
        else:
            if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
                resp = _reply(req.get("id") if isinstance(req, dict) else None, error={"code": -32600, "message": "invalid request"})
            else:
                req_id = req.get("id")
                if isinstance(req_id, bool) or (req_id is not None and not isinstance(req_id, (str, int, float))):
                    resp = _reply(None, error={"code": -32600, "message": "invalid id"})
                else:
                    wants_response = "id" in req
                    try:
                        result = dispatch(req["method"], req.get("params"), store)
                        resp = _reply(req_id, result=result) if wants_response else None
                    except KeyError:
                        resp = _reply(req_id, error={"code": -32601, "message": "method not found"}) if wants_response else None
                    except (OSError, ValueError, PermissionError):
                        # Fail closed without putting local config/path details in protocol errors.
                        resp = _reply(req_id, error={"code": -32602, "message": "invalid or unauthorized request"}) if wants_response else None
                    except Exception:
                        resp = _reply(req_id, error={"code": -32603, "message": "internal error"}) if wants_response else None
        if resp is not None:
            output_stream.write(json.dumps(resp, separators=(",", ":")) + "\n")
            output_stream.flush()
