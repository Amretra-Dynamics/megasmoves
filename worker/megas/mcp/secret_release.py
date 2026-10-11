from __future__ import annotations

import hashlib
import json
from typing import Any

from .model import MCPServer, SecretBinding, new_id, utc_now


SECRET_RELEASE_VERSION = 1


def binding_fingerprint(binding: SecretBinding) -> str:
    payload = {
        "binding_id": binding.id,
        "server_id": binding.server_id,
        "target": binding.target,
        "provider": binding.provider,
        "reference": binding.reference,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SecretReleasePolicy:
    """Machine-local, deny-by-default authorization for releasing SecretBindings.

    A SecretBinding only says *where* a value can be resolved. A release grant is
    separate authority to expose that exact binding to its configured MCP server.
    Grants are pinned to the binding fingerprint so changing the machine-local
    reference invalidates the previous approval automatically.
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self.path = store.mcp_dir / "secret_release_permissions.json"

    def _load(self) -> list[dict[str, str]]:
        raw = self.store._load_json(
            self.path,
            {"version": SECRET_RELEASE_VERSION, "grants": []},
        )
        if int(raw.get("version") or 0) != SECRET_RELEASE_VERSION:
            raise ValueError("unsupported secret release permission store version")
        rows = raw.get("grants") or []
        if not isinstance(rows, list):
            raise ValueError("secret release grants must be an array")
        result: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            binding_id = str(row.get("binding_id") or "").strip()
            fingerprint = str(row.get("binding_fingerprint") or "").strip()
            server_id = str(row.get("server_id") or "").strip()
            target = str(row.get("target") or "").strip()
            if not binding_id or not fingerprint or not server_id or not target:
                continue
            key = (binding_id, fingerprint)
            if key in seen:
                continue
            seen.add(key)
            result.append(
                {
                    "id": str(row.get("id") or new_id("perm")),
                    "binding_id": binding_id,
                    "binding_fingerprint": fingerprint,
                    "server_id": server_id,
                    "target": target,
                    "granted_at": str(row.get("granted_at") or ""),
                    "created_at": str(row.get("created_at") or row.get("granted_at") or ""),
                    "source": str(row.get("source") or "local"),
                }
            )
        return result

    def _write(self, rows: list[dict[str, str]]) -> None:
        normalized = sorted(rows, key=lambda row: (row["server_id"], row["target"], row["id"]))
        self.store._atomic_json(
            self.path,
            {
                "version": SECRET_RELEASE_VERSION,
                "grants": normalized,
            },
        )

    def _bindings(self, server_reference: str | None = None) -> list[SecretBinding]:
        return self.store.list_secret_bindings(server_reference)

    def _valid_rows(self, *, persist_prune: bool = True) -> list[dict[str, str]]:
        rows = self._load()
        current = {
            (binding.id, binding_fingerprint(binding)): binding
            for binding in self._bindings()
        }
        valid = [
            row
            for row in rows
            if (row["binding_id"], row["binding_fingerprint"]) in current
        ]
        if persist_prune and len(valid) != len(rows):
            self._write(valid)
            self.store.record(
                "secret.release.permissions.pruned",
                count=len(rows) - len(valid),
            )
        return valid

    def list(self, server_reference: str | None = None) -> list[dict[str, str]]:
        rows = self._valid_rows()
        if server_reference is None:
            return rows
        server = self.store.get_server(server_reference)
        return [row for row in rows if row["server_id"] == server.id]

    def is_allowed(self, binding: SecretBinding) -> bool:
        fingerprint = binding_fingerprint(binding)
        return any(
            row["binding_id"] == binding.id
            and row["binding_fingerprint"] == fingerprint
            for row in self._valid_rows()
        )

    def grant(self, server_reference: str, target: str) -> dict[str, str]:
        server = self.store.get_server(server_reference)
        if server.transport != "stdio":
            raise ValueError(
                "secret environment release is currently enforceable only for stdio MCP servers"
            )
        target = target.strip()
        bindings = [
            binding
            for binding in self._bindings(server.id)
            if binding.target == target
        ]
        if not bindings:
            raise KeyError(f"no SecretBinding for {server.name}/{target}")
        binding = bindings[0]
        fingerprint = binding_fingerprint(binding)
        rows = self._valid_rows()
        for row in rows:
            if row["binding_id"] == binding.id and row["binding_fingerprint"] == fingerprint:
                return row
        now = utc_now()
        grant = {
            "id": new_id("perm"),
            "binding_id": binding.id,
            "binding_fingerprint": fingerprint,
            "server_id": server.id,
            "target": binding.target,
            "granted_at": now,
            "created_at": now,
            "source": "local",
        }
        rows.append(grant)
        self._write(rows)
        self.store.record(
            "secret.release.granted",
            permission_id=grant["id"],
            binding_id=binding.id,
            server_id=server.id,
            server=server.name,
            target=binding.target,
        )
        return grant

    def revoke(self, server_reference: str, target: str) -> bool:
        server = self.store.get_server(server_reference)
        target = target.strip()
        rows = self._valid_rows()
        kept = [
            row
            for row in rows
            if not (row["server_id"] == server.id and row["target"] == target)
        ]
        changed = len(kept) != len(rows)
        if changed:
            self._write(kept)
            self.store.record(
                "secret.release.revoked",
                server_id=server.id,
                server=server.name,
                target=target,
            )
        return changed

    def prepare_server(self, server: MCPServer, *, require_all: bool = True) -> MCPServer:
        """Return a transient server descriptor containing only authorized refs."""

        bindings = self._bindings(server.id)
        approved: dict[str, str] = {}
        denied: list[str] = []
        for binding in bindings:
            if binding.provider != "env":
                denied.append(binding.target)
                continue
            if self.is_allowed(binding):
                approved[binding.target] = binding.reference
            else:
                denied.append(binding.target)
        if require_all and denied:
            raise PermissionError(
                f"secret release denied for {server.name}: {', '.join(sorted(denied))}; "
                "grant each binding explicitly with 'megas mcp allow-secret <server> <target>'"
            )

        prepared = MCPServer.from_dict(server.to_dict())
        prepared.env_refs = approved
        return prepared

    def binding_view(self, binding: SecretBinding) -> dict[str, Any]:
        return {
            **binding.to_dict(),
            "release_allowed": self.is_allowed(binding),
        }


__all__ = ["SecretReleasePolicy", "binding_fingerprint"]
