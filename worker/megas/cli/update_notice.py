"""Codex-style update notice for every human `megas` invocation.

When production has a newer CLI than this install, print a clear stderr banner
with the exact update and reinstall commands for this platform.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from megas.updater import current_version, platform_id

# Cache version checks so normal CLI use stays snappy.
_CACHE_TTL_SEC = 6 * 60 * 60
_DEFAULT_API = "https://megasmoves.io"


def _cache_path() -> Path:
    home = os.getenv("MEGAS_HOME", "").strip()
    root = Path(home).expanduser() if home else Path.home() / ".megasmoves"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        root = Path.home() / ".megasmoves"
        root.mkdir(parents=True, exist_ok=True)
    return root / "update-check.json"


def _parse_version(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(text or "").strip().lstrip("vV").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def _is_newer(remote: str, local: str) -> bool:
    r = _parse_version(remote)
    l = _parse_version(local)
    # Pad for comparison
    n = max(len(r), len(l))
    r = r + (0,) * (n - len(r))
    l = l + (0,) * (n - len(l))
    return r > l


def _platform_commands(plat: str) -> tuple[str, str]:
    # Product maintenance is always CLI once the binary exists.
    # Website scripts are only for first-time setup (fetched from megasmoves.io),
    # never presented as ``megas install`` (that installs coding agents).
    update_cmd = "megas update"
    reinstall = "megas reinstall"
    return update_cmd, reinstall


def _fetch_latest(api_base: str, plat: str, *, timeout: float = 2.5) -> dict[str, Any] | None:
    url = f"{api_base.rstrip('/')}/api/v1/cli/version?platform={plat}"
    try:
        req = Request(url, headers={"User-Agent": "MegasCLI-UpdateCheck/1.0", "Accept": "application/json"})
        with urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) >= 400:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None


def check_for_update(*, api_base: str | None = None, force: bool = False) -> dict[str, Any] | None:
    """Return notice payload when a newer production release exists."""
    if os.getenv("MEGAS_SKIP_UPDATE_CHECK", "").strip().lower() in {"1", "true", "yes", "on"}:
        return None
    try:
        plat = platform_id()
    except Exception:
        return None
    local = current_version()
    base = (api_base or os.getenv("MEGAS_API_URL") or os.getenv("MEGAS_BACKEND_URL") or _DEFAULT_API).rstrip("/")
    now = time.time()
    cache_path = _cache_path()
    cached: dict[str, Any] = {}
    if not force and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = {}
    remote = ""
    if (
        not force
        and cached.get("platform") == plat
        and cached.get("local_version") == local
        and isinstance(cached.get("checked_at"), (int, float))
        and now - float(cached["checked_at"]) < _CACHE_TTL_SEC
        and cached.get("remote_version")
    ):
        remote = str(cached["remote_version"])
    else:
        payload = _fetch_latest(base, plat)
        if not payload:
            return None
        remote = str(payload.get("version") or payload.get("release_version") or "").strip()
        if not remote:
            return None
        try:
            cache_path.write_text(
                json.dumps(
                    {
                        "checked_at": now,
                        "platform": plat,
                        "local_version": local,
                        "remote_version": remote,
                        "source_sha": payload.get("source_sha"),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    if not _is_newer(remote, local):
        return None

    update_cmd, reinstall_cmd = _platform_commands(plat)
    return {
        "local_version": local,
        "remote_version": remote,
        "platform": plat,
        "update_command": update_cmd,
        "reinstall_command": reinstall_cmd,
    }


def format_update_notice(notice: dict[str, Any], *, color: bool = True) -> str:
    local = notice.get("local_version") or "?"
    remote = notice.get("remote_version") or "?"
    update_cmd = notice.get("update_command") or "megas update"
    reinstall_cmd = notice.get("reinstall_command") or "megas reinstall"
    if color and sys.stderr.isatty() and not os.getenv("NO_COLOR"):
        bold = "\033[1m"
        yellow = "\033[33m"
        dim = "\033[2m"
        reset = "\033[0m"
        cyan = "\033[36m"
    else:
        bold = yellow = dim = reset = cyan = ""
    lines = [
        f"{yellow}{bold}⚠  A newer Megas release is available: {local} → {remote}{reset}",
        f"{dim}   Update keeps your pairing and local settings:{reset}",
        f"   {cyan}{update_cmd}{reset}",
        f"{dim}   Or force-refresh the binary (also keeps pairing + settings):{reset}",
        f"   {cyan}{reinstall_cmd}{reset}",
        "",
    ]
    return "\n".join(lines)


def maybe_print_update_notice(
    *,
    api_base: str | None = None,
    quiet: bool = False,
    machine: bool = False,
    force: bool = False,
) -> dict[str, Any] | None:
    """Print update banner to stderr for human CLI use. Never breaks the command.

    Codex-style: when production is newer, every human ``megas`` command shows
    update + reinstall commands. Skip for ``--json`` / quiet / machine output.
    """
    if quiet or machine:
        return None
    if os.getenv("CI", "").strip().lower() in {"1", "true", "yes"} and not os.getenv(
        "MEGAS_FORCE_UPDATE_NOTICE"
    ):
        return None
    try:
        notice = check_for_update(api_base=api_base, force=force)
    except Exception:
        return None
    if not notice:
        return None
    try:
        sys.stderr.write(format_update_notice(notice, color=True))
        sys.stderr.flush()
    except Exception:
        pass
    return notice


__all__ = [
    "check_for_update",
    "format_update_notice",
    "maybe_print_update_notice",
]
