from __future__ import annotations

import os
import re
from pathlib import Path


SECRET_PATTERNS = [
    (
        re.compile(
            r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|worker[_-]?token|token|secret|password|passwd)[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+)"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)(\b(?:authorization|proxy-authorization)\s*:\s*(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]+"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(https?://[^:/\s]+:)[^@\s]+(@)"), r"\1[REDACTED]\2"),
]


DANGEROUS_COMMAND_PATTERNS = [
    re.compile(r"(?i)\brm\s+-rf\b"),
    re.compile(r"(?i)\bdel\s+/[sq]\b"),
    re.compile(r"(?i)\bformat\s+[A-Z]:"),
    re.compile(r"(?i)\bshutdown\b"),
    re.compile(r"(?i)\breboot\b"),
    re.compile(r"(?i)\bgit\s+push\b"),
    re.compile(r"(?i)\bkubectl\s+delete\b"),
    re.compile(r"(?i)\bterraform\s+apply\b"),
]


def redact(text: str) -> str:
    redacted = text
    for pattern, replacement in SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _strip_win_ext_prefix(path_str: str) -> str:
    """Normalize Windows extended-length paths (\\\\?\\C:\\…) for allowlist checks."""
    raw = str(path_str or "").strip()
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    elif raw.startswith("//?/"):
        raw = raw[4:]
    # UNC extended: \\?\UNC\server\share → \\server\share
    if raw.upper().startswith("UNC\\"):
        raw = "\\" + raw[3:]
    return raw


def is_ephemeral_megas_sandbox(path: str | Path | None) -> bool:
    """True for disposable light-chat sandboxes (must never be thr_ project_path).

    Headless adapters create TemporaryDirectory prefixes like
    ``megas-opencode-XXXX`` under the system temp dir. Continuations that
    re-use that path fail with "Workspace is not allowed" once the temp dir
    is gone or /tmp was never allow-listed.
    """
    if path is None:
        return False
    try:
        raw = str(Path(_strip_win_ext_prefix(str(path))).expanduser())
    except Exception:
        raw = str(path or "")
    name = Path(raw).name.lower()
    if name.startswith("megas-") and not name.startswith("megas-workspace"):
        # TemporaryDirectory(prefix=f"megas-{adapter}-")
        return True
    lowered = raw.replace("\\", "/").lower()
    for marker in (
        "/tmp/megas-",
        "/var/tmp/megas-",
        "/private/var/folders/",  # macOS temp
        "/appdata/local/temp/megas-",
        "/local/temp/megas-",
    ):
        if marker in lowered:
            return True
    return False


def resolve_allowed_workdir(workdir: str | None, allowed_folders: list[str]) -> Path:
    if not allowed_folders:
        raise PermissionError("No allowed workspace folder configured.")

    # Ephemeral sandboxes / missing paths: never hard-fail continue — use stable root.
    if not workdir or is_ephemeral_megas_sandbox(workdir):
        return Path(_strip_win_ext_prefix(allowed_folders[0])).expanduser().resolve()

    try:
        candidate = Path(_strip_win_ext_prefix(workdir)).expanduser().resolve()
    except Exception:
        return Path(_strip_win_ext_prefix(allowed_folders[0])).expanduser().resolve()

    # Deleted thr_ project_path (common after light-chat temp sandbox cleanup).
    if not candidate.exists():
        for allowed in allowed_folders:
            root = Path(_strip_win_ext_prefix(allowed)).expanduser().resolve()
            try:
                if candidate == root or root in candidate.parents:
                    # Parent was allowed but path gone — land on the allowed root.
                    return root
            except Exception:
                continue
        return Path(_strip_win_ext_prefix(allowed_folders[0])).expanduser().resolve()

    for allowed in allowed_folders:
        root = Path(_strip_win_ext_prefix(allowed)).expanduser().resolve()
        try:
            if candidate == root or root in candidate.parents:
                return candidate
            # Case-insensitive compare on Windows (drive letters / short names).
            if os.name == "nt":
                c = os.path.normcase(str(candidate))
                r = os.path.normcase(str(root))
                if c == r or c.startswith(r.rstrip("\\/") + "\\"):
                    return candidate
        except Exception:
            continue
    # Last resort: do not fail thr_ continue for stale sandbox paths.
    if is_ephemeral_megas_sandbox(candidate):
        return Path(_strip_win_ext_prefix(allowed_folders[0])).expanduser().resolve()
    raise PermissionError(f"Workspace is not allowed: {candidate}")


def validate_shell_command(command: str) -> None:
    stripped = command.strip()
    if not stripped:
        raise PermissionError("Empty command rejected.")
    for pattern in DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(stripped):
            raise PermissionError(f"Command requires explicit high-risk approval: {stripped}")


def platform_shell() -> bool:
    return os.name == "nt"

