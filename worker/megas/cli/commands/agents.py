"""Coding agents: list, discover, install, setup, enable/disable, refresh."""

from __future__ import annotations

import argparse
from typing import Any

from megas.agent_catalog import (
    AgentInstallError,
    disable_agent,
    discover_system_agents,
    enable_agent,
    install_agent,
    install_agents,
    install_menu,
    inspect_agent,
    list_agents,
    list_installable_missing,
    refresh_agents_catalog,
    resolve_agent_id,
    setup_agent,
)
from megas.cli.context import RuntimeContext, build_context
from megas.cli.errors import CliError, InvalidArgumentsError
from megas.cli.exit_codes import EXIT_GENERAL
from megas.config import load_config


def _print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No coding agents in catalog.")
        return
    print("=" * 72)
    print("                    MEGAS CODING AGENTS")
    print("=" * 72)
    print(f"  {'AGENT':<16} {'STATUS':<12} {'VERSION':<12} BINARY / NOTE")
    print("-" * 72)
    for row in rows:
        agent = row.get("agent_id") or ""
        status = (row.get("status") or "").upper()
        version = row.get("version") or "-"
        binary = row.get("binary_path") or row.get("detail") or "-"
        if len(str(binary)) > 38:
            binary = "…" + str(binary)[-37:]
        flag = "" if row.get("certified") else " (uncertified)"
        print(f"  {agent:<16} {status:<12} {str(version)[:12]:<12} {binary}{flag}")
    print("=" * 72)
    print("Commands:")
    print("  megas install                 # show not-installed agents (installable here)")
    print("  megas install codex claude    # fuzzy names OK; bulk install")
    print("  megas install --all           # install every auto-installable missing agent")
    print("  megas agents refresh          # pull live web sources into local agent DB")
    print("  megas agents setup <name>     # detect + register without reinstall")
    print("=" * 72)


def _print_installable(menu: dict[str, Any]) -> None:
    installable = menu.get("installable_missing") or []
    installed = menu.get("installed") or []
    print("=" * 72)
    print("              MEGAS INSTALL — CODING AGENTS ON THIS PC")
    print("=" * 72)
    print(f"  Platform: {menu.get('platform')}")
    print(f"  Already installed: {len(installed)}")
    if installed:
        for row in installed:
            print(f"    [OK] {row.get('agent_id')}  {row.get('binary_path') or ''}")
    print()
    print("  Not installed (can be installed on this machine), in order:")
    if not installable:
        print("    (none — all known agents are present or no installer available)")
    else:
        for i, row in enumerate(installable, 1):
            auto = "auto" if row.get("can_auto_install") else "manual"
            cert = "certified" if row.get("certified") else "detect-only"
            methods = ",".join(row.get("install_methods") or []) or "-"
            print(
                f"    {i:>2}. {row.get('agent_id'):<16} [{auto}/{cert}]  "
                f"methods={methods}"
            )
            aliases = row.get("aliases") or []
            if aliases:
                print(f"        aliases: {', '.join(aliases[:6])}")
    print()
    if menu.get("bulk_hint"):
        print(f"  Bulk auto-install example:")
        print(f"    {menu['bulk_hint']}")
    print("  Or:  megas install --all")
    print("  Half-baked names work:  megas install cla  /  megas install kimi code")
    print("=" * 72)


def cmd_agents_list(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    certified_only = bool(getattr(args, "certified", False))
    rows = [s.to_dict() for s in list_agents(config=config, certified_only=certified_only)]
    data = {"agents": rows, "count": len(rows)}
    if ctx.output.machine or getattr(args, "json", False):
        ctx.output.result(data)
    else:
        _print_table(rows)
    return data


def cmd_agents_discover(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    data = discover_system_agents(config=config)
    if ctx.output.machine or getattr(args, "json", False):
        ctx.output.result(data)
    else:
        _print_table(data["agents"])
        print(f"Ready: {data['ready_count']}  Missing: {data['missing_count']}  Platform: {data['platform']}")
        if data.get("ready_agent_ids"):
            print("Available for phone sessions:", ", ".join(data["ready_agent_ids"]))
        missing_ids = data.get("installable_missing_ids") or []
        if missing_ids:
            print("Installable but not on this PC:", ", ".join(missing_ids))
            print("  -> megas install " + " ".join(missing_ids[:8]))
    return data


def cmd_agents_show(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    try:
        status = inspect_agent(args.name, config=config)
    except ValueError as exc:
        raise InvalidArgumentsError(str(exc)) from exc
    data = status.to_dict()
    if ctx.output.machine or getattr(args, "json", False):
        ctx.output.result(data)
    else:
        print(f"{data['display_name']} ({data['agent_id']})")
        print(f"  Status:   {data['status']}")
        print(f"  Enabled:  {data['enabled']}")
        print(f"  Certified:{data['certified']}")
        print(f"  Binary:   {data['binary_path'] or '—'}")
        print(f"  Version:  {data['version'] or '—'}")
        print(f"  Install:  {', '.join(data['install_methods']) or '—'}")
        print(f"  Aliases:  {', '.join(data['aliases'])}")
        if data.get("auth_hint"):
            print(f"  Auth:     {data['auth_hint']}")
        if data.get("detail"):
            print(f"  Note:     {data['detail']}")
    return data


def cmd_agents_install(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    """Install coding agents — or list missing installable when no names given."""
    config = load_config()
    names: list[str] = []
    raw_names = getattr(args, "names", None) or getattr(args, "name", None)
    if isinstance(raw_names, list):
        names = [str(n) for n in raw_names if str(n).strip()]
    elif isinstance(raw_names, str) and raw_names.strip():
        names = [raw_names.strip()]
    # support single .name for older callers
    if not names and getattr(args, "name", None) and not isinstance(getattr(args, "name"), list):
        names = [str(args.name)]

    do_all = bool(getattr(args, "all", False))
    refresh = bool(getattr(args, "refresh", False))
    if refresh:
        refresh_agents_catalog(force=True)

    # No names and not --all → print install menu of missing agents
    if not names and not do_all:
        menu = install_menu(config=config, refresh=refresh)
        if ctx.output.machine or getattr(args, "json", False):
            ctx.output.result(menu)
        else:
            _print_installable(menu)
        return menu

    if do_all:
        menu = install_menu(config=config, refresh=False)
        names = list(menu.get("all_auto_ids") or [])
        if not names:
            msg = "No auto-installable missing agents on this machine."
            if ctx.output.machine or getattr(args, "json", False):
                ctx.output.result({"ok": True, "installed": [], "message": msg})
            else:
                print(msg)
                _print_installable(menu)
            return {"ok": True, "installed": [], "message": msg}

    def progress(msg: str) -> None:
        if not ctx.output.machine:
            print(msg, flush=True)

    try:
        # Validate / fuzzy-resolve each name early for clearer errors
        resolved = []
        for n in names:
            try:
                resolved.append(resolve_agent_id(n))
            except ValueError as exc:
                raise InvalidArgumentsError(str(exc)) from exc

        result = install_agents(
            resolved,
            method=getattr(args, "method", None) or "auto",
            yes=bool(getattr(args, "yes", False) or getattr(args, "yes_flag", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            config=config,
            progress=progress,
        )
    except AgentInstallError as exc:
        raise CliError(str(exc), exit_code=EXIT_GENERAL) from exc

    if ctx.output.machine or getattr(args, "json", False):
        ctx.output.result(result)
    else:
        print(f"OK: {result.get('count_ok')}  FAILED: {result.get('count_failed')}")
        for item in result.get("installed") or []:
            print(
                f"  [{item.get('action')}] {item.get('agent_id')} "
                f"-> {item.get('binary_path') or item.get('plan') or ''}"
            )
            setup = item.get("setup") or {}
            if setup.get("note"):
                print(f"    {setup['note']}")
        for err in result.get("errors") or []:
            print(f"  [error] {err.get('name')}: {err.get('error')}")
        # Always show remaining missing after install
        menu = install_menu(config=config)
        remaining = menu.get("installable_missing_ids") or []
        if remaining:
            print()
            print("Still installable / not on this PC:")
            for aid in remaining:
                print(f"  - {aid}")
            print(f"  megas install {' '.join(remaining[:10])}")
    return result


def cmd_agents_setup(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    try:
        result = setup_agent(args.name, config=config, enable=not getattr(args, "no_enable", False))
    except (AgentInstallError, ValueError) as exc:
        raise CliError(str(exc), exit_code=EXIT_GENERAL) from exc
    if ctx.output.machine or getattr(args, "json", False):
        ctx.output.result(result)
    else:
        print(f"Registered {result['agent_id']} -> {result.get('binary_path')}")
        print(f"Status: {result.get('status')}  version={result.get('version') or '—'}")
        if result.get("note"):
            print(result["note"])
        if result.get("auth_hint"):
            print(f"Auth: {result['auth_hint']}")
    return result


def cmd_agents_enable(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    try:
        result = enable_agent(args.name, workspaces=getattr(args, "workspaces", None))
    except (AgentInstallError, ValueError) as exc:
        raise CliError(str(exc), exit_code=EXIT_GENERAL) from exc
    if ctx.output.machine or getattr(args, "json", False):
        ctx.output.result(result)
    else:
        print(f"Enabled agent: {result.get('agent_id')}")
        if result.get("binary_path"):
            print(f"Binary: {result['binary_path']}")
        if result.get("note"):
            print(result["note"])
    return result


def cmd_agents_disable(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    try:
        result = disable_agent(args.name)
    except ValueError as exc:
        raise InvalidArgumentsError(str(exc)) from exc
    if ctx.output.machine or getattr(args, "json", False):
        ctx.output.result(result)
    else:
        print(f"Disabled agent: {result.get('agent_id')}")
    return result


def cmd_agents_refresh(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    data = refresh_agents_catalog(force=True)
    if ctx.output.machine or getattr(args, "json", False):
        ctx.output.result(data)
    else:
        print(f"Agent store: {data.get('store_path')}")
        print(f"Agents known: {data.get('agent_count')}")
        print("IDs:", ", ".join(data.get("agent_ids") or []))
        sources = data.get("last_live_sources") or []
        if sources:
            print("Live sources (sample):")
            for s in sources[:8]:
                print(f"  - {s}")
        else:
            print("Live sources: (offline or no new sources this run — local DB kept)")
    return data


def _wrap(fn):
    def runner(args: argparse.Namespace) -> None:
        ctx = build_context(args)
        fn(ctx, args)

    runner.__name__ = getattr(fn, "__name__", "agents_cmd")
    return runner


def register(sub: argparse._SubParsersAction) -> None:
    agents = sub.add_parser(
        "agents",
        help="Discover, install, and register coding agents (local DB + live web)",
    )
    agents_sub = agents.add_subparsers(dest="agents_command")

    list_p = agents_sub.add_parser("list", help="List catalog agents and local status")
    list_p.add_argument("--certified", action="store_true", help="Only certified Megas adapters")
    list_p.add_argument("--json", action="store_true")
    list_p.set_defaults(func=_wrap(cmd_agents_list))
    agents.set_defaults(func=_wrap(cmd_agents_list))

    disc = agents_sub.add_parser("discover", help="Deep-scan this machine for coding agents")
    disc.add_argument("--json", action="store_true")
    disc.set_defaults(func=_wrap(cmd_agents_discover))

    show = agents_sub.add_parser("show", help="Show one agent")
    show.add_argument("name", help="Agent name or alias (fuzzy OK)")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=_wrap(cmd_agents_show))

    inst = agents_sub.add_parser(
        "install",
        help="Install agents (no args = list missing). Bulk: megas agents install a b c",
    )
    inst.add_argument("names", nargs="*", help="Agent names/aliases (fuzzy; multiple = bulk)")
    inst.add_argument("--all", action="store_true", help="Install all auto-installable missing agents")
    inst.add_argument("--method", default="auto", help="auto|npm|pipx|pip|brew|script")
    inst.add_argument("--yes", "-y", action="store_true", dest="yes_flag", help="Confirm remote scripts")
    inst.add_argument("--dry-run", action="store_true")
    inst.add_argument("--refresh", action="store_true", help="Refresh live catalog first")
    inst.add_argument("--json", action="store_true")
    inst.set_defaults(func=_wrap(cmd_agents_install))

    setup = agents_sub.add_parser("setup", help="Detect installed agent and register with Worker")
    setup.add_argument("name")
    setup.add_argument("--no-enable", action="store_true")
    setup.add_argument("--json", action="store_true")
    setup.set_defaults(func=_wrap(cmd_agents_setup))

    en = agents_sub.add_parser("enable", help="Enable a coding agent for dispatch")
    en.add_argument("name")
    en.add_argument("--workspaces", nargs="+")
    en.add_argument("--json", action="store_true")
    en.set_defaults(func=_wrap(cmd_agents_enable))

    dis = agents_sub.add_parser("disable", help="Disable a coding agent")
    dis.add_argument("name")
    dis.add_argument("--json", action="store_true")
    dis.set_defaults(func=_wrap(cmd_agents_disable))

    ref = agents_sub.add_parser("refresh", help="Fetch live web sources and merge into local agent DB")
    ref.add_argument("--json", action="store_true")
    ref.set_defaults(func=_wrap(cmd_agents_refresh))
