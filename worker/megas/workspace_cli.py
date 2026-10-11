from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import devtool, local_targets


MAX_DISCOVERY_FILE_BYTES = 2 * 1024 * 1024


def _read_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > MAX_DISCOVERY_FILE_BYTES:
        raise ValueError(f"discovery file is too large: {path}")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _package_manager(directory: Path) -> str:
    if (directory / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (directory / "yarn.lock").is_file():
        return "yarn"
    return "npm"


def _script_port(script: str) -> int | None:
    match = re.search(r"(?:--port(?:=|\s+)|\s-p\s+)(\d{2,5})(?:\s|$)", script)
    if not match:
        return None
    port = int(match.group(1))
    return port if 1 <= port <= 65535 else None


def _framework_default_port(package: dict[str, Any], script: str) -> tuple[int | None, str | None]:
    dependencies: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        value = package.get(key)
        if isinstance(value, dict):
            dependencies.update(value)
    lowered = script.lower()
    if "next" in dependencies or re.search(r"(?:^|\s)next(?:\s|$)", lowered):
        return 3000, "next-default"
    if "vite" in dependencies or re.search(r"(?:^|\s)vite(?:\s|$)", lowered):
        return 5173, "vite-default"
    if "react-scripts" in dependencies or "react-scripts start" in lowered:
        return 3000, "react-scripts-default"
    if "nuxt" in dependencies or re.search(r"(?:^|\s)nuxt(?:\s|$)", lowered):
        return 3000, "nuxt-default"
    if "astro" in dependencies or re.search(r"(?:^|\s)astro(?:\s|$)", lowered):
        return 4321, "astro-default"
    return None, None


def _node_service(directory: Path, *, root: Path, name: str) -> dict[str, Any] | None:
    package_path = directory / "package.json"
    if not package_path.is_file():
        return None
    package = _read_json(package_path)
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    script_name = (
        "dev"
        if isinstance(scripts.get("dev"), str)
        else "start"
        if isinstance(scripts.get("start"), str)
        else None
    )
    if script_name is None:
        return None
    script = str(scripts[script_name])
    manager = _package_manager(directory)
    command = [manager, "run", script_name]
    explicit_port = _script_port(script)
    default_port, default_source = _framework_default_port(package, script)
    port = explicit_port or default_port
    port_source = "script" if explicit_port else default_source
    relative = str(directory.resolve().relative_to(root.resolve())) or "."
    open_url = f"http://127.0.0.1:{port}" if port else None
    return {
        "name": name,
        "command": command,
        "cwd": relative,
        "env": {},
        "env_refs": {},
        "depends_on": [],
        "ports": [port] if port else [],
        "health_url": None,
        "open_url": open_url,
        "startup_timeout_seconds": 60,
        "evidence": {
            "kind": "package-script",
            "file": str(package_path.relative_to(root)),
            "script": script_name,
            "value": script,
            "port": port,
            "port_source": port_source,
        },
    }


def _fastapi_entry(directory: Path) -> tuple[str, Path] | None:
    candidates = [directory / "main.py", directory / "app" / "main.py"]
    for candidate in candidates:
        if not candidate.is_file() or candidate.stat().st_size > MAX_DISCOVERY_FILE_BYTES:
            continue
        text = candidate.read_text(encoding="utf-8", errors="replace")
        if "FastAPI(" not in text or re.search(r"\bapp\s*=\s*FastAPI\(", text) is None:
            continue
        module = "main:app" if candidate.parent == directory else "app.main:app"
        return module, candidate
    return None


def _python_command(directory: Path) -> str:
    executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    candidates = [
        directory / ".venv" / executable,
        directory / "venv" / executable,
        directory.parent / ".venv" / executable,
        directory.parent / "venv" / executable,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    if not getattr(sys, "frozen", False):
        return sys.executable
    resolved = shutil.which("python") or shutil.which("python3") or shutil.which("py")
    if resolved:
        return resolved
    raise RuntimeError(
        f"FastAPI project detected at {directory}, but no project venv/.venv or Python executable was found"
    )


def _python_service(directory: Path, *, root: Path, name: str) -> dict[str, Any] | None:
    if not ((directory / "pyproject.toml").is_file() or (directory / "requirements.txt").is_file()):
        return None
    fastapi = _fastapi_entry(directory)
    if fastapi is None:
        return None
    module, evidence_path = fastapi
    relative = str(directory.resolve().relative_to(root.resolve())) or "."
    return {
        "name": name,
        "command": [
            _python_command(directory),
            "-m",
            "uvicorn",
            module,
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        "cwd": relative,
        "env": {},
        "env_refs": {},
        "depends_on": [],
        "ports": [8000],
        "health_url": None,
        "open_url": None,
        "startup_timeout_seconds": 45,
        "evidence": {
            "kind": "fastapi-entrypoint",
            "file": str(evidence_path.relative_to(root)),
            "module": module,
            "port": 8000,
            "port_source": "fastapi-local-default",
        },
    }


def _git_branch(root: Path) -> str:
    if not (root / ".git").exists() or shutil.which("git") is None:
        return "main"
    try:
        completed = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=3,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return "main"
    branch = completed.stdout.strip()
    return branch or "main"


def discover_folder(folder: Path | str, *, name: str | None = None) -> dict[str, Any]:
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"software folder does not exist: {root}")

    detected: list[dict[str, Any]] = []
    layouts = [
        (root / "backend", "backend", "python"),
        (root / "frontend", "frontend", "node"),
        (root, "app", "python"),
        (root, "app", "node"),
    ]
    occupied_names: set[str] = set()
    for directory, service_name, kind in layouts:
        if not directory.is_dir():
            continue
        service = (
            _python_service(directory, root=root, name=service_name)
            if kind == "python"
            else _node_service(directory, root=root, name=service_name)
        )
        if service is None or service_name in occupied_names:
            continue
        detected.append(service)
        occupied_names.add(service_name)

    native_targets = local_targets.detect_project_targets({"root": str(root), "services": []})
    if not detected and not native_targets:
        raise RuntimeError(
            "Megas could not confidently find a runnable target. Supported repository evidence currently includes "
            "package.json dev/start services, FastAPI app entrypoints, Android application modules with a Gradle Wrapper, "
            "and runnable Windows .NET projects. No command was invented."
        )

    names = {row["name"] for row in detected}
    if "frontend" in names and "backend" in names:
        for service in detected:
            if service["name"] == "frontend":
                service["depends_on"] = ["backend"]

    project_name = str(name or root.name).strip()
    if not project_name:
        raise ValueError("project name must be non-empty")

    preferred = next(
        (
            row.get("open_url")
            for row in detected
            if row["name"] == "frontend" and row.get("open_url")
        ),
        None,
    ) or next((row.get("open_url") for row in detected if row.get("open_url")), None)

    services = []
    evidence = []
    for row in detected:
        copied = dict(row)
        evidence.append({"service": copied["name"], **copied.pop("evidence")})
        services.append(copied)

    target_evidence = [
        {
            "id": str(target.get("id") or ""),
            "kind": str(target.get("kind") or ""),
            "label": str(target.get("label") or ""),
            "runtime": str(target.get("runtime") or ""),
            "evidence": str(target.get("evidence") or ""),
        }
        for target in native_targets
    ]

    return {
        "format": devtool.DEV_PROJECT_FORMAT,
        "version": devtool.DEV_PROJECT_VERSION,
        "name": project_name,
        "description": f"Local software workspace discovered from {root}",
        "root": str(root),
        "open_url": preferred,
        "git": {
            "enabled": (root / ".git").exists(),
            "remote": "origin",
            "branch": _git_branch(root),
            "sync_before_start": False,
        },
        "services": services,
        "discovery": {
            "mode": "repo-evidence-plus-safe-local-defaults",
            "evidence": evidence,
            "targets": target_evidence,
        },
    }


def _workspace_manifest_path(project_name: str) -> Path:
    workspace_dir = devtool._dev_dir() / "workspaces"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return workspace_dir / f"{devtool._key(project_name)}.json"


def add_folder(folder: Path | str, *, name: str | None = None, replace: bool = False) -> dict[str, Any]:
    discovered = discover_folder(folder, name=name)
    project_data = {key: value for key, value in discovered.items() if key != "discovery"}
    project = devtool.DevProject.from_dict(project_data)
    manifest_path = _workspace_manifest_path(project.name)
    if manifest_path.exists() and not replace:
        raise FileExistsError(f"workspace already exists: {project.name}")
    devtool._atomic_json(manifest_path, project.to_dict())
    registered = devtool.register_project(manifest_path, replace=replace)
    return {
        "project": project.name,
        "folder": project.root,
        "services": [service.name for service in project.services],
        "targets": list(discovered["discovery"].get("targets") or []),
        "open_url": project.open_url,
        "evidence": discovered["discovery"]["evidence"],
        "registered": registered["registered"],
    }


def _has_open_url(project_name: str) -> bool:
    project, _ = devtool._registered_project(project_name)
    if project.open_url:
        return True
    return any(service.open_url for service in project.services)


def _resolve_executable(token: str, cwd: Path) -> str | None:
    candidate = Path(token).expanduser()
    if candidate.is_absolute():
        return str(candidate) if candidate.is_file() else None
    if any(separator in token for separator in ("/", "\\")):
        local = (cwd / candidate).resolve()
        return str(local) if local.is_file() else None
    return shutil.which(token)


def _node_dependencies_present(cwd: Path) -> tuple[bool, bool]:
    package_path = cwd / "package.json"
    if not package_path.is_file():
        return True, False
    package = _read_json(package_path)
    has_dependencies = any(
        isinstance(package.get(key), dict) and bool(package.get(key))
        for key in ("dependencies", "devDependencies", "optionalDependencies")
    )
    if not has_dependencies:
        return True, False
    return (cwd / "node_modules").is_dir(), True


def _python_module_available(executable: str, cwd: Path, module: str) -> bool:
    try:
        completed = subprocess.run(
            [executable, "-c", f"import {module}"],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def check_project(reference: str, *, service_name: str | None = None) -> dict[str, Any]:
    """Read-only prerequisite diagnosis for one human-added local project."""

    project, manifest_path = devtool._registered_project(reference)
    root = Path(project.root)
    target_services = project.start_order(service_name)
    target_names = {service.name for service in target_services}
    live = devtool.project_status(project.name)
    live_by_name = {row["service"]: row for row in live["services"]}
    issues: list[dict[str, Any]] = []
    services: list[dict[str, Any]] = []

    def issue(service: str, code: str, message: str, *, severity: str = "blocker") -> None:
        issues.append(
            {
                "service": service,
                "code": code,
                "severity": severity,
                "message": message,
            }
        )

    for service in target_services:
        cwd = root if service.cwd in {"", "."} else (root / service.cwd).resolve()
        status = live_by_name.get(service.name) or {}
        executable = _resolve_executable(service.command[0], cwd) if cwd.is_dir() else None
        service_checks: dict[str, Any] = {
            "service": service.name,
            "cwd": str(cwd),
            "cwd_exists": cwd.is_dir(),
            "command": list(service.command),
            "executable": executable,
            "executable_available": executable is not None,
            "missing_env_refs": [],
            "ports": [],
            "process_alive": bool(status.get("process_alive")),
            "ready": bool(status.get("ready")),
        }

        if not cwd.is_dir():
            issue(service.name, "cwd-missing", f"service cwd does not exist: {cwd}")
        if cwd.is_dir() and executable is None:
            issue(
                service.name,
                "executable-missing",
                f"service executable is not available: {service.command[0]}",
            )

        missing_env = sorted(
            source for source in service.env_refs.values() if os.environ.get(source) is None
        )
        service_checks["missing_env_refs"] = missing_env
        for source in missing_env:
            issue(
                service.name,
                "env-ref-missing",
                f"required machine environment variable is not set: {source}",
            )

        for port in service.ports:
            open_now = devtool._port_open(port)
            owned = bool(status.get("process_alive"))
            service_checks["ports"].append(
                {"port": port, "open": open_now, "owned_by_running_service": owned}
            )
            if open_now and not owned:
                issue(
                    service.name,
                    "port-in-use",
                    f"localhost port {port} is already in use while Megas does not own a running {service.name} process",
                )

        if cwd.is_dir() and service.command[0].lower() in {
            "npm",
            "pnpm",
            "yarn",
            "npm.cmd",
            "pnpm.cmd",
            "yarn.cmd",
        }:
            deps_present, dependencies_declared = _node_dependencies_present(cwd)
            service_checks["node_dependencies_declared"] = dependencies_declared
            service_checks["node_modules_present"] = deps_present
            if dependencies_declared and not deps_present:
                issue(
                    service.name,
                    "node-modules-missing",
                    f"{cwd / 'node_modules'} is missing; install this project's declared Node dependencies before starting it",
                )

        if (
            cwd.is_dir()
            and executable is not None
            and len(service.command) >= 3
            and service.command[1:3] == ["-m", "uvicorn"]
        ):
            uvicorn_available = _python_module_available(executable, cwd, "uvicorn")
            service_checks["uvicorn_available"] = uvicorn_available
            if not uvicorn_available:
                issue(
                    service.name,
                    "python-module-missing",
                    f"uvicorn is not importable from the Python runtime used by {service.name}",
                )

        services.append(service_checks)

    blockers = [row for row in issues if row["severity"] == "blocker"]
    return {
        "project": project.name,
        "service": service_name,
        "checked_services": sorted(target_names),
        "manifest": str(manifest_path),
        "root": project.root,
        "runtime_state": live["state"],
        "ready_to_start": not blockers,
        "blocker_count": len(blockers),
        "issues": issues,
        "services": services,
    }


def _require_startable(project: str, service_name: str | None) -> dict[str, Any]:
    report = check_project(project, service_name=service_name)
    if report["ready_to_start"]:
        return report
    first = report["issues"][0]
    target = f" --service {service_name}" if service_name else ""
    raise RuntimeError(
        f"workspace preflight blocked start: {first['message']}. Run 'megas check {project}{target}' for the full report"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="megas",
        description="Megas local software workspace capabilities.",
    )
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="workspace_command", required=True)

    add = sub.add_parser("add", help="add a local software folder using repository evidence")
    add.add_argument("folder")
    add.add_argument("--name")
    add.add_argument("--replace", action="store_true")

    sub.add_parser("projects", help="list added local software projects")

    check = sub.add_parser("check", help="diagnose local project prerequisites without changing anything")
    check.add_argument("project")
    check.add_argument("--service")

    open_cmd = sub.add_parser("open", help="open a project's declared localhost URL")
    open_cmd.add_argument("project")

    for command in ("start", "stop", "restart"):
        action = sub.add_parser(command, help=f"{command} one selected local software project")
        action.add_argument("project")
        action.add_argument("--service")
        if command in {"start", "restart"}:
            action.add_argument("--no-open", action="store_true")

    status = sub.add_parser("status", help="show process and readiness state")
    status.add_argument("project")

    ports = sub.add_parser("ports", help="show declared localhost ports and PIDs")
    ports.add_argument("project", nargs="?")

    logs = sub.add_parser("logs", help="tail bounded service logs")
    logs.add_argument("project")
    logs.add_argument("service")
    logs.add_argument("--lines", type=int, default=120)
    return parser


def run(args: argparse.Namespace) -> Any:
    command = args.workspace_command
    if command == "add":
        return add_folder(args.folder, name=args.name, replace=args.replace)
    if command == "projects":
        return devtool.list_projects()
    if command == "check":
        return check_project(args.project, service_name=args.service)
    if command == "open":
        return devtool.open_project(args.project)
    if command == "start":
        _require_startable(args.project, args.service)
        should_open = not args.no_open and args.service is None and _has_open_url(args.project)
        return devtool.start_project(args.project, service_name=args.service, open_after=should_open)
    if command == "stop":
        return devtool.stop_project(args.project, service_name=args.service)
    if command == "restart":
        _require_startable(args.project, args.service)
        should_open = not args.no_open and args.service is None and _has_open_url(args.project)
        return devtool.restart_project(args.project, service_name=args.service, open_after=should_open)
    if command == "status":
        return devtool.project_status(args.project)
    if command == "ports":
        return devtool.ports_view(args.project)
    if command == "logs":
        return devtool.tail_logs(args.project, args.service, lines=args.lines)
    raise ValueError(f"unknown workspace command: {command}")


def _emit(value: Any, machine: bool) -> None:
    if machine:
        print(json.dumps(value, indent=2, sort_keys=True, default=str))
    elif isinstance(value, list):
        if not value:
            print("(none)")
        for item in value:
            print(json.dumps(item, sort_keys=True, default=str))
    elif isinstance(value, dict):
        for key, item in value.items():
            print(
                f"{key}: {json.dumps(item, sort_keys=True, default=str) if isinstance(item, (dict, list)) else item}"
            )
    else:
        print(value)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        _emit(run(args), args.json)
    except (
        FileExistsError,
        FileNotFoundError,
        KeyError,
        ValueError,
        RuntimeError,
        TimeoutError,
        PermissionError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        if args.json:
            _emit({"error": type(exc).__name__, "message": str(exc)}, True)
        else:
            print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


__all__ = ["add_folder", "build_parser", "check_project", "discover_folder", "main", "run"]
