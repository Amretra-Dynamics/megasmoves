"""Top-level system commands: status, doctor, version."""

from __future__ import annotations

import argparse
import platform
import shutil
import socket
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from megas import __version__
from megas.cli.context import RuntimeContext, build_context
from megas.cli.features import feature_status
from megas.cli.user_credentials import load_user_token, mask_token
from megas.client import detect_agent_binaries, get_installed_agent_binaries
from megas.config import default_config_path, load_config
from megas.credentials import credential_path
from megas.operate.browser.capability import browser_capability_report
from megas.runtime_manifest import local_certification_details
from megas.service import (
    daemon_status,
    service_installed,
    watchdog_status,
    windows_autostart_status,
    worker_connection_status,
)
from megas.updater import current_version, platform_id


def cmd_version(ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    data = {
        "version": current_version(),
        "package_version": __version__,
        "platform": platform_id() if _platform_ok() else f"{platform.system()}-{platform.machine()}",
        "python": platform.python_version(),
    }
    ctx.output.result(data, human_message=f"megas {data['version']}")
    return data


def _platform_ok() -> bool:
    try:
        platform_id()
        return True
    except Exception:
        return False


def _resolve_account_email(config, *, heal: bool = True) -> str:
    """Account that owns this machine — from local binding or backend (never hardcoded)."""
    email = str(getattr(config, "account_email", None) or "").strip().lower()
    if email:
        return email
    try:
        from megas.cli.account_binding import bound_account_email

        email = bound_account_email()
        if email:
            return email
    except Exception:
        pass
    if not heal or not config.is_paired:
        return ""
    try:
        import httpx
        from megas.config import save_config
        from megas.cli.account_binding import store_account_binding

        with httpx.Client(timeout=8.0) as client:
            response = client.get(
                f"{config.rest_base_url}/api/v1/workers/{config.worker_id}/owner",
                headers={"X-Megas-Worker-Token": config.worker_token},
            )
        if response.status_code != 200:
            return ""
        owner = str(response.json().get("user_email") or "").strip().lower()
        if not owner:
            return ""
        config.account_email = owner
        try:
            save_config(config)
            store_account_binding(owner, source="owner_heal", worker_id=config.worker_id)
        except Exception:
            pass
        return owner
    except Exception:
        return ""


def cmd_status(ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    """Unified product status: account + worker + backend (full product config)."""
    config = load_config()
    daemon = daemon_status()
    supervisor = watchdog_status()
    connection = worker_connection_status(
        expected_pid=int(daemon.get("pid") or 0) if daemon.get("running") else 0
    )
    backend_connected = bool(daemon.get("running") and connection.get("connected"))
    installed = service_installed()
    autostart = windows_autostart_status() if platform.system() == "Windows" else {
        "installed": installed,
        "healthy": installed,
    }
    token = load_user_token()
    account_email = _resolve_account_email(config, heal=True)
    backend: dict[str, Any] = {"reachable": False}
    try:
        client = ctx.client()
        backend = client.healthz()
        backend["reachable"] = True
    except Exception as exc:
        backend = {"reachable": False, "error": str(exc)}

    product_ready = bool(config.is_paired and backend_connected and account_email)
    data = {
        "cli_version": current_version(),
        "api_url": ctx.api_url,
        "account_email": account_email,
        "account_bound": bool(account_email),
        "product_ready": product_ready,
        "user_authenticated": bool(token),
        "user_token_preview": mask_token(token) if token else "",
        "worker": {
            "paired": config.is_paired,
            "worker_id": config.worker_id or "",
            "machine": config.machine_name or socket.gethostname(),
            "running": daemon.get("running"),
            "pid": daemon.get("pid"),
            "backend_connected": backend_connected,
            "connection_age_seconds": connection.get("age_seconds"),
            "connection_reason": connection.get("reason"),
            "watchdog_running": supervisor.get("running"),
            "watchdog_pid": supervisor.get("pid"),
            "service_installed": installed,
            "autostart": autostart,
            "log_path": daemon.get("log_path"),
        },
        "backend": backend,
        "allowed_folders": config.allowed_folders,
    }
    if not ctx.output.machine:
        conn = (
            "CONNECTED"
            if backend_connected
            else "PAIRED / WORKER OFFLINE"
            if config.is_paired
            else "NOT PAIRED"
        )
        run = "RUNNING" if daemon.get("running") else "STOPPED"
        ctx.output.print("=" * 60)
        ctx.output.print("                     MEGAS STATUS")
        ctx.output.print("=" * 60)
        ctx.output.print(f"  Version:     {data['cli_version']}")
        ctx.output.print(f"  API:         {ctx.api_url}")
        ctx.output.print(f"  Account:     {account_email or 'NOT BOUND — re-pair via website approval'}")
        ctx.output.print(f"  Worker:      {conn}")
        ctx.output.print(f"  Local state: {run}")
        ctx.output.print(f"  Watchdog:    {'RUNNING' if supervisor.get('running') else 'STOPPED'}")
        ctx.output.print(f"  Service:     {'INSTALLED' if installed else 'NOT INSTALLED / STALE'}")
        ctx.output.print(f"  Worker ID:   {config.worker_id or 'N/A'}")
        ctx.output.print(f"  Machine:     {config.machine_name or socket.gethostname()}")
        ctx.output.print(f"  Backend:     {'OK' if backend.get('reachable') else 'UNREACHABLE'}")
        ctx.output.print(f"  PID:         {daemon.get('pid') or 'N/A'}")
        ctx.output.print(
            f"  Product:     {'READY' if product_ready else 'INCOMPLETE — account + connected worker required'}"
        )
        try:
            from megas.cli.update_notice import check_for_update

            notice = check_for_update(api_base=ctx.api_url)
            if notice:
                ctx.output.print(
                    f"  Update:      {notice['local_version']} → {notice['remote_version']}  "
                    f"run: {notice['update_command']}"
                )
                ctx.output.print(f"  Reinstall:   {notice['reinstall_command']}")
        except Exception:
            pass
        ctx.output.print("=" * 60)
    else:
        ctx.output.result(data)
    return data


def cmd_doctor(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    config_path = default_config_path()
    backend_reachable = False
    try:
        request = Request(config.backend_url.rstrip("/") + "/healthz")
        with urlopen(request, timeout=5) as response:
            backend_reachable = 200 <= response.status < 500
    except Exception:
        backend_reachable = False

    browser_report = browser_capability_report()
    daemon = daemon_status()
    supervisor = watchdog_status()
    connection = worker_connection_status(
        expected_pid=int(daemon.get("pid") or 0) if daemon.get("running") else 0
    )
    backend_connected = bool(daemon.get("running") and connection.get("connected"))
    installed = service_installed()
    autostart = windows_autostart_status() if platform.system() == "Windows" else {
        "installed": installed,
        "healthy": installed,
    }
    adapter_binaries = {
        "codex_cli": __import__("os").getenv("MEGAS_CODEX_BINARY", "codex"),
        "claude_code": __import__("os").getenv("MEGAS_CLAUDE_BINARY", "claude"),
        "gemini_cli": __import__("os").getenv("MEGAS_GEMINI_BINARY", "gemini"),
        "aider": __import__("os").getenv("MEGAS_AIDER_BINARY", "aider"),
        "antigravity": __import__("os").getenv("MEGAS_ANTIGRAVITY_BINARY", "agy"),
    }
    adapter_readiness = {
        agent_id: shutil.which(binary) is not None for agent_id, binary in adapter_binaries.items()
    }
    agents_catalog: list[dict[str, Any]] = []
    try:
        from megas.agent_catalog import list_agents as _list_agents

        catalog_rows = _list_agents(config=config)
        agents_catalog = [row.to_dict() for row in catalog_rows]
        adapter_readiness = {
            row.agent_id: row.status == "ready"
            for row in catalog_rows
            if row.certified
        }
    except Exception:
        pass
    try:
        runtime_certification = local_certification_details(config)
    except Exception as exc:
        runtime_certification = {
            "health": {
                "status": "quarantined",
                "reasons": [f"runtime_certification_failed:{type(exc).__name__}"],
            }
        }
    certified_agents = runtime_certification.get("agents")
    if isinstance(certified_agents, dict):
        adapter_readiness = {
            agent_id: isinstance(report, dict)
            and report.get("status") == "available"
            for agent_id, report in certified_agents.items()
        }
    user_token = bool(load_user_token())
    worker_ready = not config.is_paired or bool(
        backend_connected and supervisor.get("running") and autostart.get("healthy")
    )
    checks: dict[str, Any] = {
        "status": "ok" if (config.is_paired or user_token) and backend_reachable and worker_ready else "needs_attention",
        "config_exists": config_path.exists(),
        "credential_store_exists": credential_path(config_path).exists(),
        "user_token_configured": user_token,
        "worker_identity_exists": bool(config.worker_id and config.worker_token),
        "backend_url": config.backend_url,
        "api_url": ctx.api_url,
        "backend_reachable": backend_reachable,
        "worker_wss_url": f"{config.ws_base_url}/api/v1/workers/{config.worker_id or '<worker_id>'}/connect",
        "allowed_folders": config.allowed_folders,
        "allowed_folders_exist": all(Path(folder).is_dir() for folder in config.allowed_folders),
        "git_available": shutil.which("git") is not None,
        "docker_available": shutil.which("docker") is not None,
        "browser_runtime_available": browser_report.available,
        "browser_engine": browser_report.browser,
        "browser_runtime_dedicated": browser_report.dedicated,
        "browser_runtime_verified": browser_report.runtime_verified,
        "browser_playwright_version": browser_report.playwright_version,
        "browser_runtime_root": browser_report.runtime_root,
        "browser_runtime_reasons": list(browser_report.reasons),
        "certified_adapters": adapter_readiness,
        "detected_agents": detect_agent_binaries(),
        "installed_certified_agents": get_installed_agent_binaries(),
        "agents_catalog": agents_catalog,
        "runtime_certification": runtime_certification,
        "service_installed": installed,
        "autostart": autostart,
        "worker_running": daemon["running"],
        "worker_pid": daemon.get("pid"),
        "worker_backend_connected": backend_connected,
        "worker_connection": connection,
        "watchdog_running": supervisor.get("running"),
        "watchdog_pid": supervisor.get("pid"),
        "platform_release": platform_id() if _platform_ok() else platform.platform(),
        "feature_flags": feature_status(),
        "cli_version": current_version(),
    }
    selected_adapter = getattr(args, "adapter", None)
    if selected_adapter and not adapter_readiness.get(selected_adapter):
        checks["status"] = "error"
        checks["message"] = (
            f"{selected_adapter} binary '{adapter_binaries[selected_adapter]}' is unavailable."
        )
    if runtime_certification.get("health", {}).get("status") == "quarantined":
        checks["status"] = "error"
    codex_ok = checks.get("certified_adapters", {}).get("codex_cli", False)
    checks["codex_health"] = "healthy" if codex_ok else "unhealthy"
    checks["megas_version"] = __version__

    if not getattr(args, "json", False):
        print("=" * 60)
        print("                       MEGAS MOVES DOCTOR REPORT")
        print("=" * 60)
        print(f"  Status:              {checks['status'].upper()}")
        print(f"  Backend Reachable:   {'YES' if checks['backend_reachable'] else 'NO'}")
        print(f"  Browser Runtime:     {'READY' if checks['browser_runtime_available'] else 'UNAVAILABLE'}")
        print(f"  Browser Engine:      {checks['browser_engine'] or 'N/A'}")
        print(f"  Browser Verified:    {'YES' if checks['browser_runtime_verified'] else 'NO'}")
        print(f"  Codex Health:        {'HEALTHY' if codex_ok else 'UNHEALTHY'}")
        print("=" * 60)
    else:
        import json
        print(json.dumps(checks, indent=2))
    return checks


def register(sub: argparse._SubParsersAction) -> None:
    version = sub.add_parser("version", help="Show CLI version")
    version.set_defaults(func=_wrap(cmd_version))


def _wrap(fn):
    def runner(args: argparse.Namespace) -> None:
        ctx = build_context(args)
        fn(ctx, args)

    runner.__name__ = getattr(fn, "__name__", "system_cmd")
    runner.__wrapped__ = fn  # type: ignore[attr-defined]
    return runner


__all__ = ["cmd_doctor", "cmd_status", "cmd_version", "register"]
