from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .model import SCHEMA_VERSION, utc_now
from .profile_requirements import assert_profile_requirements, evaluate_profile_requirements
from .protocol import probe_server
from .runtime import MCPRuntime
from .secret_release import SecretReleasePolicy
from .store import MCPStore


ACTIVE_PROFILE_VERSION = 1
ACTIVE_PROFILE_FILE = "active_profile.json"


def _path(store: MCPStore) -> Path:
    return store.mcp_dir / ACTIVE_PROFILE_FILE


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
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


def _read_active(store: MCPStore) -> dict[str, Any] | None:
    path = _path(store)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("active profile state must be a JSON object")
    if int(raw.get("version") or 0) != ACTIVE_PROFILE_VERSION:
        raise ValueError("unsupported active profile state version")
    if int(raw.get("schema_version") or 0) != SCHEMA_VERSION:
        raise ValueError("active profile state schema version does not match this Megas build")
    profile_id = str(raw.get("profile_id") or "")
    if not profile_id:
        raise ValueError("active profile state is missing profile_id")
    return raw


def active_profile(store: MCPStore, runtime: MCPRuntime | None = None) -> dict[str, Any] | None:
    raw = _read_active(store)
    if raw is None:
        return None
    try:
        profile = store.get_profile(str(raw["profile_id"]))
    except KeyError:
        _path(store).unlink(missing_ok=True)
        store.record("profile.activation.pruned", profile_id=str(raw["profile_id"]))
        return None

    servers: list[dict[str, Any]] = []
    for server_id in profile.startup_order:
        server = store.get_server_by_id(server_id)
        row: dict[str, Any] = {
            "server_id": server.id,
            "server": server.name,
            "transport": server.transport,
            "enabled": server.enabled,
        }
        if runtime is not None:
            row["runtime"] = runtime.status(server)
        servers.append(row)
    return {
        **raw,
        "profile": profile.name,
        "server_ids": list(profile.server_ids),
        "startup_order": list(profile.startup_order),
        "health_policy": profile.health_policy,
        "startup_timeout_seconds": profile.startup_timeout_seconds,
        "project_metadata": dict(profile.project_metadata),
        "tool_permission_requirements": profile.tool_permission_requirements,
        "secret_binding_requirements": profile.secret_binding_requirements,
        "requirements": evaluate_profile_requirements(store, profile),
        "servers": servers,
    }


def _preflight_profile(
    store: MCPStore,
    runtime: MCPRuntime,
    profile_reference: str,
) -> tuple[Any, list[Any], list[dict[str, Any]], dict[str, Any]]:
    profile = store.get_profile(profile_reference)
    secret_policy = SecretReleasePolicy(store)

    # Requirements are declarations, not authority. Activation must verify that
    # the owner already granted each tool and explicitly bound/released each secret.
    requirements = assert_profile_requirements(
        store,
        profile,
        secret_policy=secret_policy,
    )

    servers = [store.get_server_by_id(server_id) for server_id in profile.startup_order]
    health: list[dict[str, Any]] = []

    for server in servers:
        if not server.enabled:
            raise RuntimeError(
                f"profile {profile.name} cannot activate because server {server.name} is disabled"
            )

        if server.transport == "stdio":
            doctor = runtime.doctor(server)
            healthy = bool(doctor.get("healthy"))
            health.append(
                {
                    "server_id": server.id,
                    "server": server.name,
                    "transport": server.transport,
                    "healthy": healthy,
                    "source": "local-doctor",
                    "problems": list(doctor.get("problems") or []),
                }
            )
            if not healthy and profile.health_policy == "required":
                problems = doctor.get("problems") or []
                detail = "; ".join(str(value) for value in problems) or "local runtime diagnostic failed"
                raise RuntimeError(
                    f"profile {profile.name} cannot activate because server {server.name} is unhealthy: {detail}"
                )
            # Secret release is authorization, not a health hint. It stays mandatory
            # even when the profile uses best-effort health checks.
            secret_policy.prepare_server(server)
            continue

        try:
            probe = probe_server(
                server,
                timeout=min(profile.startup_timeout_seconds, 30.0),
            )
            health.append(
                {
                    "server_id": server.id,
                    "server": server.name,
                    "transport": server.transport,
                    "healthy": True,
                    "source": "remote-mcp-handshake",
                    "protocol_version": probe.get("protocol_version"),
                    "server_info": probe.get("server_info") or {},
                }
            )
        except Exception as exc:
            health.append(
                {
                    "server_id": server.id,
                    "server": server.name,
                    "transport": server.transport,
                    "healthy": False,
                    "source": "remote-mcp-handshake",
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            )
            if profile.health_policy == "required":
                raise RuntimeError(
                    f"profile {profile.name} cannot activate because remote server {server.name} failed its MCP health handshake: {exc}"
                ) from exc

    return profile, servers, health, requirements


def activate_profile(
    store: MCPStore,
    runtime: MCPRuntime,
    profile_reference: str,
) -> dict[str, Any]:
    """Preflight then activate a profile transactionally across newly-started stdio servers."""

    profile, servers, health, requirements = _preflight_profile(store, runtime, profile_reference)
    previous = active_profile(store, runtime)
    already_active = previous is not None and previous["profile_id"] == profile.id

    started: list[Any] = []
    runtime_results: list[dict[str, Any]] = []
    try:
        for server in servers:
            if server.transport == "stdio":
                result = runtime.start(
                    server,
                    timeout=profile.startup_timeout_seconds,
                )
                runtime_results.append(result)
                if bool(result.get("changed")):
                    started.append(server)
            else:
                runtime_results.append(runtime.status(server))
    except Exception:
        for server in reversed(started):
            try:
                runtime.stop(server)
            except Exception:
                pass
        raise

    context_changed = not already_active
    runtime_changed = bool(started)
    if context_changed:
        # Do not silently stop a previously active profile. Activation changes the
        # session/workspace context; explicit deactivate/stop remains the shutdown action.
        payload = {
            "version": ACTIVE_PROFILE_VERSION,
            "schema_version": SCHEMA_VERSION,
            "profile_id": profile.id,
            "activated_at": utc_now(),
            "source": "local",
        }
        _atomic_json(_path(store), payload)

    if context_changed or runtime_changed:
        store.record(
            "profile.activated" if context_changed else "profile.reactivated",
            profile_id=profile.id,
            profile=profile.name,
            previous_profile_id=previous["profile_id"] if previous else None,
            started_server_ids=[server.id for server in started],
            health_policy=profile.health_policy,
            health_failures=sum(not bool(row.get("healthy")) for row in health),
            required_tools=sum(len(values) for values in profile.tool_permission_requirements.values()),
            required_secrets=sum(len(values) for values in profile.secret_binding_requirements.values()),
        )

    current = active_profile(store, runtime)
    if current is None:
        # The only way no state can exist here is if the context should have been
        # written and persistence failed or was concurrently removed.
        raise RuntimeError("profile activation state was not persisted")
    return {
        **current,
        "changed": context_changed or runtime_changed,
        "already_active": already_active,
        "runtime_results": runtime_results,
        "health": health,
        "requirements": requirements,
    }


def deactivate_profile(
    store: MCPStore,
    runtime: MCPRuntime,
    *,
    keep_running: bool = False,
) -> dict[str, Any]:
    current = active_profile(store, runtime)
    if current is None:
        return {"changed": False, "active": None, "stopped": []}

    profile = store.get_profile(str(current["profile_id"]))
    stopped: list[dict[str, Any]] = []
    if not keep_running:
        for server_id in reversed(profile.startup_order):
            server = store.get_server_by_id(server_id)
            if server.transport == "stdio":
                stopped.append(runtime.stop(server))
    _path(store).unlink(missing_ok=True)
    store.record(
        "profile.deactivated",
        profile_id=profile.id,
        profile=profile.name,
        keep_running=keep_running,
    )
    return {
        "changed": True,
        "profile_id": profile.id,
        "profile": profile.name,
        "keep_running": keep_running,
        "stopped": stopped,
    }


__all__ = [
    "ACTIVE_PROFILE_FILE",
    "ACTIVE_PROFILE_VERSION",
    "activate_profile",
    "active_profile",
    "deactivate_profile",
]
