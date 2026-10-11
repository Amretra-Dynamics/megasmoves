from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .model import MCPProfile, MCPServer, SCHEMA_VERSION, legacy_id
from .state_transaction import apply_state_transaction, recover_pending_transaction
from .store import BUNDLE_VERSION as LEGACY_BUNDLE_VERSION, MCPStore


PORTABLE_BUNDLE_VERSION = 4
MANIFEST_VERSION = 1
REBINDING_STORE_VERSION = 1
REBINDING_FILE = "secret_rebinding_requirements.json"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sorted_sections(
    servers: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        sorted(servers, key=lambda row: (str(row.get("id") or ""), str(row.get("name") or ""))),
        sorted(profiles, key=lambda row: (str(row.get("id") or ""), str(row.get("name") or ""))),
        sorted(
            requirements,
            key=lambda row: (
                str(row.get("server_id") or ""),
                str(row.get("target") or ""),
                str(row.get("provider") or ""),
            ),
        ),
    )


def _manifest(
    servers: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
) -> dict[str, Any]:
    content = {
        "servers": servers,
        "profiles": profiles,
        "secret_binding_requirements": requirements,
    }
    section_sha256 = {key: _sha256(value) for key, value in content.items()}
    return {
        "version": MANIFEST_VERSION,
        "schema_version": SCHEMA_VERSION,
        "bundle_version": PORTABLE_BUNDLE_VERSION,
        "section_sha256": section_sha256,
        "content_sha256": _sha256(content),
        "counts": {
            "servers": len(servers),
            "profiles": len(profiles),
            "secret_binding_requirements": len(requirements),
        },
    }


def build_bundle(store: MCPStore) -> dict[str, Any]:
    """Build a stable portable bundle whose hashes depend only on portable state."""

    legacy = store.export_bundle()
    raw_servers = [
        dict(row)
        for row in legacy.get("servers") or []
        if isinstance(row, dict)
    ]
    raw_profiles = [
        dict(row)
        for row in legacy.get("profiles") or []
        if isinstance(row, dict)
    ]

    # Portable rebinding requirements are the union of bindings that existed on
    # the source machine and declarative workspace requirements. A profile may
    # require a secret before the source machine ever binds it; the destination
    # must still be told to rebind that target. Values and local env references
    # remain intentionally absent.
    requirement_index: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in legacy.get("secret_binding_requirements") or []:
        if not isinstance(row, dict):
            continue
        normalized = {
            "server_id": str(row.get("server_id") or ""),
            "target": str(row.get("target") or ""),
            "provider": str(row.get("provider") or "env"),
        }
        requirement_index[
            (normalized["server_id"], normalized["target"], normalized["provider"])
        ] = normalized

    for profile in raw_profiles:
        declared = profile.get("secret_binding_requirements") or {}
        if not isinstance(declared, dict):
            raise ValueError("profile secret_binding_requirements must be an object")
        for raw_server_id, raw_targets in declared.items():
            server_id = str(raw_server_id)
            if not isinstance(raw_targets, list):
                raise ValueError("profile secret_binding_requirements values must be arrays")
            for raw_target in raw_targets:
                target = str(raw_target)
                normalized = {
                    "server_id": server_id,
                    "target": target,
                    "provider": "env",
                }
                requirement_index[(server_id, target, "env")] = normalized

    servers, profiles, requirements = _sorted_sections(
        raw_servers,
        raw_profiles,
        list(requirement_index.values()),
    )
    return {
        "format": "megas-mcp-bundle",
        "version": PORTABLE_BUNDLE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "servers": servers,
        "profiles": profiles,
        "secret_binding_requirements": requirements,
        "manifest": _manifest(servers, profiles, requirements),
    }


def _raw_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _existing_server_identities(store: MCPStore) -> list[dict[str, str]]:
    raw = _raw_json(store.registry_path, {"servers": []})
    rows = raw.get("servers") or []
    if not isinstance(rows, list):
        raise ValueError("local server registry must contain an array")
    result: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("local server registry contains a malformed record")
        name = str(row.get("name") or "")
        if not name:
            raise ValueError("local server registry contains a server without a name")
        result.append(
            {
                "id": str(row.get("id") or legacy_id("srv", name)),
                "name": name,
            }
        )
    return result


def _existing_profile_identities(store: MCPStore) -> list[dict[str, str]]:
    raw = _raw_json(store.profiles_path, {"profiles": []})
    rows = raw.get("profiles") or []
    if not isinstance(rows, list):
        raise ValueError("local profile store must contain an array")
    result: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("local profile store contains a malformed record")
        name = str(row.get("name") or "")
        if not name:
            raise ValueError("local profile store contains a profile without a name")
        result.append(
            {
                "id": str(row.get("id") or legacy_id("prf", name)),
                "name": name,
            }
        )
    return result


def _verify_manifest(bundle: dict[str, Any]) -> None:
    manifest = bundle.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("portable bundle version 4 requires a manifest")
    if int(manifest.get("version") or 0) != MANIFEST_VERSION:
        raise ValueError("unsupported portable bundle manifest version")
    if int(manifest.get("schema_version") or 0) != SCHEMA_VERSION:
        raise ValueError("portable bundle manifest schema version does not match this Megas build")
    if int(manifest.get("bundle_version") or 0) != PORTABLE_BUNDLE_VERSION:
        raise ValueError("portable bundle manifest version does not match bundle version")

    sections = {
        "servers": bundle.get("servers") or [],
        "profiles": bundle.get("profiles") or [],
        "secret_binding_requirements": bundle.get("secret_binding_requirements") or [],
    }
    hashes = manifest.get("section_sha256")
    if not isinstance(hashes, dict):
        raise ValueError("portable bundle manifest is missing section hashes")
    for key, value in sections.items():
        expected = str(hashes.get(key) or "")
        actual = _sha256(value)
        if expected != actual:
            raise ValueError(f"portable bundle checksum mismatch: {key}")
    expected_content = str(manifest.get("content_sha256") or "")
    if expected_content != _sha256(sections):
        raise ValueError("portable bundle checksum mismatch: content")

    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("portable bundle manifest is missing counts")
    for key, value in sections.items():
        if int(counts.get(key, -1)) != len(value):
            raise ValueError(f"portable bundle manifest count mismatch: {key}")


def _normalized_bundle(bundle: dict[str, Any]) -> tuple[int, list[MCPServer], list[dict[str, Any]], list[dict[str, str]]]:
    if bundle.get("format") != "megas-mcp-bundle":
        raise ValueError("not a Megas MCP bundle")
    version = int(bundle.get("version") or 1)
    if version not in {1, 2, 3, PORTABLE_BUNDLE_VERSION}:
        raise ValueError(f"unsupported Megas MCP bundle version: {version}")
    if version == PORTABLE_BUNDLE_VERSION:
        if int(bundle.get("schema_version") or 0) != SCHEMA_VERSION:
            raise ValueError("portable bundle schema version does not match this Megas build")
        _verify_manifest(bundle)

    raw_servers = bundle.get("servers") or []
    raw_profiles = bundle.get("profiles") or []
    raw_requirements = bundle.get("secret_binding_requirements") or []
    if not isinstance(raw_servers, list) or not isinstance(raw_profiles, list):
        raise ValueError("bundle servers and profiles must be arrays")
    if not isinstance(raw_requirements, list):
        raise ValueError("bundle secret binding requirements must be an array")

    servers = [MCPServer.from_dict(row) for row in raw_servers if isinstance(row, dict)]
    if len(servers) != len(raw_servers):
        raise ValueError("bundle contains malformed server records")
    server_ids = [server.id for server in servers]
    server_names = [server.name for server in servers]
    if len(server_ids) != len(set(server_ids)):
        raise ValueError("bundle contains duplicate server IDs")
    if len(server_names) != len(set(server_names)):
        raise ValueError("bundle contains duplicate server names")

    profiles = [dict(row) for row in raw_profiles if isinstance(row, dict)]
    if len(profiles) != len(raw_profiles):
        raise ValueError("bundle contains malformed profile records")

    requirements: list[dict[str, str]] = []
    for row in raw_requirements:
        if not isinstance(row, dict):
            raise ValueError("bundle contains malformed secret binding requirement")
        requirements.append(
            {
                "server_id": str(row.get("server_id") or ""),
                "target": str(row.get("target") or ""),
                "provider": str(row.get("provider") or "env"),
            }
        )
    # Legacy bundles may still contain env_refs. They are requirements, not portable bindings.
    for server in servers:
        for target in server.env_refs:
            requirements.append(
                {"server_id": server.id, "target": target, "provider": "env"}
            )
        server.env_refs = {}
    return version, servers, profiles, requirements


def _remap_profile_requirements(
    value: Any,
    *,
    field_name: str,
    id_remap: dict[str, str],
    prospective_server_ids: set[str],
) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"profile {field_name} must be an object")
    remapped: dict[str, list[str]] = {}
    for raw_server_id, raw_values in value.items():
        source_id = str(raw_server_id)
        server_id = id_remap.get(source_id, source_id)
        if server_id not in prospective_server_ids:
            raise ValueError(
                f"profile {field_name} references unknown server ID: {server_id}"
            )
        if not isinstance(raw_values, list):
            raise ValueError(f"profile {field_name} values must be arrays")
        bucket = remapped.setdefault(server_id, [])
        bucket.extend(str(item) for item in raw_values)
    return remapped


def plan_import(store: MCPStore, bundle: dict[str, Any], *, replace: bool = False) -> dict[str, Any]:
    """Validate an import and return its deterministic mutation plan without writing state."""

    version, servers, raw_profiles, requirements = _normalized_bundle(bundle)
    existing_servers = [] if replace else _existing_server_identities(store)
    existing_profiles = [] if replace else _existing_profile_identities(store)
    by_server_id = {row["id"]: row for row in existing_servers}
    by_server_name = {row["name"]: row for row in existing_servers}

    server_actions: list[dict[str, Any]] = []
    id_remap: dict[str, str] = {}
    prospective_server_ids = {row["id"] for row in existing_servers}
    prospective_name_to_id = {row["name"]: row["id"] for row in existing_servers}
    for server in servers:
        same_id = by_server_id.get(server.id)
        same_name = by_server_name.get(server.name)
        if same_id is not None and same_id["name"] != server.name:
            raise ValueError(
                f"server ID conflict: {server.id} belongs to local server {same_id['name']} but bundle names it {server.name}"
            )
        if same_name is not None and same_name["id"] != server.id:
            id_remap[server.id] = same_name["id"]
            target_id = same_name["id"]
            action = "update-by-name"
        elif same_id is not None:
            target_id = server.id
            action = "update"
        else:
            target_id = server.id
            action = "create"
        prospective_server_ids.add(target_id)
        prospective_name_to_id[server.name] = target_id
        server_actions.append(
            {
                "source_id": server.id,
                "target_id": target_id,
                "name": server.name,
                "action": action,
            }
        )

    profile_ids: set[str] = set()
    profile_names: set[str] = set()
    profile_actions: list[dict[str, Any]] = []
    by_profile_id = {row["id"]: row for row in existing_profiles}
    by_profile_name = {row["name"]: row for row in existing_profiles}
    for raw in raw_profiles:
        normalized = dict(raw)
        refs = normalized.get("server_ids")
        if refs is None:
            legacy_refs = normalized.get("servers") or []
            if not isinstance(legacy_refs, list):
                raise ValueError("profile server references must be an array")
            resolved: list[str] = []
            for value in legacy_refs:
                name = str(value)
                target_id = prospective_name_to_id.get(name)
                if target_id is None:
                    raise ValueError(f"profile {raw.get('name')} references unknown server: {name}")
                resolved.append(target_id)
            refs = resolved
        if not isinstance(refs, list):
            raise ValueError("profile server references must be an array")
        resolved_refs = [id_remap.get(str(value), str(value)) for value in refs]
        unknown = [value for value in resolved_refs if value not in prospective_server_ids]
        if unknown:
            raise ValueError(
                f"profile {raw.get('name')} references unknown server ID(s): {', '.join(unknown)}"
            )
        normalized["server_ids"] = resolved_refs
        normalized["startup_order"] = resolved_refs
        normalized.pop("servers", None)
        for field_name in (
            "tool_permission_requirements",
            "secret_binding_requirements",
        ):
            normalized[field_name] = _remap_profile_requirements(
                normalized.get(field_name),
                field_name=field_name,
                id_remap=id_remap,
                prospective_server_ids=prospective_server_ids,
            )
        profile = MCPProfile.from_dict(normalized)
        if profile.id in profile_ids:
            raise ValueError(f"bundle contains duplicate profile ID: {profile.id}")
        if profile.name in profile_names:
            raise ValueError(f"bundle contains duplicate profile name: {profile.name}")
        profile_ids.add(profile.id)
        profile_names.add(profile.name)

        same_id = by_profile_id.get(profile.id)
        same_name = by_profile_name.get(profile.name)
        if same_id is not None and same_id["name"] != profile.name:
            raise ValueError(
                f"profile ID conflict: {profile.id} belongs to local profile {same_id['name']} but bundle names it {profile.name}"
            )
        if same_name is not None and same_name["id"] != profile.id:
            target_id = same_name["id"]
            action = "update-by-name"
        elif same_id is not None:
            target_id = profile.id
            action = "update"
        else:
            target_id = profile.id
            action = "create"
        profile_actions.append(
            {
                "source_id": profile.id,
                "target_id": target_id,
                "name": profile.name,
                "server_ids": resolved_refs,
                "tool_permission_requirements": profile.tool_permission_requirements,
                "secret_binding_requirements": profile.secret_binding_requirements,
                "profile": profile.to_dict(),
                "action": action,
            }
        )

    normalized_requirements: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in requirements:
        server_id = id_remap.get(row["server_id"], row["server_id"])
        target = row["target"]
        provider = row["provider"]
        if server_id not in prospective_server_ids:
            raise ValueError(f"secret binding requirement references unknown server ID: {server_id}")
        if not target or any(ch.isspace() for ch in target) or "=" in target:
            raise ValueError("secret binding requirement has invalid target")
        if provider != "env":
            raise ValueError(f"unsupported secret binding provider in bundle: {provider}")
        normalized_requirements[(server_id, target, provider)] = {
            "server_id": server_id,
            "target": target,
            "provider": provider,
        }

    return {
        "format": "megas-mcp-import-plan",
        "bundle_version": version,
        "replace": replace,
        "content_sha256": (
            str((bundle.get("manifest") or {}).get("content_sha256") or "")
            if version == PORTABLE_BUNDLE_VERSION
            else _sha256(
                {
                    "servers": bundle.get("servers") or [],
                    "profiles": bundle.get("profiles") or [],
                    "secret_binding_requirements": bundle.get("secret_binding_requirements") or [],
                }
            )
        ),
        "servers": server_actions,
        "profiles": profile_actions,
        "secret_binding_requirements": sorted(
            normalized_requirements.values(),
            key=lambda row: (row["server_id"], row["target"], row["provider"]),
        ),
        "summary": {
            "servers_create": sum(row["action"] == "create" for row in server_actions),
            "servers_update": sum(row["action"] != "create" for row in server_actions),
            "profiles_create": sum(row["action"] == "create" for row in profile_actions),
            "profiles_update": sum(row["action"] != "create" for row in profile_actions),
            "profile_tool_permissions_required": sum(
                len(values)
                for row in profile_actions
                for values in row["tool_permission_requirements"].values()
            ),
            "profile_secret_bindings_required": sum(
                len(values)
                for row in profile_actions
                for values in row["secret_binding_requirements"].values()
            ),
            "secret_bindings_required": len(normalized_requirements),
        },
    }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _rebinding_path(store: MCPStore) -> Path:
    return store.mcp_dir / REBINDING_FILE


def _stored_requirements(store: MCPStore) -> list[dict[str, str]]:
    raw = _raw_json(
        _rebinding_path(store),
        {"version": REBINDING_STORE_VERSION, "schema_version": SCHEMA_VERSION, "requirements": []},
    )
    rows = raw.get("requirements") or []
    if not isinstance(rows, list):
        raise ValueError("secret rebinding requirement store must contain an array")
    result: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("secret rebinding requirement store contains a malformed record")
        result.append(
            {
                "id": str(row.get("id") or legacy_id("rebind", f"{row.get('server_id')}:{row.get('target')}:{row.get('provider') or 'env'}")),
                "server_id": str(row.get("server_id") or ""),
                "target": str(row.get("target") or ""),
                "provider": str(row.get("provider") or "env"),
                "source": str(row.get("source") or "import"),
            }
        )
    return result


def _write_requirements(store: MCPStore, rows: list[dict[str, str]]) -> None:
    deduped = {
        (row["server_id"], row["target"], row["provider"]): row
        for row in rows
    }
    ordered = sorted(deduped.values(), key=lambda row: (row["server_id"], row["target"], row["provider"]))
    _atomic_json(
        _rebinding_path(store),
        {
            "version": REBINDING_STORE_VERSION,
            "schema_version": SCHEMA_VERSION,
            "requirements": ordered,
        },
    )


def list_rebinding_requirements(store: MCPStore) -> list[dict[str, Any]]:
    """Return only still-unresolved machine bindings and prune satisfied/stale rows."""

    rows = _stored_requirements(store)
    servers = {server.id: server for server in store.list_servers()}
    bindings = {(binding.server_id, binding.target, binding.provider) for binding in store.list_secret_bindings()}
    unresolved: list[dict[str, str]] = []
    view: list[dict[str, Any]] = []
    for row in rows:
        key = (row["server_id"], row["target"], row["provider"])
        server = servers.get(row["server_id"])
        if server is None or key in bindings:
            continue
        unresolved.append(row)
        view.append(
            {
                **row,
                "server": server.name,
                "resolved": False,
                "next_action": f"megas mcp bind-env {server.name} {row['target']} <LOCAL_ENV_NAME>",
            }
        )
    if len(unresolved) != len(rows):
        _write_requirements(store, unresolved)
    return view


def _copy_candidate_file(source: Path, target: Path) -> None:
    if not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def _candidate_replacements(
    store: MCPStore,
    compatibility: dict[str, Any],
    plan: dict[str, Any],
    *,
    replace: bool,
) -> tuple[dict[Path, bytes | None], list[dict[str, Any]]]:
    """Materialize the complete post-import state away from authoritative live files."""

    with tempfile.TemporaryDirectory(prefix=".bundle-candidate-", dir=str(store.mcp_dir)) as temporary:
        candidate = MCPStore(Path(temporary) / "home")
        for source, target in (
            (store.registry_path, candidate.registry_path),
            (store.profiles_path, candidate.profiles_path),
            (store.secret_bindings_path, candidate.secret_bindings_path),
            (_rebinding_path(store), _rebinding_path(candidate)),
        ):
            _copy_candidate_file(source, target)

        candidate.import_bundle(compatibility, replace=replace)
        existing = [] if replace else _stored_requirements(candidate)
        imported_requirements = [
            {
                "id": legacy_id(
                    "rebind",
                    f"{row['server_id']}:{row['target']}:{row['provider']}",
                ),
                "server_id": row["server_id"],
                "target": row["target"],
                "provider": row["provider"],
                "source": "import",
            }
            for row in plan["secret_binding_requirements"]
        ]
        _write_requirements(candidate, [*existing, *imported_requirements])
        unresolved = list_rebinding_requirements(candidate)

        pairs = (
            (store.registry_path, candidate.registry_path),
            (store.profiles_path, candidate.profiles_path),
            (store.secret_bindings_path, candidate.secret_bindings_path),
            (_rebinding_path(store), _rebinding_path(candidate)),
        )
        replacements: dict[Path, bytes | None] = {
            live: staged.read_bytes() if staged.is_file() else None
            for live, staged in pairs
        }
        return replacements, unresolved


def import_bundle(
    store: MCPStore,
    bundle: dict[str, Any],
    *,
    replace: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    recovered = recover_pending_transaction(store.mcp_dir)
    if recovered is not None:
        store.record(
            "state.transaction.recovered",
            transaction_id=recovered["transaction_id"],
            purpose=recovered["purpose"],
            restored=recovered["restored"],
            removed=recovered["removed"],
        )

    plan = plan_import(store, bundle, replace=replace)
    if dry_run:
        return {**plan, "dry_run": True, "mutated": False}

    compatibility = {
        "format": "megas-mcp-bundle",
        "version": min(int(bundle.get("version") or 1), LEGACY_BUNDLE_VERSION),
        "schema_version": int(bundle.get("schema_version") or SCHEMA_VERSION),
        "servers": bundle.get("servers") or [],
        # Use the validated/remapped profile representation from the plan. This is
        # critical when the destination already has the same server name under a
        # different stable ID: requirement maps must follow the destination ID too.
        "profiles": [row["profile"] for row in plan["profiles"]],
        "secret_binding_requirements": bundle.get("secret_binding_requirements") or [],
    }
    replacements, unresolved = _candidate_replacements(
        store,
        compatibility,
        plan,
        replace=replace,
    )
    transaction = apply_state_transaction(
        store.mcp_dir,
        replacements,
        purpose="portable-bundle-import",
        metadata={
            "bundle_version": plan["bundle_version"],
            "content_sha256": plan["content_sha256"],
            "replace": replace,
        },
    )
    store.record(
        "bundle.portable_imported",
        bundle_version=plan["bundle_version"],
        content_sha256=plan["content_sha256"],
        replace=replace,
        transaction_id=transaction["transaction_id"],
        secret_bindings_unresolved=len(unresolved),
    )
    return {
        **plan,
        "dry_run": False,
        "mutated": True,
        "transaction_id": transaction["transaction_id"],
        "transaction_files": transaction["files"],
        "unresolved_secret_bindings": unresolved,
    }


__all__ = [
    "MANIFEST_VERSION",
    "PORTABLE_BUNDLE_VERSION",
    "REBINDING_FILE",
    "build_bundle",
    "import_bundle",
    "list_rebinding_requirements",
    "plan_import",
]
