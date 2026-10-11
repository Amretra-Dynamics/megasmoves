from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .cli_bridge import CLI_BRIDGE_FORMAT, execute_tool, load_manifest, save_manifest
from .cli_server import serve_manifest as serve_cli_manifest
from .gateway import (
    GATEWAY_FORMAT,
    CLISource,
    GatewayCatalog,
    MCPSource,
    load_gateway,
    refresh_mcp_fingerprints,
    save_gateway,
)
from .gateway_server import serve_gateway


MAX_CAPABILITY_MANIFEST_BYTES = 2 * 1024 * 1024


def manifest_format(path: Path | str) -> str:
    file_path = Path(path).expanduser()
    raw = file_path.read_bytes()
    if len(raw) > MAX_CAPABILITY_MANIFEST_BYTES:
        raise ValueError("Megas capability manifest exceeds 2 MiB")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Megas capability manifest must be a JSON object")
    format_name = str(value.get("format") or "")
    if format_name not in {CLI_BRIDGE_FORMAT, GATEWAY_FORMAT}:
        raise ValueError(f"unsupported Megas capability manifest format: {format_name}")
    return format_name


def validate_capability_manifest(path: Path | str) -> dict[str, Any]:
    format_name = manifest_format(path)
    if format_name == CLI_BRIDGE_FORMAT:
        manifest = load_manifest(path)
        return {
            "format": manifest.format,
            "version": manifest.version,
            "name": manifest.name,
            "review_state": manifest.review_state,
            "generated_by": manifest.generated_by,
            "command": manifest.command,
            "fixed_args": manifest.fixed_args,
            "tool_count": len(manifest.tools),
            "tools": [
                tool.mcp_tool()
                for tool in sorted(manifest.tools, key=lambda row: row.name)
            ],
        }

    gateway = load_gateway(path)
    stored_fingerprints = {
        source.namespace: dict(source.tool_fingerprints)
        for source in gateway.sources
        if isinstance(source, MCPSource)
    }
    current_fingerprints = refresh_mcp_fingerprints(gateway)
    tools = GatewayCatalog(gateway, enforce_mcp_fingerprints=False).tools()
    fingerprints_match = all(
        stored_fingerprints.get(namespace, {}) == fingerprints
        for namespace, fingerprints in current_fingerprints.items()
    )
    return {
        "format": gateway.format,
        "version": gateway.version,
        "name": gateway.name,
        "review_state": gateway.review_state,
        "source_count": len(gateway.sources),
        "sources": [source.to_dict() for source in gateway.sources],
        "tool_count": len(tools),
        "tools": tools,
        "stored_tool_fingerprints": stored_fingerprints,
        "current_tool_fingerprints": current_fingerprints,
        "tool_fingerprints_match_review": fingerprints_match,
        "ready_to_serve": gateway.review_state == "reviewed" and fingerprints_match,
    }


def execute_capability_tool(
    path: Path | str,
    tool: str,
    arguments: dict[str, Any],
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    format_name = manifest_format(path)
    if format_name == CLI_BRIDGE_FORMAT:
        manifest = load_manifest(path)
        if manifest.review_state != "reviewed":
            raise PermissionError(
                "CLI→MCP manifest is unreviewed; explicitly approve it before execution"
            )
        execution = execute_tool(
            manifest,
            tool,
            arguments,
            timeout_seconds=timeout_seconds,
        )
        return {"tool": execution.tool, **execution.structured()}

    gateway = load_gateway(path)
    if gateway.review_state != "reviewed":
        raise PermissionError("gateway manifest is unreviewed; explicitly approve it first")
    return {"tool": tool, **GatewayCatalog(gateway).call(tool, arguments)}


def serve_capability_manifest(path: Path | str) -> None:
    format_name = manifest_format(path)
    if format_name == CLI_BRIDGE_FORMAT:
        serve_cli_manifest(path)
    else:
        serve_gateway(path)


def approve_capability_manifest(
    path: Path | str,
    *,
    output: Path | str | None = None,
) -> dict[str, Any]:
    format_name = manifest_format(path)
    source_path = Path(path).expanduser().resolve()
    destination = (
        Path(output).expanduser().resolve()
        if output is not None
        else source_path
    )
    if format_name == CLI_BRIDGE_FORMAT:
        manifest = load_manifest(source_path)
        manifest.review_state = "reviewed"
        saved = save_manifest(destination, manifest)
        return {
            "written": str(saved),
            "format": manifest.format,
            "review_state": manifest.review_state,
            "tool_count": len(manifest.tools),
            "tools": [tool.name for tool in sorted(manifest.tools, key=lambda row: row.name)],
        }

    gateway = load_gateway(source_path)
    fingerprints = refresh_mcp_fingerprints(gateway)
    # Review is a complete gateway preflight, not just a state flip. This verifies
    # referenced CLI manifests are already reviewed, every allowlisted tool exists,
    # namespace exposure is collision-free, and the just-pinned MCP contracts form
    # a coherent tools/list surface before the gateway can become executable.
    reviewed_tools = GatewayCatalog(
        gateway,
        enforce_mcp_fingerprints=True,
    ).tools()
    gateway.review_state = "reviewed"
    if destination.parent != source_path.parent:
        for source in gateway.sources:
            if isinstance(source, CLISource):
                source.manifest = str(gateway.resolve_cli_manifest(source))
    saved = save_gateway(destination, gateway)
    return {
        "written": str(saved),
        "format": gateway.format,
        "review_state": gateway.review_state,
        "source_count": len(gateway.sources),
        "tool_count": len(reviewed_tools),
        "tools": [str(tool.get("name") or "") for tool in reviewed_tools],
        "pinned_upstream_tool_fingerprints": fingerprints,
    }


__all__ = [
    "approve_capability_manifest",
    "execute_capability_tool",
    "manifest_format",
    "serve_capability_manifest",
    "validate_capability_manifest",
]
