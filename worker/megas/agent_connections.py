from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .mcp.model import MCPServer
from .mcp.operator_probe import probe_embedded_operator_modern
from .mcp.protocol import StdioMCPClient


CLIENTS = ("codex", "claude", "cursor")
MAX_CLIENT_CONFIG_BYTES = 4 * 1024 * 1024
MAX_PROCESS_OUTPUT_BYTES = 64 * 1024


def _megas_home() -> Path:
    configured = os.environ.get("MEGAS_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".megas"


def _launcher() -> Path:
    configured = os.environ.get("MEGAS_EXECUTABLE", "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
    elif getattr(sys, "frozen", False):
        path = Path(sys.executable).resolve()
    else:
        command = (
            shutil.which("MegasMoves.exe")
            or shutil.which("megasmoves")
            or shutil.which("megas")
        )
        if not command:
            raise RuntimeError(
                "cannot locate the installed MegasMoves executable; set MEGAS_EXECUTABLE explicitly"
            )
        path = Path(command).resolve()
    if not path.is_file():
        raise RuntimeError(f"MegasMoves executable does not exist: {path}")
    return path


def _config_path(client: str) -> Path:
    if client == "codex":
        configured = os.environ.get("CODEX_HOME", "").strip()
        root = Path(configured).expanduser() if configured else Path.home() / ".codex"
        return root / "config.toml"
    if client == "claude":
        return Path.home() / ".claude.json"
    if client == "cursor":
        return Path.home() / ".cursor" / "mcp.json"
    raise ValueError(f"unsupported agent client: {client}")


def _backup_dir(client: str) -> Path:
    if client not in CLIENTS:
        raise ValueError(f"unsupported agent client: {client}")
    return _megas_home() / "agent-config-backups" / client


def _read_optional_config(path: Path) -> bytes | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"agent configuration path is not a regular file: {path}")
    if path.stat().st_size > MAX_CLIENT_CONFIG_BYTES:
        raise ValueError(
            f"agent configuration exceeds {MAX_CLIENT_CONFIG_BYTES // (1024 * 1024)} MiB: {path}"
        )
    with path.open("rb") as handle:
        value = handle.read(MAX_CLIENT_CONFIG_BYTES + 1)
    if len(value) > MAX_CLIENT_CONFIG_BYTES:
        raise ValueError(
            f"agent configuration exceeds {MAX_CLIENT_CONFIG_BYTES // (1024 * 1024)} MiB: {path}"
        )
    return value


def _atomic_bytes(path: Path, value: bytes) -> None:
    if len(value) > MAX_CLIENT_CONFIG_BYTES:
        raise ValueError("agent configuration exceeds the 4 MiB safety limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
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


def _backup(client: str, path: Path) -> tuple[Path | None, bytes | None]:
    original = _read_optional_config(path)
    if original is None:
        return None, None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = _backup_dir(client)
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / f"{path.name}.{stamp}.bak"
    _atomic_bytes(backup, original)
    return backup, original


def _rollback(path: Path, original: bytes | None) -> None:
    if original is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_bytes(path, original)


def list_backups(client: str) -> list[dict[str, Any]]:
    directory = _backup_dir(client)
    if not directory.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.bak"), reverse=True):
        if not path.is_file():
            continue
        rows.append(
            {
                "client": client,
                "path": str(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return rows


def restore_backup(client: str, backup_path: str, *, yes: bool) -> dict[str, Any]:
    if not yes:
        raise PermissionError("restoring agent configuration requires explicit --yes confirmation")
    directory = _backup_dir(client).resolve()
    backup = Path(backup_path).expanduser().resolve()
    try:
        backup.relative_to(directory)
    except ValueError as exc:
        raise PermissionError("backup must come from Megas' private backup directory for this client") from exc
    value = _read_optional_config(backup)
    if value is None:
        raise FileNotFoundError(f"agent backup does not exist: {backup}")
    destination = _config_path(client)
    safety_backup, _original = _backup(client, destination)
    _atomic_bytes(destination, value)
    return {
        "client": client,
        "restored": str(backup),
        "config_path": str(destination),
        "pre_restore_backup": str(safety_backup) if safety_backup else None,
        "restart_client_recommended": True,
    }


def _tail(path: Path) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > MAX_PROCESS_OUTPUT_BYTES:
            handle.seek(size - MAX_PROCESS_OUTPUT_BYTES)
        data = handle.read(MAX_PROCESS_OUTPUT_BYTES)
    return data.decode("utf-8", errors="replace").strip()


def _windows_wrapped_argv(argv: list[str]) -> list[str]:
    if os.name != "nt" or Path(argv[0]).suffix.lower() not in {".cmd", ".bat"}:
        return argv
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    return [comspec, "/d", "/s", "/c", subprocess.list2cmdline(argv)]


def _run_bounded(
    argv: list[str],
    *,
    timeout: float = 20.0,
    check: bool = True,
) -> dict[str, Any]:
    if not argv:
        raise ValueError("command argv must not be empty")
    command = _windows_wrapped_argv(list(argv))
    with tempfile.TemporaryDirectory(prefix="megas-agent-cli-") as directory:
        stdout_path = Path(directory) / "stdout.txt"
        stderr_path = Path(directory) / "stderr.txt"
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                shell=False,
            )
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait(timeout=5)
                raise TimeoutError(
                    f"agent configuration command timed out: {Path(argv[0]).name}"
                ) from exc
        result = {
            "returncode": returncode,
            "stdout": _tail(stdout_path),
            "stderr": _tail(stderr_path),
        }
    if check and returncode != 0:
        detail = result["stderr"] or result["stdout"] or f"exit code {returncode}"
        raise RuntimeError(f"agent configuration command failed: {detail}")
    return result


def _client_executable(client: str) -> str | None:
    candidates = {
        "codex": ("codex", "codex.exe", "codex.cmd"),
        "claude": ("claude", "claude.exe", "claude.cmd"),
        "cursor": ("agent", "agent.exe", "cursor-agent", "cursor-agent.exe"),
    }[client]
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _cursor_document(path: Path) -> dict[str, Any]:
    raw = _read_optional_config(path)
    if raw is None:
        return {"mcpServers": {}}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cursor MCP config is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("Cursor MCP config must contain a JSON object")
    servers = value.get("mcpServers")
    if servers is None:
        value["mcpServers"] = {}
    elif not isinstance(servers, dict):
        raise ValueError("Cursor MCP config mcpServers must be an object")
    return value


def _write_cursor(path: Path, value: dict[str, Any]) -> None:
    encoded = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_bytes(path, encoded)


def _register_cursor(path: Path, launcher: Path) -> None:
    document = _cursor_document(path)
    document["mcpServers"]["megas"] = {
        "type": "stdio",
        "command": str(launcher),
        "args": ["serve"],
    }
    _write_cursor(path, document)


def _remove_cursor(path: Path) -> bool:
    if not path.exists():
        return False
    document = _cursor_document(path)
    changed = document["mcpServers"].pop("megas", None) is not None
    if changed:
        _write_cursor(path, document)
    return changed


def _cursor_configured(path: Path, launcher: Path | None = None) -> bool:
    if not path.exists():
        return False
    try:
        row = _cursor_document(path)["mcpServers"].get("megas")
    except ValueError:
        return False
    if not isinstance(row, dict) or row.get("args") != ["serve"]:
        return False
    command = str(row.get("command") or "").strip()
    if launcher is None:
        return bool(command)
    try:
        return Path(command).expanduser().resolve() == launcher.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _native_commands(
    client: str,
    executable: str,
    launcher: Path,
) -> tuple[list[str], list[str], list[str]]:
    if client == "codex":
        return (
            [executable, "mcp", "remove", "megas"],
            [executable, "mcp", "add", "megas", "--", str(launcher), "serve"],
            [executable, "mcp", "get", "megas"],
        )
    if client == "claude":
        return (
            [executable, "mcp", "remove", "megas", "--scope", "user"],
            [
                executable,
                "mcp",
                "add",
                "--transport",
                "stdio",
                "--scope",
                "user",
                "megas",
                "--",
                str(launcher),
                "serve",
            ],
            [executable, "mcp", "get", "megas"],
        )
    raise ValueError(f"native registration is not defined for {client}")


def generic_configuration(launcher: Path | None = None) -> dict[str, Any]:
    executable = launcher or _launcher()
    return {
        "name": "megas",
        "transport": "stdio",
        "command": str(executable),
        "args": ["serve"],
        "description": "Use the already-installed MegasMoves.exe embedded Megas MCP operator.",
    }


def verify_megas_server(
    launcher: Path | None = None,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    executable = launcher or _launcher()

    current = probe_embedded_operator_modern(executable, timeout=timeout)

    server = MCPServer(
        name="megas-agent-registration-check",
        command=str(executable),
        args=["serve"],
    )
    with StdioMCPClient(server, timeout=timeout) as client:
        # The compatibility client intentionally performs the legacy initialize
        # handshake. The modern stateless path is proven separately above.
        legacy_protocol_version = client.protocol_version
        legacy_server_info = dict(client.server_info)
        tools = client.request("tools/list", {}, timeout=timeout)
        resources = client.request("resources/list", {}, timeout=timeout)

    tool_names = {
        str(row.get("name") or "")
        for row in tools.get("tools", [])
        if isinstance(row, dict)
    }
    resource_uris = {
        str(row.get("uri") or "")
        for row in resources.get("resources", [])
        if isinstance(row, dict)
    }
    required_tools = {"operator_status", "workspace_projects", "workspace_check"}
    required_resources = {"megas://operator/status", "megas://workspace/projects"}
    missing_tools = sorted(required_tools - tool_names)
    missing_resources = sorted(required_resources - resource_uris)
    if missing_tools or missing_resources:
        raise RuntimeError(
            "Megas MCP compatibility verification failed: missing "
            + "; ".join(
                part
                for part in (
                    f"tools {', '.join(missing_tools)}" if missing_tools else "",
                    f"resources {', '.join(missing_resources)}" if missing_resources else "",
                )
                if part
            )
        )
    return {
        "current_protocol_verified": True,
        "current": current,
        "legacy_protocol_verified": bool(legacy_protocol_version),
        "legacy": {
            "protocol_version": legacy_protocol_version,
            "server_info": legacy_server_info,
            "tool_count": len(tool_names),
            "resource_count": len(resource_uris),
            "required_tools_present": True,
            "required_resources_present": True,
        },
        "transport": "stdio",
        "command": str(executable),
        "args": ["serve"],
    }


def client_status(client: str) -> dict[str, Any]:
    if client not in CLIENTS:
        raise ValueError(f"unsupported agent client: {client}")
    executable = _client_executable(client)
    path = _config_path(client)
    launcher: Path | None
    try:
        launcher = _launcher()
    except (RuntimeError, OSError):
        launcher = None

    detected = executable is not None
    configured: bool | None
    exact_launcher: bool | None = None
    if client == "cursor":
        detected = detected or path.exists() or path.parent.exists()
        configured = _cursor_configured(path)
        exact_launcher = _cursor_configured(path, launcher) if launcher is not None else None
    elif executable:
        try:
            result = _run_bounded(
                [executable, "mcp", "get", "megas"],
                timeout=8,
                check=False,
            )
            configured = result["returncode"] == 0
        except (OSError, TimeoutError):
            configured = None
    else:
        configured = False

    if configured is True and exact_launcher is not False:
        state = "configured"
    elif configured is True and exact_launcher is False:
        state = "repair_needed"
    elif detected:
        state = "available"
    else:
        state = "not_detected"
    return {
        "client": client,
        "detected": detected,
        "client_executable": executable,
        "config_path": str(path),
        "megas_configured": configured,
        "exact_megasmoves_launcher": exact_launcher,
        "state": state,
    }


def all_status() -> list[dict[str, Any]]:
    return [client_status(client) for client in CLIENTS]


def connect(client: str, *, yes: bool, verify: bool = True) -> dict[str, Any]:
    if client not in CLIENTS:
        raise ValueError(f"unsupported agent client: {client}")
    if not yes:
        raise PermissionError("agent configuration changes require explicit --yes confirmation")
    launcher = _launcher()
    path = _config_path(client)
    backup, original = _backup(client, path)
    executable = _client_executable(client)

    try:
        if client == "cursor":
            _register_cursor(path, launcher)
        else:
            if executable is None:
                raise FileNotFoundError(f"{client} CLI was not detected on PATH")
            remove, add, get = _native_commands(client, executable, launcher)
            _run_bounded(remove, timeout=12, check=False)
            _run_bounded(add, timeout=20, check=True)
            _run_bounded(get, timeout=20, check=True)
        verification = verify_megas_server(launcher) if verify else None
    except Exception:
        _rollback(path, original)
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
    }


def disconnect(client: str, *, yes: bool) -> dict[str, Any]:
    if client not in CLIENTS:
        raise ValueError(f"unsupported agent client: {client}")
    if not yes:
        raise PermissionError("agent configuration changes require explicit --yes confirmation")
    path = _config_path(client)
    backup, original = _backup(client, path)
    executable = _client_executable(client)
    try:
        if client == "cursor":
            changed = _remove_cursor(path)
        else:
            if executable is None:
                raise FileNotFoundError(f"{client} CLI was not detected on PATH")
            command = (
                [executable, "mcp", "remove", "megas"]
                if client == "codex"
                else [executable, "mcp", "remove", "megas", "--scope", "user"]
            )
            result = _run_bounded(command, timeout=20, check=False)
            changed = result["returncode"] == 0
            if not changed and original is not None:
                detail = result["stderr"] or result["stdout"] or "native client rejected removal"
                raise RuntimeError(f"failed to disconnect Megas from {client}: {detail}")
    except Exception:
        _rollback(path, original)
        raise
    return {
        "client": client,
        "connected": False,
        "changed": changed,
        "config_path": str(path),
        "backup": str(backup) if backup else None,
        "restart_client_recommended": changed,
    }


def repair(client: str, *, yes: bool) -> dict[str, Any]:
    if not yes:
        raise PermissionError("repairing agent configuration requires explicit --yes confirmation")
    before = client_status(client)
    connected = connect(client, yes=True, verify=True)
    after = client_status(client)
    return {
        "client": client,
        "repaired": True,
        "before": before,
        "after": after,
        "connection": connected,
    }


def connect_detected(*, yes: bool) -> dict[str, Any]:
    if not yes:
        raise PermissionError("connecting detected agents requires explicit --yes confirmation")
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for status in all_status():
        if not status.get("detected"):
            continue
        client = str(status["client"])
        try:
            results.append(connect(client, yes=True, verify=True))
        except (FileNotFoundError, ValueError, RuntimeError, TimeoutError, PermissionError, OSError) as exc:
            errors.append({"client": client, "error": type(exc).__name__, "message": str(exc)})
    return {
        "connected": results,
        "errors": errors,
        "ok": bool(results) and not errors,
    }


def doctor(*, timeout: float = 20.0) -> dict[str, Any]:
    launcher = _launcher()
    verification = verify_megas_server(launcher, timeout=timeout)
    statuses = all_status()
    return {
        "ok": True,
        "launcher": str(launcher),
        "generic_mcp": generic_configuration(launcher),
        "verification": verification,
        "clients": statuses,
        "configured_clients": [row["client"] for row in statuses if row.get("megas_configured") is True],
        "repair_needed": [row["client"] for row in statuses if row.get("state") == "repair_needed"],
    }


def manual_configuration(client: str) -> dict[str, Any]:
    if client not in CLIENTS:
        raise ValueError(f"unsupported agent client: {client}")
    launcher = _launcher()
    if client == "codex":
        return {
            "client": client,
            "command": f'codex mcp add megas -- "{launcher}" serve',
            "verify": "codex mcp get megas",
            "remove": "codex mcp remove megas",
        }
    if client == "claude":
        return {
            "client": client,
            "command": f'claude mcp add --transport stdio --scope user megas -- "{launcher}" serve',
            "verify": "claude mcp get megas",
            "remove": "claude mcp remove megas --scope user",
        }
    return {
        "client": client,
        "config_path": str(_config_path(client)),
        "mcpServers": {
            "megas": {
                "type": "stdio",
                "command": str(launcher),
                "args": ["serve"],
            }
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="megas agents",
        description="Detect, connect, verify and recover local MCP-capable coding-agent configuration.",
    )
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="agent_command", required=True)
    sub.add_parser("status")
    verify = sub.add_parser("verify")
    verify.add_argument("--timeout", type=float, default=20.0)
    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--timeout", type=float, default=20.0)
    sub.add_parser("descriptor")
    for action in ("connect", "disconnect", "repair"):
        command = sub.add_parser(action)
        command.add_argument("client", choices=CLIENTS)
        command.add_argument(
            "--yes",
            action="store_true",
            help="confirm the local client configuration change",
        )
        if action == "connect":
            command.add_argument("--no-verify", action="store_true")
    connect_all = sub.add_parser("connect-detected")
    connect_all.add_argument("--yes", action="store_true")
    manual = sub.add_parser("manual")
    manual.add_argument("client", choices=CLIENTS)
    backups = sub.add_parser("backups")
    backups.add_argument("client", choices=CLIENTS)
    restore = sub.add_parser("restore")
    restore.add_argument("client", choices=CLIENTS)
    restore.add_argument("backup")
    restore.add_argument("--yes", action="store_true")
    return parser


def run(args: argparse.Namespace) -> Any:
    if args.agent_command == "status":
        return all_status()
    if args.agent_command == "verify":
        return verify_megas_server(timeout=args.timeout)
    if args.agent_command == "doctor":
        return doctor(timeout=args.timeout)
    if args.agent_command == "descriptor":
        return generic_configuration()
    if args.agent_command == "connect":
        return connect(args.client, yes=args.yes, verify=not args.no_verify)
    if args.agent_command == "disconnect":
        return disconnect(args.client, yes=args.yes)
    if args.agent_command == "repair":
        return repair(args.client, yes=args.yes)
    if args.agent_command == "connect-detected":
        return connect_detected(yes=args.yes)
    if args.agent_command == "manual":
        return manual_configuration(args.client)
    if args.agent_command == "backups":
        return list_backups(args.client)
    if args.agent_command == "restore":
        return restore_backup(args.client, args.backup, yes=args.yes)
    raise ValueError(f"unknown agents command: {args.agent_command}")


def _emit(value: Any, machine: bool) -> None:
    if machine:
        print(json.dumps(value, indent=2, sort_keys=True, default=str))
    elif isinstance(value, list):
        if not value:
            print("(none)")
        for row in value:
            print(json.dumps(row, sort_keys=True, default=str))
    elif isinstance(value, dict):
        for key, item in value.items():
            print(
                f"{key}: {json.dumps(item, sort_keys=True) if isinstance(item, (dict, list)) else item}"
            )
    else:
        print(value)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        _emit(run(args), args.json)
    except (
        FileNotFoundError,
        ValueError,
        RuntimeError,
        TimeoutError,
        PermissionError,
        OSError,
    ) as exc:
        if args.json:
            _emit({"error": type(exc).__name__, "message": str(exc)}, True)
        else:
            print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


__all__ = [
    "CLIENTS",
    "all_status",
    "build_parser",
    "client_status",
    "connect",
    "connect_detected",
    "disconnect",
    "doctor",
    "generic_configuration",
    "list_backups",
    "main",
    "manual_configuration",
    "repair",
    "restore_backup",
    "run",
    "verify_megas_server",
]
