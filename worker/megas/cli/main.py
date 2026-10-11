from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
from pathlib import Path
import shutil
import socket
import sys
import time
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import webbrowser

import httpx

from megas.client import WorkerRuntime, detect_agent_binaries, get_installed_agent_binaries, pair_worker, runtime_capabilities
from megas.config import WorkerConfig, default_config_path, load_config, save_config
from megas.credentials import credential_path, delete_worker_token
from megas.service import (
    daemon_status,
    ensure_worker_and_watchdog,
    install_service,
    log_path,
    run_service,
    run_watchdog,
    service_installed,
    start_background,
    start_installed_service,
    stop_background,
    stop_installed_service,
    stop_watchdog,
    tail_logs,
    uninstall_service,
    wait_for_worker_connection,
    watchdog_status,
)
from megas.updater import platform_id, update_cli

from megas.cli.commands import agents as agents_cmds
from megas.cli.commands import auth as auth_cmds
from megas.cli.commands import config_cmd
from megas.cli.commands import monitor as monitor_cmds
from megas.cli.commands import notifications as notifications_cmds
from megas.cli.commands import operate as operate_cmds
from megas.cli.commands import runs as runs_cmds
from megas.cli.commands import security as security_cmds
from megas.cli.commands import system as system_cmds
from megas.cli.commands import workers as workers_cmds
from megas.cli.completions import render as render_completion
from megas.cli.context import add_global_flags, build_context
from megas.cli.errors import CliError
from megas.cli.exit_codes import EXIT_GENERAL, EXIT_INVALID_ARGS, EXIT_SUCCESS
from megas.cli.output import emit_json, redact_text


def _ensure_paired(config: WorkerConfig) -> WorkerConfig:
    if config.is_paired:
        return config
    print("Megas is not connected yet. Starting secure setup.")
    return run_sso_login(config)


_lock_socket = None

def acquire_instance_lock() -> None:
    global _lock_socket
    try:
        _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock_socket.bind(('127.0.0.1', 50055))
    except socket.error:
        import sys
        print("Error: Another instance of Megas Worker is already running on this machine.", file=sys.stderr)
        sys.exit(1)


def cmd_connect(args: argparse.Namespace) -> None:
    config = load_config()
    config.backend_url = args.backend.rstrip("/")
    config.worker_name = args.name
    from megas.config import resolve_machine_label

    config.machine_name = resolve_machine_label(getattr(args, "machine", None) or "")
    if args.code:
        asyncio.run(pair_worker(config, args.code))
        print(f"Megas connected this machine ({config.worker_id}).")
        return
    run_sso_login(config)


def cmd_allow(args: argparse.Namespace) -> None:
    import sys
    try:
        config = load_config()
        path = Path(args.path).expanduser().resolve()
        if not path.exists():
            print(f"Error: Workspace path does not exist: {path}", file=sys.stderr)
            sys.exit(1)
        if not path.is_dir():
            print(f"Error: Workspace path must be a directory, not a file: {path}", file=sys.stderr)
            sys.exit(1)
        if path.parents == () or path.parent == path:
            print("Error: Adding the root drive directly as a workspace is not allowed.", file=sys.stderr)
            sys.exit(1)
            
        folder = str(path)
        if folder not in config.allowed_folders:
            config.allowed_folders.append(folder)
        save_config(config)
        print(f"Allowed: {folder}")
    except Exception as e:
        print(f"Error allowing folder: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_disallow(args: argparse.Namespace) -> None:
    import sys
    try:
        config = load_config()
        path = Path(args.path).expanduser().resolve()
        folder = str(path)
        if folder in config.allowed_folders:
            config.allowed_folders.remove(folder)
            save_config(config)
            print(f"Removed allowed folder: {folder}")
        else:
            print(f"Folder was not allowed: {folder}")
    except Exception as e:
        print(f"Error removing folder: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_enable_agent(args: argparse.Namespace) -> None:
    """Legacy top-level enable-agent → catalog enable/setup."""
    try:
        from megas.agent_catalog import enable_agent

        result = enable_agent(args.agent, workspaces=getattr(args, "workspaces", None))
        print(f"Enabled agent: {result.get('agent_id')}")
        if result.get("binary_path"):
            print(f"Binary: {result['binary_path']}")
        if result.get("note"):
            print(result["note"])
    except Exception as e:
        print(f"Error enabling agent: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_disable_agent(args: argparse.Namespace) -> None:
    """Legacy top-level disable-agent → catalog disable."""
    try:
        from megas.agent_catalog import disable_agent

        result = disable_agent(args.agent)
        print(f"Disabled agent: {result.get('agent_id')}")
    except Exception as e:
        print(f"Error disabling agent: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_install_agent(args: argparse.Namespace) -> None:
    """Top-level: megas install [names...] | megas install-agent ...

    No names → print not-installed but installable agents (ordered) for bulk pick.
    Multiple names → fuzzy resolve + bulk install.
    """
    for name in (
        "json",
        "output",
        "quiet",
        "verbose",
        "debug",
        "no_color",
        "profile",
        "api_url",
        "timeout",
        "token",
        "yes",
        "non_interactive",
    ):
        if not hasattr(args, name):
            setattr(
                args,
                name,
                False if name not in {"output", "profile", "api_url", "timeout", "token"} else None,
            )
    # Normalize name sources: nargs=* → names; single agent → names
    names = getattr(args, "names", None)
    if names is None and getattr(args, "agent", None):
        agent = args.agent
        names = agent if isinstance(agent, list) else [agent]
    args.names = list(names or [])
    args.method = getattr(args, "method", "auto")
    args.yes_flag = bool(getattr(args, "yes", False) or getattr(args, "yes_flag", False))
    args.dry_run = bool(getattr(args, "dry_run", False))
    args.all = bool(getattr(args, "all", False))
    args.refresh = bool(getattr(args, "refresh", False))
    ctx = build_context(args)
    agents_cmds.cmd_agents_install(ctx, args)



def run_sso_login(config: WorkerConfig) -> WorkerConfig:
    backend_url = config.backend_url.rstrip("/")
    capabilities = runtime_capabilities(config)
    # Use product platform id (macos-arm64, …). Full platform.platform() strings
    # have caused 422 validation failures and forced users to re-pair.
    try:
        plat = platform_id()
    except Exception:
        plat = platform.system().lower()
    payload = {
        "worker_name": config.worker_name or "Megas Local Worker",
        "machine_name": config.machine_name or socket.gethostname(),
        "platform": plat,
        "capabilities": capabilities,
        "agent_binaries": {},
    }
    try:
        with httpx.Client(timeout=20.0) as client:
            started = client.post(f"{backend_url}/api/v1/cli/device-authorizations", json=payload)
            started.raise_for_status()
            authorization = started.json()
    except httpx.HTTPError as exc:
        raise SystemExit(f"Could not start Megas sign-in: {exc}") from exc

    print("Open this Megas link on any signed-in device:")
    print(f"\n  {authorization['verification_uri_complete']}\n")
    try:
        webbrowser.open(authorization["verification_uri_complete"])
    except Exception:
        pass
    print("Waiting for approval...")

    # Poll immediately, then wait only after a pending (428) response.
    # Backend default interval is 1s; clamp so old/misconfigured values cannot stall UX.
    poll_interval = int(authorization.get("interval", 1) or 1)
    poll_interval = 1 if poll_interval < 1 else (2 if poll_interval > 2 else poll_interval)
    deadline = time.monotonic() + 15 * 60
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.post(
                    f"{backend_url}/api/v1/cli/device-authorizations/token",
                    json={"device_code": authorization["device_code"]},
                )
            if response.status_code == 428:
                time.sleep(poll_interval)
                continue
            response.raise_for_status()
            registered = response.json()
        except httpx.HTTPStatusError as exc:
            raise SystemExit(f"Megas sign-in did not complete: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise SystemExit(f"Could not check Megas sign-in: {exc}") from exc
        config.worker_id = registered["worker_id"]
        config.worker_token = registered["worker_token"]
        config.machine_id = registered.get("machine_id", "")
        config.machine_name = payload["machine_name"]
        config.capabilities = capabilities
        # Owner email is whatever account approved on the website — dynamic only.
        owner = str(registered.get("user_email") or "").strip().lower()
        if owner:
            config.account_email = owner
            try:
                from megas.cli.account_binding import store_account_binding

                store_account_binding(owner, source="device_pairing", worker_id=config.worker_id)
            except Exception:
                pass
        save_config(config)
        print(f"Megas connected this machine ({config.worker_id}).")
        if owner:
            print(f"Account: {owner}")
        return config
    raise SystemExit("Megas sign-in expired. Run `megas connect` to try again.")


def _start_managed() -> dict:
    # Always keep the watchdog supervisor online so dead Workers recover without
    # a human restart (phone sessions must not sit queued).
    # Linux upgrades must rewrite the user unit: merely finding an old unit can
    # keep launching an obsolete virtualenv forever after a CLI reinstall.
    from megas.service import install_service, service_installed, start_installed_service, ensure_worker_and_watchdog

    must_refresh = platform.system() == "Linux" or not service_installed()
    if must_refresh:
        try:
            install_service()
            print("Megas autostart is installed for this user (survives reboot).")
        except Exception as exc:
            print(f"Note: could not install autostart yet ({exc}). Continuing start.")
    if service_installed():
        return start_installed_service()
    return ensure_worker_and_watchdog()


def cmd_start(args: argparse.Namespace) -> None:
    acquire_instance_lock()
    config = _ensure_paired(load_config())
    if getattr(args, "foreground", False):
        print("Megas is running in the foreground. Press Ctrl+C to stop.")
        try:
            asyncio.run(WorkerRuntime(config).start_forever())
        except KeyboardInterrupt:
            print("Megas stopped.")
        return
    status = _start_managed()
    extra = ""
    if status.get("watchdog_pid"):
        extra = f", watchdog PID {status.get('watchdog_pid')}"
    print(f"Megas started (PID {status.get('pid')}{extra}).")


def cmd_status(args: argparse.Namespace) -> None:
    """Local + product status. Supports global --json via RuntimeContext."""
    # Ensure global flag attrs exist when invoked from interactive control.
    for name, default in (
        ("json", False),
        ("output", None),
        ("quiet", False),
        ("verbose", False),
        ("debug", False),
        ("no_color", False),
        ("profile", None),
        ("api_url", None),
        ("timeout", None),
        ("token", None),
        ("yes", False),
        ("non_interactive", False),
        ("watch", False),
        ("since", None),
        ("limit", None),
    ):
        if not hasattr(args, name):
            setattr(args, name, default)
    ctx = build_context(args)
    system_cmds.cmd_status(ctx, args)


def _legacy_status() -> None:
    config = load_config()
    daemon = daemon_status()
    supervisor = watchdog_status()
    print("=" * 60)
    print("                       MEGAS STATUS")
    print("=" * 60)
    print(f"  Connection:  {'CONNECTED' if config.is_paired else 'NOT CONNECTED'}")
    print(f"  Local state: {'RUNNING' if daemon['running'] else 'STOPPED'}")
    print(f"  Watchdog:    {'RUNNING' if supervisor['running'] else 'STOPPED'}")
    print(f"  Service:     {'INSTALLED' if service_installed() else 'NOT INSTALLED'}")
    print(f"  Worker:      {config.worker_id or 'N/A'}")
    print(f"  Machine:     {config.machine_name or socket.gethostname()}")
    print(f"  Backend:     {config.backend_url}")
    print(f"  PID:         {daemon.get('pid') or 'N/A'}")
    print(f"  Watchdog PID:{supervisor.get('pid') or 'N/A'}")
    print(f"  Log:         {daemon['log_path']}")
    print()
    print("Allowed folders:")
    if config.allowed_folders:
        for folder in config.allowed_folders:
            marker = "OK" if Path(folder).is_dir() else "MISSING"
            print(f"  [{marker}] {folder}")
    else:
        print("  None")
    print()
    print("Local coding agents:")
    try:
        from megas.agent_catalog import list_agents

        for row in list_agents(config=config):
            label = row.display_name
            if row.status == "ready":
                mode = "controlled write" if row.agent_id == "codex_cli" else "isolated read-only"
                print(f"  [READY]    {label}: {mode} ({row.binary_path})")
            elif row.status == "disabled":
                print(f"  [DISABLED] {label}")
            elif row.status in {"detected", "quarantined"}:
                print(f"  [{row.status.upper():<8}] {label}: {row.binary_path or row.detail}")
            else:
                print(f"  [MISSING]  {label}  →  megas agents install {row.agent_id}")
    except Exception:
        detected = detect_agent_binaries()
        certified = get_installed_agent_binaries()
        for agent_id, path in certified.items():
            print(f"  [READY]    {agent_id}: {path}")
        for agent_id, path in detected.items():
            if agent_id not in certified:
                print(f"  [DETECTED] {agent_id}: {path}")
    print("Manage: megas agents list | megas agents install <name>")
    print("=" * 60)


def cmd_logs(args: argparse.Namespace) -> None:
    for line in tail_logs(getattr(args, "lines", 100)):
        print(line, end="")
    if not getattr(args, "follow", False):
        if not log_path().exists():
            print(f"No Megas logs yet ({log_path()}).")
        return
    try:
        position = log_path().stat().st_size if log_path().exists() else 0
        while True:
            time.sleep(0.4)
            if not log_path().exists():
                position = 0
                continue
            size = log_path().stat().st_size
            if size < position:
                position = 0
            if size == position:
                continue
            with log_path().open("r", encoding="utf-8", errors="replace") as stream:
                stream.seek(position)
                print(stream.read(), end="", flush=True)
                position = stream.tell()
    except KeyboardInterrupt:
        return


def cmd_stop(_: argparse.Namespace) -> None:
    was_running = daemon_status()["running"] or watchdog_status()["running"]
    # Stop supervisor first so it cannot resurrect the Worker mid-stop.
    stop_watchdog()
    if service_installed():
        stop_installed_service()
    stop_background()
    print("Megas stopped." if was_running else "Megas is already stopped.")


def _product_reconnect_online() -> dict:
    """Product bar after update: autostart + Worker + backend connected.

    Same success criteria as first install (``megas worker install-service``).
    """
    _ensure_paired(load_config())
    install_service()
    status = ensure_worker_and_watchdog()
    running = bool(status.get("running"))
    pid = status.get("pid")
    if not running or not pid:
        raise RuntimeError(
            "Megas update finished the binary step, but the Worker process did not stay running."
        )
    connection = wait_for_worker_connection(expected_pid=int(pid), timeout=90)
    if not connection.get("connected"):
        raise RuntimeError(
            "Megas Worker started after update but did not connect to the backend "
            f"({connection.get('reason')}). Inspect Worker and watchdog logs."
        )
    return {
        "pid": int(pid),
        "watchdog_pid": status.get("watchdog_pid"),
        "age_seconds": float(connection.get("age_seconds") or 0.0),
        "connected": True,
    }


def _repair_product_path() -> None:
    """Best-effort PATH/env repair after update/reinstall (Unix)."""
    try:
        from megas.updater import _target_binary, repair_cli_path_links

        repair_cli_path_links(_target_binary())
    except Exception:
        pass


def cmd_update(args: argparse.Namespace) -> None:
    """Update *this product install* on the machine (not a developer deploy).

    Always:
      1) stop Worker + watchdog cleanly
      2) pull latest production CLI binary when a newer release exists
      3) reinstall autostart (Run key / tasks / LaunchAgent / systemd)
      4) start Worker + watchdog and wait until backend-connected

    "Already latest" is success for the binary step — we still refresh autostart
    and reconnect so a stale Worker is repaired.
    """
    from megas.updater import current_version, _target_binary

    _ensure_paired(load_config())
    # Product update always tears down first so we never half-upgrade a live PID.
    cmd_stop(argparse.Namespace())

    force = bool(getattr(args, "force", False))
    is_dev_install = False
    
    try:
        target = _target_binary()
        # Check if this is a development/editable install
        if not target.is_file() or "site-packages" in str(target) or ".pth" in str(target):
            is_dev_install = True
    except Exception:
        is_dev_install = True
    
    if is_dev_install:
        # Development install: just refresh runtime without binary update
        print("Development install detected. Refreshing runtime only (no binary update)...")
        _repair_product_path()
        try:
            from megas.updater import repair_windows_product_path
            repair_windows_product_path()
        except Exception:
            pass
        print("Reinstalling autostart and waiting until CONNECTED…")
        online = _product_reconnect_online()
        version = current_version()
        print(
            f"Megas is RUNNING and CONNECTED "
            f"(version {version}, PID {online['pid']}, "
            f"connected_age={online['age_seconds']:.0f}s). "
            "Autostart reinstalled."
        )
        return
    
    try:
        result = update_cli(load_config(), force=force, restart=False)
    except RuntimeError as exc:
        message = str(exc)
        # Source checkouts have no published-binary channel; still repair runtime.
        if "standalone" in message.lower() or "development checkout" in message.lower():
            result = {
                "updated": False,
                "deferred": False,
                "version": current_version(),
                "message": (
                    "This install has no production binary channel "
                    "(developer checkout). Refreshing runtime only."
                ),
            }
        else:
            # Best-effort bring-back if binary download failed after stop.
            try:
                _product_reconnect_online()
            except Exception:
                pass
            raise

    if result.get("deferred"):
        # Windows self-replace: helper runs install-service after this process exits.
        print(result["message"])
        target = result.get("target") or ""
        if target:
            print(f"Replacing binary at:\n  {target}")
        print(
            "\nIMPORTANT:\n"
            "  1) Wait 15 seconds (do not run megas again immediately)\n"
            "  2) Close this window and open a NEW terminal\n"
            "  3) Run:  megas version\n"
            "     Must show the NEW version, not the old one.\n"
            "  4) If still old:  type where megas\n"
            "     and check %LOCALAPPDATA%\\MegasMoves\\logs\\update-replace.log\n"
        )
        return

    _repair_product_path()
    try:
        from megas.updater import repair_windows_product_path

        repair_windows_product_path()
    except Exception:
        pass
    print(result.get("message") or "Refreshing Megas Worker connection…")
    print("Reinstalling autostart and waiting until CONNECTED…")
    online = _product_reconnect_online()
    version = result.get("version") or current_version()
    print(
        f"Megas is RUNNING and CONNECTED "
        f"(version {version}, PID {online['pid']}, "
        f"connected_age={online['age_seconds']:.0f}s). "
        "Autostart reinstalled."
    )


def cmd_reinstall(args: argparse.Namespace) -> None:
    """Force-reinstall this machine's Megas product (keep pairing).

    Same bar as first install: latest binary, autostart, Worker CONNECTED.
    Does **not** require website re-login when credentials are still valid.

    Force website re-login instead:
      MEGAS_FORCE_RELOGIN=1 curl -fsSL https://megasmoves.io/install.sh | sh
    """
    from megas.updater import current_version

    print("Force-refreshing Megas binary + autostart (pairing + settings kept)…")
    # Always re-download binary even if version matches.
    force_args = argparse.Namespace(force=True)
    cmd_update(force_args)
    _repair_product_path()
    print(
        f"Refresh complete (version {current_version()}). "
        "Commands: megas | megas status | megas update | megas reinstall"
    )


def cmd_doctor(args: argparse.Namespace) -> None:
    try:
        ctx = build_context(args)
        system_cmds.cmd_doctor(ctx, args)
        return
    except TypeError:
        pass
    # Legacy doctor path
    config = load_config()
    config_path = default_config_path()
    codex_binary = os.getenv("MEGAS_CODEX_BINARY", "codex")
    try:
        request = Request(config.backend_url.rstrip("/") + "/healthz")
        with urlopen(request, timeout=5) as response:
            backend_reachable = 200 <= response.status < 500
    except Exception:
        backend_reachable = False
    browsers = [name for name in ("chrome", "google-chrome", "chromium", "msedge") if shutil.which(name)]
    daemon = daemon_status()
    adapter_binaries = {
        "codex_cli": os.getenv("MEGAS_CODEX_BINARY", "codex"),
        "claude_code": os.getenv("MEGAS_CLAUDE_BINARY", "claude"),
        "gemini_cli": os.getenv("MEGAS_GEMINI_BINARY", "gemini"),
        "aider": os.getenv("MEGAS_AIDER_BINARY", "aider"),
    }
    adapter_readiness = {agent_id: shutil.which(binary) is not None for agent_id, binary in adapter_binaries.items()}
    try:
        live_capabilities = runtime_capabilities(config)
    except Exception:
        live_capabilities = list(config.capabilities or [])
    windows_cua = {
        name: name in live_capabilities
        for name in ("desktop.windows.uia", "desktop.windows.input", "windows_cua")
    }
    try:
        from megas.operate.windows.capability import build_capability_reports

        cua_reports = [report.to_dict() for report in build_capability_reports()]
    except Exception as exc:
        cua_reports = [{"error": type(exc).__name__}]
    checks = {
        "status": "ok" if config.is_paired and backend_reachable else "needs_attention",
        "product": {
            "name": "megas",
            "binary": sys.executable if getattr(sys, "frozen", False) else "python -m megas.cli",
            "frozen": bool(getattr(sys, "frozen", False)),
            "includes_cli": True,
            "includes_local_worker": True,
            "includes_windows_cua": os.name == "nt" or sys.platform.startswith("win"),
        },
        "config_exists": config_path.exists(),
        "credential_store_exists": credential_path(config_path).exists(),
        "worker_identity_exists": bool(config.worker_id and config.worker_token),
        "backend_url": config.backend_url,
        "backend_reachable": backend_reachable,
        "worker_wss_url": f"{config.ws_base_url}/api/v1/workers/{config.worker_id or '<worker_id>'}/connect",
        "allowed_folders": config.allowed_folders,
        "allowed_folders_exist": all(Path(folder).is_dir() for folder in config.allowed_folders),
        "git_available": shutil.which("git") is not None,
        "docker_available": shutil.which("docker") is not None,
        "browser_runtime_available": bool(browsers),
        "browser_binaries": browsers,
        "codex_binary": codex_binary,
        "codex_adapter_ready": shutil.which(codex_binary) is not None,
        "certified_adapters": adapter_readiness,
        "capabilities": live_capabilities,
        "windows_cua": windows_cua,
        "windows_cua_reports": cua_reports,
        "service_installed": service_installed(),
        "worker_running": daemon["running"],
        "worker_pid": daemon.get("pid"),
        "platform_release": platform_id(),
    }
    selected_adapter = getattr(args, "adapter", None)
    if selected_adapter and not adapter_readiness[selected_adapter]:
        checks["status"] = "error"
        checks["message"] = f"{selected_adapter} binary '{adapter_binaries[selected_adapter]}' is unavailable."
    print(json.dumps(checks, indent=2))


def cmd_install_service(_: argparse.Namespace) -> None:
    _ensure_paired(load_config())
    install_service()
    # Production bar: installation succeeds only after this exact daemon PID
    # emits a fresh positive WebSocket pulse.
    status = ensure_worker_and_watchdog()
    running = bool(status.get("running"))
    pid = status.get("pid")
    if not running or not pid:
        raise RuntimeError("Megas autostart was installed, but the Worker process did not stay running.")
    connection = wait_for_worker_connection(expected_pid=int(pid), timeout=90)
    if not connection.get("connected"):
        raise RuntimeError(
            "Megas Worker started but did not connect to the backend "
            f"({connection.get('reason')}). "
            "If Windows Network Security / Firewall prompted, click Allow for "
            "megasmoves.exe, then re-run: megas worker install-service. "
            "Inspect the Worker and watchdog logs if it still fails."
        )
    age = float(connection.get("age_seconds") or 0.0)
    print(
        f"Megas is RUNNING and CONNECTED (PID {pid}, connected_age={age:.0f}s). "
        "Autostart (watchdog) is installed for sign-in."
    )


def cmd_uninstall_service(_: argparse.Namespace) -> None:
    stop_installed_service()
    stop_background()
    uninstall_service()
    print("Megas automatic startup was removed.")


def cmd_run_service(_: argparse.Namespace) -> None:
    config = load_config()
    if not config.is_paired:
        raise SystemExit("Megas service cannot start before `megas connect` completes.")
    run_service(config)


def cmd_run_watchdog(_: argparse.Namespace) -> None:
    config = load_config()
    if not config.is_paired:
        raise SystemExit("Megas watchdog cannot start before `megas connect` completes.")
    run_watchdog()


def cmd_revoke(args: argparse.Namespace) -> None:
    config_path = default_config_path()
    config = load_config(config_path)
    if not config.worker_id:
        print("This machine is not connected to Megas.")
        return
    cmd_stop(argparse.Namespace())
    if not getattr(args, "local_only", False):
        try:
            response = httpx.post(
                f"{config.rest_base_url}/api/v1/workers/{config.worker_id}/revoke",
                headers={"X-Megas-Worker-Token": config.worker_token},
                timeout=20,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SystemExit("Megas Backend could not revoke this machine. Use --local-only only if the credential is already revoked.") from exc
    delete_worker_token(config.worker_id, config_path)
    try:
        from megas.cli.account_binding import clear_account_binding

        clear_account_binding()
    except Exception:
        pass
    config.worker_id = ""
    config.worker_token = ""
    config.machine_id = ""
    config.account_email = ""
    save_config(config, config_path)
    print("This machine was disconnected from Megas.")


def cmd_bootstrap(_: argparse.Namespace) -> None:
    if sys.stdin.isatty():
        raise SystemExit("Bootstrap credentials must be provided through standard input.")
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise SystemExit("Bootstrap payload is invalid JSON.") from exc
    backend_url = str(payload.get("backend_url") or "https://megasmoves.io").rstrip("/")
    parsed = urlparse(backend_url)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("Bootstrap Backend must use HTTPS.")
    owner = str(payload.get("account_email") or payload.get("user_email") or "").strip().lower()
    config = WorkerConfig(
        backend_url=backend_url,
        worker_id=str(payload.get("worker_id") or ""),
        worker_token=str(payload.get("worker_token") or ""),
        machine_id=str(payload.get("machine_id") or ""),
        machine_name=str(payload.get("machine_name") or socket.gethostname()),
        worker_name=str(payload.get("worker_name") or "Megas Local Worker"),
        account_email=owner,
        allowed_folders=[str(item) for item in payload.get("allowed_folders", []) if isinstance(item, str)],
        capabilities=[str(item) for item in payload.get("capabilities", []) if isinstance(item, str)],
        agents={
            str(key): value
            for key, value in (payload.get("agents") or {}).items()
            if isinstance(key, str) and isinstance(value, dict)
        } if isinstance(payload.get("agents"), dict) else {},
    )
    if not config.is_paired:
        raise SystemExit("Bootstrap payload is missing worker credentials.")
    # Prefer live runtime capabilities from this binary (CLI + worker + platform CUA).
    try:
        config.capabilities = runtime_capabilities(config)
    except Exception:
        # Keep installer-provided list if detection fails during bootstrap.
        pass
    save_config(config)
    if owner:
        try:
            from megas.cli.account_binding import store_account_binding

            store_account_binding(owner, source="bootstrap", worker_id=config.worker_id)
        except Exception:
            pass
    print(f"Megas connected this machine ({config.worker_id}).")
    if owner:
        print(f"Account: {owner}")
    print("Product: megas CLI + Local Worker" + (" + Windows CUA" if os.name == "nt" else "") + ".")


def _print_product_options() -> None:
    """Primary product surface once the CLI binary is already on the machine.

    First-time product setup is always from the website (curl / irm), never
    ``megas install`` — that subcommand installs coding agents only.
    """
    print()
    print("Commands:")
    print("  megas status       Connection + Worker status")
    print("  megas start        Start Worker")
    print("  megas stop         Stop Worker")
    print("  megas update       Pull latest binary + reconnect (keeps pairing)")
    print("  megas reinstall    Force-refresh binary + autostart (keeps pairing)")
    print("  megas connect      Pair this machine (website approval)")
    print("  megas doctor       Diagnose local problems")
    print("  megas logs         Show local logs")
    print()
    print("Note: coding agents use `megas install codex` — not product setup.")
    print()


def _apply_recommended_action(recommended: str, *, interactive: bool) -> None:
    """Apply smart default without wiping user configuration."""
    if recommended == "healthy":
        return
    if recommended == "connect":
        if not interactive:
            print("Next: megas connect")
            return
        choice = input("Open website pairing for this machine? [Y/n]: ").strip().lower()
        if choice in {"", "y", "yes"}:
            # Preserve allowed_folders / agents / machine_name on existing config.
            config = load_config()
            preserved_folders = list(config.allowed_folders or [])
            preserved_agents = dict(config.agents or {})
            preserved_name = config.machine_name
            config = run_sso_login(config)
            # run_sso_login only rewrites pairing fields; re-assert user settings.
            if preserved_folders:
                config.allowed_folders = list(
                    dict.fromkeys([*config.allowed_folders, *preserved_folders])
                )
            if preserved_agents:
                merged = dict(preserved_agents)
                merged.update(config.agents or {})
                config.agents = merged
            if preserved_name and (
                not config.machine_name or config.machine_name == socket.gethostname()
            ):
                config.machine_name = preserved_name
            save_config(config)
            try:
                _product_reconnect_online()
            except Exception as exc:
                print(f"Paired, but Worker reconnect needs attention: {exc}")
        return
    if recommended == "repair":
        print("Repairing Worker + autostart (pairing and settings kept)…")
        try:
            _repair_product_path()
            online = _product_reconnect_online()
            print(
                f"Repaired. CONNECTED (PID {online['pid']}, "
                f"connected_age={online['age_seconds']:.0f}s)."
            )
        except Exception as exc:
            print(f"Repair failed: {exc}")
            print("Try: megas reinstall   # force binary refresh, keeps pairing")
        return
    if recommended == "update":
        if interactive:
            choice = input("Update to latest Megas now? [Y/n]: ").strip().lower()
            if choice not in {"", "y", "yes"}:
                print("Skipped. When ready: megas update")
                return
        cmd_update(argparse.Namespace(force=False))
        return
    if recommended == "reinstall":
        if interactive:
            choice = input(
                "Force-refresh Megas binary + autostart now? (keeps pairing) [Y/n]: "
            ).strip().lower()
            if choice not in {"", "y", "yes"}:
                print("Skipped. When ready: megas reinstall")
                return
        cmd_reinstall(argparse.Namespace())
        return


def cmd_control() -> None:
    """Default ``megas`` entry: detect connect / update / reinstall / repair.

    Product is never installed via ``megas install`` — that is coding agents only.
    Website scripts put the binary on the machine; this command keeps it healthy.
    """
    from megas.product_lifecycle import assess_product, format_assessment, recommended_command

    interactive = sys.stdin.isatty()
    assessment = assess_product()
    print(format_assessment(assessment))
    print(f"Recommended: {recommended_command(assessment)}")
    print()

    # Safe auto-repair when Worker/autostart is broken; never auto-wipe config.
    if assessment.recommended == "repair":
        _apply_recommended_action("repair", interactive=interactive)
    elif assessment.recommended in {"connect", "update", "reinstall"}:
        _apply_recommended_action(assessment.recommended, interactive=interactive)
    elif assessment.recommended == "healthy":
        try:
            cmd_status(argparse.Namespace())
        except Exception:
            pass

    if not interactive:
        if assessment.recommended == "healthy":
            _print_product_options()
        return

    while True:
        _print_product_options()
        choice = input(
            "[S]tart  [U]pdate  [R]einstall  [C]onnect  [L]ogs  S[t]op  [D]octor  [Q]uit: "
        ).strip().lower()
        if choice in {"s", "start"}:
            cmd_start(argparse.Namespace(foreground=False))
        elif choice in {"u", "update"}:
            cmd_update(argparse.Namespace(force=False))
        elif choice in {"r", "reinstall"}:
            cmd_reinstall(argparse.Namespace())
        elif choice in {"c", "connect"}:
            config = load_config()
            run_sso_login(config)
            try:
                _product_reconnect_online()
            except Exception as exc:
                print(f"Connected, Worker start needs attention: {exc}")
        elif choice in {"l", "logs"}:
            cmd_logs(argparse.Namespace(lines=40, follow=False))
        elif choice in {"t", "stop"}:
            cmd_stop(argparse.Namespace())
        elif choice in {"d", "doctor"}:
            cmd_doctor(argparse.Namespace(adapter=None, json=False))
        elif choice in {"q", "quit", ""}:
            return


def cmd_completion(args: argparse.Namespace) -> None:
    script = render_completion(args.shell)
    print(script, end="" if script.endswith("\n") else "\n")


def _add_connect_parser(sub: argparse._SubParsersAction, name: str, *, code_required: bool = False) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, help="Connect this machine to Megas")
    parser.add_argument("--backend", default="https://megasmoves.io")
    parser.add_argument("--code", required=code_required, default="", help="Short-lived pairing code from Megas")
    parser.add_argument("--name", default="Megas Local Worker")
    parser.add_argument("--machine", default="")
    parser.set_defaults(func=cmd_connect)
    return parser


def _add_start_parser(sub: argparse._SubParsersAction, name: str) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, help="Start Megas on this machine")
    parser.add_argument("--foreground", action="store_true", help="Keep Megas attached to this terminal")
    parser.set_defaults(func=cmd_start)
    return parser


def _add_status_parser(sub: argparse._SubParsersAction, name: str) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, help="Show Megas status (local worker + API auth)")
    parser.set_defaults(func=cmd_status)
    return parser


def _add_allow_parser(sub: argparse._SubParsersAction, name: str) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, help="Allow Megas to operate inside a workspace folder")
    parser.add_argument("path")
    parser.set_defaults(func=cmd_allow)
    return parser


def _add_worker_namespace(sub: argparse._SubParsersAction) -> None:
    worker = sub.add_parser("worker", help="Advanced Megas Worker commands")
    worker_sub = worker.add_subparsers(required=True)
    _add_connect_parser(worker_sub, "pair", code_required=True)
    _add_connect_parser(worker_sub, "connect")
    _add_start_parser(worker_sub, "start")
    _add_status_parser(worker_sub, "status")
    _add_allow_parser(worker_sub, "allow")
    disallow = worker_sub.add_parser("disallow", help="Remove an allowed workspace folder")
    disallow.add_argument("path")
    disallow.set_defaults(func=cmd_disallow)
    worker_sub.add_parser("run-service", help=argparse.SUPPRESS).set_defaults(func=cmd_run_service)
    worker_sub.add_parser("run-watchdog", help=argparse.SUPPRESS).set_defaults(func=cmd_run_watchdog)
    worker_sub.add_parser("bootstrap", help=argparse.SUPPRESS).set_defaults(func=cmd_bootstrap)
    worker_sub.add_parser("install-service", help="Start Megas automatically at sign-in").set_defaults(func=cmd_install_service)
    worker_sub.add_parser("uninstall-service", help="Remove Megas automatic startup").set_defaults(func=cmd_uninstall_service)
    worker_sub.add_parser("stop", help="Stop Megas Worker service").set_defaults(func=cmd_stop)
    revoke = worker_sub.add_parser("revoke", help="Disconnect and revoke this machine")
    revoke.add_argument("--local-only", action="store_true", help=argparse.SUPPRESS)
    revoke.set_defaults(func=cmd_revoke)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="megas",
        description="Megas Moves CLI — local worker control and production operations",
    )
    add_global_flags(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    # --- Legacy / local worker surface (preserved) ---
    _add_connect_parser(sub, "connect")
    _add_connect_parser(sub, "pair")
    _add_start_parser(sub, "start")
    _add_status_parser(sub, "status")
    _add_allow_parser(sub, "allow")
    _add_allow_parser(sub, "allow-folder")
    disallow = sub.add_parser("disallow", help="Remove an allowed workspace folder")
    disallow.add_argument("path")
    disallow.set_defaults(func=cmd_disallow)

    enable_agent = sub.add_parser("enable-agent", help="Enable a coding agent (alias of `megas agents enable`)")
    enable_agent.add_argument("agent", help="Agent id or alias (codex, claude, gemini, aider, agy, …)")
    enable_agent.add_argument("--workspaces", nargs="+", help="Restrict to specific workspaces")
    enable_agent.set_defaults(func=cmd_enable_agent)

    disable_agent = sub.add_parser("disable-agent", help="Disable a coding agent (alias of `megas agents disable`)")
    disable_agent.add_argument("agent", help="Agent id or alias")
    disable_agent.set_defaults(func=cmd_disable_agent)

    def _add_install_parser(name: str, *, help_text: str) -> None:
        p = sub.add_parser(name, help=help_text)
        p.add_argument(
            "names",
            nargs="*",
            help="Coding agent names/aliases (fuzzy OK). Omit to list not-installed installable agents.",
        )
        p.add_argument("--all", action="store_true", help="Install every auto-installable missing agent")
        p.add_argument("--method", default="auto", help="auto|npm|pipx|pip|brew|script")
        p.add_argument("--yes", "-y", action="store_true", help="Confirm remote installer scripts")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--refresh", action="store_true", help="Refresh live web catalog first")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=cmd_install_agent)

    _add_install_parser(
        "install",
        help_text="Install coding agents (no args = list missing). Example: megas install codex claude",
    )
    _add_install_parser(
        "install-agent",
        help_text="Alias of `megas install` (coding agents, not Megas itself)",
    )
    logs = sub.add_parser("logs", help="Show Megas local logs")
    logs.add_argument("--lines", type=int, default=100)
    logs.add_argument("--follow", "-f", action="store_true")
    logs.set_defaults(func=cmd_logs)
    sub.add_parser("stop", help="Stop Megas on this machine").set_defaults(func=cmd_stop)
    update = sub.add_parser(
        "update",
        help="Update this machine's Megas install (binary, autostart, Worker, backend connection)",
    )
    update.add_argument(
        "--force",
        action="store_true",
        help="Re-download the production binary even if the version matches",
    )
    update.set_defaults(func=cmd_update)
    reinstall = sub.add_parser(
        "reinstall",
        help="Force reinstall binary + autostart + reconnect (keeps pairing; no website re-login)",
    )
    reinstall.set_defaults(func=cmd_reinstall)
    doctor = sub.add_parser("doctor", help="Check Megas local environment and ops readiness")
    doctor.add_argument(
        "--adapter",
        choices=["codex_cli", "claude_code", "gemini_cli", "aider", "antigravity"],
        help="Check a certified adapter",
    )
    doctor.add_argument("--json", action="store_true", help="Output doctor check results in JSON format")
    doctor.set_defaults(func=cmd_doctor)
    _add_worker_namespace(sub)

    # --- Ops surface ---
    agents_cmds.register(sub)
    auth_cmds.register(sub)
    config_cmd.register(sub)
    system_cmds.register(sub)
    workers_cmds.register(sub)
    runs_cmds.register(sub)
    monitor_cmds.register(sub)
    operate_cmds.register(sub)
    security_cmds.register(sub)
    notifications_cmds.register(sub)

    completion = sub.add_parser("completion", help="Print shell completion script")
    completion.add_argument("shell", choices=["bash", "zsh", "fish", "powershell"])
    completion.set_defaults(func=cmd_completion)

    return parser


def _emit_error(exc: BaseException, *, want_json: bool) -> int:
    if isinstance(exc, CliError):
        payload = exc.to_dict()
        message = redact_text(exc.message)
        if want_json:
            emit_json(payload, stream=sys.stderr)
        else:
            sys.stderr.write(redact_text(message) + "\n")
            if exc.hint:
                sys.stderr.write(redact_text(f"hint: {exc.hint}") + "\n")
        return int(exc.exit_code)
    if isinstance(exc, SystemExit):
        code = exc.code
        if code is None:
            return EXIT_SUCCESS
        if isinstance(code, int):
            return code
        sys.stderr.write(redact_text(str(code)) + "\n")
        return EXIT_GENERAL
    if isinstance(exc, KeyboardInterrupt):
        sys.stderr.write("\ninterrupted\n")
        return 130
    if isinstance(exc, BrokenPipeError):
        return EXIT_SUCCESS
    sys.stderr.write(redact_text(f"error: {exc}") + "\n")
    return EXIT_GENERAL


def _should_show_update_notice(argv: list[str], *, want_json: bool) -> bool:
    if want_json or "--quiet" in argv or "-q" in argv:
        return False
    # Avoid noise / recursion on update itself, completions, pure version dumps.
    if not argv:
        return True
    head = argv[0].lstrip("-").lower()
    if head in {"update", "reinstall", "completion", "version", "help"}:
        return False
    if "--help" in argv or "-h" in argv:
        return False
    return True


def main(argv: list[str] | None = None) -> None:
    # Force UTF-8 on stdout/stderr so print() never crashes with 'charmap'
    # codec errors when agent output contains emoji or non-ASCII characters.
    if sys.platform == "win32":
        for stream_name in ("stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            if stream and hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass
    argv = list(sys.argv[1:] if argv is None else argv)
    want_json = "--json" in argv or any(
        a == "json" and i > 0 and argv[i - 1] == "--output" for i, a in enumerate(argv)
    )
    if _should_show_update_notice(argv, want_json=want_json):
        try:
            from megas.cli.update_notice import maybe_print_update_notice

            maybe_print_update_notice(quiet=False, machine=want_json)
        except Exception:
            pass

    if not argv:
        acquire_instance_lock()
        cmd_control()
        return

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse already wrote to stderr
        code = exc.code if isinstance(exc.code, int) else EXIT_INVALID_ARGS
        raise SystemExit(code) from exc

    try:
        args.func(args)
    except CliError as exc:
        raise SystemExit(_emit_error(exc, want_json=want_json or getattr(args, "json", False) or getattr(args, "output", None) == "json")) from exc
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise SystemExit(_emit_error(KeyboardInterrupt(), want_json=want_json)) from None
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        raise SystemExit(EXIT_SUCCESS) from None
    except Exception as exc:
        raise SystemExit(_emit_error(exc, want_json=want_json)) from exc


__all__ = ["build_parser", "main", "_ensure_paired", "_start_managed", "acquire_instance_lock"]


if __name__ == "__main__":
    main()
