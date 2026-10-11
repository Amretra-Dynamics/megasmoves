from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .model import MCPServer
from .store import MCPStore


CONFIG_NAMES = {
    ".mcp.json",
    "mcp.json",
    "mcp_config.json",
    "mcp-config.json",
    "claude_desktop_config.json",
}
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_SCAN_FILES = 500
MAX_SCAN_DEPTH = 5
_ENV_REF = re.compile(r"^\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}$|^\$([A-Za-z_][A-Za-z0-9_]*)$")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fingerprint(path: Path, name: str, descriptor: dict[str, Any]) -> str:
    payload = {"path": str(path), "name": name, "descriptor": descriptor}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _common_config_paths(home: Path | None = None) -> list[Path]:
    base = (home or Path.home()).expanduser()
    paths = [
        base / ".mcp.json",
        base / ".config" / "mcp" / "mcp.json",
        base / ".cursor" / "mcp.json",
        base / ".vscode" / "mcp.json",
        base / ".claude.json",
    ]
    if sys.platform == "darwin":
        paths.append(base / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    else:
        paths.append(base / ".config" / "Claude" / "claude_desktop_config.json")
    return paths


def _iter_requested_files(paths: Iterable[Path]) -> Iterable[Path]:
    seen: set[str] = set()
    yielded = 0
    for requested in paths:
        path = requested.expanduser()
        if path.is_file():
            key = str(path.resolve())
            if key not in seen:
                seen.add(key)
                yielded += 1
                yield path
            if yielded >= MAX_SCAN_FILES:
                return
            continue
        if not path.is_dir():
            continue
        root_depth = len(path.resolve().parts)
        try:
            iterator = path.rglob("*")
            for candidate in iterator:
                if yielded >= MAX_SCAN_FILES:
                    return
                try:
                    resolved = candidate.resolve()
                except OSError:
                    continue
                if len(resolved.parts) - root_depth > MAX_SCAN_DEPTH:
                    continue
                if not candidate.is_file() or candidate.name.lower() not in CONFIG_NAMES:
                    continue
                key = str(resolved)
                if key in seen:
                    continue
                seen.add(key)
                yielded += 1
                yield candidate
        except OSError:
            continue


def _normalize_transport(value: Any, *, has_url: bool) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    if raw in {"sse"}:
        return "sse"
    if raw in {"streamable-http", "streamablehttp", "streamable"}:
        return "streamable-http"
    if raw in {"http", "https"}:
        return "http"
    return "http" if has_url else "stdio"


def _env_reference(target: str, value: Any) -> tuple[str, bool]:
    text = str(value or "").strip()
    match = _ENV_REF.match(text)
    if match:
        return str(match.group(1) or match.group(2)), False
    # Literal values are deliberately not copied. Rebind from the same variable
    # name on this machine instead of leaking the source config's value.
    return target, bool(text)


@dataclass(slots=True)
class DiscoveryCandidate:
    fingerprint: str
    source_path: str
    source_section: str
    server: MCPServer
    warnings: list[str] = field(default_factory=list)

    def safe_dict(self) -> dict[str, Any]:
        descriptor = self.server.to_dict()
        descriptor["env"] = {}
        return {
            "fingerprint": self.fingerprint,
            "source_path": self.source_path,
            "source_section": self.source_section,
            "server": descriptor,
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class DiscoveryError:
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


@dataclass(slots=True)
class DiscoveryResult:
    candidates: list[DiscoveryCandidate]
    errors: list[DiscoveryError]
    scanned_files: int


class DiscoveryEngine:
    """Deterministic local MCP config discovery with explicit import preview."""

    def __init__(self, store: MCPStore) -> None:
        self.store = store

    @staticmethod
    def common_paths() -> list[Path]:
        return _common_config_paths()

    @staticmethod
    def _server_sections(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        for key in ("mcpServers", "mcp_servers", "servers"):
            value = payload.get(key)
            if isinstance(value, dict):
                return [
                    (str(name), descriptor)
                    for name, descriptor in value.items()
                    if isinstance(descriptor, dict)
                ]
        return []

    @staticmethod
    def _candidate(path: Path, name: str, raw: dict[str, Any], section: str) -> DiscoveryCandidate:
        url = raw.get("url") or raw.get("endpoint")
        transport = _normalize_transport(raw.get("type") or raw.get("transport"), has_url=bool(url))
        command = raw.get("command")
        if isinstance(command, list):
            parts = [str(value) for value in command]
            command = parts[0] if parts else None
            command_args = parts[1:]
        else:
            command = str(command) if command is not None else None
            command_args = []
        args = command_args + [str(value) for value in (raw.get("args") or [])]

        cwd_value = raw.get("cwd")
        cwd: str | None = None
        if cwd_value:
            candidate_cwd = Path(str(cwd_value)).expanduser()
            if not candidate_cwd.is_absolute():
                candidate_cwd = path.parent / candidate_cwd
            cwd = str(candidate_cwd.resolve(strict=False))

        env_refs: dict[str, str] = {}
        warnings: list[str] = []
        env = raw.get("env") or {}
        literal_targets: list[str] = []
        if isinstance(env, dict):
            for key, value in env.items():
                target = str(key).strip()
                if not target:
                    continue
                source, was_literal = _env_reference(target, value)
                env_refs[target] = source
                if was_literal:
                    literal_targets.append(target)
        if literal_targets:
            warnings.append(
                "literal environment values were not imported; bind local environment variables for: "
                + ", ".join(sorted(literal_targets))
            )
        if raw.get("headers"):
            warnings.append("HTTP headers were not imported; bind sensitive headers explicitly later")

        server = MCPServer(
            name=name.strip(),
            transport=transport,
            command=command if transport == "stdio" else None,
            args=args if transport == "stdio" else [],
            url=str(url) if url is not None else None,
            cwd=cwd if transport == "stdio" else None,
            env={},
            env_refs=env_refs,
            enabled=bool(raw.get("enabled", True)),
            description=str(raw.get("description") or f"Discovered from {path.name}"),
            source=f"discovery:{path}",
        )
        server.validate()
        safe_descriptor = server.to_dict()
        safe_descriptor.pop("id", None)
        safe_descriptor.pop("created_at", None)
        safe_descriptor.pop("updated_at", None)
        fingerprint = _fingerprint(path.resolve(strict=False), server.name, safe_descriptor)
        return DiscoveryCandidate(
            fingerprint=fingerprint,
            source_path=str(path.resolve(strict=False)),
            source_section=section,
            server=server,
            warnings=warnings,
        )

    def scan(
        self,
        paths: Iterable[str | os.PathLike[str]] = (),
        *,
        include_common: bool = True,
    ) -> DiscoveryResult:
        requested = [Path(value) for value in paths]
        if include_common:
            requested = [*self.common_paths(), *requested]
        files = list(_iter_requested_files(requested))
        candidates: list[DiscoveryCandidate] = []
        errors: list[DiscoveryError] = []
        seen_fingerprints: set[str] = set()

        for path in files:
            try:
                size = path.stat().st_size
                if size > MAX_CONFIG_BYTES:
                    raise ValueError(f"config exceeds {MAX_CONFIG_BYTES} byte limit")
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("config root must be a JSON object")
                sections = self._server_sections(payload)
                if not sections:
                    continue
                section_name = next(
                    key for key in ("mcpServers", "mcp_servers", "servers") if isinstance(payload.get(key), dict)
                )
                for name, descriptor in sections:
                    try:
                        candidate = self._candidate(path, name, descriptor, section_name)
                    except (ValueError, TypeError) as exc:
                        errors.append(DiscoveryError(str(path), f"{name}: {exc}"))
                        continue
                    if candidate.fingerprint in seen_fingerprints:
                        continue
                    seen_fingerprints.add(candidate.fingerprint)
                    candidates.append(candidate)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                errors.append(DiscoveryError(str(path), str(exc)))

        return DiscoveryResult(
            candidates=sorted(candidates, key=lambda item: (item.server.name.lower(), item.source_path)),
            errors=errors,
            scanned_files=len(files),
        )

    @staticmethod
    def _comparable(server: MCPServer) -> dict[str, Any]:
        return {
            "transport": server.transport,
            "command": server.command,
            "args": server.args,
            "url": server.url,
            "cwd": server.cwd,
            "env_refs": server.env_refs,
            "enabled": server.enabled,
            "description": server.description,
        }

    def preview(self, result: DiscoveryResult) -> list[dict[str, Any]]:
        existing = {server.name: server for server in self.store.list_servers()}
        discovered_name_counts: dict[str, int] = {}
        for candidate in result.candidates:
            discovered_name_counts[candidate.server.name] = discovered_name_counts.get(candidate.server.name, 0) + 1

        rows: list[dict[str, Any]] = []
        for candidate in result.candidates:
            current = existing.get(candidate.server.name)
            if discovered_name_counts[candidate.server.name] > 1:
                action = "conflict"
            elif current is None:
                action = "add"
            elif self._comparable(current) == self._comparable(candidate.server):
                action = "unchanged"
            else:
                action = "update"
            changed_fields: list[str] = []
            if current is not None and action == "update":
                before = self._comparable(current)
                after = self._comparable(candidate.server)
                changed_fields = sorted(key for key in after if before.get(key) != after.get(key))
            rows.append(
                {
                    **candidate.safe_dict(),
                    "action": action,
                    "existing_server_id": current.id if current else None,
                    "changed_fields": changed_fields,
                }
            )
        return rows

    def accept(self, result: DiscoveryResult, fingerprint: str) -> MCPServer:
        matches = [candidate for candidate in result.candidates if candidate.fingerprint == fingerprint]
        if len(matches) != 1:
            raise ValueError("discovery fingerprint must identify exactly one candidate")
        candidate = matches[0]
        same_name = [item for item in result.candidates if item.server.name == candidate.server.name]
        if len(same_name) > 1:
            raise ValueError(
                f"multiple discovered configs define {candidate.server.name}; choose one source by narrowing scan paths"
            )
        saved = self.store.save_server(candidate.server)
        self.store.record(
            "discovery.accepted",
            server_id=saved.id,
            server=saved.name,
            fingerprint=fingerprint,
            source_path=candidate.source_path,
        )
        return saved
