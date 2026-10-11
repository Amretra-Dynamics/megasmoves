from __future__ import annotations

import json
import os
from typing import Any

from .model import MCPProfile, MCPServer, SecretBinding, legacy_id
from .secret_release import SecretReleasePolicy, binding_fingerprint
from .store import MCPStore


def parse_requirement_pairs(
    store: MCPStore,
    values: list[str],
    *,
    member_server_ids: list[str],
    kind: str,
) -> dict[str, list[str]]:
    """Resolve repeatable SERVER=VALUE CLI requirements to stable server IDs."""

    result: dict[str, list[str]] = {}
    members = set(member_server_ids)
    for raw in values:
        if "=" not in raw:
            raise ValueError(f"expected SERVER={kind}, got: {raw}")
        server_reference, value = raw.split("=", 1)
        server_reference = server_reference.strip()
        value = value.strip()
        if not server_reference or not value:
            raise ValueError(f"profile {kind} requirement needs non-empty SERVER and value")
        server = store.get_server(server_reference)
        if server.id not in members:
            raise ValueError(
                f"profile {kind} requirement references server outside the profile: {server.name}"
            )
        result.setdefault(server.id, []).append(value)
    return result


def _raw_object(path: Any, default: dict[str, Any]) -> dict[str, Any]:
    """Read local state without triggering migration, pruning, or audit writes."""

    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _readonly_servers(store: MCPStore) -> dict[str, MCPServer]:
    raw = _raw_object(store.registry_path, {"servers": []})
    rows = raw.get("servers") or []
    if not isinstance(rows, list):
        raise ValueError("server registry must contain an array")
    result: dict[str, MCPServer] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("server registry contains a malformed record")
        normalized = dict(row)
        if not normalized.get("id"):
            name = str(normalized.get("name") or "")
            normalized["id"] = legacy_id("srv", name)
        server = MCPServer.from_dict(normalized)
        if server.id in result:
            raise ValueError("server registry contains duplicate stable IDs")
        result[server.id] = server
    return result


def _readonly_permissions(store: MCPStore, servers: dict[str, MCPServer]) -> set[tuple[str, str]]:
    raw = _raw_object(store.permissions_path, {"grants": []})
    by_name = {server.name: server.id for server in servers.values()}
    allowed: set[tuple[str, str]] = set()

    grants = raw.get("grants") or []
    if not isinstance(grants, list):
        raise ValueError("permission store grants must contain an array")
    for row in grants:
        if not isinstance(row, dict):
            raise ValueError("permission store contains a malformed grant")
        raw_server = str(row.get("server_id") or row.get("server") or "").strip()
        tool = str(row.get("tool") or "").strip()
        if not raw_server or not tool:
            continue
        server_id = raw_server if raw_server in servers else by_name.get(raw_server)
        if server_id:
            allowed.add((server_id, tool))

    legacy_allow = raw.get("allow")
    if isinstance(legacy_allow, list):
        for value in legacy_allow:
            text = str(value)
            if ":" not in text:
                continue
            server_name, tool = text.split(":", 1)
            server_id = by_name.get(server_name.strip())
            tool = tool.strip()
            if server_id and tool:
                allowed.add((server_id, tool))
    return allowed


def _readonly_bindings(store: MCPStore) -> dict[tuple[str, str], SecretBinding]:
    raw = _raw_object(store.secret_bindings_path, {"bindings": []})
    rows = raw.get("bindings") or []
    if not isinstance(rows, list):
        raise ValueError("secret binding store must contain an array")
    result: dict[tuple[str, str], SecretBinding] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("secret binding store contains a malformed record")
        binding = SecretBinding.from_dict(row)
        key = (binding.server_id, binding.target)
        if key in result:
            raise ValueError("secret binding store contains duplicate server/target bindings")
        result[key] = binding
    return result


def _readonly_release_grants(secret_policy: SecretReleasePolicy) -> set[tuple[str, str]]:
    """Return only the exact binding/fingerprint authority represented on disk.

    Stale rows are simply ignored by the caller because they cannot match a current
    binding fingerprint. Unlike SecretReleasePolicy.is_allowed(), this check never
    prunes old rows and therefore remains suitable for a pure readiness evaluation.
    """

    return {
        (str(row.get("binding_id") or ""), str(row.get("binding_fingerprint") or ""))
        for row in secret_policy._load()
        if str(row.get("binding_id") or "") and str(row.get("binding_fingerprint") or "")
    }


def evaluate_profile_requirements(
    store: MCPStore,
    profile: MCPProfile,
    *,
    secret_policy: SecretReleasePolicy | None = None,
) -> dict[str, Any]:
    """Evaluate requirements without granting authority, releasing secrets, or mutating stores."""

    secret_policy = secret_policy or SecretReleasePolicy(store)
    servers = _readonly_servers(store)
    allowed_tools = _readonly_permissions(store, servers)
    bindings_by_key = _readonly_bindings(store)
    release_grants = _readonly_release_grants(secret_policy)

    tool_rows: list[dict[str, Any]] = []
    secret_rows: list[dict[str, Any]] = []
    missing_tools: list[dict[str, str]] = []
    missing_bindings: list[dict[str, str]] = []
    missing_secret_values: list[dict[str, str]] = []
    missing_secret_release: list[dict[str, str]] = []

    for server_id, tools in sorted(profile.tool_permission_requirements.items()):
        server = servers.get(server_id)
        if server is None:
            raise ValueError(
                f"profile {profile.name} requirement references unknown server ID: {server_id}"
            )
        for tool in sorted(tools):
            allowed = (server.id, tool) in allowed_tools
            row = {
                "server_id": server.id,
                "server": server.name,
                "tool": tool,
                "allowed": allowed,
            }
            tool_rows.append(row)
            if not allowed:
                missing_tools.append(
                    {
                        "server_id": server.id,
                        "server": server.name,
                        "tool": tool,
                        "next_action": f"megas mcp allow {server.name} {tool}",
                    }
                )

    for server_id, targets in sorted(profile.secret_binding_requirements.items()):
        server = servers.get(server_id)
        if server is None:
            raise ValueError(
                f"profile {profile.name} secret requirement references unknown server ID: {server_id}"
            )
        if server.transport != "stdio" and targets:
            raise ValueError(
                f"profile secret binding requirements currently support stdio servers only: {server.name}"
            )
        for target in sorted(targets):
            binding = bindings_by_key.get((server.id, target))
            bound = binding is not None
            release_allowed = bool(
                binding
                and (binding.id, binding_fingerprint(binding)) in release_grants
            )
            available = bool(
                binding
                and binding.provider == "env"
                and bool(os.environ.get(binding.reference, ""))
            )
            secret_rows.append(
                {
                    "server_id": server.id,
                    "server": server.name,
                    "target": target,
                    "bound": bound,
                    "release_allowed": release_allowed,
                    "available": available,
                }
            )
            if binding is None:
                missing_bindings.append(
                    {
                        "server_id": server.id,
                        "server": server.name,
                        "target": target,
                        "next_action": (
                            f"megas mcp bind-env {server.name} {target} <LOCAL_ENV_NAME>"
                        ),
                    }
                )
            elif not release_allowed:
                missing_secret_release.append(
                    {
                        "server_id": server.id,
                        "server": server.name,
                        "target": target,
                        "next_action": f"megas mcp allow-secret {server.name} {target}",
                    }
                )
            elif not available:
                missing_secret_values.append(
                    {
                        "server_id": server.id,
                        "server": server.name,
                        "target": target,
                        "next_action": (
                            f"set machine environment variable {binding.reference} before activation"
                        ),
                    }
                )

    ready = (
        not missing_tools
        and not missing_bindings
        and not missing_secret_values
        and not missing_secret_release
    )
    return {
        "ready": ready,
        "tools": tool_rows,
        "secrets": secret_rows,
        "missing_tool_permissions": missing_tools,
        "missing_secret_bindings": missing_bindings,
        "missing_secret_values": missing_secret_values,
        "missing_secret_release_permissions": missing_secret_release,
    }


def assert_profile_requirements(
    store: MCPStore,
    profile: MCPProfile,
    *,
    secret_policy: SecretReleasePolicy | None = None,
) -> dict[str, Any]:
    evidence = evaluate_profile_requirements(
        store,
        profile,
        secret_policy=secret_policy,
    )
    if evidence["ready"]:
        return evidence

    actions = [
        row["next_action"]
        for key in (
            "missing_tool_permissions",
            "missing_secret_bindings",
            "missing_secret_values",
            "missing_secret_release_permissions",
        )
        for row in evidence[key]
    ]
    raise PermissionError(
        f"profile {profile.name} requirements are not satisfied; explicit owner action required: "
        + "; ".join(actions)
    )


__all__ = [
    "assert_profile_requirements",
    "evaluate_profile_requirements",
    "parse_requirement_pairs",
]
