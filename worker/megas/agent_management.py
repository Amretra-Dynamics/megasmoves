from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
from typing import Any

from . import agent_connections
from .agent_installer import (
    AGENT_CATALOG,
    install as install_managed,
    managed_executable,
    managed_status,
    uninstall as uninstall_managed,
)
from .agent_tasks import AgentTaskStore


def _product_launcher() -> Path:
    configured = os.environ.get("MEGAS_EXECUTABLE", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())

    current = Path(sys.executable).resolve()
    if current.name.lower() == "megasmoves.exe":
        candidates.append(current)
    if current.name.lower() == "megasmoves.core.exe":
        candidates.extend(
            [
                current.parent.parent / "MegasMoves.exe",
                current.parent / "MegasMoves.exe",
            ]
        )

    discovered = shutil.which("MegasMoves.exe") or shutil.which("megasmoves")
    if discovered:
        candidates.append(Path(discovered))

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.is_file() and resolved.name.lower() != "megasmoves.core.exe":
            return resolved
    raise RuntimeError(
        "cannot locate the installed MegasMoves.exe consumer launcher; the private Core executable is never registered directly"
    )


def _connect_with_managed_executable(client: str, executable: Path, *, verify: bool) -> dict[str, Any]:
    """Register a Megas-managed client with the embedded operator.

    The executable and every argv token are selected by reviewed Megas code,
    never supplied by a model or arbitrary desktop request.
    """
    launcher = _product_launcher()
    path = agent_connections._config_path(client)
    backup, original = agent_connections._backup(client, path)
    try:
        if client == "cursor":
            agent_connections._register_cursor(path, launcher)
        else:
            remove, add, get = agent_connections._native_commands(client, str(executable), launcher)
            agent_connections._run_bounded(remove, timeout=12, check=False)
            agent_connections._run_bounded(add, timeout=20, check=True)
            agent_connections._run_bounded(get, timeout=20, check=True)
        verification = agent_connections.verify_megas_server(launcher) if verify else None
    except Exception:
        agent_connections._rollback(path, original)
        raise
    return {
        "client": client,
        "connected": True,
        "server": "megas",
        "transport": "stdio",
        "command": str(launcher),
        "args": ["serve"],
        "config_path": str(path),
        "backup": str(backup) if backup else None,
        "mcp_verification": verification,
        "restart_client_recommended": True,
        "managed_executable": str(executable),
    }


def _disconnect_with_managed_executable(client: str, executable: Path, *, confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise PermissionError("disconnecting an agent requires explicit human confirmation")
    path = agent_connections._config_path(client)
    backup, original = agent_connections._backup(client, path)
    try:
        if client == "cursor":
            changed = agent_connections._remove_cursor(path)
        else:
            if client == "codex":
                command = [str(executable), "mcp", "remove", "megas"]
            elif client == "claude":
                command = [str(executable), "mcp", "remove", "megas", "--scope", "user"]
            else:
                raise ValueError("native managed disconnect is not defined for this client")
            result = agent_connections._run_bounded(command, timeout=20, check=False)
            changed = result["returncode"] == 0
            if not changed and original is not None:
                detail = result["stderr"] or result["stdout"] or "native client rejected removal"
                raise RuntimeError(f"failed to disconnect Megas from {client}: {detail}")
    except Exception:
        agent_connections._rollback(path, original)
        raise
    return {
        "client": client,
        "connected": False,
        "changed": changed,
        "config_path": str(path),
        "backup": str(backup) if backup else None,
        "restart_client_recommended": changed,
        "managed_executable": str(executable),
    }


def status() -> list[dict[str, Any]]:
    rows = {str(row.get("client") or ""): dict(row) for row in agent_connections.all_status()}
    for client, catalog in AGENT_CATALOG.items():
        managed = managed_status(client)
        row = rows.setdefault(
            client,
            {
                "client": client,
                "detected": False,
                "client_executable": None,
                "config_path": None,
                "megas_configured": False,
                "exact_megasmoves_launcher": None,
                "state": "not_detected",
            },
        )
        row["downloadable"] = True
        row["catalog"] = {
            "display_name": catalog["display_name"],
            "repository": catalog["repository"],
            "homepage": catalog["homepage"],
            "platform": catalog["platform"],
        }
        row["managed_install"] = managed
        if managed.get("installed"):
            row["detected"] = True
            row["client_executable"] = managed.get("executable")
            if not row.get("megas_configured"):
                row["state"] = "available"
    for row in rows.values():
        if "downloadable" not in row:
            row["downloadable"] = False
            row["managed_install"] = None
    return [rows[key] for key in sorted(rows)]


def install(client: str, *, confirm: bool, connect: bool = True) -> dict[str, Any]:
    result = install_managed(client, confirm=confirm)
    connection = None
    if connect:
        executable = managed_executable(client)
        if executable is None:
            raise RuntimeError("managed agent install completed without a verified executable")
        connection = _connect_with_managed_executable(client, executable, verify=True)
    return {
        "install": result,
        "connection": connection,
        "status": next(row for row in status() if row.get("client") == client),
    }


def connect(client: str, *, confirm: bool, verify: bool = True) -> dict[str, Any]:
    if not confirm:
        raise PermissionError("connecting an agent requires explicit human confirmation")
    executable = managed_executable(client) if client in AGENT_CATALOG else None
    if executable is not None:
        return _connect_with_managed_executable(client, executable, verify=verify)
    return agent_connections.connect(client, yes=True, verify=verify)


def disconnect(client: str, *, confirm: bool) -> dict[str, Any]:
    executable = managed_executable(client) if client in AGENT_CATALOG else None
    if executable is not None:
        return _disconnect_with_managed_executable(client, executable, confirm=confirm)
    return agent_connections.disconnect(client, yes=confirm)


def uninstall(client: str, *, confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise PermissionError("uninstalling an agent requires explicit human confirmation")
    disconnected: dict[str, Any] | None = None
    current = next((row for row in status() if row.get("client") == client), None)
    if current and current.get("megas_configured"):
        disconnected = disconnect(client, confirm=True)
    removed = uninstall_managed(client, confirm=True)
    return {"disconnect": disconnected, "uninstall": removed}


def create_task(title: str, *, project: str | None = None) -> dict[str, Any]:
    return AgentTaskStore().create(title, project=project)


def list_tasks(*, include_terminal: bool = True) -> list[dict[str, Any]]:
    return AgentTaskStore().list(include_terminal=include_terminal)


def assign_task(task_id: str, client: str) -> dict[str, Any]:
    selected = str(client or "").strip().lower()
    row = next((item for item in status() if item.get("client") == selected), None)
    if row is None or not row.get("detected"):
        raise RuntimeError("agent must be installed or detected before assignment")
    return AgentTaskStore().assign(task_id, selected)


def unassign_task(task_id: str) -> dict[str, Any]:
    return AgentTaskStore().unassign(task_id)


def download_and_assign(
    client: str,
    *,
    task_id: str | None = None,
    title: str | None = None,
    project: str | None = None,
    confirm: bool,
) -> dict[str, Any]:
    if not confirm:
        raise PermissionError("download and assignment require explicit human confirmation")
    selected = str(client or "").strip().lower()
    if selected not in AGENT_CATALOG:
        raise ValueError("download-and-assign is available only for reviewed managed agents")
    current = managed_status(selected)
    if not current.get("installed"):
        installation = install(selected, confirm=True, connect=True)
    else:
        connection = connect(selected, confirm=True, verify=True)
        installation = {"install": current, "connection": connection}

    store = AgentTaskStore()
    if task_id:
        task = store.assign(str(task_id), selected)
    else:
        task = store.create(str(title or ""), project=project)
        task = store.assign(str(task["id"]), selected)
    return {
        "client": selected,
        "installation": installation,
        "task": task,
        "task_survives_agent_disconnect": True,
    }


__all__ = [
    "assign_task",
    "connect",
    "create_task",
    "disconnect",
    "download_and_assign",
    "install",
    "list_tasks",
    "status",
    "unassign_task",
    "uninstall",
]
