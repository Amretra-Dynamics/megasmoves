from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .model import (
    MCPProfile,
    MCPServer,
    SCHEMA_VERSION,
    SecretBinding,
    legacy_id,
    new_id,
    utc_now,
)
from .secret_policy import is_sensitive_env_key


REGISTRY_VERSION = 3
PROFILES_VERSION = 2
PERMISSIONS_VERSION = 2
SECRET_BINDINGS_VERSION = 1
BUNDLE_VERSION = 3


class MCPStore:
    """Durable, local-only Megas state.

    MEGAS_HOME can redirect all state for tests or portable installs.
    Nothing in this class requires network access or authentication.
    Secret values are never stored by SecretBinding; the local binding stores
    only a provider and a machine-local reference such as an environment name.
    """

    def __init__(self, home: str | os.PathLike[str] | None = None) -> None:
        configured = home or os.environ.get("MEGAS_HOME")
        self.home = Path(configured).expanduser() if configured else Path.home() / ".megas"
        self.mcp_dir = self.home / "mcp"
        self.runtime_dir = self.mcp_dir / "runtime"
        self.logs_dir = self.mcp_dir / "logs"
        self.registry_path = self.mcp_dir / "servers.json"
        self.profiles_path = self.mcp_dir / "profiles.json"
        self.permissions_path = self.mcp_dir / "permissions.json"
        self.secret_bindings_path = self.mcp_dir / "secret_bindings.json"
        self.history_path = self.mcp_dir / "history.jsonl"
        for path in (self.mcp_dir, self.runtime_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)

    def _atomic_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    @staticmethod
    def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError(f"{path} must contain a JSON object")
        return raw

    @staticmethod
    def _server_storage_dict(server: MCPServer) -> dict[str, Any]:
        row = server.to_dict()
        # Secret references have their own canonical stable-ID records.
        row["env_refs"] = {}
        return row

    @classmethod
    def _server_payload(cls, servers: list[MCPServer]) -> dict[str, Any]:
        return {
            "version": REGISTRY_VERSION,
            "schema_version": SCHEMA_VERSION,
            "servers": [
                cls._server_storage_dict(server)
                for server in sorted(servers, key=lambda item: item.name.lower())
            ],
        }

    @staticmethod
    def _secret_binding_payload(bindings: list[SecretBinding]) -> dict[str, Any]:
        return {
            "version": SECRET_BINDINGS_VERSION,
            "schema_version": SCHEMA_VERSION,
            "bindings": [
                binding.to_dict()
                for binding in sorted(bindings, key=lambda item: (item.server_id, item.target.lower()))
            ],
        }

    def _load_secret_bindings_unchecked(self) -> list[SecretBinding]:
        raw = self._load_json(
            self.secret_bindings_path,
            {
                "version": SECRET_BINDINGS_VERSION,
                "schema_version": SCHEMA_VERSION,
                "bindings": [],
            },
        )
        rows = raw.get("bindings") or []
        if not isinstance(rows, list):
            raise ValueError("secret bindings must be an array")
        bindings = [SecretBinding.from_dict(row) for row in rows if isinstance(row, dict)]
        if len(bindings) != len(rows):
            raise ValueError("secret binding store contains malformed records")
        keys = [(binding.server_id, binding.target) for binding in bindings]
        ids = [binding.id for binding in bindings]
        if len(keys) != len(set(keys)):
            raise ValueError("secret binding store contains duplicate server/target bindings")
        if len(ids) != len(set(ids)):
            raise ValueError("secret binding store contains duplicate stable IDs")
        return bindings

    def _write_secret_bindings(self, bindings: list[SecretBinding]) -> None:
        self._atomic_json(
            self.secret_bindings_path,
            self._secret_binding_payload(bindings),
        )

    @staticmethod
    def _upsert_binding_in_list(
        bindings: list[SecretBinding],
        *,
        server_id: str,
        target: str,
        reference: str,
        source: str = "local",
    ) -> tuple[SecretBinding, bool]:
        for binding in bindings:
            if binding.server_id == server_id and binding.target == target:
                changed = binding.reference != reference or binding.provider != "env"
                if changed:
                    binding.reference = reference
                    binding.provider = "env"
                    binding.updated_at = utc_now()
                    binding.source = source
                    binding.validate()
                return binding, changed
        binding = SecretBinding(
            server_id=server_id,
            target=target,
            reference=reference,
            provider="env",
            source=source,
        )
        binding.validate()
        bindings.append(binding)
        return binding, True

    def list_servers(self) -> list[MCPServer]:
        raw = self._load_json(
            self.registry_path,
            {"version": REGISTRY_VERSION, "schema_version": SCHEMA_VERSION, "servers": []},
        )
        rows = raw.get("servers") or []
        if not isinstance(rows, list):
            raise ValueError("servers must be an array")
        servers = [MCPServer.from_dict(row) for row in rows if isinstance(row, dict)]
        if len(servers) != len(rows):
            raise ValueError("server registry contains malformed records")
        ids = [server.id for server in servers]
        names = [server.name for server in servers]
        if len(ids) != len(set(ids)):
            raise ValueError("server registry contains duplicate stable IDs")
        if len(names) != len(set(names)):
            raise ValueError("server registry contains duplicate names")

        bindings = self._load_secret_bindings_unchecked()
        bindings_changed = False
        legacy_refs_migrated = 0
        plaintext_scrubbed: list[tuple[str, str]] = []
        binding_keys = {(binding.server_id, binding.target) for binding in bindings}

        for server in servers:
            # Migrate legacy env_refs into stable SecretBinding records.
            for target, reference in list(server.env_refs.items()):
                if (server.id, target) not in binding_keys:
                    _binding, changed = self._upsert_binding_in_list(
                        bindings,
                        server_id=server.id,
                        target=target,
                        reference=reference,
                        source="legacy-env-ref",
                    )
                    if changed:
                        bindings_changed = True
                        legacy_refs_migrated += 1
                        binding_keys.add((server.id, target))

            # Older versions permitted arbitrary --env values. Credential-like
            # entries are scrubbed instead of being copied into another plaintext
            # store. The same target name becomes an unresolved env reference.
            for key in list(server.env):
                if not is_sensitive_env_key(key):
                    continue
                server.env.pop(key, None)
                plaintext_scrubbed.append((server.id, key))
                if (server.id, key) not in binding_keys:
                    _binding, changed = self._upsert_binding_in_list(
                        bindings,
                        server_id=server.id,
                        target=key,
                        reference=key,
                        source="plaintext-scrub-migration",
                    )
                    if changed:
                        bindings_changed = True
                        binding_keys.add((server.id, key))
            server.env_refs = {}

        if bindings_changed:
            self._write_secret_bindings(bindings)

        needs_migration = (
            int(raw.get("version") or 1) != REGISTRY_VERSION
            or int(raw.get("schema_version") or 1) != SCHEMA_VERSION
            or any(not isinstance(row, dict) or not row.get("id") for row in rows)
            or any(isinstance(row, dict) and bool(row.get("env_refs")) for row in rows)
            or bool(plaintext_scrubbed)
        )
        if needs_migration:
            self._atomic_json(self.registry_path, self._server_payload(servers))
            self.record(
                "storage.migrated",
                target="servers",
                from_version=int(raw.get("version") or 1),
                to_version=REGISTRY_VERSION,
                secret_refs_migrated=legacy_refs_migrated,
                plaintext_secret_keys_scrubbed=len(plaintext_scrubbed),
            )
            for server_id, target in plaintext_scrubbed:
                self.record(
                    "secret.plaintext_scrubbed",
                    server_id=server_id,
                    target=target,
                    next_action="bind-env",
                )

        refs_by_server: dict[str, dict[str, str]] = {}
        for binding in bindings:
            refs_by_server.setdefault(binding.server_id, {})[binding.target] = binding.reference
        for server in servers:
            server.env_refs = dict(refs_by_server.get(server.id, {}))
        return sorted(servers, key=lambda item: item.name.lower())

    def get_server(self, reference: str) -> MCPServer:
        reference = reference.strip()
        for server in self.list_servers():
            if server.id == reference or server.name == reference:
                return server
        raise KeyError(f"unknown MCP server: {reference}")

    def get_server_by_id(self, server_id: str) -> MCPServer:
        for server in self.list_servers():
            if server.id == server_id:
                return server
        raise KeyError(f"unknown MCP server id: {server_id}")

    def save_server(self, server: MCPServer) -> MCPServer:
        server.validate()
        sensitive = [key for key in server.env if is_sensitive_env_key(key)]
        if sensitive:
            raise ValueError(
                "credential-like environment values are not persisted; use SecretBinding/--env-ref for: "
                + ", ".join(sorted(sensitive))
            )
        requested_refs = dict(server.env_refs)
        existing = self.list_servers()
        by_id = {item.id: item for item in existing}
        same_name = next((item for item in existing if item.name == server.name), None)

        if same_name is not None and same_name.id != server.id:
            # `megas mcp add <same-name>` is an update, not a new identity.
            server.id = same_name.id
            server.created_at = same_name.created_at
        other_same_name = next(
            (item for item in existing if item.name == server.name and item.id != server.id),
            None,
        )
        if other_same_name is not None:
            raise ValueError(f"server name already exists: {server.name}")

        server.updated_at = utc_now()
        server.env_refs = {}
        by_id[server.id] = server
        self._atomic_json(self.registry_path, self._server_payload(list(by_id.values())))

        if requested_refs:
            bindings = self._load_secret_bindings_unchecked()
            changed = False
            for target, reference in requested_refs.items():
                _binding, binding_changed = self._upsert_binding_in_list(
                    bindings,
                    server_id=server.id,
                    target=target,
                    reference=reference,
                )
                changed = changed or binding_changed
            if changed:
                self._write_secret_bindings(bindings)
        server.env_refs = self.secret_env_refs(server.id)
        self.record(
            "server.saved",
            server_id=server.id,
            server=server.name,
            transport=server.transport,
        )
        return server

    def list_secret_bindings(self, server_reference: str | None = None) -> list[SecretBinding]:
        bindings = self._load_secret_bindings_unchecked()
        known_server_ids = {server.id for server in self.list_servers()}
        stale = [binding for binding in bindings if binding.server_id not in known_server_ids]
        if stale:
            bindings = [binding for binding in bindings if binding.server_id in known_server_ids]
            self._write_secret_bindings(bindings)
            self.record("secret.bindings.pruned", count=len(stale))
        if server_reference is not None:
            server_id = self.get_server(server_reference).id
            bindings = [binding for binding in bindings if binding.server_id == server_id]
        return sorted(bindings, key=lambda item: (item.server_id, item.target.lower()))

    def secret_env_refs(self, server_reference: str) -> dict[str, str]:
        server_id = self.get_server(server_reference).id if not server_reference.startswith("srv_") else server_reference
        return {
            binding.target: binding.reference
            for binding in self._load_secret_bindings_unchecked()
            if binding.server_id == server_id and binding.provider == "env"
        }

    def bind_env_secret(self, server_reference: str, target: str, reference: str) -> SecretBinding:
        server = self.get_server(server_reference)
        candidate = SecretBinding(server_id=server.id, target=target, reference=reference)
        candidate.validate()
        bindings = self._load_secret_bindings_unchecked()
        binding, changed = self._upsert_binding_in_list(
            bindings,
            server_id=server.id,
            target=target,
            reference=reference,
        )
        if changed:
            self._write_secret_bindings(bindings)
            self.record(
                "secret.binding.saved",
                binding_id=binding.id,
                server_id=server.id,
                server=server.name,
                target=target,
                provider="env",
                reference=reference,
            )
        return binding

    def unbind_secret(self, server_reference: str, target: str) -> bool:
        server = self.get_server(server_reference)
        bindings = self._load_secret_bindings_unchecked()
        kept = [
            binding
            for binding in bindings
            if not (binding.server_id == server.id and binding.target == target)
        ]
        changed = len(kept) != len(bindings)
        if changed:
            self._write_secret_bindings(kept)
            self.record(
                "secret.binding.removed",
                server_id=server.id,
                server=server.name,
                target=target,
            )
        return changed

    def delete_server(self, reference: str) -> None:
        server = self.get_server(reference)
        referenced_by = [
            profile.name for profile in self.list_profiles() if server.id in profile.server_ids
        ]
        if referenced_by:
            raise ValueError(
                f"server {server.name} is referenced by profile(s): {', '.join(referenced_by)}; "
                "remove it from those profiles first"
            )
        rows = [item for item in self.list_servers() if item.id != server.id]
        self._atomic_json(self.registry_path, self._server_payload(rows))
        grants = [row for row in self.list_permissions() if row["server_id"] != server.id]
        self._write_permissions(grants)
        bindings = [
            binding
            for binding in self._load_secret_bindings_unchecked()
            if binding.server_id != server.id
        ]
        self._write_secret_bindings(bindings)
        self.record("server.deleted", server_id=server.id, server=server.name)

    @staticmethod
    def _profile_payload(profiles: list[MCPProfile]) -> dict[str, Any]:
        return {
            "version": PROFILES_VERSION,
            "schema_version": SCHEMA_VERSION,
            "profiles": [profile.to_dict() for profile in sorted(profiles, key=lambda item: item.name.lower())],
        }

    def _profile_from_row(
        self,
        row: dict[str, Any],
        *,
        servers: list[MCPServer],
    ) -> tuple[MCPProfile, bool]:
        by_name = {server.name: server.id for server in servers}
        by_id = {server.id: server for server in servers}
        legacy_refs = row.get("server_ids") is None
        raw_refs = row.get("server_ids") if not legacy_refs else (row.get("servers") or [])
        if not isinstance(raw_refs, list):
            raise ValueError("profile server references must be an array")
        server_ids: list[str] = []
        for value in raw_refs:
            ref = str(value)
            if ref in by_id:
                server_ids.append(ref)
            elif ref in by_name:
                server_ids.append(by_name[ref])
            else:
                raise ValueError(f"profile {row.get('name')} references unknown server: {ref}")
        normalized = dict(row)
        normalized["server_ids"] = server_ids
        normalized.pop("servers", None)
        return MCPProfile.from_dict(normalized), legacy_refs or not row.get("id")

    def list_profiles(self) -> list[MCPProfile]:
        raw = self._load_json(
            self.profiles_path,
            {"version": PROFILES_VERSION, "schema_version": SCHEMA_VERSION, "profiles": []},
        )
        rows = raw.get("profiles") or []
        if not isinstance(rows, list):
            raise ValueError("profiles must be an array")
        servers = self.list_servers()
        profiles: list[MCPProfile] = []
        row_migration = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            profile, migrated = self._profile_from_row(row, servers=servers)
            profiles.append(profile)
            row_migration = row_migration or migrated
        ids = [profile.id for profile in profiles]
        names = [profile.name for profile in profiles]
        if len(ids) != len(set(ids)):
            raise ValueError("profile store contains duplicate stable IDs")
        if len(names) != len(set(names)):
            raise ValueError("profile store contains duplicate names")
        needs_migration = (
            int(raw.get("version") or 1) != PROFILES_VERSION
            or int(raw.get("schema_version") or 1) != SCHEMA_VERSION
            or row_migration
        )
        if needs_migration:
            self._atomic_json(self.profiles_path, self._profile_payload(profiles))
            self.record(
                "storage.migrated",
                target="profiles",
                from_version=int(raw.get("version") or 1),
                to_version=PROFILES_VERSION,
            )
        return sorted(profiles, key=lambda item: item.name.lower())

    def get_profile(self, reference: str) -> MCPProfile:
        reference = reference.strip()
        for profile in self.list_profiles():
            if profile.id == reference or profile.name == reference:
                return profile
        raise KeyError(f"unknown MCP profile: {reference}")

    def profile_server_names(self, profile: MCPProfile) -> list[str]:
        return [self.get_server_by_id(server_id).name for server_id in profile.server_ids]

    def save_profile(self, profile: MCPProfile) -> MCPProfile:
        # Compatibility: callers may still pass server names through `servers=`.
        resolved_ids = [self.get_server(reference).id for reference in profile.server_ids]
        profile.server_ids = resolved_ids
        profile.validate()
        existing = self.list_profiles()
        by_id = {item.id: item for item in existing}
        same_name = next((item for item in existing if item.name == profile.name), None)
        if same_name is not None and same_name.id != profile.id:
            profile.id = same_name.id
            profile.created_at = same_name.created_at
        profile.updated_at = utc_now()
        by_id[profile.id] = profile
        self._atomic_json(self.profiles_path, self._profile_payload(list(by_id.values())))
        self.record(
            "profile.saved",
            profile_id=profile.id,
            profile=profile.name,
            server_ids=profile.server_ids,
        )
        return profile

    def delete_profile(self, reference: str) -> None:
        profile = self.get_profile(reference)
        rows = [item for item in self.list_profiles() if item.id != profile.id]
        self._atomic_json(self.profiles_path, self._profile_payload(rows))
        self.record("profile.deleted", profile_id=profile.id, profile=profile.name)

    def _write_permissions(self, grants: list[dict[str, str]]) -> None:
        normalized: list[dict[str, str]] = []
        for row in grants:
            server_id = str(row.get("server_id") or "").strip()
            tool = str(row.get("tool") or "").strip()
            if not server_id or not tool:
                continue
            granted_at = str(row.get("granted_at") or utc_now())
            normalized.append(
                {
                    "id": str(row.get("id") or new_id("perm")),
                    "server_id": server_id,
                    "tool": tool,
                    "granted_at": granted_at,
                    "created_at": str(row.get("created_at") or granted_at),
                    "schema_version": str(SCHEMA_VERSION),
                    "source": str(row.get("source") or "local"),
                }
            )
        normalized.sort(key=lambda row: (row["server_id"], row["tool"].lower()))
        self._atomic_json(
            self.permissions_path,
            {
                "version": PERMISSIONS_VERSION,
                "schema_version": SCHEMA_VERSION,
                "grants": normalized,
            },
        )

    def _permission_rows(self, raw: dict[str, Any]) -> tuple[list[dict[str, str]], bool]:
        servers = self.list_servers()
        by_name = {server.name: server.id for server in servers}
        by_id = {server.id: server for server in servers}
        rows_by_key: dict[tuple[str, str], dict[str, str]] = {}
        migration = int(raw.get("version") or 1) != PERMISSIONS_VERSION or "allow" in raw

        grants = raw.get("grants") or []
        if isinstance(grants, list):
            for row in grants:
                if not isinstance(row, dict):
                    continue
                raw_server = str(row.get("server_id") or row.get("server") or "").strip()
                tool = str(row.get("tool") or "").strip()
                if not raw_server or not tool:
                    continue
                if raw_server in by_id:
                    server_id = raw_server
                elif raw_server in by_name:
                    server_id = by_name[raw_server]
                    migration = True
                else:
                    # Stale permissions are not authorization authority.
                    migration = True
                    continue
                granted_at = str(row.get("granted_at") or "")
                rows_by_key[(server_id, tool)] = {
                    "id": str(row.get("id") or legacy_id("perm", f"{server_id}:{tool}")),
                    "server_id": server_id,
                    "tool": tool,
                    "granted_at": granted_at,
                    "created_at": str(row.get("created_at") or granted_at),
                    "source": str(row.get("source") or "local"),
                }
                if not row.get("id") or not row.get("server_id"):
                    migration = True

        legacy_allow = raw.get("allow")
        if isinstance(legacy_allow, list):
            for value in legacy_allow:
                key = str(value)
                if ":" not in key:
                    continue
                server_name, tool = key.split(":", 1)
                server_name = server_name.strip()
                tool = tool.strip()
                server_id = by_name.get(server_name)
                if not server_id or not tool:
                    continue
                rows_by_key.setdefault(
                    (server_id, tool),
                    {
                        "id": legacy_id("perm", f"{server_id}:{tool}"),
                        "server_id": server_id,
                        "tool": tool,
                        "granted_at": "",
                        "created_at": "",
                        "source": "local",
                    },
                )

        return sorted(rows_by_key.values(), key=lambda row: (row["server_id"], row["tool"])), migration

    def list_permissions(self) -> list[dict[str, str]]:
        raw = self._load_json(
            self.permissions_path,
            {"version": PERMISSIONS_VERSION, "schema_version": SCHEMA_VERSION, "grants": []},
        )
        rows, needs_migration = self._permission_rows(raw)
        if needs_migration:
            self._write_permissions(rows)
            self.record(
                "permissions.migrated",
                from_version=int(raw.get("version") or 1),
                to_version=PERMISSIONS_VERSION,
            )
            raw = self._load_json(self.permissions_path, {"grants": []})
            rows, _ = self._permission_rows(raw)
        result: list[dict[str, str]] = []
        for row in rows:
            server = self.get_server_by_id(row["server_id"])
            result.append({**row, "server": server.name})
        return result

    def is_tool_allowed(self, server_reference: str, tool: str) -> bool:
        server_id = self.get_server(server_reference).id
        return any(
            row["server_id"] == server_id and row["tool"] == tool
            for row in self.list_permissions()
        )

    def grant_tool(self, server_reference: str, tool: str) -> dict[str, str]:
        tool = tool.strip()
        if not tool:
            raise ValueError("tool must be non-empty")
        server = self.get_server(server_reference)
        grants = self.list_permissions()
        for row in grants:
            if row["server_id"] == server.id and row["tool"] == tool:
                return row
        now = utc_now()
        grant = {
            "id": new_id("perm"),
            "server_id": server.id,
            "server": server.name,
            "tool": tool,
            "granted_at": now,
            "created_at": now,
            "source": "local",
        }
        grants.append(grant)
        self._write_permissions(grants)
        self.record(
            "tool.permission.granted",
            permission_id=grant["id"],
            server_id=server.id,
            server=server.name,
            tool=tool,
        )
        return grant

    def revoke_tool(self, server_reference: str, tool: str) -> bool:
        tool = tool.strip()
        if not tool:
            raise ValueError("tool must be non-empty")
        server = self.get_server(server_reference)
        grants = self.list_permissions()
        kept = [
            row
            for row in grants
            if not (row["server_id"] == server.id and row["tool"] == tool)
        ]
        changed = len(kept) != len(grants)
        if changed:
            self._write_permissions(kept)
            self.record(
                "tool.permission.revoked",
                server_id=server.id,
                server=server.name,
                tool=tool,
            )
        return changed

    def record(self, event: str, **data: Any) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": utc_now(), "event": event, **data}
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(self.history_path, 0o600)
        except OSError:
            pass

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.history_path.exists():
            return []
        lines = self.history_path.read_text(encoding="utf-8").splitlines()
        rows: list[dict[str, Any]] = []
        for line in lines[-max(1, limit):]:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    def export_bundle(self) -> dict[str, Any]:
        # Grants and machine-local secret references are intentionally not exported.
        servers = self.list_servers()
        portable_servers: list[dict[str, Any]] = []
        for server in servers:
            row = self._server_storage_dict(server)
            portable_servers.append(row)
        requirements = [
            {
                "server_id": binding.server_id,
                "target": binding.target,
                "provider": binding.provider,
            }
            for binding in self.list_secret_bindings()
        ]
        return {
            "format": "megas-mcp-bundle",
            "version": BUNDLE_VERSION,
            "schema_version": SCHEMA_VERSION,
            "exported_at": utc_now(),
            "servers": portable_servers,
            "profiles": [profile.to_dict() for profile in self.list_profiles()],
            "secret_binding_requirements": requirements,
        }

    def import_bundle(self, bundle: dict[str, Any], *, replace: bool = False) -> None:
        if bundle.get("format") != "megas-mcp-bundle":
            raise ValueError("not a Megas MCP bundle")
        version = int(bundle.get("version") or 1)
        if version not in {1, 2, BUNDLE_VERSION}:
            raise ValueError(f"unsupported Megas MCP bundle version: {version}")

        raw_servers = bundle.get("servers") or []
        raw_profiles = bundle.get("profiles") or []
        raw_requirements = bundle.get("secret_binding_requirements") or []
        if not isinstance(raw_servers, list) or not isinstance(raw_profiles, list):
            raise ValueError("bundle servers and profiles must be arrays")
        if not isinstance(raw_requirements, list):
            raise ValueError("bundle secret binding requirements must be an array")
        imported_servers = [MCPServer.from_dict(row) for row in raw_servers if isinstance(row, dict)]
        if len(imported_servers) != len(raw_servers):
            raise ValueError("bundle contains malformed server records")

        # A portable bundle never authorizes or binds secret material. Legacy
        # env_refs are treated only as rebinding requirements and are stripped.
        legacy_requirements: list[dict[str, str]] = []
        for server in imported_servers:
            for target in server.env_refs:
                legacy_requirements.append(
                    {"server_id": server.id, "target": target, "provider": "env"}
                )
            sensitive = [key for key in server.env if is_sensitive_env_key(key)]
            if sensitive:
                raise ValueError(
                    "bundle contains credential-like plaintext environment value(s): "
                    + ", ".join(sorted(sensitive))
                )
            server.env_refs = {}

        candidate_servers = {} if replace else {item.id: item for item in self.list_servers()}
        existing_by_name = {item.name: item for item in candidate_servers.values()}
        id_remap: dict[str, str] = {}
        for server in imported_servers:
            existing = existing_by_name.get(server.name)
            if existing is not None and existing.id != server.id:
                id_remap[server.id] = existing.id
                server.id = existing.id
                server.created_at = existing.created_at
            server.updated_at = utc_now()
            candidate_servers[server.id] = server
            existing_by_name[server.name] = server

        server_ids = set(candidate_servers)
        server_name_to_id = {item.name: item.id for item in candidate_servers.values()}
        candidate_profiles = {} if replace else {item.id: item for item in self.list_profiles()}
        existing_profile_by_name = {item.name: item for item in candidate_profiles.values()}

        for raw in raw_profiles:
            if not isinstance(raw, dict):
                raise ValueError("bundle contains malformed profile records")
            if raw.get("server_ids") is not None:
                refs = [id_remap.get(str(value), str(value)) for value in (raw.get("server_ids") or [])]
            else:
                refs = []
                for value in raw.get("servers") or []:
                    name = str(value)
                    if name not in server_name_to_id:
                        raise ValueError(f"profile {raw.get('name')} references unknown server: {name}")
                    refs.append(server_name_to_id[name])
            unknown = [server_id for server_id in refs if server_id not in server_ids]
            if unknown:
                raise ValueError(
                    f"profile {raw.get('name')} references unknown server ID(s): {', '.join(unknown)}"
                )
            normalized = dict(raw)
            normalized["server_ids"] = refs
            normalized.pop("servers", None)
            profile = MCPProfile.from_dict(normalized)
            existing = existing_profile_by_name.get(profile.name)
            if existing is not None and existing.id != profile.id:
                profile.id = existing.id
                profile.created_at = existing.created_at
            profile.updated_at = utc_now()
            candidate_profiles[profile.id] = profile
            existing_profile_by_name[profile.name] = profile

        requirements = [*raw_requirements, *legacy_requirements]
        normalized_requirements: list[dict[str, str]] = []
        for raw in requirements:
            if not isinstance(raw, dict):
                raise ValueError("bundle contains malformed secret binding requirement")
            source_id = str(raw.get("server_id") or "")
            server_id = id_remap.get(source_id, source_id)
            target = str(raw.get("target") or "")
            provider = str(raw.get("provider") or "env")
            if server_id not in server_ids:
                raise ValueError(f"secret binding requirement references unknown server ID: {server_id}")
            if not target or any(ch.isspace() for ch in target) or "=" in target:
                raise ValueError("secret binding requirement has invalid target")
            if provider != "env":
                raise ValueError(f"unsupported secret binding provider in bundle: {provider}")
            normalized_requirements.append(
                {"server_id": server_id, "target": target, "provider": provider}
            )

        # All references are validated before authoritative files are replaced.
        self._atomic_json(self.registry_path, self._server_payload(list(candidate_servers.values())))
        self._atomic_json(self.profiles_path, self._profile_payload(list(candidate_profiles.values())))
        if replace:
            local_bindings = [
                binding
                for binding in self._load_secret_bindings_unchecked()
                if binding.server_id in server_ids
            ]
            self._write_secret_bindings(local_bindings)
        self.record(
            "bundle.imported",
            version=version,
            replace=replace,
            servers=len(imported_servers),
            profiles=len(raw_profiles),
            secret_bindings_required=len(normalized_requirements),
        )
