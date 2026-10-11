"""MegasMoves CLI entry point: both megas and megasmoves launch this interface."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .actions import MegasActions
from .browser import browser
from .core import TrustStore
from .mcp import serve


def _native_gui() -> int:
    """Launch the existing separately installed native GUI, never a replacement."""
    if os.name != "nt":
        print("Native MegasMoves GUI is currently Windows-only; CLI and MCP work here.")
        return 2
    options = [
        os.environ.get("MEGASMOVES_GUI", ""),
        shutil.which("MegasMoves.exe") or "",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "MegasMoves" / "MegasMoves.exe"),
        str(Path(os.environ.get("PROGRAMFILES", "")) / "MegasMoves" / "MegasMoves.exe"),
    ]
    for item in options:
        if item and Path(item).is_file():
            subprocess.Popen([str(Path(item).resolve())], close_fds=True)
            print("Started MegasMoves native desktop.")
            return 0
    print("The native MegasMoves.exe is not installed. Install its verified desktop build first.")
    print("You can still run 'megas connect' to serve MCP from this Python package.")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="megas", description="MegasMoves SI-agent tools")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("connect", help="Activate MegasMCP over local stdio for the calling client")
    sub.add_parser("serve", help="Alias for connect, for MCP client registrations")
    sub.add_parser("gui", help="Start installed MegasMoves native desktop")
    sub.add_parser("status", help="Report local Megas capabilities")

    group = sub.add_parser("actions", help="Original Megas local actions, no Chitraagni required")
    act = group.add_subparsers(dest="action")
    act.add_parser("list")
    get = act.add_parser("get")
    get.add_argument("action_id")
    create = act.add_parser("create")
    create.add_argument("name")
    create.add_argument("--description", default="")
    create.add_argument("--path", action="append", default=[])
    update = act.add_parser("update")
    update.add_argument("action_id")
    update.add_argument("expected_revision", type=int)
    update.add_argument("name")
    update.add_argument("--description", default="")
    update.add_argument("--path", action="append", default=[])
    task = act.add_parser("task-create")
    task.add_argument("action_id")
    task.add_argument("title")
    move = act.add_parser("task-move")
    move.add_argument("action_id")
    move.add_argument("task_id")

    add = sub.add_parser("add", help="Trust a local workspace")
    add.add_argument("path")
    sub.add_parser("projects", help="List explicitly trusted workspace roots")
    check = sub.add_parser("check", help="Inspect a trusted local workspace")
    check.add_argument("workspace_id")
    rm = sub.add_parser("remove", help="Remove trust for a workspace")
    rm.add_argument("workspace_id")
    b = sub.add_parser("browser", help="Browser capability is enabled by default")
    browser_commands = b.add_subparsers(dest="browser_command")
    browser_commands.add_parser("status")

    args = parser.parse_args(argv)
    if args.command in ("connect", "serve"):
        serve()
        return 0
    if args.command in (None, "gui"):
        return _native_gui()
    try:
        if args.command == "actions":
            if args.action is None:
                group.print_help()
                return 0
            params = {}
            operation = args.action.replace("-", "_")
            if operation == "get":
                params = {"action_id": args.action_id}
            elif operation == "create":
                params = {"name": args.name, "description": args.description, "paths": args.path}
            elif operation == "update":
                params = {"action_id": args.action_id, "expected_revision": args.expected_revision,
                          "name": args.name, "description": args.description, "paths": args.path}
            elif operation == "task_create":
                params = {"action_id": args.action_id, "title": args.title}
            elif operation == "task_move":
                params = {"action_id": args.action_id, "task_id": args.task_id}
            value = MegasActions().call("megas_action_" + operation, params)["structuredContent"]
        elif args.command == "browser":
            value = browser.status()
        else:
            store = TrustStore()
            if args.command == "add":
                value = store.add(args.path)
            elif args.command == "projects":
                value = {"projects": store.list()}
            elif args.command == "check":
                value = store.check(args.workspace_id)
            elif args.command == "remove":
                value = {"removed": store.remove(args.workspace_id)}
            else:
                value = {"version": __version__, "mcp_ready": True, "mcp_command": "megas connect",
                         "browser": browser.status(), "megas_actions": True, "chitraagni_required": False}
        print(json.dumps(value, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"megas: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
