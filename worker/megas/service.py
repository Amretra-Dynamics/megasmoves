from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import json
import os
import platform
import plistlib
import secrets
import shlex
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from .client import WorkerRuntime
from .config import WorkerConfig, default_config_path


SERVICE_LABEL = "io.megasmoves.megasworker"
MAX_LOG_BYTES = 10 * 1024 * 1024
WATCHDOG_INTERVAL_SEC = 3.0
WATCHDOG_RESTART_BURST = 20
WATCHDOG_BURST_WINDOW_SEC = 120.0
WATCHDOG_BACKOFF_SEC = 20.0
# PID can be alive while WebSocket is dead (hung process). Restart if no
# connected heartbeat *pulse* within this window.
# Keep > heartbeat interval (20s) + slack for slow deep-health / reconnect.
WORKER_CONNECTED_STALE_SEC = 90.0
# Cold start / brief reconnect: do not kill the process before this age.
WORKER_CONNECTED_GRACE_SEC = 90.0
WORKER_CONNECTED_FUTURE_SKEW_SEC = 30.0
# Survive the starter console/job (agent shells, CI, IDE terminals).
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
# Detach from the parent console so daemon starts never flash a window.
_DETACHED_PROCESS = 0x00000008

# PyInstaller one-file sets LD_LIBRARY_PATH to the extract dir (libcrypto.so.3).
# Host tools like systemctl need the distro OpenSSL (e.g. OPENSSL_3.4.0 on Ubuntu
# 26.04). Always strip frozen library path vars when calling host daemons.
_HOST_LIB_ENV_STRIP = (
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "DYLD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "PYTHONHOME",
    "PYTHONPATH",
)


def _host_subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _HOST_LIB_ENV_STRIP:
        env.pop(key, None)
    # Restore pre-PyInstaller path when present (set by the bootloader).
    original = os.environ.get("LD_LIBRARY_PATH_ORIG") or os.environ.get(
        "DYLD_LIBRARY_PATH_ORIG"
    )
    if original is not None:
        if platform.system() == "Darwin":
            env["DYLD_LIBRARY_PATH"] = original
        else:
            env["LD_LIBRARY_PATH"] = original
    return env


def _run_host(
    argv: list[str],
    *,
    check: bool = False,
    capture_output: bool = False,
    text: bool = False,
    timeout: float | None = 30,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Run a host binary with an env that does not poison system OpenSSL."""
    kwargs.setdefault("env", _host_subprocess_env())
    return subprocess.run(
        argv,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        **kwargs,
    )


def state_root() -> Path:
    root = default_config_path().parent
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


def package_root() -> Path:
    """Directory that must be on PYTHONPATH (contains the ``megas`` package)."""
    return Path(__file__).resolve().parents[1]


def state_path() -> Path:
    return state_root() / "worker.pid.json"


def stop_path() -> Path:
    return state_root() / "worker.stop.json"


def lock_path() -> Path:
    return state_root() / "worker.lock"


def log_path() -> Path:
    return state_root() / "worker.log"


def watchdog_log_path() -> Path:
    """Separate from worker.log — Windows exclusive opens were killing the supervisor."""
    return state_root() / "watchdog.log"


def keepalive_log_path() -> Path:
    return state_root() / "keepalive.log"


def boot_log_path() -> Path:
    return state_root() / "worker.boot.log"


def watchdog_state_path() -> Path:
    return state_root() / "watchdog.pid.json"


def watchdog_stop_path() -> Path:
    return state_root() / "watchdog.stop.json"


def watchdog_lock_path() -> Path:
    return state_root() / "watchdog.lock"


def watchdog_boot_script_path() -> Path:
    return state_root() / "boot_watchdog.py"


def connected_path() -> Path:
    """Written by the Worker while the backend WebSocket is live."""
    return state_root() / "worker.connected.json"


def mark_worker_connected(*, pid: int | None = None) -> None:
    """Refresh the connected pulse so the watchdog knows WS is alive.

    ``last_pulse_at`` is what the watchdog ages — must be rewritten on every
    successful heartbeat, not only on the first connect frame.
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        prev: dict[str, Any] = {}
        try:
            prev = json.loads(connected_path().read_text(encoding="utf-8"))
        except Exception:
            prev = {}
        current_pid = int(pid or os.getpid())
        same_connection = (
            int(prev.get("pid") or 0) == current_pid
            and prev.get("connected") is True
        )
        connected_at = (
            str(prev.get("connected_at") or "").strip()
            if same_connection
            else ""
        ) or now
        _atomic_json(
            connected_path(),
            {
                "pid": current_pid,
                "connected": True,
                "connected_at": connected_at,
                "last_pulse_at": now,
            },
        )
    except Exception:
        pass


def mark_worker_disconnected(*, pid: int | None = None) -> None:
    """Mark WS down without erasing pulse history.

    Unlinking caused false restarts: watchdog saw age=None mid-reconnect and
    killed a healthy Worker that was already reopening the socket.
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        prev: dict[str, Any] = {}
        try:
            prev = json.loads(connected_path().read_text(encoding="utf-8"))
        except Exception:
            prev = {}
        current_pid = int(pid or os.getpid())
        same_process = int(prev.get("pid") or 0) == current_pid
        last_pulse = (
            str(prev.get("last_pulse_at") or prev.get("connected_at") or "")
            if same_process
            else ""
        )
        _atomic_json(
            connected_path(),
            {
                "pid": current_pid,
                "connected": False,
                "connected_at": str(prev.get("connected_at") or last_pulse) if same_process else "",
                "last_pulse_at": last_pulse,
                "disconnected_at": now,
            },
        )
    except Exception:
        pass


def _timestamp_age_sec(raw: Any) -> float | None:
    try:
        value = str(raw or "").strip()
        if not value:
            return None
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).total_seconds()
    except Exception:
        return None


def worker_connection_status(
    *,
    expected_pid: int | None = None,
    max_age_sec: float = WORKER_CONNECTED_STALE_SEC,
) -> dict[str, Any]:
    """Return honest local WebSocket health for the current Worker process."""
    try:
        payload = json.loads(connected_path().read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    pulse_pid = int(payload.get("pid") or 0)
    age = _timestamp_age_sec(payload.get("last_pulse_at") or payload.get("connected_at"))
    disconnected_age = _timestamp_age_sec(payload.get("disconnected_at"))
    connected = payload.get("connected") is True
    pid_matches = expected_pid is None or pulse_pid == int(expected_pid)
    fresh = (
        age is not None
        and age >= -WORKER_CONNECTED_FUTURE_SKEW_SEC
        and age <= max_age_sec
    )
    healthy = bool(connected and pulse_pid > 0 and pid_matches and fresh)
    if not payload:
        reason = "pulse_missing"
    elif not pid_matches:
        reason = "pulse_pid_mismatch"
    elif not connected:
        reason = "disconnected"
    elif age is None:
        reason = "pulse_timestamp_missing"
    elif age < -WORKER_CONNECTED_FUTURE_SKEW_SEC:
        reason = "pulse_timestamp_in_future"
    elif not fresh:
        reason = "pulse_stale"
    else:
        reason = "connected"
    return {
        "connected": healthy,
        "reported_connected": connected,
        "pid": pulse_pid or None,
        "pid_matches": pid_matches,
        "fresh": fresh,
        "age_seconds": max(0.0, age) if age is not None else None,
        "disconnected_age_seconds": max(0.0, disconnected_age) if disconnected_age is not None else None,
        "last_pulse_at": payload.get("last_pulse_at") or payload.get("connected_at"),
        "reason": reason,
    }


def worker_connection_age_sec() -> float | None:
    """Compatibility accessor for the last local WebSocket pulse age."""
    return worker_connection_status()["age_seconds"]


def worker_connection_stale(
    max_age_sec: float = WORKER_CONNECTED_STALE_SEC,
    *,
    expected_pid: int | None = None,
) -> bool:
    return not worker_connection_status(
        expected_pid=expected_pid,
        max_age_sec=max_age_sec,
    )["connected"]


def _no_window_creationflags(
    *,
    new_group: bool = True,
    breakaway: bool = False,
    detached: bool = False,
) -> int:
    if os.name != "nt":
        return 0
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    if new_group:
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    if detached:
        # No parent console inheritance — avoids the brief flash VBS→python.exe
        # still produces on some Windows builds even with Run style 0.
        flags |= int(getattr(subprocess, "DETACHED_PROCESS", _DETACHED_PROCESS))
    if breakaway:
        # Leave the parent Job Object so IDE/agent/CI shells cannot kill the
        # Worker when their session ends (root cause of "first message waits").
        flags |= _CREATE_BREAKAWAY_FROM_JOB
    return flags


def _hide_window_startupinfo() -> Any | None:
    if os.name != "nt":
        return None
    try:
        info = subprocess.STARTUPINFO()
        info.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0x1))
        info.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
        return info
    except Exception:
        return None


def _hidden_windows_process_kwargs(
    *,
    new_group: bool = False,
    breakaway: bool = False,
    detached: bool = False,
) -> dict[str, Any]:
    """Fail-safe Windows launch policy for every service child process.

    ``CREATE_NO_WINDOW`` prevents console allocation and ``SW_HIDE`` covers
    console tools that still honor STARTUPINFO independently. Keep both on all
    watchdog, task-maintenance, stop, and recovery launches.
    """
    if os.name != "nt":
        return {}
    kwargs: dict[str, Any] = {
        "creationflags": _no_window_creationflags(
            new_group=new_group,
            breakaway=breakaway,
            detached=detached,
        )
    }
    startupinfo = _hide_window_startupinfo()
    if startupinfo is not None:
        kwargs["startupinfo"] = startupinfo
    return kwargs


def _child_env() -> dict[str, str]:
    """Environment for Worker/watchdog children: durable PYTHONPATH + quiet CLIs."""
    env = {str(k): str(v) for k, v in os.environ.items()}
    if getattr(sys, "frozen", False):
        # The daemon outlives this one-file PyInstaller launcher. Without a
        # reset, the child reuses the launcher's _MEI directory, which is
        # deleted as soon as `megas start` exits.
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        # Never pass the launcher's temporary _MEI path back through
        # PYTHONPATH/PYTHONHOME. The independent child extracts its own archive
        # and must resolve bundled extensions (notably _sqlite3.pyd) there.
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
    else:
        root = str(package_root())
        existing = [part for part in (env.get("PYTHONPATH") or "").split(os.pathsep) if part]
        if root not in existing:
            env["PYTHONPATH"] = os.pathsep.join([root, *existing])
    env["PYTHONUNBUFFERED"] = "1"
    # Pin state directory so test MEGAS_HOME (or production home) is always
    # inherited by daemon children — never silently fall back to another tree.
    env["MEGAS_HOME"] = str(default_config_path().parent)
    # Keep agent/git tools non-interactive so they never flash prompts.
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("CI", "1")
    env.setdefault("TERM", "dumb")
    # This is a production invariant: never silently fall back to Git\cmd or
    # other console-wrapper paths when preparing daemon children.
    from .adapters.base import prefer_console_free_tool_path

    env = prefer_console_free_tool_path(env)
    return env


def _daemon_working_directory() -> Path:
    """Return a durable cwd for a daemon that may outlive its launcher."""
    if getattr(sys, "frozen", False):
        return state_root()
    return package_root()


def _windows_environment_block(env: dict[str, str]) -> Any:
    """Build the double-NUL-terminated Unicode block required by CreateProcessW."""
    import ctypes

    entries = [
        f"{key}={value}"
        for key, value in sorted(env.items(), key=lambda item: item[0].casefold())
        if key and "=" not in key and "\0" not in key and "\0" not in value
    ]
    # create_unicode_buffer adds the final NUL; append one so the environment
    # block itself is terminated by two NUL characters.
    return ctypes.create_unicode_buffer("\0".join(entries) + "\0")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_state() -> dict[str, Any]:
    try:
        payload = json.loads(state_path().read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        access = 0x1000 | 0x00100000
        process = ctypes.windll.kernel32.OpenProcess(access, False, int(pid))
        if not process:
            err = int(ctypes.GetLastError() or 0)
            # 5 = ACCESS_DENIED → process exists (common for protected/system-ish procs).
            # 87 = INVALID_PARAMETER → pid does not exist.
            if err in {5, 0x5}:
                return True
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = bool(ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)))
            if not ok:
                # Can't query exit code but handle opened → treat as alive.
                return True
            return int(exit_code.value) == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:
        reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            return False
    except ChildProcessError:
        pass
    if sys.platform.startswith("linux"):
        try:
            process_stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            closing_parenthesis = process_stat.rfind(")")
            process_state = process_stat[closing_parenthesis + 2 : closing_parenthesis + 3]
            if process_state in {"Z", "X"}:
                return False
        except OSError:
            pass
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def daemon_status() -> dict[str, Any]:
    state = read_state()
    pid = int(state.get("pid") or 0)
    running = process_is_running(pid)
    if state and not running:
        # Process gone: clear pid state and the WS pulse so backend + phone
        # flip to offline promptly (do not leave connected=true for 90s).
        try:
            mark_worker_disconnected(pid=pid or None)
        except Exception:
            pass
        state_path().unlink(missing_ok=True)
        lock_path().unlink(missing_ok=True)
    return {**state, "pid": pid or None, "running": running, "log_path": str(log_path())}


def watchdog_status() -> dict[str, Any]:
    try:
        state = json.loads(watchdog_state_path().read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, json.JSONDecodeError):
        state = {}
    pid = int(state.get("pid") or 0)
    running = process_is_running(pid)
    if state and not running:
        watchdog_state_path().unlink(missing_ok=True)
    return {**state, "pid": pid or None, "running": running}


def _service_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "worker", "run-service"]
    # Unbuffered so crash logs land in worker.log immediately.
    return [sys.executable, "-u", "-m", "megas.cli", "worker", "run-service"]


def _daemon_python_executable() -> str:
    """Interpreter for Worker/watchdog daemons.

    Prefer ``python.exe`` (not ``pythonw.exe``). pythonw was observed to exit
    right after ``run_service`` prints \"starting\" and before the backend
    WebSocket connects, leaving phone sessions stuck in ``session_queued``.
    Console flashing is prevented by CREATE_NO_WINDOW + DETACHED_PROCESS on
    programmatic starts (VBS window-style 0 only for logon Run-key).
    """
    exe = Path(sys.executable)
    # If we were started as pythonw, prefer sibling python.exe.
    if os.name == "nt" and exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.is_file():
            return str(candidate)
    return str(exe)


def _direct_boot_command(boot_script: Path) -> list[str]:
    """Launch a boot script under the daemon interpreter (no VBS, no console)."""
    return [_daemon_python_executable(), "-u", str(boot_script)]


def write_watchdog_boot_script() -> Path:
    """Write a tiny boot module so Run-key autostart keeps PYTHONPATH correct."""
    boot = watchdog_boot_script_path()
    root = str(package_root())
    home = str(default_config_path().parent)
    boot.write_text(
        "# Auto-generated by Megas Local Worker. Do not edit.\n"
        "from __future__ import annotations\n"
        "import os\n"
        "import sys\n"
        f"sys.path.insert(0, {root!r})\n"
        f"os.environ['MEGAS_HOME'] = {home!r}\n"
        f"os.environ['PYTHONPATH'] = {root!r} + os.pathsep + os.environ.get('PYTHONPATH', '')\n"
        "from megas.service import run_watchdog\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    run_watchdog()\n",
        encoding="utf-8",
    )
    try:
        os.chmod(boot, 0o600)
    except OSError:
        pass
    return boot


def write_worker_boot_script() -> Path:
    """Boot module for Worker daemon (used by VBS / Run-key style launches)."""
    boot = state_root() / "boot_worker.py"
    root = str(package_root())
    home = str(default_config_path().parent)
    boot.write_text(
        "# Auto-generated by Megas Local Worker. Do not edit.\n"
        "from __future__ import annotations\n"
        "import os\n"
        "import sys\n"
        f"sys.path.insert(0, {root!r})\n"
        f"os.environ['MEGAS_HOME'] = {home!r}\n"
        f"os.environ['PYTHONPATH'] = {root!r} + os.pathsep + os.environ.get('PYTHONPATH', '')\n"
        "from megas.config import load_config\n"
        "from megas.service import run_service\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    run_service(load_config())\n",
        encoding="utf-8",
    )
    try:
        os.chmod(boot, 0o600)
    except OSError:
        pass
    return boot


def _vbs_string(value: str) -> str:
    """Return a VBScript string literal (VBScript does not use JSON escapes)."""
    return '"' + value.replace('"', '""') + '"'


def _powershell_literal(value: str) -> str:
    """Return an interpolation-free PowerShell string literal."""
    return "'" + value.replace("'", "''") + "'"


def write_vbs_launcher(boot_script: Path, name: str, *, wait: bool = False) -> Path:
    """WScript launcher for logon Run-key / Task Scheduler (no parent CreateProcess flags).

    Programmatic starts (``start_background`` / ``start_watchdog_background``) use
    direct ``python.exe`` with CREATE_NO_WINDOW|DETACHED_PROCESS instead — VBS→
    console-subsystem python can still flash a window on some Windows builds.
    """
    vbs = state_root() / f"start_{name}.vbs"
    python_exe = _daemon_python_executable().replace("/", "\\")
    boot = str(boot_script).replace("/", "\\")
    # Window style 0 = hidden, False = do not wait for process.
    # Chr(34) quoting avoids broken escapes on Windows paths.
    vbs.write_text(
        "' Auto-generated by Megas Local Worker. Do not edit.\r\n"
        "Set sh = CreateObject(\"WScript.Shell\")\r\n"
        f"py = {_vbs_string(python_exe)}\r\n"
        f"boot = {_vbs_string(boot)}\r\n"
        "cmd = Chr(34) & py & Chr(34) & \" -u \" & Chr(34) & boot & Chr(34)\r\n"
        + (
            "rc = sh.Run(cmd, 0, True)\r\nWScript.Quit rc\r\n"
            if wait
            else "sh.Run cmd, 0, False\r\n"
        ),
        encoding="utf-8",
    )
    try:
        os.chmod(vbs, 0o600)
    except OSError:
        pass
    return vbs


def write_vbs_command_launcher(command: list[str], name: str, *, wait: bool = False) -> Path:
    """Hidden VBS wrapper for a full argv (used by frozen megasmoves.exe autostart)."""
    vbs = state_root() / f"start_{name}.vbs"
    # list2cmdline produces Windows-safe quoting for the whole command line.
    cmd_line = subprocess.list2cmdline(command)
    vbs.write_text(
        "' Auto-generated by Megas Local Worker. Do not edit.\r\n"
        "Set sh = CreateObject(\"WScript.Shell\")\r\n"
        f"cmd = {_vbs_string(cmd_line)}\r\n"
        + (
            "rc = sh.Run(cmd, 0, True)\r\nWScript.Quit rc\r\n"
            if wait
            else "sh.Run cmd, 0, False\r\n"
        ),
        encoding="utf-8",
    )
    try:
        os.chmod(vbs, 0o600)
    except OSError:
        pass
    return vbs


def _watchdog_command(*, for_autostart: bool = False) -> list[str]:
    """Watchdog launch argv.

    * Programmatic start: direct python/exe (CREATE_NO_WINDOW from parent).
    * Autostart (Run-key / schtasks): VBS window-style 0 — Task Scheduler and
      Run have no CREATE_NO_WINDOW, so raw console binaries would pop a window.
    """
    if getattr(sys, "frozen", False):
        frozen_cmd = [sys.executable, "worker", "run-watchdog"]
        if os.name == "nt" and for_autostart:
            # wait=False: fire-and-forget so wscript exits and the watchdog is
            # not a child of a short-lived Run-key / agent shell. Wait=True was
            # killing Worker+watchdog when the parent job ended ("went offline").
            vbs = write_vbs_command_launcher(frozen_cmd, "watchdog", wait=False)
            return ["wscript.exe", "//B", "//Nologo", str(vbs)]
        return frozen_cmd
    boot = write_watchdog_boot_script()
    if os.name == "nt" and for_autostart:
        vbs = write_vbs_launcher(boot, "watchdog", wait=False)
        return ["wscript.exe", "//B", "//Nologo", str(vbs)]
    return _direct_boot_command(boot)


def _worker_launch_command(*, for_autostart: bool = False) -> list[str]:
    """Worker daemon launch argv (see ``_watchdog_command`` for autostart notes)."""
    if getattr(sys, "frozen", False):
        frozen_cmd = _service_command()
        if os.name == "nt" and for_autostart:
            vbs = write_vbs_command_launcher(frozen_cmd, "worker")
            return ["wscript.exe", "//B", "//Nologo", str(vbs)]
        return frozen_cmd
    if os.name == "nt":
        boot = write_worker_boot_script()
        if for_autostart:
            vbs = write_vbs_launcher(boot, "worker")
            return ["wscript.exe", "//B", "//Nologo", str(vbs)]
        return _direct_boot_command(boot)
    return _service_command()


def _daemon_popen_kwargs() -> dict[str, Any]:
    """Shared no-console kwargs for Worker/watchdog programmatic starts."""
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": _child_env(),
        "cwd": str(_daemon_working_directory()),
    }
    if os.name == "nt":
        kwargs.update(
            _hidden_windows_process_kwargs(
                new_group=True,
                breakaway=True,
                detached=True,
            )
        )
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _windows_createprocess_hidden(command: list[str]) -> int:
    """CreateProcessW with NO_WINDOW + DETACHED + BREAKAWAY — no console flash.

    WMI Win32_Process.Create was used for job survival but launches console
    python.exe with a visible window. This path keeps job breakaway without popups.
    """
    if os.name != "nt":
        raise RuntimeError("Windows-only")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    CREATE_NO_WINDOW = 0x08000000
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    DETACHED_PROCESS = 0x00000008
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    STARTF_USESHOWWINDOW = 0x00000001
    SW_HIDE = 0

    cmdline = subprocess.list2cmdline(command)
    cwd = str(_daemon_working_directory())
    # CreateProcess requires a mutable buffer for the command line.
    cmd_buf = ctypes.create_unicode_buffer(cmdline)
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.dwFlags = STARTF_USESHOWWINDOW
    si.wShowWindow = SW_HIDE
    pi = PROCESS_INFORMATION()

    flags = (
        CREATE_NO_WINDOW
        | CREATE_NEW_PROCESS_GROUP
        | DETACHED_PROCESS
        | CREATE_BREAKAWAY_FROM_JOB
        | CREATE_UNICODE_ENVIRONMENT
    )
    # The preferred CreateProcessW path must receive the same child environment
    # as the Popen fallback. Passing NULL here silently dropped
    # PYINSTALLER_RESET_ENVIRONMENT, so frozen daemons reused a launcher's _MEI
    # directory after it had been cleaned and thread sync could not load SQLite.
    env_block = _windows_environment_block(_child_env())
    env = ctypes.cast(env_block, wintypes.LPVOID)
    ok = kernel32.CreateProcessW(
        None,
        cmd_buf,
        None,
        None,
        False,
        flags,
        env,
        cwd,
        ctypes.byref(si),
        ctypes.byref(pi),
    )
    if not ok:
        # Retry without breakaway (some jobs forbid it).
        flags = (
            CREATE_NO_WINDOW
            | CREATE_NEW_PROCESS_GROUP
            | DETACHED_PROCESS
            | CREATE_UNICODE_ENVIRONMENT
        )
        ok = kernel32.CreateProcessW(
            None,
            cmd_buf,
            None,
            None,
            False,
            flags,
            env,
            cwd,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(err, f"CreateProcessW failed error={err}")
    try:
        kernel32.CloseHandle(pi.hThread)
        kernel32.CloseHandle(pi.hProcess)
    except Exception:
        pass
    return int(pi.dwProcessId)


def _spawn_daemon_process(command: list[str], *, boot_handle: Any | None = None) -> int:
    """Spawn Worker/watchdog daemon hidden (no console popup) and job-breakaway on Windows."""
    if os.name == "nt":
        # Prefer CreateProcess flags over WMI: WMI always flashes a python console.
        try:
            pid = _windows_createprocess_hidden(command)
            if boot_handle is not None:
                boot_handle.write(
                    f"CreateProcess hidden pid={pid} cmd={' '.join(command)}\n"
                )
                boot_handle.flush()
            return pid
        except Exception as exc:
            if boot_handle is not None:
                boot_handle.write(
                    f"CreateProcess hidden failed ({exc}); fallback Popen\n"
                )
                boot_handle.flush()
        # VBS window-style 0 as second hidden path (wscript //B never shows a console).
        try:
            joined = " ".join(command).lower()
            if "boot_worker" in joined:
                vbs = write_vbs_launcher(Path(command[-1]), "worker", wait=False)
            elif "boot_watchdog" in joined:
                vbs = write_vbs_launcher(Path(command[-1]), "watchdog", wait=False)
            else:
                vbs = write_vbs_command_launcher(command, "daemon", wait=False)
            wscript = ["wscript.exe", "//B", "//Nologo", str(vbs)]
            pid = _windows_createprocess_hidden(wscript)
            if boot_handle is not None:
                boot_handle.write(f"VBS hidden launch pid={pid} vbs={vbs}\n")
                boot_handle.flush()
            return pid
        except Exception as exc2:
            if boot_handle is not None:
                boot_handle.write(f"VBS hidden launch failed ({exc2})\n")
                boot_handle.flush()
    kwargs = _daemon_popen_kwargs()
    if boot_handle is not None and os.name != "nt":
        kwargs["stdout"] = boot_handle
        kwargs["stderr"] = boot_handle
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError:
        if os.name == "nt":
            kwargs["creationflags"] = _no_window_creationflags(
                new_group=True, breakaway=False, detached=False
            )
            process = subprocess.Popen(command, **kwargs)
        else:
            raise
    return int(process.pid)


def _acquire_pid_lock(path: Path, nonce: str, label: str) -> int:
    # O_EXCL creation and the first PID write are two syscalls. A second frozen
    # process can observe the new file before its contents land; treating that
    # brief empty file as stale lets both daemons run. Retry fresh/malformed
    # locks, and only reclaim one after it has remained stale for two seconds.
    for _ in range(40):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, f"{os.getpid()}:{nonce}".encode("ascii"))
            return descriptor
        except FileExistsError:
            try:
                existing_pid = int(path.read_text(encoding="ascii").split(":", 1)[0])
            except (OSError, ValueError):
                existing_pid = 0
            if process_is_running(existing_pid):
                raise RuntimeError(f"{label} is already running on this machine.")
            if existing_pid <= 0:
                try:
                    age = max(0.0, time.time() - path.stat().st_mtime)
                except OSError:
                    age = 0.0
                if age < 2.0:
                    time.sleep(0.05)
                    continue
            path.unlink(missing_ok=True)
            time.sleep(0.01)
    raise RuntimeError(f"{label} could not acquire its local service lock.")


def _release_pid_lock(path: Path, descriptor: int, nonce: str) -> None:
    os.close(descriptor)
    try:
        current = path.read_text(encoding="ascii").split(":", 1)
    except OSError:
        return
    if len(current) == 2 and current[1] == nonce:
        path.unlink(missing_ok=True)


def _acquire_lock(nonce: str) -> int:
    return _acquire_pid_lock(lock_path(), nonce, "Megas")


def _rotate_log() -> None:
    path = log_path()
    if path.is_file() and path.stat().st_size >= MAX_LOG_BYTES:
        rotated = path.with_suffix(".log.1")
        rotated.unlink(missing_ok=True)
        os.replace(path, rotated)


async def _serve(config: WorkerConfig, nonce: str) -> None:
    runtime = WorkerRuntime(config)
    worker_task = asyncio.create_task(runtime.start_forever())

    async def wait_for_stop() -> None:
        while True:
            await asyncio.sleep(0.5)
            try:
                request = json.loads(stop_path().read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if request.get("nonce") == nonce:
                return

    stop_task = asyncio.create_task(wait_for_stop())
    done, _ = await asyncio.wait({worker_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    if stop_task in done:
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
    else:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        await worker_task


def run_service(config: WorkerConfig) -> None:
    nonce = secrets.token_urlsafe(24)
    descriptor = _acquire_lock(nonce)
    state = {
        "pid": os.getpid(),
        "nonce": nonce,
        "worker_id": config.worker_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "command": "megas worker run-service",
    }
    _atomic_json(state_path(), state)
    stop_path().unlink(missing_ok=True)
    # A pulse belongs to one concrete daemon PID. Clear any previous process's
    # fresh-looking pulse before this process attempts its first connection.
    mark_worker_disconnected(pid=os.getpid())
    _rotate_log()
    try:
        with log_path().open("a", encoding="utf-8", buffering=1) as output:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                print(f"[{datetime.now(timezone.utc).isoformat()}] Megas Local Worker starting")
                try:
                    asyncio.run(_serve(config, nonce))
                    print(f"[{datetime.now(timezone.utc).isoformat()}] Megas Local Worker stopped")
                except Exception:
                    traceback.print_exc(file=output)
                    raise
    finally:
        # Always clear the WS pulse on exit — leaving connected=true after a crash
        # made the phone show a ghost "online" window until the stale timer elapsed,
        # and confused operators about how the PC "went offline".
        try:
            mark_worker_disconnected(pid=os.getpid())
        except Exception:
            pass
        current = read_state()
        if current.get("nonce") == nonce:
            state_path().unlink(missing_ok=True)
        stop_path().unlink(missing_ok=True)
        _release_pid_lock(lock_path(), descriptor, nonce)


def start_background() -> dict[str, Any]:
    existing = daemon_status()
    if existing["running"]:
        return existing
    # Direct python + CREATE_NO_WINDOW (not VBS) — avoids console flash storms
    # when the watchdog restarts or operators run `megas start`.
    command = _worker_launch_command(for_autostart=False)
    boot_log_path().parent.mkdir(parents=True, exist_ok=True)
    with boot_log_path().open("a", encoding="utf-8") as boot_handle:
        boot_handle.write(
            f"\n[{datetime.now(timezone.utc).isoformat()}] launching {' '.join(command)}\n"
        )
        boot_handle.flush()
        _spawn_daemon_process(command, boot_handle=boot_handle)
    # Cold boot / version probes can take several seconds; 2s stability caused
    # false "did not become ready" even when the daemon ended up healthy.
    timeout_sec = 60 if os.getenv("PYTEST_CURRENT_TEST") else 30
    deadline = time.monotonic() + timeout_sec
    seen_running_at: float | None = None
    while time.monotonic() < deadline:
        state = daemon_status()
        if state["running"]:
            # Require the process to stay alive briefly — pythonw used to write
            # pid then die before backend connect, false-green for ~1s.
            if seen_running_at is None:
                seen_running_at = time.monotonic()
            elif time.monotonic() - seen_running_at >= 6.0:
                return state
        else:
            seen_running_at = None
        time.sleep(0.15)
    # Final check — if still running, accept even without full stability window.
    state = daemon_status()
    if state.get("running"):
        return state
    raise RuntimeError(
        f"Megas did not become ready within 30s. "
        f"Run `megas status` — the worker may still be starting. "
        f"Inspect {log_path()} and {boot_log_path()}."
    )


def ensure_running() -> dict[str, Any]:
    """Return running worker status, starting the daemon if it is dead."""
    status = daemon_status()
    if status["running"]:
        return status
    return start_background()


def stop_background(timeout: float = 10.0) -> bool:
    state = daemon_status()
    if not state["running"]:
        return False
    _atomic_json(stop_path(), {"nonce": state.get("nonce"), "requested_at": datetime.now(timezone.utc).isoformat()})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_is_running(int(state["pid"])):
            return True
        time.sleep(0.2)
    pid = int(state["pid"])
    if os.name == "nt":
        kill_kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "timeout": 10,
            "check": False,
        }
        kill_kwargs.update(_hidden_windows_process_kwargs(new_group=False))
        # Soft kill the worker process first without /T so we do not cascade into
        # unrelated breakaway trees; force /T only if it refuses to exit.
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], **kill_kwargs)
        if process_is_running(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], **kill_kwargs)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not process_is_running(pid):
            return True
        time.sleep(0.1)
    return False


def _append_log(path: Path, line: str) -> None:
    """Best-effort append; never raise into the supervisor loop."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", buffering=1) as handle:
            handle.write(line if line.endswith("\n") else f"{line}\n")
    except Exception:
        pass


def run_watchdog() -> None:
    """Supervisor loop: restart Local Worker when the PID dies.

    Installed as the Windows Run-key / primary durability process so phone sessions
    do not sit queued after a silent Worker crash.

    Uses watchdog.log only — never worker.log — so exclusive log opens on Windows
    cannot take down the supervisor while the Worker is writing session output.
    """
    nonce = secrets.token_urlsafe(24)
    log = watchdog_log_path()
    try:
        descriptor = _acquire_pid_lock(watchdog_lock_path(), nonce, "Megas watchdog")
    except RuntimeError as exc:
        _append_log(log, f"[watchdog] duplicate start ignored: {exc}")
        return
    state = {
        "pid": os.getpid(),
        "nonce": nonce,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "command": "megas worker run-watchdog",
    }
    try:
        _atomic_json(watchdog_state_path(), state)
    except Exception:
        _release_pid_lock(watchdog_lock_path(), descriptor, nonce)
        raise
    watchdog_stop_path().unlink(missing_ok=True)
    restarts: list[float] = []
    try:
        _append_log(
            log,
            f"[{datetime.now(timezone.utc).isoformat()}] Watchdog started pid={os.getpid()}",
        )
        # Bring worker up immediately on boot.
        try:
            ensure_running()
        except Exception as exc:
            _append_log(log, f"[watchdog] initial start failed: {exc}")
        while True:
            try:
                request = json.loads(watchdog_stop_path().read_text(encoding="utf-8"))
                if request.get("nonce") == nonce or request.get("force"):
                    _append_log(
                        log,
                        f"[{datetime.now(timezone.utc).isoformat()}] Watchdog stop requested",
                    )
                    break
            except (OSError, json.JSONDecodeError):
                pass
            try:
                status = daemon_status()
                need_restart = False
                reason = ""
                if not status.get("running"):
                    need_restart = True
                    reason = "dead Worker PID"
                else:
                    connection = worker_connection_status(
                        expected_pid=int(status.get("pid") or 0),
                        max_age_sec=WORKER_CONNECTED_STALE_SEC,
                    )
                if status.get("running") and not connection["connected"]:
                    # Cold start + brief reconnect: never kill before grace window.
                    started_raw = str(status.get("started_at") or "")
                    process_age = None
                    if started_raw:
                        try:
                            started = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
                            if started.tzinfo is None:
                                started = started.replace(tzinfo=timezone.utc)
                            process_age = (
                                datetime.now(timezone.utc) - started
                            ).total_seconds()
                        except Exception:
                            process_age = None
                    age = connection.get("age_seconds")
                    disconnected_age = connection.get("disconnected_age_seconds")
                    if process_age is not None and process_age < WORKER_CONNECTED_GRACE_SEC:
                        need_restart = False
                    elif (
                        connection.get("reason") == "disconnected"
                        and disconnected_age is not None
                        and disconnected_age < WORKER_CONNECTED_GRACE_SEC
                    ):
                        need_restart = False
                    elif age is None and (
                        process_age is None or process_age < WORKER_CONNECTED_GRACE_SEC * 2
                    ):
                        # Still no pulse file — give longer grace before recycle.
                        need_restart = False
                    else:
                        # Process alive but WS hung long enough — recycle so phone
                        # sessions do not sit session_queued forever.
                        need_restart = True
                        reason = (
                            f"Worker PID alive but backend connection stale "
                            f"(reason={connection.get('reason')}, "
                            f"age={age if age is not None else 'never'}s)"
                        )
                if need_restart:
                    now = time.time()
                    restarts = [stamp for stamp in restarts if now - stamp < WATCHDOG_BURST_WINDOW_SEC]
                    if len(restarts) >= WATCHDOG_RESTART_BURST:
                        _append_log(
                            log,
                            f"[watchdog] restart burst limit; sleeping {WATCHDOG_BACKOFF_SEC:.0f}s",
                        )
                        time.sleep(WATCHDOG_BACKOFF_SEC)
                        restarts.clear()
                        continue
                    _append_log(
                        log,
                        f"[{datetime.now(timezone.utc).isoformat()}] "
                        f"Watchdog restarting Worker ({reason})",
                    )
                    try:
                        if status.get("running"):
                            stop_background(timeout=5)
                            mark_worker_disconnected(pid=int(status.get("pid") or 0))
                        ensure_running()
                        restarts.append(now)
                        _append_log(
                            log,
                            f"[watchdog] Worker restarted pid="
                            f"{daemon_status().get('pid')}",
                        )
                    except Exception as exc:
                        _append_log(log, f"[watchdog] restart failed: {exc}")
                        restarts.append(now)
                else:
                    # Quiet liveness ping so ops can see the supervisor is alive.
                    # Log every ~15s (not only when time % 30 is tiny — that
                    # window was easy to miss and looked like a dead watchdog).
                    if int(time.time()) % 15 < WATCHDOG_INTERVAL_SEC:
                        age = worker_connection_age_sec()
                        conn = worker_connection_status(
                            expected_pid=int(status.get("pid") or 0)
                        )
                        _append_log(
                            log,
                            f"[watchdog] ok worker_pid={status.get('pid')} "
                            f"running={status.get('running')} "
                            f"ws={conn.get('reason')} "
                            f"connected_age_s={age if age is not None else 'n/a'}",
                        )
            except Exception as exc:
                _append_log(log, f"[watchdog] loop error: {exc}")
            time.sleep(WATCHDOG_INTERVAL_SEC)
    finally:
        current = {}
        try:
            current = json.loads(watchdog_state_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        if current.get("nonce") == nonce:
            watchdog_state_path().unlink(missing_ok=True)
        watchdog_stop_path().unlink(missing_ok=True)
        _release_pid_lock(watchdog_lock_path(), descriptor, nonce)


def start_watchdog_background() -> dict[str, Any]:
    existing = watchdog_status()
    if existing["running"]:
        return existing
    command = _watchdog_command(for_autostart=False)
    with boot_log_path().open("a", encoding="utf-8") as boot_handle:
        boot_handle.write(
            f"\n[{datetime.now(timezone.utc).isoformat()}] launching watchdog {' '.join(command)}\n"
        )
        boot_handle.flush()
        _spawn_daemon_process(command, boot_handle=boot_handle)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = watchdog_status()
        if status["running"]:
            return status
        time.sleep(0.15)
    raise RuntimeError(f"Megas watchdog did not become ready. Inspect {log_path()}.")


def stop_watchdog(timeout: float = 8.0) -> bool:
    status = watchdog_status()
    if not status["running"]:
        return False
    _atomic_json(
        watchdog_stop_path(),
        {"nonce": status.get("nonce"), "force": True, "requested_at": datetime.now(timezone.utc).isoformat()},
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_is_running(int(status["pid"])):
            return True
        time.sleep(0.2)
    pid = int(status["pid"])
    if os.name == "nt":
        kill_kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "timeout": 10,
            "check": False,
        }
        kill_kwargs.update(_hidden_windows_process_kwargs(new_group=False))
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], **kill_kwargs)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not process_is_running(pid):
            return True
        time.sleep(0.1)
    return False


def ensure_worker_and_watchdog() -> dict[str, Any]:
    """Production entry: keep supervisor + worker online for phone sessions.

    Start the Worker first, then the watchdog. Starting both in parallel races
    the single-instance lock and can surface a false install failure.
    """
    worker = ensure_running()
    watchdog = start_watchdog_background()
    connection = worker_connection_status(expected_pid=int(worker.get("pid") or 0))
    return {
        "running": bool(worker.get("running")),
        "pid": worker.get("pid"),
        "worker_id": worker.get("worker_id"),
        "watchdog_pid": watchdog.get("pid"),
        "watchdog_running": bool(watchdog.get("running")),
        "connected": bool(connection.get("connected")),
        "connection": connection,
        "log_path": str(log_path()),
    }


def wait_for_worker_connection(
    *,
    expected_pid: int,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Wait for a fresh, positive pulse emitted by the expected daemon PID."""
    deadline = time.monotonic() + max(0.0, timeout)
    latest = worker_connection_status(expected_pid=expected_pid)
    while not latest["connected"] and time.monotonic() < deadline:
        daemon = daemon_status()
        if not daemon.get("running") or int(daemon.get("pid") or 0) != int(expected_pid):
            return {**latest, "reason": "worker_process_exited"}
        time.sleep(0.2)
        latest = worker_connection_status(expected_pid=expected_pid)
    return latest


def tail_logs(lines: int = 100) -> list[str]:
    path = log_path()
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        return stream.readlines()[-max(1, min(lines, 5000)) :]


def _windows_run_value_name() -> str:
    return "MegasMovesWorker"


def _windows_run_key_value() -> str:
    if os.name != "nt":
        return ""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        ) as key:
            value, _ = winreg.QueryValueEx(key, _windows_run_value_name())
            return str(value or "").strip()
    except OSError:
        return ""


def _windows_expected_autostart_command() -> list[str]:
    return [
        "wscript.exe",
        "//B",
        "//Nologo",
        str(state_root() / "start_watchdog.vbs"),
    ]


def _windows_launcher_valid() -> bool:
    launcher = state_root() / "start_watchdog.vbs"
    try:
        text = launcher.read_text(encoding="utf-8").casefold()
    except OSError:
        return False
    if "run-watchdog" not in text and "boot_watchdog.py" not in text:
        return False
    expected_runtime = (
        str(Path(sys.executable).resolve())
        if getattr(sys, "frozen", False)
        else _daemon_python_executable()
    ).replace("/", "\\").casefold()
    return expected_runtime in text


def _windows_run_value_matches(value: str, command: list[str]) -> bool:
    return value.strip().casefold() == subprocess.list2cmdline(command).strip().casefold()


def _windows_run_key_installed() -> bool:
    value = _windows_run_key_value()
    return bool(
        value
        and _windows_run_value_matches(value, _windows_expected_autostart_command())
        and _windows_launcher_valid()
    )


def windows_autostart_status() -> dict[str, Any]:
    if platform.system() != "Windows":
        return {"installed": service_installed(), "healthy": service_installed()}
    value = _windows_run_key_value()
    command_matches = bool(
        value and _windows_run_value_matches(value, _windows_expected_autostart_command())
    )
    launcher_valid = _windows_launcher_valid()
    return {
        "installed": bool(value or _windows_task_installed()),
        "healthy": bool(value and command_matches and launcher_valid),
        "run_key_present": bool(value),
        "command_matches": command_matches,
        "launcher_valid": launcher_valid,
        "watchdog_task_present": _windows_task_query("MegasMovesWatchdog"),
        "legacy_worker_task_present": _windows_task_query("MegasMovesWorker"),
    }


def _windows_install_run_key(command: list[str]) -> None:
    """User-level autostart that never needs admin (real Windows user journey)."""
    import winreg

    value = subprocess.list2cmdline(command)
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(key, _windows_run_value_name(), 0, winreg.REG_SZ, value)


def _windows_uninstall_run_key() -> None:
    if os.name != "nt":
        return
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, _windows_run_value_name())
    except OSError:
        pass


def _windows_task_query(name: str) -> bool:
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        **_hidden_windows_process_kwargs(new_group=False),
    )
    return result.returncode == 0


def _windows_task_installed() -> bool:
    return _windows_task_query("MegasMovesWorker") or _windows_task_query("MegasMovesWatchdog")


def _windows_delete_task(name: str) -> None:
    subprocess.run(
        ["schtasks", "/End", "/TN", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        **_hidden_windows_process_kwargs(new_group=False),
    )
    subprocess.run(
        ["schtasks", "/Delete", "/TN", name, "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        **_hidden_windows_process_kwargs(new_group=False),
    )


def _windows_register_task(task_name: str, command: list[str], user: str) -> bool:
    """Register a current-user logon task with restart-on-failure (no admin).

    Logon-only (no 1-minute spam). The in-process watchdog owns keep-alive;
    minute-interval tasks launching python/wscript caused console popup storms.
    """
    execute = command[0]
    argument = subprocess.list2cmdline(command[1:]) if len(command) > 1 else ""
    ps = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute {_powershell_literal(execute)} -Argument {_powershell_literal(argument)}
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User {_powershell_literal(user)}
$principal = New-ScheduledTaskPrincipal -UserId {_powershell_literal(user)} -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -MultipleInstances IgnoreNew `
  -Hidden
Register-ScheduledTask -TaskName {_powershell_literal(task_name)} -Action $action -Trigger $triggerLogon -Settings $settings -Principal $principal -Force | Out-Null
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        **_hidden_windows_process_kwargs(new_group=False),
    )
    return result.returncode == 0


def service_installed() -> bool:
    system = platform.system()
    if system == "Windows":
        # The Run entry is the non-admin durability floor. A stale task alone
        # must not make status claim that current autostart is healthy.
        return _windows_run_key_installed()
    if system == "Darwin":
        return (Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist").is_file()
    return (Path.home() / ".config" / "systemd" / "user" / "megasworker.service").is_file()


def start_installed_service() -> dict[str, Any]:
    system = platform.system()
    if system == "Windows":
        # Always prefer watchdog durability. Task is a bonus restart layer.
        return ensure_worker_and_watchdog()
    if system == "Darwin":
        target = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
        result = subprocess.run(
            ["launchctl", "kickstart", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
            capture_output=True,
            text=True,
            check=False,
            **_hidden_windows_process_kwargs(new_group=False),
        )
        if result.returncode != 0:
            bootstrap = subprocess.run(
                ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)],
                capture_output=True,
                text=True,
                check=False,
                **_hidden_windows_process_kwargs(new_group=False),
            )
            if bootstrap.returncode != 0:
                raise RuntimeError(bootstrap.stderr.strip() or "macOS could not start the Megas LaunchAgent.")
    else:
        _run_host(
            ["systemctl", "--user", "start", "megasworker.service"],
            check=True,
            timeout=30,
            **_hidden_windows_process_kwargs(new_group=False),
        )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = daemon_status()
        if status["running"]:
            return status
        time.sleep(0.15)
    # Soft success: process may still be connecting.
    status = daemon_status()
    if status.get("running"):
        return status
    raise RuntimeError(
        f"Megas service did not become ready within 30s. "
        f"Run `megas status`. Inspect {log_path()}."
    )


def stop_installed_service() -> None:
    system = platform.system()
    if system == "Windows":
        stop_watchdog()
        for task_name in ("MegasMovesWatchdog", "MegasMovesWorker"):
            subprocess.run(
                ["schtasks", "/End", "/TN", task_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                **_hidden_windows_process_kwargs(new_group=False),
            )
        stop_background()
    elif system == "Darwin":
        target = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            **_hidden_windows_process_kwargs(new_group=False),
        )
    else:
        _run_host(
            ["systemctl", "--user", "stop", "megasworker.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            **_hidden_windows_process_kwargs(new_group=False),
        )


def install_service() -> None:
    system = platform.system()
    if system == "Windows":
        # Real user journey: never require admin/elevation.
        # 1) HKCU Run key -> watchdog via VBS (hidden at logon)
        # 2) Optional Scheduled Task for watchdog (also VBS — never raw python.exe)
        # 3) Start watchdog+worker immediately with CREATE_NO_WINDOW (no reboot)
        # Do NOT register a 1-minute raw-python worker task — that floods console
        # windows. The in-process watchdog already restarts a dead Worker.
        watchdog_cmd = _watchdog_command(for_autostart=True)
        _windows_install_run_key(watchdog_cmd)
        if not _windows_run_key_installed():
            raise RuntimeError("Megas could not verify its Windows autostart command.")
        # End/delete both historical definitions before registering the new
        # hidden watchdog action. ``Register-ScheduledTask -Force`` updates a
        # definition but does not reliably stop an already-running old action.
        for stale_task in ("MegasMovesWorker", "MegasMovesWatchdog"):
            _windows_delete_task(stale_task)
        user = os.environ.get("USERNAME") or Path.home().name
        # Prefer watchdog task (supervises Worker). Fall back silently if blocked.
        if not _windows_register_task("MegasMovesWatchdog", watchdog_cmd, user):
            subprocess.run(
                [
                    "schtasks",
                    "/Create",
                    "/TN",
                    "MegasMovesWatchdog",
                    "/SC",
                    "ONLOGON",
                    "/TR",
                    subprocess.list2cmdline(watchdog_cmd),
                    "/RL",
                    "LIMITED",
                    "/IT",
                    "/F",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                **_hidden_windows_process_kwargs(new_group=False),
            )
        # Always bring watchdog + Worker up now (do not make the user reboot).
        ensure_worker_and_watchdog()
        return
    if system == "Darwin":
        command = _service_command()
        target = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": SERVICE_LABEL,
            "ProgramArguments": command,
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(log_path()),
            "StandardErrorPath": str(log_path()),
        }
        temporary = target.with_suffix(".plist.tmp")
        with temporary.open("wb") as stream:
            plistlib.dump(payload, stream)
        os.replace(temporary, target)
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(target)],
            capture_output=True,
            check=False,
            **_hidden_windows_process_kwargs(new_group=False),
        )
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)],
            capture_output=True,
            text=True,
            check=False,
            **_hidden_windows_process_kwargs(new_group=False),
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "macOS could not install the Megas LaunchAgent.")
        # RunAtLoad should start the agent; kickstart + ensure_running so install
        # does not claim success while the Worker is still dead.
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
            capture_output=True,
            text=True,
            check=False,
            **_hidden_windows_process_kwargs(new_group=False),
        )
        try:
            ensure_running()
        except Exception:
            # LaunchAgent may own the process; give it a moment then require ready.
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if daemon_status().get("running"):
                    return
                time.sleep(0.25)
            raise RuntimeError(
                f"Megas LaunchAgent installed but Worker did not start. Inspect {log_path()}."
            )
        return

    # systemd supervises the Megas watchdog, which in turn owns Worker
    # process health and backend-heartbeat recovery. The generated watchdog boot
    # script pins the exact current interpreter/package/config paths.
    command = _watchdog_command(for_autostart=False)
    target = Path.home() / ".config" / "systemd" / "user" / "megasworker.service"
    target.parent.mkdir(parents=True, exist_ok=True)
    unit = "\n".join(
        [
            "[Unit]",
            "Description=MegasMoves Local Worker watchdog",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            f"ExecStart={shlex.join(command)}",
            "Restart=always",
            "RestartSec=3",
            "KillMode=control-group",
            "TimeoutStopSec=20",
            "Environment=PYTHONUNBUFFERED=1",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    previous_unit = ""
    try:
        previous_unit = target.read_text(encoding="utf-8")
    except OSError:
        pass
    unit_changed = previous_unit != unit
    temporary = target.with_suffix(".service.tmp")
    temporary.write_text(unit, encoding="utf-8")
    os.replace(temporary, target)
    _run_host(
        ["systemctl", "--user", "daemon-reload"],
        check=True,
        timeout=30,
        **_hidden_windows_process_kwargs(new_group=False),
    )
    _run_host(
        ["systemctl", "--user", "enable", "megasworker.service"],
        check=True,
        timeout=30,
        **_hidden_windows_process_kwargs(new_group=False),
    )
    _run_host(
        [
            "systemctl",
            "--user",
            "restart" if unit_changed else "start",
            "megasworker.service",
        ],
        check=True,
        timeout=30,
        **_hidden_windows_process_kwargs(new_group=False),
    )


def uninstall_service() -> None:
    system = platform.system()
    if system == "Windows":
        stop_watchdog()
        stop_background()
        for task_name in ("MegasMovesWatchdog", "MegasMovesWorker"):
            _windows_delete_task(task_name)
        _windows_uninstall_run_key()
        for generated in (
            watchdog_boot_script_path(),
            state_root() / "boot_worker.py",
            state_root() / "start_watchdog.vbs",
            state_root() / "start_worker.vbs",
        ):
            generated.unlink(missing_ok=True)
        return
    if system == "Darwin":
        target = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(target)],
            capture_output=True,
            check=False,
            **_hidden_windows_process_kwargs(new_group=False),
        )
        target.unlink(missing_ok=True)
        return
    target = Path.home() / ".config" / "systemd" / "user" / "megasworker.service"
    _run_host(
        ["systemctl", "--user", "disable", "--now", "megasworker.service"],
        capture_output=True,
        check=False,
        **_hidden_windows_process_kwargs(new_group=False),
    )
    target.unlink(missing_ok=True)
    _run_host(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
        check=False,
        **_hidden_windows_process_kwargs(new_group=False),
    )
