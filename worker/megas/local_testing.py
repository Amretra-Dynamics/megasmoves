from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from . import devtool, local_targets, workspace_cli


ADB_TIMEOUT_SECONDS = 10.0
ADB_WIRELESS_TIMEOUT_SECONDS = 15.0
MAX_ADB_OUTPUT_BYTES = 64 * 1024
MAX_LOG_LINES = 500
MAX_LOCAL_SETTINGS_BYTES = 64 * 1024
LOCAL_TESTING_SETTINGS_VERSION = 1
WORKSPACE_ACTIONS = frozenset({"check", "start", "stop", "restart", "open", "logs"})

_OWNED_RUN_LOCK = threading.RLock()
_OWNED_WINDOWS_HANDLES: dict[str, int] = {}
_OWNED_ANDROID_RUNS: dict[str, tuple[str, str]] = {}


def _local_testing_settings_path() -> Path:
    return devtool._dev_dir() / "local-testing-settings.json"


def local_testing_settings() -> dict[str, Any]:
    """Read local-testing preferences with conservative defaults.

    Multiple concurrent local runs are intentionally opt-in to keep CPU/RAM/device
    load low on normal developer machines.
    """

    defaults = {
        "version": LOCAL_TESTING_SETTINGS_VERSION,
        "allow_multiple_local_runs": False,
    }
    path = _local_testing_settings_path()
    if not path.is_file():
        return defaults
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_LOCAL_SETTINGS_BYTES:
            return defaults
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return defaults
    if not isinstance(value, dict):
        return defaults
    return {
        "version": LOCAL_TESTING_SETTINGS_VERSION,
        "allow_multiple_local_runs": bool(value.get("allow_multiple_local_runs", False)),
    }


def set_allow_multiple_local_runs(enabled: bool) -> dict[str, Any]:
    value = {
        "version": LOCAL_TESTING_SETTINGS_VERSION,
        "allow_multiple_local_runs": bool(enabled),
    }
    devtool._atomic_json(_local_testing_settings_path(), value)
    return value


def _adb_candidates() -> list[Path | str]:
    candidates: list[Path | str] = []
    resolved = shutil.which("adb")
    if resolved:
        candidates.append(resolved)

    for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env_name)
        if root:
            candidates.append(Path(root) / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb"))

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Android" / "Sdk" / "platform-tools" / "adb.exe")

    unique: list[Path | str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def find_adb() -> str | None:
    """Resolve ADB from PATH or standard Android SDK locations."""

    for candidate in _adb_candidates():
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path.resolve())
        if isinstance(candidate, str) and shutil.which(candidate):
            return str(shutil.which(candidate))
    return None


def _bounded_text(raw: bytes) -> str:
    return raw[:MAX_ADB_OUTPUT_BYTES].decode("utf-8", errors="replace")


def _adb_transport(serial: str, metadata: dict[str, str]) -> str:
    lowered = serial.lower()
    if "._adb-tls-connect._tcp" in lowered or lowered.startswith("adb-"):
        return "wireless"
    if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}:\d+$", serial):
        return "wireless"
    if serial.startswith("[") and "]:" in serial:
        return "wireless"
    if metadata.get("usb") or ":" not in serial:
        return "usb"
    return "unknown"


def _parse_adb_devices(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("List of devices attached") or stripped.startswith("*"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        metadata: dict[str, str] = {}
        for token in parts[2:]:
            if ":" not in token:
                continue
            key, value = token.split(":", 1)
            if key and value:
                metadata[key] = value
        rows.append(
            {
                "serial": serial,
                "state": state,
                "ready": state == "device",
                "transport": _adb_transport(serial, metadata),
                "product": metadata.get("product"),
                "model": metadata.get("model"),
                "device": metadata.get("device"),
                "transport_id": metadata.get("transport_id"),
            }
        )
    return rows


def adb_snapshot() -> dict[str, Any]:
    """Return a bounded, read-only snapshot of locally attached ADB targets."""

    executable = find_adb()
    if executable is None:
        return {
            "available": False,
            "executable": None,
            "devices": [],
            "ready_count": 0,
            "status": "adb-not-found",
        }

    try:
        completed = subprocess.run(
            [executable, "devices", "-l"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=ADB_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "available": True,
            "executable": executable,
            "devices": [],
            "ready_count": 0,
            "status": "adb-timeout",
        }
    except OSError as exc:
        return {
            "available": True,
            "executable": executable,
            "devices": [],
            "ready_count": 0,
            "status": "adb-error",
            "error_type": type(exc).__name__,
        }

    stdout = _bounded_text(completed.stdout or b"")
    stderr = _bounded_text(completed.stderr or b"")
    devices = _parse_adb_devices(stdout)
    if completed.returncode != 0:
        status = "adb-error"
    elif any(row["state"] == "unauthorized" for row in devices):
        status = "device-unauthorized"
    elif any(row["state"] == "offline" for row in devices):
        status = "device-offline"
    elif any(row["ready"] for row in devices):
        status = "ready"
    elif devices:
        status = "device-unavailable"
    else:
        status = "no-devices"

    return {
        "available": True,
        "executable": executable,
        "devices": devices,
        "ready_count": sum(bool(row["ready"]) for row in devices),
        "status": status,
        "returncode": completed.returncode,
        "stderr": stderr[:2048] if completed.returncode != 0 else "",
    }


_TAILSCALE_CGNAT_IPV4 = ipaddress.ip_network("100.64.0.0/10")
_ALLOWED_LOCAL_HOST_SUFFIXES = (".ts.net", ".tailscale.net", ".local", ".lan")
_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$")


def _wireless_endpoint(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 96:
        raise ValueError("wireless ADB address must be IP:port")
    if raw.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\]:(\d{1,5})", raw)
        if not match:
            raise ValueError("wireless ADB address must be IP:port")
        host, port_text = match.groups()
    else:
        host, sep, port_text = raw.rpartition(":")
        if not sep or not host:
            raise ValueError("wireless ADB address must be IP:port")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("wireless ADB port must be between 1 and 65535")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Check for permitted local/tailnet hostnames
        host_lower = host.lower()
        if (
            _HOSTNAME_RE.fullmatch(host)
            and (
                host_lower == "localhost"
                or any(host_lower.endswith(sfx) for sfx in _ALLOWED_LOCAL_HOST_SUFFIXES)
            )
        ):
            return f"{host}:{port}"
        raise ValueError("wireless ADB host must be on a private/local network")

    is_local_network = (
        address.is_private
        or address.is_link_local
        or address.is_loopback
        or (address.version == 4 and address in _TAILSCALE_CGNAT_IPV4)
    )
    if not is_local_network:
        raise ValueError("wireless ADB host must be on a private/local network")
    rendered_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"{rendered_host}:{port}"


def adb_pair_wireless(endpoint: str, pairing_code: str) -> dict[str, Any]:
    """Human-only bounded pairing flow for Android 11+ wireless debugging."""

    executable = find_adb()
    if executable is None:
        raise FileNotFoundError("ADB is not available")
    selected_endpoint = _wireless_endpoint(endpoint)
    code = str(pairing_code or "").strip()
    if re.fullmatch(r"\d{6}", code) is None:
        raise ValueError("wireless ADB pairing code must be six digits")
    completed = subprocess.run(
        [executable, "pair", selected_endpoint],
        input=(code + "\n").encode("ascii"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        timeout=ADB_WIRELESS_TIMEOUT_SECONDS,
        check=False,
    )
    return {
        "ok": completed.returncode == 0,
        "stage": "pair",
        "endpoint": selected_endpoint,
        "returncode": int(completed.returncode),
        "stdout": _bounded_text(completed.stdout or b""),
        "stderr": _bounded_text(completed.stderr or b""),
    }


def adb_connect_wireless(endpoint: str) -> dict[str, Any]:
    """Human-only bounded connect flow to an already-paired local device."""

    executable = find_adb()
    if executable is None:
        raise FileNotFoundError("ADB is not available")
    selected_endpoint = _wireless_endpoint(endpoint)
    completed = subprocess.run(
        [executable, "connect", selected_endpoint],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        timeout=ADB_WIRELESS_TIMEOUT_SECONDS,
        check=False,
    )
    return {
        "ok": completed.returncode == 0,
        "stage": "connect",
        "endpoint": selected_endpoint,
        "returncode": int(completed.returncode),
        "stdout": _bounded_text(completed.stdout or b""),
        "stderr": _bounded_text(completed.stderr or b""),
    }


def _project_row(status: dict[str, Any]) -> dict[str, Any]:
    row = {
        "project": status["project"],
        "state": status["state"],
        "root": status["root"],
        "services": status["services"],
    }
    try:
        row["targets"] = local_targets.detect_project_targets(row)
    except (OSError, ValueError, RuntimeError) as exc:
        row["targets"] = []
        row["target_error_type"] = type(exc).__name__
    return row


def workspace_snapshot() -> dict[str, Any]:
    """Project-centric local testing projection over the existing workspace core."""

    projects: list[dict[str, Any]] = []
    for registered in devtool.list_projects():
        name = str(registered.get("name") or "").strip()
        if not name:
            continue
        try:
            status = devtool.project_status(name)
        except (FileNotFoundError, KeyError, ValueError, RuntimeError, OSError) as exc:
            projects.append(
                {
                    "project": name,
                    "state": "error",
                    "root": registered.get("root"),
                    "services": [],
                    "targets": [],
                    "error_type": type(exc).__name__,
                }
            )
            continue
        projects.append(_project_row(status))

    services = [
        service
        for project in projects
        for service in project.get("services") or []
        if isinstance(service, dict)
    ]
    targets = [
        target
        for project in projects
        for target in project.get("targets") or []
        if isinstance(target, dict)
    ]
    return {
        "projects": projects,
        "project_count": len(projects),
        "ready_count": sum(row["state"] == "ready" for row in projects),
        "running_count": sum(row["state"] != "stopped" for row in projects if row["state"] != "error"),
        "service_count": len(services),
        "ready_service_count": sum(bool(service.get("ready")) for service in services),
        "running_service_count": sum(bool(service.get("process_alive")) for service in services),
        "local_target_count": len(targets),
        "web_target_count": sum(target.get("kind") == "web" for target in targets),
        "windows_target_count": sum(target.get("kind") == "windows" for target in targets),
        "android_target_count": sum(target.get("kind") == "android" for target in targets),
    }


def local_testing_snapshot() -> dict[str, Any]:
    """Single read-only snapshot for the Local Testing desktop home."""

    return {
        "workspace": workspace_snapshot(),
        "adb": adb_snapshot(),
        "settings": local_testing_settings(),
    }


def add_workspace_folder(folder: str) -> dict[str, Any]:
    """Human-initiated trust/add flow using the canonical repository discovery path."""

    value = str(folder).strip()
    if not value:
        raise ValueError("software folder must be non-empty")
    return workspace_cli.add_folder(value)


def _run_workspace_cli(argv: list[str]) -> Any:
    parsed = workspace_cli.build_parser().parse_args(argv)
    return workspace_cli.run(parsed)


def _stop_workspace_runs() -> list[dict[str, Any]]:
    stopped: list[dict[str, Any]] = []
    for registered in devtool.list_projects():
        name = str(registered.get("name") or "").strip()
        if not name:
            continue
        try:
            status = devtool.project_status(name)
        except (FileNotFoundError, KeyError, ValueError, RuntimeError, OSError):
            continue
        services = [row for row in status.get("services") or [] if isinstance(row, dict)]
        if not any(bool(row.get("process_alive")) for row in services):
            continue
        try:
            _run_workspace_cli(["stop", name])
            stopped.append({"project": name, "kind": "workspace"})
        except (FileNotFoundError, KeyError, ValueError, RuntimeError, OSError) as exc:
            stopped.append({"project": name, "kind": "workspace", "error_type": type(exc).__name__})
    return stopped


def _open_windows_process_handle(pid: int) -> int | None:
    if os.name != "nt" or pid <= 0:
        return None
    try:
        import ctypes

        process_terminate = 0x0001
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(process_terminate | synchronize, False, int(pid))
        return int(handle) if handle else None
    except (AttributeError, OSError, ValueError):
        return None


def _close_windows_process_handle(handle: int) -> None:
    if os.name != "nt" or not handle:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        pass


def _register_owned_target_run(project: str, target_id: str, result: dict[str, Any]) -> None:
    if not result.get("ok") or result.get("stage") != "launch":
        return
    key = f"{project}\n{target_id}"
    kind = str(result.get("kind") or "")
    with _OWNED_RUN_LOCK:
        if kind == "windows":
            handle = _open_windows_process_handle(int(result.get("pid") or 0))
            if handle:
                old = _OWNED_WINDOWS_HANDLES.pop(key, None)
                if old:
                    _close_windows_process_handle(old)
                _OWNED_WINDOWS_HANDLES[key] = handle
        elif kind == "android":
            serial = str(result.get("device_serial") or "").strip()
            package_id = str(result.get("package_id") or "").strip()
            if serial and package_id:
                _OWNED_ANDROID_RUNS[key] = (serial, package_id)


def _stop_owned_native_runs() -> list[dict[str, Any]]:
    stopped: list[dict[str, Any]] = []
    with _OWNED_RUN_LOCK:
        windows = list(_OWNED_WINDOWS_HANDLES.items())
        android = list(_OWNED_ANDROID_RUNS.items())
        _OWNED_WINDOWS_HANDLES.clear()
        _OWNED_ANDROID_RUNS.clear()

    if os.name == "nt":
        try:
            import ctypes
        except ImportError:
            ctypes = None
        if ctypes is not None:
            for key, handle in windows:
                try:
                    ok = bool(ctypes.windll.kernel32.TerminateProcess(handle, 0))
                    stopped.append({"run": key, "kind": "windows", "stopped": ok})
                except (AttributeError, OSError, ValueError) as exc:
                    stopped.append({"run": key, "kind": "windows", "stopped": False, "error_type": type(exc).__name__})
                finally:
                    _close_windows_process_handle(handle)
    else:
        for _key, handle in windows:
            _close_windows_process_handle(handle)

    executable = find_adb()
    if executable:
        for key, (serial, package_id) in android:
            try:
                completed = subprocess.run(
                    [executable, "-s", serial, "shell", "am", "force-stop", package_id],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    timeout=ADB_WIRELESS_TIMEOUT_SECONDS,
                    check=False,
                )
                stopped.append(
                    {
                        "run": key,
                        "kind": "android",
                        "stopped": completed.returncode == 0,
                        "device_serial": serial,
                        "package_id": package_id,
                    }
                )
            except (OSError, subprocess.SubprocessError) as exc:
                stopped.append({"run": key, "kind": "android", "stopped": False, "error_type": type(exc).__name__})
    return stopped


def _prepare_single_local_run() -> list[dict[str, Any]]:
    if local_testing_settings()["allow_multiple_local_runs"]:
        return []
    stopped = _stop_workspace_runs()
    stopped.extend(_stop_owned_native_runs())
    return stopped


def workspace_action(
    action: str,
    project: str,
    *,
    service: str | None = None,
    lines: int = 120,
) -> Any:
    """Run one narrow local-testing action through the canonical workspace CLI path."""

    selected_action = str(action).strip().lower()
    if selected_action not in WORKSPACE_ACTIONS:
        raise ValueError(f"unsupported local testing action: {selected_action}")
    selected_project = str(project).strip()
    if not selected_project:
        raise ValueError("project must be non-empty")

    if selected_action in {"start", "restart"}:
        _prepare_single_local_run()

    argv = [selected_action, selected_project]
    if selected_action in {"start", "restart"}:
        if service:
            argv.extend(["--service", str(service)])
        argv.append("--no-open")
    elif selected_action == "stop":
        if service:
            argv.extend(["--service", str(service)])
    elif selected_action == "check":
        if service:
            argv.extend(["--service", str(service)])
    elif selected_action == "logs":
        selected_service = str(service or "").strip()
        if not selected_service:
            raise ValueError("logs requires a declared service")
        bounded_lines = max(1, min(MAX_LOG_LINES, int(lines)))
        argv.extend([selected_service, "--lines", str(bounded_lines)])
    elif service:
        raise ValueError(f"{selected_action} does not accept a service")

    return _run_workspace_cli(argv)


def project_target_action(
    project: str,
    target_id: str,
    action: str,
    *,
    device_serial: str | None = None,
) -> dict[str, Any]:
    """Build/open one detected target belonging to an already trusted project."""

    selected_project = str(project).strip()
    if not selected_project:
        raise ValueError("project must be non-empty")
    selected_action = str(action).strip().lower()
    status = devtool.project_status(selected_project)
    row = _project_row(status)
    selected_target = next(
        (
            target
            for target in row.get("targets") or []
            if isinstance(target, dict) and str(target.get("id") or "") == str(target_id)
        ),
        None,
    )
    if selected_target is None:
        raise ValueError("target is unavailable or ambiguous; refresh Local Testing")

    # Starting a web target already passes through workspace_action("start"), which
    # enforces single-run mode. Merely opening its browser URL must never stop the
    # service that URL depends on. Native target opens create a new local run and
    # therefore enforce the policy here.
    if selected_action == "open" and str(selected_target.get("kind") or "") in {"windows", "android"}:
        _prepare_single_local_run()

    result = local_targets.target_action(
        row,
        str(target_id),
        selected_action,
        device_serial=device_serial,
    )
    if selected_action == "open" and str(selected_target.get("kind") or "") in {"windows", "android"}:
        _register_owned_target_run(selected_project, str(target_id), result)
    return result


__all__ = [
    "ADB_TIMEOUT_SECONDS",
    "ADB_WIRELESS_TIMEOUT_SECONDS",
    "LOCAL_TESTING_SETTINGS_VERSION",
    "MAX_ADB_OUTPUT_BYTES",
    "MAX_LOG_LINES",
    "WORKSPACE_ACTIONS",
    "adb_connect_wireless",
    "adb_pair_wireless",
    "adb_snapshot",
    "add_workspace_folder",
    "find_adb",
    "local_testing_settings",
    "local_testing_snapshot",
    "project_target_action",
    "set_allow_multiple_local_runs",
    "workspace_action",
    "workspace_snapshot",
]
