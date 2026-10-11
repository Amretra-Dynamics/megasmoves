"""Human CLI. MCP cannot change trusted folders."""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .core import TrustStore
from .mcp import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="megas", description="MegasMoves Community read-only local preview")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="Trust an existing local project folder")
    add.add_argument("path")
    sub.add_parser("projects", help="List human-approved project folders")
    check = sub.add_parser("check", help="Inspect trusted project metadata (read-only)")
    check.add_argument("workspace_id")
    remove = sub.add_parser("remove", help="Revoke a previously trusted folder")
    remove.add_argument("workspace_id")
    sub.add_parser("status", help="Show read-only Community status")
    sub.add_parser("serve", help="Start local MCP stdio server")
    args = parser.parse_args(argv)
    if args.command == "serve":
        serve()
        return 0
    store = TrustStore()
    try:
        if args.command == "add":
            value = store.add(args.path)
        elif args.command == "projects":
            value = {"projects": store.list()}
        elif args.command == "check":
            value = store.check(args.workspace_id)
        elif args.command == "remove":
            value = {"removed": store.remove(args.workspace_id)}
        else:
            value = {"version": __version__, "mode": "local_read_only",
                     "trusted_workspaces": len(store.list()), "device_control": False}
        print(json.dumps(value, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, PermissionError) as exc:
        print(f"megas: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
