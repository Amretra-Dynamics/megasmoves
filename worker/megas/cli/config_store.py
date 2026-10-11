"""Configuration precedence and named profiles for the Megas CLI.

Precedence (highest wins):
  1. command flags
  2. environment variables
  3. named profile
  4. user config file
  5. project config file (.megas/config.json)
  6. defaults
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_API_URL = "https://megasmoves.io"
DEFAULT_TIMEOUT = 30.0
DEFAULT_OUTPUT = "table"


def megas_home() -> Path:
    root = os.getenv("MEGAS_HOME")
    if root:
        return Path(root).expanduser()
    return Path.home() / ".megasmoves"


def user_config_path() -> Path:
    return megas_home() / "config.json"


def project_config_path(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        candidate = directory / ".megasmoves" / "config.json"
        if candidate.is_file():
            return candidate
        # stop at filesystem root naturally via parents
    return None


def profile_path(name: str) -> Path:
    return megas_home() / "profiles" / f"{name}.json"


@dataclass
class CliSettings:
    api_url: str = DEFAULT_API_URL
    timeout: float = DEFAULT_TIMEOUT
    output: str = DEFAULT_OUTPUT
    profile: str = "default"
    color: bool = True
    non_interactive: bool = False
    # Free-form overrides for advanced keys
    extras: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        if hasattr(self, key) and key != "extras":
            return getattr(self, key)
        return self.extras.get(key, default)

    def set(self, key: str, value: Any) -> None:
        if key in {"api_url", "timeout", "output", "profile", "color", "non_interactive"}:
            if key == "timeout":
                setattr(self, key, float(value))
            elif key == "color":
                setattr(self, key, _as_bool(value))
            elif key == "non_interactive":
                setattr(self, key, _as_bool(value))
            else:
                setattr(self, key, value)
        else:
            self.extras[key] = value

    def unset(self, key: str) -> bool:
        if key in {"api_url", "timeout", "output", "profile", "color", "non_interactive"}:
            defaults = CliSettings()
            setattr(self, key, getattr(defaults, key))
            return True
        return self.extras.pop(key, None) is not None

    def to_public_dict(self) -> dict[str, Any]:
        data = {
            "api_url": self.api_url,
            "timeout": self.timeout,
            "output": self.output,
            "profile": self.profile,
            "color": self.color,
            "non_interactive": self.non_interactive,
        }
        data.update(self.extras)
        return data


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _apply_dict(settings: CliSettings, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if key == "backend_url" and "api_url" not in data:
            settings.api_url = str(value).rstrip("/")
            continue
        if key in {"api_url", "timeout", "output", "profile", "color", "non_interactive"}:
            settings.set(key, value)
        else:
            settings.extras[key] = value


def load_settings(
    *,
    profile: str | None = None,
    flag_overrides: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> CliSettings:
    settings = CliSettings()
    project = project_config_path(cwd)
    if project:
        _apply_dict(settings, _read_json(project))
    user_path = user_config_path()
    if user_path.is_file():
        _apply_dict(settings, _read_json(user_path))

    profile_name = profile or settings.profile or "default"
    env_map = env if env is not None else os.environ
    profile_name = env_map.get("MEGAS_PROFILE", profile_name)
    settings.profile = profile_name
    ppath = profile_path(profile_name)
    if ppath.is_file():
        _apply_dict(settings, _read_json(ppath))

    # Environment variables
    if env_map.get("MEGAS_API_URL"):
        settings.api_url = env_map["MEGAS_API_URL"].rstrip("/")
    elif env_map.get("MEGAS_BACKEND_URL"):
        settings.api_url = env_map["MEGAS_BACKEND_URL"].rstrip("/")
    if env_map.get("MEGAS_TIMEOUT"):
        try:
            settings.timeout = float(env_map["MEGAS_TIMEOUT"])
        except ValueError:
            pass
    if env_map.get("MEGAS_OUTPUT"):
        settings.output = env_map["MEGAS_OUTPUT"]
    if env_map.get("MEGAS_NO_COLOR") or env_map.get("NO_COLOR"):
        settings.color = False
    if env_map.get("MEGAS_NON_INTERACTIVE", "").lower() in {"1", "true", "yes"}:
        settings.non_interactive = True
    if env_map.get("CI", "").lower() in {"1", "true", "yes"}:
        settings.non_interactive = True

    if flag_overrides:
        cleaned = {k: v for k, v in flag_overrides.items() if v is not None}
        _apply_dict(settings, cleaned)
    settings.api_url = settings.api_url.rstrip("/")
    return settings


def save_user_settings(settings: CliSettings, path: Path | None = None) -> Path:
    target = path or user_config_path()
    payload = settings.to_public_dict()
    # profile name is meta; keep it
    _write_json(target, payload)
    return target


def save_profile(name: str, settings: CliSettings) -> Path:
    target = profile_path(name)
    payload = settings.to_public_dict()
    payload["profile"] = name
    _write_json(target, payload)
    return target


def list_config_paths() -> dict[str, str]:
    project = project_config_path()
    return {
        "megas_home": str(megas_home()),
        "user_config": str(user_config_path()),
        "project_config": str(project) if project else "",
        "profiles_dir": str(megas_home() / "profiles"),
        "worker_config": str(megas_home() / "worker.json"),
        "user_token": str(megas_home() / "user.token.json"),
    }


def flatten_for_list(settings: CliSettings) -> list[dict[str, Any]]:
    rows = []
    for key, value in settings.to_public_dict().items():
        rows.append({"key": key, "value": value})
    return rows


KNOWN_KEYS = frozenset(
    {
        "api_url",
        "backend_url",
        "timeout",
        "output",
        "profile",
        "color",
        "non_interactive",
    }
)


__all__ = [
    "CliSettings",
    "DEFAULT_API_URL",
    "DEFAULT_OUTPUT",
    "DEFAULT_TIMEOUT",
    "KNOWN_KEYS",
    "flatten_for_list",
    "list_config_paths",
    "load_settings",
    "megas_home",
    "profile_path",
    "project_config_path",
    "save_profile",
    "save_user_settings",
    "user_config_path",
]
