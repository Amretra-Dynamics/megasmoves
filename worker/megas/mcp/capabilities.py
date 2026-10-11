from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .model import MCPServer, SCHEMA_VERSION, new_id, utc_now
from .store import MCPStore


CAPABILITY_STORE_VERSION = 2
SUPPORTED_CAPABILITY_STORE_VERSIONS = {1, CAPABILITY_STORE_VERSION}
MAX_SNAPSHOTS_PER_SERVER = 50


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _items(section: Any) -> list[dict[str, Any]]:
    if not isinstance(section, dict):
        return []
    values = section.get("items") or []
    return [dict(value) for value in values if isinstance(value, dict)]


def _identity(kind: str, item: dict[str, Any]) -> str:
    if kind == "resources":
        return str(item.get("uri") or item.get("name") or "")
    return str(item.get("name") or "")


def _index(kind: str, items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        identity = _identity(kind, item)
        if identity:
            out[identity] = item
    return out


@dataclass(slots=True)
class CapabilitySnapshot:
    server_id: str
    fingerprint: str
    protocol_version: str | None
    server_info: dict[str, Any]
    capabilities: dict[str, Any]
    instructions: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("cap"))
    observed_at: str = field(default_factory=utc_now)
    created_at: str = field(default_factory=utc_now)
    schema_version: int = SCHEMA_VERSION
    source: str = "mcp.initialize"

    def to_dict(self) -> dict[str, Any]:
        if not self.server_id:
            raise ValueError("capability snapshot requires server_id")
        if not self.fingerprint:
            raise ValueError("capability snapshot requires fingerprint")
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "observed_at": self.observed_at,
            "source": self.source,
            "server_id": self.server_id,
            "fingerprint": self.fingerprint,
            "protocol_version": self.protocol_version,
            "server_info": self.server_info,
            "capabilities": self.capabilities,
            "instructions": self.instructions,
            "tools": self.tools,
            "resources": self.resources,
            "prompts": self.prompts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CapabilitySnapshot":
        snapshot = cls(
            server_id=str(data["server_id"]),
            fingerprint=str(data["fingerprint"]),
            protocol_version=(
                str(data["protocol_version"]) if data.get("protocol_version") is not None else None
            ),
            server_info=dict(data.get("server_info") or {}),
            capabilities=dict(data.get("capabilities") or {}),
            instructions=str(data.get("instructions") or ""),
            tools=[dict(item) for item in (data.get("tools") or []) if isinstance(item, dict)],
            resources=[dict(item) for item in (data.get("resources") or []) if isinstance(item, dict)],
            prompts=[dict(item) for item in (data.get("prompts") or []) if isinstance(item, dict)],
            id=str(data.get("id") or new_id("cap")),
            observed_at=str(data.get("observed_at") or data.get("created_at") or utc_now()),
            created_at=str(data.get("created_at") or utc_now()),
            schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
            source=str(data.get("source") or "mcp.initialize"),
        )
        snapshot.to_dict()
        return snapshot


def capability_diff(
    previous: CapabilitySnapshot | None,
    current: CapabilitySnapshot,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "previous_snapshot_id": previous.id if previous else None,
        "current_snapshot_id": current.id,
        "protocol_version_changed": bool(
            previous and previous.protocol_version != current.protocol_version
        ),
        "instructions_changed": bool(previous and previous.instructions != current.instructions),
        "sections": {},
    }
    for kind in ("tools", "resources", "prompts"):
        before = _index(kind, getattr(previous, kind) if previous else [])
        after = _index(kind, getattr(current, kind))
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(
            identity
            for identity in set(before) & set(after)
            if _canonical(before[identity]) != _canonical(after[identity])
        )
        result["sections"][kind] = {
            "added": added,
            "removed": removed,
            "changed": changed,
        }
    result["changed"] = bool(
        result["protocol_version_changed"]
        or result["instructions_changed"]
        or any(
            section[key]
            for section in result["sections"].values()
            for key in ("added", "removed", "changed")
        )
    )
    return result


class CapabilityStore:
    """Persistent protocol-reported capability snapshots and upgrade diffs."""

    def __init__(self, store: MCPStore) -> None:
        self.store = store
        self.path = store.mcp_dir / "capabilities.json"

    def _load(self) -> list[CapabilitySnapshot]:
        raw = self.store._load_json(
            self.path,
            {
                "version": CAPABILITY_STORE_VERSION,
                "schema_version": SCHEMA_VERSION,
                "snapshots": [],
            },
        )
        version = int(raw.get("version") or 1)
        if version not in SUPPORTED_CAPABILITY_STORE_VERSIONS:
            raise ValueError(f"unsupported capability store version: {raw.get('version')}")
        rows = raw.get("snapshots") or []
        if not isinstance(rows, list):
            raise ValueError("capability snapshots must be an array")
        snapshots = [
            CapabilitySnapshot.from_dict(row) for row in rows if isinstance(row, dict)
        ]
        ids = [snapshot.id for snapshot in snapshots]
        if len(ids) != len(set(ids)):
            raise ValueError("capability store contains duplicate snapshot IDs")
        return snapshots

    def _write(self, snapshots: list[CapabilitySnapshot]) -> None:
        ordered = sorted(snapshots, key=lambda item: (item.server_id, item.observed_at, item.id))
        self.store._atomic_json(
            self.path,
            {
                "version": CAPABILITY_STORE_VERSION,
                "schema_version": SCHEMA_VERSION,
                "snapshots": [snapshot.to_dict() for snapshot in ordered],
            },
        )

    def list(self, server_reference: str | None = None) -> list[CapabilitySnapshot]:
        snapshots = self._load()
        if server_reference is not None:
            server_id = self.store.get_server(server_reference).id
            snapshots = [snapshot for snapshot in snapshots if snapshot.server_id == server_id]
        return sorted(snapshots, key=lambda item: (item.observed_at, item.id), reverse=True)

    def latest(self, server_reference: str) -> CapabilitySnapshot | None:
        snapshots = self.list(server_reference)
        return snapshots[0] if snapshots else None

    @staticmethod
    def _snapshot_from_probe(server: MCPServer, probe: dict[str, Any]) -> CapabilitySnapshot:
        payload = {
            "protocol_version": probe.get("protocol_version"),
            "server_info": probe.get("server_info") or {},
            "capabilities": probe.get("capabilities") or {},
            "instructions": str(probe.get("instructions") or ""),
            "tools": _items(probe.get("tools")),
            "resources": _items(probe.get("resources")),
            "prompts": _items(probe.get("prompts")),
        }
        fingerprint = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        return CapabilitySnapshot(
            server_id=server.id,
            fingerprint=fingerprint,
            protocol_version=(
                str(payload["protocol_version"])
                if payload["protocol_version"] is not None
                else None
            ),
            server_info=dict(payload["server_info"]),
            capabilities=dict(payload["capabilities"]),
            instructions=str(payload["instructions"]),
            tools=list(payload["tools"]),
            resources=list(payload["resources"]),
            prompts=list(payload["prompts"]),
        )

    def capture(
        self,
        server: MCPServer,
        probe: dict[str, Any],
    ) -> tuple[CapabilitySnapshot, dict[str, Any], bool]:
        previous = self.latest(server.id)
        current = self._snapshot_from_probe(server, probe)
        if previous is not None and previous.fingerprint == current.fingerprint:
            return previous, capability_diff(previous, previous), True

        diff = capability_diff(previous, current)
        snapshots = self._load()
        snapshots.append(current)

        server_snapshots = sorted(
            [snapshot for snapshot in snapshots if snapshot.server_id == server.id],
            key=lambda item: (item.observed_at, item.id),
            reverse=True,
        )
        keep_ids = {snapshot.id for snapshot in server_snapshots[:MAX_SNAPSHOTS_PER_SERVER]}
        snapshots = [
            snapshot
            for snapshot in snapshots
            if snapshot.server_id != server.id or snapshot.id in keep_ids
        ]
        self._write(snapshots)

        # A tool that is new, removed, or changed across an upgrade must not inherit
        # an old approval merely because its display name matches.
        tool_changes = diff["sections"]["tools"]
        invalidated = sorted(
            set(tool_changes["added"])
            | set(tool_changes["removed"])
            | set(tool_changes["changed"])
        )
        revoked: list[str] = []
        for tool in invalidated:
            if self.store.is_tool_allowed(server.id, tool):
                self.store.revoke_tool(server.id, tool)
                revoked.append(tool)
        diff["permissions_revoked"] = revoked

        self.store.record(
            "capability.snapshot.saved",
            snapshot_id=current.id,
            server_id=server.id,
            server=server.name,
            fingerprint=current.fingerprint,
            previous_snapshot_id=previous.id if previous else None,
            changed=bool(diff["changed"]),
            permissions_revoked=revoked,
        )
        return current, diff, False

    def summary(self, snapshot: CapabilitySnapshot) -> dict[str, Any]:
        server = self.store.get_server_by_id(snapshot.server_id)
        return {
            "id": snapshot.id,
            "server_id": snapshot.server_id,
            "server": server.name,
            "observed_at": snapshot.observed_at,
            "fingerprint": snapshot.fingerprint,
            "protocol_version": snapshot.protocol_version,
            "server_info": snapshot.server_info,
            "instructions": snapshot.instructions,
            "counts": {
                "tools": len(snapshot.tools),
                "resources": len(snapshot.resources),
                "prompts": len(snapshot.prompts),
            },
        }
