from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .credentials import load_worker_token, store_worker_token


CANONICAL_BACKEND_URL = "https://megasmoves.io"
LEGACY_BACKEND_HOSTS = {"megas.amretra.com", "account.amretra.com"}

# Windows auto hostnames are not product labels for Connected Agents.
_AUTO_HOST_RE = re.compile(
    r"^(laptop|desktop|win|pc|macbook|imac|mac)-[a-z0-9]+$",
    re.IGNORECASE,
)


def default_owner_label() -> str:
    """Human label for Connected Agents: OS login name, not random PC hostname.

    Product display is ``{label}'s Codex CLI``. Users expect their Windows/macOS
    account name (e.g. hoids), not COMPUTERNAME (e.g. LAPTOP-E88LAUSQ).
    """
    for key in ("MEGAS_MACHINE_NAME", "USERNAME", "USER", "LOGNAME"):
        raw = str(os.environ.get(key) or "").strip()
        if not raw:
            continue
        if "\\" in raw:  # DOMAIN\\user
            raw = raw.rsplit("\\", 1)[-1].strip()
        if "@" in raw:  # user@realm
            raw = raw.split("@", 1)[0].strip()
        if raw and raw.lower() not in {
            "system",
            "root",
            "administrator",
            "admin",
            "local service",
            "network service",
        }:
            return raw
    host = str(socket.gethostname() or "").strip()
    return host or "PC"


def is_auto_machine_hostname(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if _AUTO_HOST_RE.match(text):
        return True
    # Generic product defaults that should not win as owner labels.
    return text.lower() in {
        "megas local worker",
        "local worker",
        "developer machine",
        "worker",
        "pc",
    }


def resolve_machine_label(configured: str | None = None) -> str:
    """Prefer configured friendly name; replace auto hostnames with OS user."""
    current = str(configured or "").strip()
    preferred = default_owner_label()
    if current and not is_auto_machine_hostname(current):
        return current
    return preferred or current or "PC"


def canonical_state_root() -> Path:
    return Path.home() / ".megasmoves"


def legacy_state_root() -> Path:
    return Path.home() / ".megas"


def default_config_path() -> Path:
    root = os.getenv("MEGAS_HOME")
    if root:
        return Path(root).expanduser() / "worker.json"
    return canonical_state_root() / "worker.json"


def _migrate_legacy_default_config() -> Path:
    """Copy a paired legacy install into the canonical state directory.

    The legacy directory is retained for rollback. ``load_config`` decrypts the
    existing protected credential in memory and ``save_config`` immediately
    writes it through the current credential backend, so a token is never added
    to the new ``worker.json``.
    """
    canonical = default_config_path()
    if os.getenv("MEGAS_HOME") or canonical.exists():
        return canonical
    legacy = legacy_state_root() / "worker.json"
    if not legacy.is_file():
        return canonical
    legacy_config = load_config(legacy)
    if not legacy_config.is_paired:
        # Do not create a canonical half-migration that would hide a recoverable
        # legacy credential from a later repair attempt.
        return canonical
    save_config(legacy_config, canonical)
    return canonical


@dataclass
class WorkerConfig:
    backend_url: str = CANONICAL_BACKEND_URL
    worker_id: str = ""
    worker_token: str = ""
    machine_id: str = ""
    machine_name: str = ""
    worker_name: str = "Megas Local Worker"
    # Megas account that approved this machine on the website (product owner).
    # This is the real product identity — not optional "worker-only" setup.
    account_email: str = ""
    allowed_folders: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=lambda: ["heartbeat", "session_events", "evidence"])
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def is_paired(self) -> bool:
        return bool(self.worker_id and self.worker_token)

    @property
    def is_account_bound(self) -> bool:
        return bool(str(self.account_email or "").strip())

    @property
    def rest_base_url(self) -> str:
        override = os.getenv("MEGAS_BACKEND_URL", "").strip() or os.getenv("MEGAS_REST_BASE_URL", "").strip()
        if override:
            return override.rstrip("/")
        return self.backend_url.rstrip("/")

    @property
    def ws_base_url(self) -> str:
        base = self.rest_base_url
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):]
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):]
        return "wss://" + base


def get_or_create_persistent_machine_id() -> str:
    """Persistent hardware/OS installation UUID stored independently of credentials/pairing."""
    path = canonical_state_root() / "machine_id"
    if path.is_file():
        try:
            val = path.read_text(encoding="utf-8").strip()
            if val and len(val) >= 8:
                return val
        except Exception:
            pass
    legacy_path = legacy_state_root() / "machine_id"
    if legacy_path.is_file():
        try:
            val = legacy_path.read_text(encoding="utf-8").strip()
            if val and len(val) >= 8:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(val, encoding="utf-8")
                return val
        except Exception:
            pass
    import uuid
    new_id = f"mach_{uuid.uuid4().hex}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id, encoding="utf-8")
    except Exception:
        pass
    return new_id


def load_config(path: Path | None = None) -> WorkerConfig:
    config_path = path or _migrate_legacy_default_config()
    if not config_path.exists():
        cfg = WorkerConfig()
        cfg.machine_id = get_or_create_persistent_machine_id()
        return cfg
    try:
        data: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception:
        try:
            backup_path = config_path.with_suffix(".json.corrupt")
            if config_path.exists():
                if backup_path.exists():
                    backup_path.unlink()
                config_path.rename(backup_path)
        except Exception:
            pass
        cfg = WorkerConfig()
        cfg.machine_id = get_or_create_persistent_machine_id()
        return cfg

    legacy_token = str(data.pop("worker_token", "") or "")
    allowed = {field.name for field in WorkerConfig.__dataclass_fields__.values()}
    config = WorkerConfig(**{key: value for key, value in data.items() if key in allowed})
    if not config.machine_id:
        config.machine_id = get_or_create_persistent_machine_id()
    backend_migrated = urlparse(config.backend_url).hostname in LEGACY_BACKEND_HOSTS
    env_backend = os.getenv("MEGAS_BACKEND_URL", "").strip() or os.getenv("MEGAS_REST_BASE_URL", "").strip()
    if env_backend:
        config.backend_url = env_backend
    elif backend_migrated:
        config.backend_url = CANONICAL_BACKEND_URL
    config.worker_token = load_worker_token(config.worker_id, config_path) or legacy_token
    healed_label = resolve_machine_label(config.machine_name)
    label_healed = bool(healed_label and healed_label != str(config.machine_name or "").strip())
    if label_healed:
        config.machine_name = healed_label
    if backend_migrated or (legacy_token and config.worker_id) or label_healed:
        save_config(config, config_path)
    return config


def save_config(config: WorkerConfig, path: Path | None = None) -> Path:
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(config_path.parent, 0o700)
    except OSError:
        pass
    if config.worker_id and config.worker_token:
        store_worker_token(config.worker_id, config.worker_token, config_path)
    payload = asdict(config)
    payload.pop("worker_token", None)
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, config_path)
    finally:
        temporary.unlink(missing_ok=True)
    return config_path

