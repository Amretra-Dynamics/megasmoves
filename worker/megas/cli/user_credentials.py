"""Secure storage for user API tokens (Firebase ID tokens / access tokens).

Reuses the same platform backends as worker credentials (DPAPI / keychain /
secret-service / protected file) without coupling to worker identity.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from megas.adapters.base import subprocess_hide_window_kwargs

from .config_store import megas_home

SERVICE_NAME = "io.megasmoves.cli.user"
ACCOUNT_NAME = "default"


def user_credential_path() -> Path:
    return megas_home() / "user.token.json"


def _protect_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
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


def _secret_tool_available() -> bool:
    return bool(
        platform.system() == "Linux"
        and shutil.which("secret-tool")
        and os.getenv("DBUS_SESSION_BUS_ADDRESS")
    )


def _secret_tool_store(token: str) -> bool:
    if not _secret_tool_available():
        return False
    try:
        result = subprocess.run(
            [
                "secret-tool",
                "store",
                "--label=Megas CLI user token",
                "service",
                SERVICE_NAME,
                "account",
                ACCOUNT_NAME,
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


def _secret_tool_load() -> str:
    if not _secret_tool_available():
        return ""
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "service", SERVICE_NAME, "account", ACCOUNT_NAME],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            **subprocess_hide_window_kwargs(),
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _secret_tool_clear() -> None:
    if not _secret_tool_available():
        return
    try:
        subprocess.run(
            ["secret-tool", "clear", "service", SERVICE_NAME, "account", ACCOUNT_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            **subprocess_hide_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _macos_store(token: str) -> bool:
    if platform.system() != "Darwin" or not shutil.which("security"):
        return False
    try:
        result = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-a",
                ACCOUNT_NAME,
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


def _macos_load() -> str:
    if platform.system() != "Darwin" or not shutil.which("security"):
        return ""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                ACCOUNT_NAME,
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


def _macos_delete() -> None:
    if platform.system() != "Darwin" or not shutil.which("security"):
        return
    try:
        subprocess.run(
            [
                "security",
                "delete-generic-password",
                "-a",
                ACCOUNT_NAME,
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


def store_user_token(token: str, *, email: str = "", source: str = "manual") -> None:
    token = (token or "").strip()
    if not token:
        raise ValueError("User token is required.")
    path = user_credential_path()
    if os.name == "nt":
        # Reuse worker DPAPI helpers via lazy import to avoid circular deps
        from megas.credentials import _dpapi_protect

        payload = {
            "version": 1,
            "backend": "windows-dpapi",
            "email": email,
            "source": source,
            "ciphertext": _dpapi_protect(token, ACCOUNT_NAME),
        }
    elif _macos_store(token):
        payload = {"version": 1, "backend": "macos-keychain", "email": email, "source": source}
    elif _secret_tool_store(token):
        payload = {"version": 1, "backend": "secret-service", "email": email, "source": source}
    else:
        payload = {
            "version": 1,
            "backend": "protected-file",
            "email": email,
            "source": source,
            "token": token,
        }
    _atomic_json(path, payload)


def load_user_token() -> str:
    # Environment always wins for CI/automation
    for env_name in ("MEGAS_TOKEN", "MEGAS_API_TOKEN", "MEGAS_ID_TOKEN"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    path = user_credential_path()
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    backend = payload.get("backend")
    try:
        if backend == "windows-dpapi" and os.name == "nt":
            from megas.credentials import _dpapi_unprotect

            return _dpapi_unprotect(str(payload.get("ciphertext") or ""), ACCOUNT_NAME)
        if backend == "macos-keychain":
            return _macos_load()
        if backend == "secret-service":
            return _secret_tool_load()
        if backend == "protected-file":
            return str(payload.get("token") or "")
    except (OSError, ValueError):
        return ""
    return ""


def load_user_meta() -> dict[str, Any]:
    path = user_credential_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        "email": payload.get("email") or "",
        "source": payload.get("source") or "",
        "backend": payload.get("backend") or "",
        "present": bool(load_user_token()),
    }


def delete_user_token() -> None:
    _macos_delete()
    _secret_tool_clear()
    user_credential_path().unlink(missing_ok=True)


def mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}…{token[-4:]}"


__all__ = [
    "delete_user_token",
    "load_user_meta",
    "load_user_token",
    "mask_token",
    "store_user_token",
    "user_credential_path",
]
