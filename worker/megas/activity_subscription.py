from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from .mcp.activity import ACTIVITY_SOURCES, MAX_ACTIVITY_CALL_FILES, build_activity_feed

MAX_ACTIVITY_SUBSCRIPTION_BYTES = 16 * 1024
MAX_ACTIVITY_RESPONSE_BYTES = 256 * 1024
MAX_ACTIVITY_CURSOR_CHARS = 128
MAX_ACTIVITY_QUERY_CHARS = 512
MAX_ACTIVITY_ROWS = 500
ACTIVITY_HEARTBEAT_MS = 5_000


def _pipe_name(value: str) -> str:
    import re

    name = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name) is None:
        raise ValueError("Activity subscription pipe name is invalid")
    return name


def _pipe_token() -> str:
    token = os.environ.get("MEGAS_DESKTOP_IPC_TOKEN", "").strip()
    if not 32 <= len(token) <= 256:
        raise RuntimeError("Activity subscription IPC token is missing or invalid")
    return token


def _path_stamp(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return "0:0"
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _recent_call_stamps(calls_dir: Path) -> list[str]:
    candidates: list[tuple[int, str, int]] = []
    try:
        paths = calls_dir.glob("call_*.json")
    except OSError:
        return []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file():
            candidates.append((stat.st_mtime_ns, path.name, stat.st_size))
    candidates.sort()
    return [
        f"{mtime}:{name}:{size}"
        for mtime, name, size in candidates[-MAX_ACTIVITY_CALL_FILES:]
    ]


def activity_cursor(bridge: Any) -> str:
    """Durable fingerprint over the bounded authoritative Activity source set."""

    components = [
        _path_stamp(bridge.core.store.history_path),
        _path_stamp(bridge.core.timeline.events_path),
        *_recent_call_stamps(bridge.core.timeline.calls_dir),
    ]
    payload = "|".join(components).encode("utf-8", errors="strict")
    return hashlib.sha256(payload).hexdigest()


def _visible_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Send only the redacted fields rendered/exported by WinUI, never raw details."""

    keys = (
        "id",
        "at",
        "source",
        "severity",
        "event",
        "operation_id",
        "project",
        "session_id",
        "tool_call_id",
        "server",
        "tool",
        "duration_ms",
        "message",
    )
    return [{key: row.get(key) for key in keys} for row in rows]


def _stable_activity_snapshot(
    bridge: Any,
    *,
    limit: int,
    source: str,
    errors_only: bool,
    query: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Avoid advancing a cursor past an event that arrived during feed assembly."""

    rows: list[dict[str, Any]] = []
    after = activity_cursor(bridge)
    for _attempt in range(3):
        before = activity_cursor(bridge)
        rows = build_activity_feed(
            bridge.core.store,
            bridge.core.timeline,
            limit=limit,
            sources={source} if source else None,
            errors_only=errors_only,
            query=query,
        )
        after = activity_cursor(bridge)
        if before == after:
            break
    return after, _visible_rows(rows)


def _read_frame(kernel32: Any, handle: Any) -> bytes | None:
    from ctypes import wintypes

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
    if length <= 0 or length > MAX_ACTIVITY_SUBSCRIPTION_BYTES:
        raise ValueError("Activity subscription frame length is invalid")
    return read_exact(length)


def _encode_bounded_payload(payload: dict[str, Any]) -> bytes:
    current = dict(payload)
    rows = list(current.get("rows") or [])
    while True:
        current["rows"] = rows
        encoded = json.dumps(current, separators=(",", ":"), default=str).encode("utf-8")
        if encoded and len(encoded) <= MAX_ACTIVITY_RESPONSE_BYTES:
            return encoded
        if not rows:
            raise ValueError("Activity subscription response exceeds the response bound")
        rows = rows[: max(0, len(rows) // 2)]
        current["truncated"] = True


def _write_frame(kernel32: Any, handle: Any, payload: dict[str, Any]) -> bool:
    from ctypes import wintypes

    encoded = _encode_bounded_payload(payload)
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


def _subscription_request(raw: bytes, expected_token: str) -> dict[str, Any]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise TypeError("Activity subscription request must be an object")
    supplied = str(value.get("auth") or "")
    if not supplied or not hmac.compare_digest(supplied, expected_token):
        raise PermissionError("Activity subscription IPC authentication failed")

    cursor = str(value.get("cursor") or "").strip()
    if len(cursor) > MAX_ACTIVITY_CURSOR_CHARS:
        raise ValueError("Activity cursor exceeds the bound")
    source = str(value.get("source") or "").strip()
    if source and source not in ACTIVITY_SOURCES:
        raise ValueError("Unknown Activity source")
    query = str(value.get("query") or "").strip()
    if len(query) > MAX_ACTIVITY_QUERY_CHARS:
        raise ValueError("Activity query exceeds the bound")
    limit = max(1, min(MAX_ACTIVITY_ROWS, int(value.get("limit") or 300)))
    return {
        "cursor": cursor,
        "source": source,
        "errors_only": bool(value.get("errors_only", False)),
        "query": query,
        "limit": limit,
    }


class _ActivityChangeWatcher:
    """One persistent Windows change notification removes cursor/watch race windows."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._kernel32: Any | None = None
        self._handle: Any | None = None
        directory.mkdir(parents=True, exist_ok=True)

        if os.name != "nt":
            return

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.FindFirstChangeNotificationW.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_uint32]
        kernel32.FindFirstChangeNotificationW.restype = ctypes.c_void_p
        kernel32.FindNextChangeNotification.argtypes = [ctypes.c_void_p]
        kernel32.FindNextChangeNotification.restype = ctypes.c_int
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.FindCloseChangeNotification.argtypes = [ctypes.c_void_p]
        kernel32.FindCloseChangeNotification.restype = ctypes.c_int

        flags = 0x00000001 | 0x00000008 | 0x00000010
        handle = kernel32.FindFirstChangeNotificationW(str(directory), True, flags)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self._kernel32 = kernel32
        self._handle = handle

    def wait(self, timeout_ms: int = ACTIVITY_HEARTBEAT_MS) -> bool:
        if self._kernel32 is None or self._handle is None:
            import threading

            threading.Event().wait(max(0.05, timeout_ms / 1000.0))
            return False

        result = self._kernel32.WaitForSingleObject(
            self._handle,
            max(1, min(timeout_ms, ACTIVITY_HEARTBEAT_MS)),
        )
        if result == 0x00000000:
            if not self._kernel32.FindNextChangeNotification(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            return True
        if result == 0x00000102:
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._kernel32 is not None and self._handle is not None:
            self._kernel32.FindCloseChangeNotification(self._handle)
            self._handle = None

    def __enter__(self) -> "_ActivityChangeWatcher":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def _serve_subscription_connection(kernel32: Any, handle: Any, bridge: Any, token: str) -> None:
    raw = _read_frame(kernel32, handle)
    if raw is None:
        return
    try:
        request = _subscription_request(raw, token)
        # Register the directory notification before taking the initial snapshot.
        # Any write racing the snapshot therefore leaves the watcher signaled.
        with _ActivityChangeWatcher(bridge.core.store.mcp_dir) as watcher:
            cursor, rows = _stable_activity_snapshot(
                bridge,
                limit=request["limit"],
                source=request["source"],
                errors_only=request["errors_only"],
                query=request["query"],
            )
            if not _write_frame(
                kernel32,
                handle,
                {
                    "ok": True,
                    "initial": True,
                    "changed": cursor != request["cursor"],
                    "cursor": cursor,
                    "rows": rows,
                },
            ):
                return

            last_cursor = cursor
            while True:
                watcher.wait(ACTIVITY_HEARTBEAT_MS)
                cursor = activity_cursor(bridge)
                if cursor == last_cursor:
                    if not _write_frame(
                        kernel32,
                        handle,
                        {"ok": True, "initial": False, "changed": False, "cursor": cursor, "rows": []},
                    ):
                        return
                    continue

                cursor, rows = _stable_activity_snapshot(
                    bridge,
                    limit=request["limit"],
                    source=request["source"],
                    errors_only=request["errors_only"],
                    query=request["query"],
                )
                last_cursor = cursor
                if not _write_frame(
                    kernel32,
                    handle,
                    {
                        "ok": True,
                        "initial": False,
                        "changed": True,
                        "cursor": cursor,
                        "rows": rows,
                    },
                ):
                    return
    except Exception as exc:
        _write_frame(
            kernel32,
            handle,
            {
                "ok": False,
                "error": {
                    "type": type(exc).__name__,
                    "message": "Activity subscription failed.",
                },
            },
        )


def serve_activity_pipe(pipe_name: str, bridge: Any) -> int:
    """Serve sequential authenticated Activity subscriptions from the shared core."""

    if os.name != "nt":
        raise RuntimeError("Activity subscription pipe is available only on Windows")
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
            MAX_ACTIVITY_RESPONSE_BYTES + 4,
            MAX_ACTIVITY_SUBSCRIPTION_BYTES + 4,
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
            _serve_subscription_connection(kernel32, handle, bridge, token)
        finally:
            try:
                kernel32.DisconnectNamedPipe(handle)
            finally:
                kernel32.CloseHandle(handle)
