from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .cli_bridge import CLIBridgeManifest, execute_tool, load_manifest


SERVER_NAME = "Megas CLI MCP"
SERVER_VERSION = "0.3.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2024-11-05")


def _error(code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        payload["data"] = data
    return payload


def _response(request_id: Any, *, result: Any = None, error: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return payload


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


@dataclass(slots=True)
class CLIMCPServer:
    manifest: CLIBridgeManifest

    def __post_init__(self) -> None:
        self.manifest.validate()
        if self.manifest.review_state != "reviewed":
            raise PermissionError(
                "CLI→MCP manifest is unreviewed; explicitly approve it before serving"
            )

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = str(params.get("protocolVersion") or "")
        protocol = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return {
            "protocolVersion": protocol,
            "serverInfo": {
                "name": self.manifest.name or SERVER_NAME,
                "version": SERVER_VERSION,
            },
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "instructions": (
                self.manifest.description.strip()
                or "Use the exposed CLI tools as typed capabilities. Megas executes only declared argv templates and never provides arbitrary shell access."
            ),
        }

    def tools_list(self) -> dict[str, Any]:
        return {"tools": [tool.mcp_tool() for tool in sorted(self.manifest.tools, key=lambda row: row.name)]}

    def tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        arguments = params.get("arguments") or {}
        if not name:
            raise ValueError("tools/call requires a non-empty tool name")
        if not isinstance(arguments, dict):
            raise ValueError("tools/call arguments must be a JSON object")
        execution = execute_tool(self.manifest, name, arguments)
        structured = execution.structured()
        primary = execution.result if execution.result not in (None, "") else execution.stderr
        return {
            "content": [{"type": "text", "text": _text_content(primary)}],
            "structuredContent": structured,
            "isError": not execution.ok,
        }

    def dispatch(self, method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        actual = params or {}
        if method == "initialize":
            return self.initialize(actual)
        if method == "tools/list":
            return self.tools_list()
        if method == "tools/call":
            return self.tools_call(actual)
        if method == "ping":
            return {}
        if method == "resources/list":
            return {"resources": []}
        if method == "prompts/list":
            return {"prompts": []}
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        raise KeyError(method)


def serve_stdio(
    manifest: CLIBridgeManifest,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    server = CLIMCPServer(manifest)

    for line in input_stream:
        text = line.strip()
        if not text:
            continue
        request_id: Any = None
        try:
            message = json.loads(text)
            if not isinstance(message, dict):
                raise ValueError("MCP message must be a JSON object")
            if message.get("jsonrpc") != "2.0":
                raise ValueError("MCP message must use jsonrpc 2.0")
            request_id = message.get("id")
            method = str(message.get("method") or "")
            if not method:
                raise ValueError("MCP message is missing method")
            params = message.get("params")
            if params is not None and not isinstance(params, dict):
                raise ValueError("MCP params must be a JSON object")
            result = server.dispatch(method, params)
            if request_id is None or result is None:
                continue
            response = _response(request_id, result=result)
        except KeyError as exc:
            if request_id is None:
                continue
            response = _response(
                request_id,
                error=_error(-32601, f"method not found: {exc.args[0]}"),
            )
        except (ValueError, RuntimeError, TimeoutError, PermissionError, OSError) as exc:
            if request_id is None:
                print(f"Megas CLI MCP request error: {type(exc).__name__}", file=error_stream, flush=True)
                continue
            response = _response(
                request_id,
                error=_error(-32602, str(exc), {"type": type(exc).__name__}),
            )
        except Exception as exc:
            if request_id is None:
                print(f"Megas CLI MCP internal error: {type(exc).__name__}", file=error_stream, flush=True)
                continue
            response = _response(
                request_id,
                error=_error(-32603, "internal CLI bridge error", {"type": type(exc).__name__}),
            )
            print(
                "Megas CLI MCP internal error:\n" + "".join(traceback.format_exception(exc)),
                file=error_stream,
                flush=True,
            )
        output_stream.write(json.dumps(response, separators=(",", ":"), ensure_ascii=False) + "\n")
        output_stream.flush()


def serve_manifest(path: Path | str) -> None:
    serve_stdio(load_manifest(path))


__all__ = ["CLIMCPServer", "serve_manifest", "serve_stdio"]
