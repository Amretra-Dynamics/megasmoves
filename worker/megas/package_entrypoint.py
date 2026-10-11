"""Primary console entrypoint for the MegasMoves product."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from megas.actions import MegasActions


def _desktop() -> int:
    """Open the official native application already installed on this computer."""
    if sys.platform == "win32":
        options = [
            os.environ.get("MEGASMOVES_GUI", ""),
            shutil.which("MegasMoves.exe") or "",
            str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "MegasMoves" / "MegasMoves.exe"),
            str(Path(os.environ.get("PROGRAMFILES", "")) / "MegasMoves" / "MegasMoves.exe"),
        ]
        for path in options:
            if path and Path(path).is_file():
                subprocess.Popen([str(Path(path).resolve())], close_fds=True)
                return 0
    elif sys.platform == "darwin":
        if shutil.which("open") and subprocess.run(
            ["open", "-Ra", "MegasMoves"], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode == 0:
            subprocess.Popen(["open", "-a", "MegasMoves"], close_fds=True)
            return 0
    else:
        binary = shutil.which("megasmoves_gtk")
        if binary:
            subprocess.Popen([binary], close_fds=True)
            return 0
    print("MegasMoves native application is not installed on this system.", file=sys.stderr)
    return 2


def _action_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="megas actions", description="Manage Megas actions")
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("list")
    get = sub.add_parser("get")
    get.add_argument("action_id")
    create = sub.add_parser("create")
    create.add_argument("name")
    create.add_argument("--description", default="")
    create.add_argument("--path", action="append", default=[])
    edit = sub.add_parser("update")
    edit.add_argument("action_id")
    edit.add_argument("expected_revision", type=int)
    edit.add_argument("name")
    edit.add_argument("--description", default="")
    edit.add_argument("--path", action="append", default=[])
    create_task = sub.add_parser("task-create")
    create_task.add_argument("action_id")
    create_task.add_argument("title")
    move = sub.add_parser("task-move")
    move.add_argument("action_id")
    move.add_argument("task_id")
    args = parser.parse_args(argv)
    params: dict[str, object] = {}
    name = args.operation.replace("-", "_")
    if name in {"get", "update", "task_create", "task_move"}:
        params["action_id"] = args.action_id
    if name in {"create", "update"}:
        params.update(name=args.name, description=args.description,
                      paths=[str(Path(path).expanduser().resolve(strict=True)) for path in args.path])
    if name == "update":
        params["expected_revision"] = args.expected_revision
    if name == "task_create":
        params["title"] = args.title
    if name == "task_move":
        params["task_id"] = args.task_id
    result = MegasActions().call("megas_action_" + name, params)
    print(json.dumps(result["structuredContent"], indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return _desktop()
    if argv in (["--help"], ["-h"], ["help"]):
        print("MegasMoves | your always-on SI agent for all your devices")
        print("Usage: megas [connect | gui | actions | browser | status | workspace | --version]")
        print("  connect             Serve native MegasMCP over stdio")
        print("  gui                 Open installed native MegasMoves application")
        print("  actions ...         Manage local Megas actions")
        print("  browser status      Inspect dedicated browser capability")
        print("  workspace ...       Use the original local workspace CLI")
        return 0
    if argv == ["--version"]:
        from importlib.metadata import version
        print(version("megasmoves"))
        return 0
    if argv == ["connect"]:
        from megas.mcp.standalone_operator import serve
        serve()
        return 0
    if argv == ["gui"]:
        return _desktop()
    if argv[0] == "actions":
        return _action_cli(argv[1:])
    if argv == ["browser", "status"] or argv == ["status"]:
        from megas.mcp.browser_operator import BrowserOperatorCatalog
        catalog = BrowserOperatorCatalog()
        try:
            print(json.dumps({"product": "MegasMoves", "browser": catalog.status()}, indent=2, sort_keys=True))
        finally:
            catalog.host.shutdown()
        return 0
    if argv[0] == "workspace":
        from megas.workspace_cli import main as workspace_main
        output = workspace_main(argv[1:])
        return output if isinstance(output, int) else 0
    print("Unknown command. Run 'megas --help'.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
