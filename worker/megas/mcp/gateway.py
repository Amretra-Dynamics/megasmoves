from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cli_bridge import CLIBridgeManifest, load_manifest
from .model import MCPServer
from .protocol import call_tool, probe_server
from .workspace_server import WorkspaceCapabilityCatalog


GATEWAY_FORMAT = "megas-mcp-gateway"
GATEWAY_VERSION = 1
MAX_GATEWAY_BYTES = 2 * 1024 * 1024
MAX_SOURCES = 64
MAX_ALLOWED_TOOLS = 1024
REDACTED = "[REDACTED]"


def _nonempty(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must be non-empty")
    return text


def _namespace(value: Any) -> str:
    text = _nonempty(value, field_name="source namespace").lower().replace("-", "_")
    if not all(ch.isalnum() or ch == "_" for ch in text):
        raise ValueError("source namespace may contain only letters, numbers, '_' and '-'")
    return text


def exposed_tool_name(namespace: str, upstream_name: str) -> str:
    original = _nonempty(upstream_name, field_name="tool name")
    prefix = namespace + "_"
    return original if original.lower().startswith(prefix.lower()) else f"{namespace}_{original}"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def tool_contract_fingerprint(item: dict[str, Any]) -> str:
    contract = {
        "name": str(item.get("name") or ""),
        "title": str(item.get("title") or ""),
        "description": str(item.get("description") or ""),
        "input_schema": item.get("input_schema") or {},
        "output_schema": item.get("output_schema") or {},
        "annotations": item.get("annotations") or {},
        "security_schemes": item.get("security_schemes") or [],
        "_meta": item.get("_meta") or {},
    }
    return hashlib.sha256(_canonical_bytes(contract)).hexdigest()


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value.lower())


def _redact(value: Any, secrets: list[str]) -> Any:
    active = [secret for secret in secrets if secret]
    if isinstance(value, str):
        text = value
        for secret in active:
            text = text.replace(secret, REDACTED)
        return text
    if isinstance(value, dict):
        return {str(key): _redact(item, active) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, active) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, active) for item in value]
    return value


def _tool_from_probe(item: dict[str, Any], exposed_name: str) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": exposed_name,
        "title": str(item.get("title") or exposed_name.replace("_", " ").title()),
        "description": str(item.get("description") or ""),
        "inputSchema": item.get("input_schema") or {},
    }
    output_schema = item.get("output_schema")
    if isinstance(output_schema, dict) and output_schema:
        tool["outputSchema"] = output_schema
    annotations = item.get("annotations")
    if isinstance(annotations, dict):
        tool["annotations"] = dict(annotations)
    schemes = item.get("security_schemes")
    if isinstance(schemes, list) and schemes:
        tool["securitySchemes"] = list(schemes)
    meta = item.get("_meta")
    if isinstance(meta, dict) and meta:
        tool["_meta"] = dict(meta)
    return tool


@dataclass(slots=True)
class CLISource:
    namespace: str
    manifest: str
    allow_tools: list[str]

    def validate(self) -> None:
        self.namespace = _namespace(self.namespace)
        self.manifest = _nonempty(self.manifest, field_name=f"CLI source {self.namespace} manifest")
        self.allow_tools = sorted({_nonempty(value, field_name="allowed CLI tool") for value in self.allow_tools})
        if not self.allow_tools:
            raise ValueError(f"CLI source {self.namespace} must explicitly allow at least one tool")
        if len(self.allow_tools) > MAX_ALLOWED_TOOLS:
            raise ValueError("CLI source allow_tools exceeds maximum")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "type": "cli",
            "namespace": self.namespace,
            "manifest": self.manifest,
            "allow_tools": list(self.allow_tools),
        }


@dataclass(slots=True)
class WorkspaceSource:
    namespace: str = "workspace"
    allow_tools: list[str] = field(default_factory=list)

    def validate(self) -> None:
        self.namespace = _namespace(self.namespace)
        self.allow_tools = sorted(
            {_nonempty(value, field_name="allowed workspace tool") for value in self.allow_tools}
        )
        if not self.allow_tools:
            raise ValueError(f"workspace source {self.namespace} must explicitly allow at least one tool")
        if len(self.allow_tools) > MAX_ALLOWED_TOOLS:
            raise ValueError("workspace source allow_tools exceeds maximum")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "type": "workspace",
            "namespace": self.namespace,
            "allow_tools": list(self.allow_tools),
        }


@dataclass(slots=True)
class MCPSource:
    namespace: str
    server: MCPServer
    allow_tools: list[str]
    timeout_seconds: float = 10.0
    tool_fingerprints: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        self.namespace = _namespace(self.namespace)
        self.server.validate()
        self.allow_tools = sorted({_nonempty(value, field_name="allowed MCP tool") for value in self.allow_tools})
        if not self.allow_tools:
            raise ValueError(f"MCP source {self.namespace} must explicitly allow at least one tool")
        if len(self.allow_tools) > MAX_ALLOWED_TOOLS:
            raise ValueError("MCP source allow_tools exceeds maximum")
        self.timeout_seconds = float(self.timeout_seconds)
        if not (0 < self.timeout_seconds <= 300):
            raise ValueError("MCP source timeout_seconds must be between 0 and 300")
        normalized: dict[str, str] = {}
        for raw_name, raw_fingerprint in self.tool_fingerprints.items():
            name = _nonempty(raw_name, field_name="fingerprinted MCP tool")
            if name not in self.allow_tools:
                raise ValueError(
                    f"MCP source {self.namespace} fingerprints non-allowlisted tool: {name}"
                )
            fingerprint = str(raw_fingerprint or "").strip().lower()
            if not _valid_sha256(fingerprint):
                raise ValueError(f"invalid SHA-256 fingerprint for MCP tool {name}")
            normalized[name] = fingerprint
        self.tool_fingerprints = dict(sorted(normalized.items()))

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "type": "mcp",
            "namespace": self.namespace,
            "server": self.server.to_dict(),
            "allow_tools": list(self.allow_tools),
            "timeout_seconds": self.timeout_seconds,
            "tool_fingerprints": dict(self.tool_fingerprints),
        }


GatewaySource = CLISource | WorkspaceSource | MCPSource


@dataclass(slots=True)
class GatewayManifest:
    name: str
    sources: list[GatewaySource]
    description: str = ""
    review_state: str = "unreviewed"
    format: str = GATEWAY_FORMAT
    version: int = GATEWAY_VERSION
    base_dir: Path | None = field(default=None, repr=False)

    def validate(self) -> None:
        if self.format != GATEWAY_FORMAT:
            raise ValueError(f"unsupported gateway format: {self.format}")
        if self.version != GATEWAY_VERSION:
            raise ValueError(f"unsupported gateway version: {self.version}")
        self.name = _nonempty(self.name, field_name="gateway name")
        self.review_state = str(self.review_state or "unreviewed").strip().lower()
        if self.review_state not in {"reviewed", "unreviewed"}:
            raise ValueError(f"unsupported gateway review_state: {self.review_state}")
        if not self.sources:
            raise ValueError("gateway must contain at least one source")
        if len(self.sources) > MAX_SOURCES:
            raise ValueError("gateway source count exceeds maximum")
        namespaces: set[str] = set()
        for source in self.sources:
            source.validate()
            if source.namespace in namespaces:
                raise ValueError(f"duplicate gateway source namespace: {source.namespace}")
            namespaces.add(source.namespace)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "format": self.format,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "review_state": self.review_state,
            "sources": [source.to_dict() for source in self.sources],
        }

    def resolve_cli_manifest(self, source: CLISource) -> Path:
        path = Path(source.manifest).expanduser()
        if path.is_absolute() or self.base_dir is None:
            return path
        return (self.base_dir / path).resolve()


def gateway_from_dict(data: dict[str, Any], *, base_dir: Path | None = None) -> GatewayManifest:
    raw_sources = data.get("sources") or []
    if not isinstance(raw_sources, list):
        raise ValueError("gateway sources must be an array")
    sources: list[GatewaySource] = []
    for row in raw_sources:
        if not isinstance(row, dict):
            raise ValueError("gateway source must be an object")
        source_type = str(row.get("type") or "").strip().lower()
        if source_type == "cli":
            sources.append(
                CLISource(
                    namespace=str(row.get("namespace") or ""),
                    manifest=str(row.get("manifest") or ""),
                    allow_tools=[str(value) for value in (row.get("allow_tools") or [])],
                )
            )
        elif source_type == "workspace":
            sources.append(
                WorkspaceSource(
                    namespace=str(row.get("namespace") or "workspace"),
                    allow_tools=[str(value) for value in (row.get("allow_tools") or [])],
                )
            )
        elif source_type == "mcp":
            server_data = row.get("server")
            if not isinstance(server_data, dict):
                raise ValueError("MCP gateway source requires a server object")
            raw_fingerprints = row.get("tool_fingerprints") or {}
            if not isinstance(raw_fingerprints, dict):
                raise ValueError("MCP source tool_fingerprints must be an object")
            sources.append(
                MCPSource(
                    namespace=str(row.get("namespace") or ""),
                    server=MCPServer.from_dict(server_data),
                    allow_tools=[str(value) for value in (row.get("allow_tools") or [])],
                    timeout_seconds=float(row.get("timeout_seconds") or 10.0),
                    tool_fingerprints={
                        str(key): str(value)
                        for key, value in raw_fingerprints.items()
                    },
                )
            )
        else:
            raise ValueError(f"unsupported gateway source type: {source_type}")
    manifest = GatewayManifest(
        format=str(data.get("format") or ""),
        version=int(data.get("version") or 0),
        name=str(data.get("name") or ""),
        description=str(data.get("description") or ""),
        review_state=str(data.get("review_state") or "unreviewed"),
        sources=sources,
        base_dir=base_dir,
    )
    manifest.validate()
    return manifest


def load_gateway(path: Path | str) -> GatewayManifest:
    file_path = Path(path).expanduser()
    raw = file_path.read_bytes()
    if len(raw) > MAX_GATEWAY_BYTES:
        raise ValueError("gateway manifest exceeds 2 MiB")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("gateway manifest must be a JSON object")
    return gateway_from_dict(data, base_dir=file_path.resolve().parent)


def save_gateway(path: Path | str, manifest: GatewayManifest) -> Path:
    manifest.validate()
    file_path = Path(path).expanduser()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(file_path, 0o600)
    except OSError:
        pass
    return file_path


def refresh_mcp_fingerprints(manifest: GatewayManifest) -> dict[str, dict[str, str]]:
    """Probe approved upstream tools and pin their exact current public contracts."""

    evidence: dict[str, dict[str, str]] = {}
    for source in manifest.sources:
        if not isinstance(source, MCPSource):
            continue
        probe = probe_server(source.server, timeout=source.timeout_seconds)
        reported = {
            str(item.get("name") or ""): item
            for item in (probe.get("tools", {}).get("items") or [])
            if isinstance(item, dict) and item.get("name")
        }
        missing = [name for name in source.allow_tools if name not in reported]
        if missing:
            raise RuntimeError(
                f"MCP source {source.namespace} does not report approved tool(s): {', '.join(missing)}"
            )
        source.tool_fingerprints = {
            name: tool_contract_fingerprint(reported[name])
            for name in source.allow_tools
        }
        evidence[source.namespace] = dict(source.tool_fingerprints)
    manifest.validate()
    return evidence


@dataclass(slots=True)
class RoutedTool:
    exposed_name: str
    original_name: str
    source: GatewaySource
    tool: dict[str, Any]
    cli_manifest: CLIBridgeManifest | None = None
    workspace_catalog: WorkspaceCapabilityCatalog | None = None


class GatewayCatalog:
    def __init__(
        self,
        manifest: GatewayManifest,
        *,
        enforce_mcp_fingerprints: bool = True,
    ) -> None:
        manifest.validate()
        self.manifest = manifest
        self.enforce_mcp_fingerprints = enforce_mcp_fingerprints

    def _cli_tools(self, source: CLISource) -> list[RoutedTool]:
        cli = load_manifest(self.manifest.resolve_cli_manifest(source))
        if cli.review_state != "reviewed":
            raise PermissionError(
                f"CLI source {source.namespace} is unreviewed; approve its manifest before serving it through the gateway"
            )
        rows: list[RoutedTool] = []
        for original in source.allow_tools:
            tool = cli.tool(original)
            exposed = exposed_tool_name(source.namespace, original)
            mcp_tool = tool.mcp_tool()
            mcp_tool["name"] = exposed
            rows.append(
                RoutedTool(
                    exposed_name=exposed,
                    original_name=original,
                    source=source,
                    tool=mcp_tool,
                    cli_manifest=cli,
                )
            )
        return rows

    def _workspace_tools(self, source: WorkspaceSource) -> list[RoutedTool]:
        catalog = WorkspaceCapabilityCatalog()
        reported = {str(item.get("name") or ""): item for item in catalog.tools()}
        missing = [name for name in source.allow_tools if name not in reported]
        if missing:
            raise RuntimeError(
                f"workspace source {source.namespace} does not provide approved tool(s): {', '.join(missing)}"
            )
        rows: list[RoutedTool] = []
        for original in source.allow_tools:
            exposed = exposed_tool_name(source.namespace, original)
            tool = dict(reported[original])
            tool["name"] = exposed
            rows.append(
                RoutedTool(
                    exposed_name=exposed,
                    original_name=original,
                    source=source,
                    tool=tool,
                    workspace_catalog=catalog,
                )
            )
        return rows

    def _mcp_tools(self, source: MCPSource) -> list[RoutedTool]:
        probe = probe_server(source.server, timeout=source.timeout_seconds)
        reported = {
            str(item.get("name") or ""): item
            for item in (probe.get("tools", {}).get("items") or [])
            if isinstance(item, dict) and item.get("name")
        }
        missing = [name for name in source.allow_tools if name not in reported]
        if missing:
            raise RuntimeError(
                f"MCP source {source.namespace} no longer reports approved tool(s): {', '.join(missing)}"
            )
        if self.enforce_mcp_fingerprints:
            for name in source.allow_tools:
                expected = source.tool_fingerprints.get(name)
                if not expected:
                    raise PermissionError(
                        f"MCP source {source.namespace}/{name} has no reviewed tool fingerprint; run 'megas review <gateway> --approve'"
                    )
                actual = tool_contract_fingerprint(reported[name])
                if actual != expected:
                    raise PermissionError(
                        f"MCP source {source.namespace}/{name} contract changed after review; review the gateway again before exposing it"
                    )
        return [
            RoutedTool(
                exposed_name=exposed_tool_name(source.namespace, original),
                original_name=original,
                source=source,
                tool=_tool_from_probe(
                    reported[original],
                    exposed_tool_name(source.namespace, original),
                ),
            )
            for original in source.allow_tools
        ]

    def routes(self) -> dict[str, RoutedTool]:
        routes: dict[str, RoutedTool] = {}
        for source in self.manifest.sources:
            if isinstance(source, CLISource):
                rows = self._cli_tools(source)
            elif isinstance(source, WorkspaceSource):
                rows = self._workspace_tools(source)
            else:
                rows = self._mcp_tools(source)
            for row in rows:
                if row.exposed_name in routes:
                    previous = routes[row.exposed_name]
                    raise ValueError(
                        f"gateway tool collision: {row.exposed_name} from {source.namespace} conflicts with {previous.source.namespace}"
                    )
                routes[row.exposed_name] = row
        return routes

    def tools(self) -> list[dict[str, Any]]:
        return [route.tool for _, route in sorted(self.routes().items())]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("tools/call arguments must be a JSON object")
        route = self.routes().get(name)
        if route is None:
            raise KeyError(f"gateway tool is not approved or does not exist: {name}")
        if isinstance(route.source, CLISource):
            assert route.cli_manifest is not None
            from .cli_bridge import execute_tool

            execution = execute_tool(route.cli_manifest, route.original_name, arguments)
            primary = execution.result if execution.result not in (None, "") else execution.stderr
            return {
                "content": [{"type": "text", "text": _text(primary)}],
                "structuredContent": execution.structured(),
                "isError": not execution.ok,
            }
        if isinstance(route.source, WorkspaceSource):
            assert route.workspace_catalog is not None
            return route.workspace_catalog.call(route.original_name, arguments)

        source = route.source
        result = call_tool(
            source.server,
            route.original_name,
            arguments,
            timeout=source.timeout_seconds,
        )
        secrets = [
            os.environ.get(env_name, "")
            for env_name in source.server.env_refs.values()
        ]
        clean = _redact(result, secrets)
        content = clean.get("content") or []
        structured = clean.get("structured_content")
        return {
            "content": content,
            **({"structuredContent": structured} if structured is not None else {}),
            "isError": bool(clean.get("is_error", False)),
        }


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


__all__ = [
    "CLISource",
    "GATEWAY_FORMAT",
    "GATEWAY_VERSION",
    "GatewayCatalog",
    "GatewayManifest",
    "MCPSource",
    "RoutedTool",
    "WorkspaceSource",
    "exposed_tool_name",
    "gateway_from_dict",
    "load_gateway",
    "refresh_mcp_fingerprints",
    "save_gateway",
    "tool_contract_fingerprint",
]
