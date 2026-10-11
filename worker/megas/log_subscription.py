from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from . import devtool
from .activity_subscription import _ActivityChangeWatcher

MAX_LOG_SUBSCRIPTION_BYTES = 16 * 1024
MAX_LOG_RESPONSE_BYTES = 256 * 1024
MAX_LOG_PROJECT_CHARS = 128
MAX_LOG_SERVICE_CHARS = 128
MAX_LOG_LINES = 500
LOG_HEARTBEAT_MS = 5_000


def _pipe_name(value: str) -> str:
    import re

    name = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name) is None:
        raise ValueError("Raw-log subscription pipe name is invalid")
    return name


def _pipe_token() -> str:
    token = os.environ.get("MEGAS_DESKTOP_IPC_TOKEN", "").strip()
    if not 32 <= len(token) <= 256:
        raise RuntimeError("Raw-log subscription IPC token is missing or invalid")
    return token


def _managed_log_path(project_reference: str, service_name: str) -> Path:
    """Resolve only Megas-owned service log files; never expose arbitrary file watching."""

    project, _manifest = devtool._registered_project(project_reference)
    service = project.service(service_name)
    state = devtool._read_state(project)
    record = state.get("services", {}).get(service.name) or {}
    path_text = str(record.get("log") or "").strip()
    if path_text:
        candidate = Path(path_text).expanduser().resolve()
    else:
        candidate = (
            devtool._logs_dir()
            / devtool._key(project.name)
            / f"{devtool._key(service.name)}.log"
        ).resolve()

    managed_root = devtool._logs_dir().resolve()
    try:
        candidate.relative_to(managed_root)
    except ValueError as exc:
        raise ValueError("Service log path escaped the managed Megas log root") from exc
    return candidate


def _path_cursor(path: Path) -> str:
    try:
        stat = path.stat()
        stamp = f"{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        stamp = "0:0"
    return hashlib.sha256(stamp.encode("ascii", errors="strict")).hexdigest()


def _bounded_result(project: str, service: str, lines: int) -> dict[str, Any]:
    result = devtool.tail_logs(project, service, lines=lines)
    text = str(result.get("text") or "")
    encoded = text.encode("utf-8", errors="replace")
    truncated = False
    if len(encoded) > 192 * 1024:
        encoded = encoded[-192 * 1024 :]
        text = encoded.decode("utf-8", errors="replace")
        text = "[older raw log output truncated to fit the live diagnostic bound]\n" + text
        truncated = True
    return {
        "project": str(result.get("project") or project),
        "service": str(result.get("service") or service),
        "lines": len(text.splitlines()),
        "text": text,
        "truncated": truncated,
    }


def _read_frame(kernel32: Any, handle: Any) -> bytes | None:
    from ctypes import wintypes
    import ctypes

    def read_exact(length: int) -> bytes | None:
        result = bytearray()
        while len(result) < length:
            size = min(length - len(result), 16 * 1024)
            buffer = ctypes.create_string_buffer(size)
            read = wintypes.DWORD()
            if not kernel32.ReadFile(handle, buffer, size, ctypes.byref(read), None):
                error = ctypes.get_last_error()
                if error in {109, 232}:
                    return None
                raise ctypes.WinError(error)
            if read.value <= 0:
                return None
            result.extend(buffer.raw[: read.value])
        return bytes(result)

    header = read_exact(4)
    if header is None:
        return None
    length = int.from_bytes(header, "little", signed=False)
    if length <= 0 or length > MAX_LOG_SUBSCRIPTION_BYTES:
        raise ValueError("Raw-log subscription frame length is invalid")
    return read_exact(length)


def _write_frame(kernel32: Any, handle: Any, payload: dict[str, Any]) -> bool:
    from ctypes import wintypes
    import ctypes

    encoded = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    if not encoded or len(encoded) > MAX_LOG_RESPONSE_BYTES:
        raise ValueError("Raw-log subscription response exceeds the 256 KiB bound")
    framed = len(encoded).to_bytes(4, "little", signed=False) + encoded
    offset = 0
    while offset < len(framed):
        chunk = framed[offset : offset + 64 * 1024]
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(chunk)
        if not kernel32.WriteFile(handle, buffer, len(chunk), ctypes.byref(written), None):
            error = ctypes.get_last_error()
            if error in {109, 232}:
                return False
            raise ctypes.WinError(error)
        if written.value <= 0:
            return False
        offset += written.value
    return True


def _request(raw: bytes, expected_token: str) -> dict[str, Any]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise TypeError("Raw-log subscription request must be an object")
    supplied = str(value.get("auth") or "")
    if not supplied or not hmac.compare_digest(supplied, expected_token):
        raise PermissionError("Raw-log subscription IPC authentication failed")

    project = str(value.get("project") or "").strip()
    service = str(value.get("service") or "").strip()
    if not project or len(project) > MAX_LOG_PROJECT_CHARS:
        raise ValueError("Raw-log project reference is invalid")
    if not service or len(service) > MAX_LOG_SERVICE_CHARS:
        raise ValueError("Raw-log service reference is invalid")
    lines = max(1, min(MAX_LOG_LINES, int(value.get("lines") or MAX_LOG_LINES)))
    return {"project": project, "service": service, "lines": lines}


def _serve_connection(kernel32: Any, handle: Any, token: str) -> None:
    raw = _read_frame(kernel32, handle)
    if raw is None:
        return
    try:
        request = _request(raw, token)
        path = _managed_log_path(request["project"], request["service"])
        path.parent.mkdir(parents=True, exist_ok=True)

        # Register the directory change notification before taking the initial
        # snapshot so a write cannot land in the watch/snapshot gap.
        with _ActivityChangeWatcher(path.parent) as watcher:
            cursor = _path_cursor(path)
            result = _bounded_result(request["project"], request["service"], request["lines"])
            if not _write_frame(
                kernel32,
                handle,
                {"ok": True, "initial": True, "changed": True, "cursor": cursor, "result": result},
            ):
                return

            last_cursor = cursor
            while True:
                watcher.wait(LOG_HEARTBEAT_MS)
                cursor = _path_cursor(path)
                if cursor == last_cursor:
                    if not _write_frame(
                        kernel32,
                        handle,
                        {"ok": True, "initial": False, "changed": False, "cursor": cursor},
                    ):
                        return
                    continue

                result = _bounded_result(request["project"], request["service"], request["lines"])
                last_cursor = cursor
                if not _write_frame(
                    kernel32,
                    handle,
                    {"ok": True, "initial": False, "changed": True, "cursor": cursor, "result": result},
                ):
                    return
    except Exception as exc:
        # The caller gets a bounded failure class but never a path-bearing raw
        # exception message. Detailed diagnostics remain inside the trusted core.
        _write_frame(
            kernel32,
            handle,
            {
                "ok": False,
                "error": {
                    "type": type(exc).__name__,
                    "message": "Raw-log subscription failed.",
                },
            },
        )


def serve_log_pipe(pipe_name: str) -> int:
    """Serve sequential authenticated raw-log subscriptions from the core process."""

    if os.name != "nt":
        raise RuntimeError("Raw-log subscription pipe is available only on Windows")

    import ctypes
    from ctypes import wintypes

    name = _pipe_name(pipe_name)
    token = _pipe_token()
    full_name = rf"\\.\pipe\{name}"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    kernel32.ConnectNamedPipe.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    pipe_access_duplex = 0x00000003
    pipe_type_byte = 0x00000000
    pipe_readmode_byte = 0x00000000
    pipe_wait = 0x00000000
    pipe_reject_remote_clients = 0x00000008
    invalid_handle_value = ctypes.c_void_p(-1).value

    while True:
        handle = kernel32.CreateNamedPipeW(
            full_name,
            pipe_access_duplex,
            pipe_type_byte | pipe_readmode_byte | pipe_wait | pipe_reject_remote_clients,
            1,
            MAX_LOG_RESPONSE_BYTES + 4,
            MAX_LOG_SUBSCRIPTION_BYTES + 4,
            0,
            None,
        )
        if handle == invalid_handle_value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            connected = kernel32.ConnectNamedPipe(handle, None)
            if not connected:
                error = ctypes.get_last_error()
                if error != 535:
                    raise ctypes.WinError(error)
            _serve_connection(kernel32, handle, token)
        finally:
            try:
                kernel32.DisconnectNamedPipe(handle)
            finally:
                kernel32.CloseHandle(handle)


__all__ = ["serve_log_pipe"]
