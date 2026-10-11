from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

from megas.adapters.base import subprocess_hide_window_kwargs


SERVICE_NAME = "io.megasmoves.megasworker"


def credential_path(config_path: Path) -> Path:
    return config_path.with_name("worker.token.json")


def _protect_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _atomic_json(path: Path, payload: dict) -> None:
    _protect_directory(path.parent)
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


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _dpapi_protect(value: str, worker_id: str) -> str:
    value_blob, value_buffer = _blob(value.encode("utf-8"))
    entropy_blob, entropy_buffer = _blob(worker_id.encode("utf-8"))
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    protect = crypt32.CryptProtectData
    protect.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    protect.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    success = protect(
        ctypes.byref(value_blob),
        "Megas Worker credential",
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(output),
    )
    _ = (value_buffer, entropy_buffer)
    if not success:
        raise OSError(ctypes.get_last_error(), "Windows DPAPI could not protect the worker credential.")
    try:
        protected = ctypes.string_at(output.pbData, output.cbData)
        return base64.b64encode(protected).decode("ascii")
    finally:
        local_free(ctypes.cast(output.pbData, ctypes.c_void_p))


def _dpapi_unprotect(value: str, worker_id: str) -> str:
    protected = base64.b64decode(value.encode("ascii"), validate=True)
    value_blob, value_buffer = _blob(protected)
    entropy_blob, entropy_buffer = _blob(worker_id.encode("utf-8"))
    output = _DataBlob()
    description = ctypes.c_wchar_p()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    unprotect.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    success = unprotect(
        ctypes.byref(value_blob),
        ctypes.byref(description),
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(output),
    )
    _ = (value_buffer, entropy_buffer)
    if not success:
        raise OSError(ctypes.get_last_error(), "Windows DPAPI could not unlock the worker credential.")
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        if description:
            local_free(ctypes.cast(description, ctypes.c_void_p))
        local_free(ctypes.cast(output.pbData, ctypes.c_void_p))


def _secret_tool_available() -> bool:
    return bool(
        platform.system() == "Linux"
        and shutil.which("secret-tool")
        and os.getenv("DBUS_SESSION_BUS_ADDRESS")
    )


def _secret_tool_store(worker_id: str, token: str) -> bool:
    if not _secret_tool_available():
        return False
    try:
        result = subprocess.run(
            [
                "secret-tool",
                "store",
                "--label=Megas Worker credential",
                "service",
                SERVICE_NAME,
                "worker_id",
                worker_id,
            ],
            input=token,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            **subprocess_hide_window_kwargs(),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _secret_tool_load(worker_id: str) -> str:
    if not _secret_tool_available():
        return ""
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "service", SERVICE_NAME, "worker_id", worker_id],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            **subprocess_hide_window_kwargs(),
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _macos_keychain_store(worker_id: str, token: str) -> bool:
    if platform.system() != "Darwin" or not shutil.which("security"):
        return False
    try:
        result = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-a",
                worker_id,
                "-s",
                SERVICE_NAME,
                "-w",
                token,
                "-U",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            **subprocess_hide_window_kwargs(),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _macos_keychain_load(worker_id: str) -> str:
    if platform.system() != "Darwin" or not shutil.which("security"):
        return ""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                worker_id,
                "-s",
                SERVICE_NAME,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            **subprocess_hide_window_kwargs(),
        )
        return result.stdout.rstrip("\r\n") if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def store_worker_token(worker_id: str, token: str, config_path: Path) -> None:
    if not worker_id or not token:
        raise ValueError("Worker identity and token are required.")
    path = credential_path(config_path)
    if os.name == "nt":
        payload = {
            "version": 1,
            "backend": "windows-dpapi",
            "worker_id": worker_id,
            "ciphertext": _dpapi_protect(token, worker_id),
        }
        _atomic_json(path, payload)
        return

    # Non-Windows: ALWAYS keep a protected-file copy so:
    # - website reinstall can detect pairing without keychain
    # - LaunchAgent/systemd can load the token without interactive keychain unlock
    # Keychain / secret-service remain optional secondary stores.
    backend = "protected-file"
    if platform.system() == "Darwin" and _macos_keychain_store(worker_id, token):
        backend = "macos-keychain+file"
    elif _secret_tool_store(worker_id, token):
        backend = "secret-service+file"
    payload = {
        "version": 1,
        "backend": backend,
        "worker_id": worker_id,
        "token": token,
    }
    _atomic_json(path, payload)


def load_worker_token(worker_id: str, config_path: Path) -> str:
    path = credential_path(config_path)
    if not worker_id or not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if payload.get("worker_id") != worker_id:
        return ""
    backend = str(payload.get("backend") or "")
    try:
        if backend == "windows-dpapi" and os.name == "nt":
            return _dpapi_unprotect(str(payload.get("ciphertext") or ""), worker_id)
        if backend.startswith("macos-keychain"):
            token = _macos_keychain_load(worker_id)
            if token:
                return token
            # Fall back to on-disk copy (launchd / reinstall).
            return str(payload.get("token") or "")
        if backend.startswith("secret-service"):
            token = _secret_tool_load(worker_id)
            if token:
                return token
            return str(payload.get("token") or "")
        if backend == "protected-file" or os.name != "nt":
            # Legacy "macos-keychain" without file also lands here if empty keychain.
            file_token = str(payload.get("token") or "")
            if file_token:
                return file_token
            if backend == "macos-keychain":
                return _macos_keychain_load(worker_id)
            if backend == "secret-service":
                return _secret_tool_load(worker_id)
    except (OSError, ValueError):
        return ""
    return ""


def delete_worker_token(worker_id: str, config_path: Path) -> None:
    if worker_id and platform.system() == "Darwin" and shutil.which("security"):
        try:
            subprocess.run(
                [
                    "security",
                    "delete-generic-password",
                    "-a",
                    worker_id,
                    "-s",
                    SERVICE_NAME,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                **subprocess_hide_window_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    if worker_id and _secret_tool_available():
        try:
            subprocess.run(
                ["secret-tool", "clear", "service", SERVICE_NAME, "worker_id", worker_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                **subprocess_hide_window_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    credential_path(config_path).unlink(missing_ok=True)
