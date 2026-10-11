"""Coding-agent catalog projected through installation runtime evidence."""

from __future__ import annotations

from typing import Any

from . import agent_catalog_legacy as _legacy
from .launch_agent_catalog import apply_launch_install_overrides

for _name, _value in vars(_legacy).items():
    if not _name.startswith("__"):
        globals()[_name] = _value


_original_sync_catalog_from_store = _legacy.sync_catalog_from_store
_original_get_catalog = _legacy.get_catalog


def _apply_launch_catalog(catalog):
    apply_launch_install_overrides(
        catalog,
        spec_type=_legacy.AgentSpec,
        strategy_type=_legacy.InstallStrategy,
    )
    _legacy.AGENT_CATALOG = catalog
    _legacy._rebuild_alias_index()
    return catalog


def sync_catalog_from_store(
    *,
    force_refresh: bool = False,
    query: str | None = None,
):
    """Load the persistent store, then enforce the reviewed launch matrix."""
    return _apply_launch_catalog(
        _original_sync_catalog_from_store(
            force_refresh=force_refresh,
            query=query,
        )
    )


def get_catalog(
    *,
    ensure: bool = True,
    force_refresh: bool = False,
    query: str | None = None,
):
    """Return a catalog whose launch-agent records cannot be made stale by cache."""
    return _apply_launch_catalog(
        _original_get_catalog(
            ensure=ensure,
            force_refresh=force_refresh,
            query=query,
        )
    )


# Legacy functions retain their defining module globals. Route those lookups
# through the same authoritative catalog rather than maintaining two registries.
_legacy.sync_catalog_from_store = sync_catalog_from_store
_legacy.get_catalog = get_catalog


def _runtime_decision(agent_id: str, *, config: Any | None = None):
    try:
        from .runtime_manifest import (
            _agent_adapter_classes,
            _auth_mode,
            _certification_for,
        )

        adapter_class = _agent_adapter_classes().get(agent_id)
        if adapter_class is None:
            return None, "adapter_not_registered"
        adapter = adapter_class([])
        executable = adapter.discover_executable()
        if executable is None:
            try:
                spec = _legacy.get_spec(agent_id)
                executable = _legacy.discover_binary(spec, config=config)
            except Exception:
                executable = None
        if executable is None:
            return None, "binary_not_found"
        version = adapter.detect_version()
        if not version:
            return None, "provider_version_unverified"
        health = str(adapter.health_status())
        if health != "available":
            return None, f"adapter_health:{health}"
        auth_mode = _auth_mode(agent_id, adapter)
        if auth_mode == "unauthenticated":
            return None, "provider_authentication_required"
        decision = _certification_for(
            agent_id=agent_id,
            adapter=adapter,
            executable=executable,
            version=version,
            auth_mode=auth_mode,
            execution_mode="inspect",
        )
        return decision, decision.reason
    except Exception as exc:
        return None, f"runtime_certification_error:{type(exc).__name__}"


def _project_status(row, *, config: Any | None = None):
    decision, reason = _runtime_decision(row.agent_id, config=config)
    row.certified = bool(decision and decision.certified)
    if not row.enabled:
        row.status = "disabled"
        row.detail = "Disabled in Megas config."
    elif not row.binary_path:
        row.status = "missing"
        row.detail = "Not found on this machine."
    elif row.certified:
        row.status = "ready"
        row.detail = "Runtime-certified for exact native sync, resume, stream, and reconciliation."
    else:
        row.status = "detected"
        row.detail = (
            "Detected but dispatch is disabled until runtime certification passes: "
            + str(reason or "runtime_certification_required")
        )
    return row


def inspect_agent(name: str, *, config: Any | None = None):
    row = _legacy.inspect_agent(name, config=config)
    return _project_status(row, config=config)


def list_agents(
    *,
    config: Any | None = None,
    certified_only: bool = False,
    auto_register: bool = True,
):
    rows = _legacy.list_agents(
        config=config,
        certified_only=False,
        auto_register=auto_register,
    )
    projected = [_project_status(row, config=config) for row in rows]
    if certified_only:
        projected = [row for row in projected if row.certified]
    return projected


def discover_system_agents(*, config: Any | None = None) -> dict[str, Any]:
    rows = [row.to_dict() for row in list_agents(config=config)]
    ready = [row for row in rows if row["status"] == "ready"]
    detected = [row for row in rows if row["status"] == "detected"]
    missing = [row for row in rows if row["status"] == "missing"]
    installable = _legacy.list_installable_missing(config=config)
    return {
        "platform": _legacy.host_platform(),
        "agents": rows,
        "ready_count": len(ready),
        "detected_uncertified_count": len(detected),
        "missing_count": len(missing),
        "ready_agent_ids": [row["agent_id"] for row in ready],
        "detected_uncertified_ids": [row["agent_id"] for row in detected],
        "installable_missing": installable,
        "installable_missing_ids": [row["agent_id"] for row in installable],
        "path_sample": [str(path) for path in _legacy._candidate_search_dirs()[:12]],
    }


def certify_agent(name: str, *, timeout_seconds: float = 300.0) -> dict[str, Any]:
    from .certification_runner import certify_agent_installation

    agent_id = _legacy.resolve_agent_id(name)
    result = certify_agent_installation(
        agent_id,
        timeout_seconds=timeout_seconds,
    )
    return {
        "agent_id": result.provider_id,
        "certified": result.certified,
        "certification_status": result.status,
        "certification_reason": result.reason,
        "certificate_path": result.certificate_path,
        "checks": result.checks,
        "verified_capabilities": list(result.verified_capabilities),
    }


def _refresh_saved_capabilities(config: Any) -> list[str]:
    from .config import save_config
    from .runtime_manifest import (
        build_runtime_manifest,
        runtime_capabilities_from_manifest,
    )

    manifest = build_runtime_manifest(config)
    capabilities = runtime_capabilities_from_manifest(manifest)
    config.capabilities = capabilities
    save_config(config)
    return capabilities


def setup_agent(
    name: str,
    *,
    config: Any | None = None,
    binary=None,
    enable: bool = True,
) -> dict[str, Any]:
    from .config import load_config

    cfg = config if config is not None else load_config()
    registered = _legacy.setup_agent(
        name,
        config=cfg,
        binary=binary,
        enable=enable,
    )
    certification: dict[str, Any]
    if not enable:
        certification = {
            "certified": False,
            "certification_status": "disabled",
            "certification_reason": "disabled_in_config",
            "checks": {},
            "verified_capabilities": [],
        }
    else:
        try:
            certification = certify_agent(
                str(registered.get("agent_id") or name),
                timeout_seconds=float(
                    __import__("os").getenv("MEGAS_CERTIFICATION_TIMEOUT_SEC", "300")
                    or "300"
                ),
            )
        except Exception as exc:
            certification = {
                "certified": False,
                "certification_status": "not_verified",
                "certification_reason": f"{type(exc).__name__}:{str(exc)[:500]}",
                "checks": {},
                "verified_capabilities": [],
            }
    capabilities = _refresh_saved_capabilities(cfg)
    registered.update(certification)
    registered["capabilities_include_agent"] = (
        bool(certification.get("certified"))
        and str(registered.get("agent_id") or "") in capabilities
    )
    registered["note"] = (
        "Agent registered and runtime-certified. Restarting the Worker is not required; "
        "the next heartbeat advertises it."
        if certification.get("certified")
        else "Agent registered as detected only. Dispatch remains disabled until the "
        "reported certification check passes; rerun `megas agents setup` after fixing it."
    )
    return registered
