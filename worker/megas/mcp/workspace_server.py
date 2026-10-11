from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, TextIO

from megas import local_testing, workspace_cli

from .argparse_bridge import convert_argparse_parser
from .cli_bridge import build_argv


SERVER_VERSION = "0.4.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2024-11-05")
WORKSPACE_TOOL_NAMES = (
    "projects",
    "check",
    "open",
    "start",
    "stop",
    "restart",
    "status",
    "ports",
    "logs",
)
READ_ONLY_WORKSPACE_TOOLS = {"projects", "check", "status", "ports", "logs"}
DESTRUCTIVE_WORKSPACE_TOOLS = {"stop", "restart"}


def _error(code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        value["data"] = data
    return value


def _response(request_id: Any, *, result: Any = None, error: Any = None) -> dict[str, Any]:
    value: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is None:
        value["result"] = result
    else:
        value["error"] = error
    return value


def _tool_definition(tool: Any) -> dict[str, Any]:
    exposed_name = f"workspace_{tool.name}"
    return {
        "name": exposed_name,
        "title": f"Workspace {tool.title or tool.name.replace('_', ' ').title()}",
        "description": tool.description,
        "inputSchema": tool.input_schema(),
        "annotations": {
            "readOnlyHint": tool.name in READ_ONLY_WORKSPACE_TOOLS,
            "destructiveHint": tool.name in DESTRUCTIVE_WORKSPACE_TOOLS,
            "openWorldHint": False,
        },
        "securitySchemes": [{"type": "noauth"}],
    }


@dataclass(slots=True)
class WorkspaceCapabilityCatalog:
    parser: Any = field(init=False, repr=False)
    manifest: Any = field(init=False, repr=False)
    tools_by_exposed_name: dict[str, Any] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.parser = workspace_cli.build_parser()
        conversion = convert_argparse_parser(
            self.parser,
            command=["megas"],
            name="MegasMoves Local Workspace",
            description=(
                "Operate only software folders that a human has already added to MegasMoves. "
                "Executable commands remain owner-derived local configuration and are never MCP arguments."
            ),
            include_prefixes=list(WORKSPACE_TOOL_NAMES),
        )
        self.manifest = conversion.manifest
        self.tools_by_exposed_name = {
            f"workspace_{tool.name}": tool for tool in self.manifest.tools
        }

    def tools(self) -> list[dict[str, Any]]:
        return [
            _tool_definition(tool)
            for _, tool in sorted(self.tools_by_exposed_name.items())
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("tools/call arguments must be a JSON object")
        tool = self.tools_by_exposed_name.get(name)
        if tool is None:
            raise KeyError(name)

        argv = build_argv(self.manifest, tool, arguments)
        parsed = self.parser.parse_args(argv[1:])
        if tool.name in {"start", "restart"}:
            result = local_testing.workspace_action(
                tool.name,
                str(parsed.project),
                service=getattr(parsed, "service", None),
            )
        else:
            result = workspace_cli.run(parsed)
        text = json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": result,
            "isError": False,
        }


@dataclass(slots=True)
class WorkspaceMCPServer:
    catalog: WorkspaceCapabilityCatalog = field(default_factory=WorkspaceCapabilityCatalog)

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = str(params.get("protocolVersion") or "")
        protocol = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return {
            "protocolVersion": protocol,
            "serverInfo": {"name": "MegasMoves Workspace", "version": SERVER_VERSION},
            "capabilities": {"tools": {"listChanged": False}},
            "instructions": (
                "MegasMoves workspace tools operate only folders already added by a human. "
                "Use workspace_check/status/ports before starting or changing local runtime state."
            ),
        }

    def dispatch(self, method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        actual = params or {}
        if method == "initialize":
            return self.initialize(actual)
        if method == "tools/list":
            return {"tools": self.catalog.tools()}
        if method == "tools/call":
            name = str(actual.get("name") or "").strip()
            arguments = actual.get("arguments") or {}
            if not name:
                raise ValueError("tools/call requires a non-empty tool name")
            if not isinstance(arguments, dict):
                raise ValueError("tools/call arguments must be a JSON object")
            return self.catalog.call(name, arguments)
        if method == "ping":
            return {}
        if method == "resources/list":
            return {"resources": []}
        if method == "prompts/list":
            return {"prompts": []}
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        raise KeyError(method)


def serve_workspace_stdio(
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    server = WorkspaceMCPServer()

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
                error=_error(-32601, f"method not found or workspace tool unavailable: {exc.args[0]}"),
            )
        except (ValueError, RuntimeError, TimeoutError, PermissionError, OSError) as exc:
            if request_id is None:
                print(
                    f"MegasMoves workspace MCP request error: {type(exc).__name__}",
                    file=error_stream,
                    flush=True,
                )
                continue
            response = _response(
                request_id,
                error=_error(-32602, str(exc), {"type": type(exc).__name__}),
            )
        except Exception as exc:
            if request_id is None:
                print(
                    f"MegasMoves workspace MCP internal error: {type(exc).__name__}",
                    file=error_stream,
                    flush=True,
                )
                continue
            response = _response(
                request_id,
                error=_error(-32603, "internal workspace error", {"type": type(exc).__name__}),
            )
            print(
                "MegasMoves workspace MCP internal error:\n" + "".join(traceback.format_exception(exc)),
                file=error_stream,
                flush=True,
            )
        output_stream.write(json.dumps(response, separators=(",", ":"), ensure_ascii=False) + "\n")
        output_stream.flush()


__all__ = [
    "WorkspaceCapabilityCatalog",
    "WorkspaceMCPServer",
    "serve_workspace_stdio",
]
