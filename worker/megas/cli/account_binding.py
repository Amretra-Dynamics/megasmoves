"""Persist the Megas account that owns this machine after website device approval.

Device pairing already binds the worker to a user email on the backend.
That email is the product account for this PC — not an optional add-on.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from megas.cli.config_store import megas_home


def account_path() -> Path:
    return megas_home() / "account.json"


def store_account_binding(email: str, *, source: str = "device_pairing", worker_id: str = "") -> None:
    email = str(email or "").strip().lower()
    if not email or "@" not in email:
        return
    root = megas_home()
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    payload = {
        "email": email,
        "source": source,
        "worker_id": worker_id or "",
        "bound_at": datetime.now(timezone.utc).isoformat(),
    }
    path = account_path()
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_account_binding() -> dict[str, Any]:
    path = account_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    email = str(data.get("email") or "").strip().lower()
    if not email:
        return {}
    return {
        "email": email,
        "source": str(data.get("source") or ""),
        "worker_id": str(data.get("worker_id") or ""),
        "bound_at": str(data.get("bound_at") or ""),
    }


def bound_account_email() -> str:
    return str(load_account_binding().get("email") or "")


def clear_account_binding() -> None:
    account_path().unlink(missing_ok=True)


__all__ = [
    "account_path",
    "bound_account_email",
    "clear_account_binding",
    "load_account_binding",
    "store_account_binding",
]
