from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any


BROWSER_ERROR_CODES = frozenset(
    {
        "TARGET_NOT_FOUND",
        "ACTION_NOT_ALLOWED",
        "ACTION_TIMEOUT",
        "NAVIGATION_TIMEOUT",
        "UPLOAD_FAILED",
        "PAGE_CRASHED",
        "BROWSER_DISCONNECTED",
        "AUTH_REQUIRED",
        "HUMAN_REQUIRED",
        "OUTCOME_AMBIGUOUS",
        "SITE_POLICY_BLOCK",
        "RECOVERY_EXHAUSTED",
    }
)
IRREVERSIBLE_ACTIONS = frozenset(
    {
        "submit_form",
        "submit_purchase",
        "transfer_money",
        "delete_data",
        "message_send",
        "legal_attestation",
    }
)
SAFE_RETRY_ACTIONS = frozenset(
    {
        "list_tabs",
        "get_page_state",
        "inspect_page",
        "screenshot",
        "hover",
        "focus",
        "fill_field",
        "select_option",
        "check",
        "uncheck",
        "scroll",
        "wait",
        "switch_tab",
    }
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _root() -> Path:
    configured = os.getenv("MEGAS_HOME", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".megas"
    path = root / "browser"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class BrowserFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        recoverable: bool,
        human_required: bool = False,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        normalized = str(code or "").strip().upper()
        if normalized not in BROWSER_ERROR_CODES:
            raise ValueError(f"unknown browser error code: {normalized}")
        super().__init__(message)
        self.code = normalized
        self.recoverable = bool(recoverable)
        self.human_required = bool(human_required)
        self.evidence = dict(evidence or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "recoverable": self.recoverable,
            "human_required": self.human_required,
            "evidence": self.evidence,
        }


@dataclass(slots=True)
class BrowserCheckpoint:
    task_id: str
    session_id: str
    step_index: int
    objective: str | None
    current_url: str
    page_signature: str
    completed_actions: list[str] = field(default_factory=list)
    uploaded_files: list[str] = field(default_factory=list)
    irreversible_actions: list[str] = field(default_factory=list)
    pending_questions: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    auth_profile_id: str | None = None
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BrowserCheckpointStore:
    def __init__(self) -> None:
        self.root = _root() / "checkpoints"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_task(task_id: str) -> str:
        value = str(task_id or "").strip()
        if not value or len(value) > 128:
            raise ValueError("browser checkpoint task id is invalid")
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _path(self, task_id: str) -> Path:
        return self.root / f"{self._safe_task(task_id)}.json"

    def save(self, checkpoint: BrowserCheckpoint) -> dict[str, Any]:
        payload = {"format_version": 1, "checkpoint": checkpoint.to_dict()}
        _atomic_json(self._path(checkpoint.task_id), payload)
        return checkpoint.to_dict()

    def latest(self, task_id: str) -> dict[str, Any] | None:
        path = self._path(task_id)
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or int(value.get("format_version") or 0) != 1:
            raise ValueError("browser checkpoint format is unsupported")
        checkpoint = value.get("checkpoint")
        return dict(checkpoint) if isinstance(checkpoint, dict) else None

    def clear(self, task_id: str) -> None:
        self._path(task_id).unlink(missing_ok=True)


class BrowserFileVault:
    """Task/session-scoped local file capabilities.

    Browser actions receive opaque refs, never arbitrary local paths. Grants are
    constructed only from already human-trusted Worker folders and the current
    browser session descriptor.
    """

    def __init__(self, allowed_folders: list[str], grants: list[dict[str, Any]] | None = None) -> None:
        self.allowed_roots: list[Path] = []
        for value in allowed_folders:
            try:
                root = Path(value).expanduser().resolve(strict=True)
            except OSError:
                continue
            if root.is_dir():
                self.allowed_roots.append(root)
        self._files: dict[str, Path] = {}
        for grant in grants or []:
            if not isinstance(grant, dict):
                continue
            ref = str(grant.get("ref") or "").strip()
            path = str(grant.get("path") or "").strip()
            if ref and path:
                self.grant(ref, path)

    def grant(self, ref: str, path_value: str) -> None:
        key = str(ref or "").strip()
        if not key or len(key) > 128 or not key.replace("_", "").replace("-", "").isalnum():
            raise ValueError("browser file reference is invalid")
        candidate = Path(path_value).expanduser().resolve(strict=True)
        if not candidate.is_file():
            raise PermissionError("approved browser file must be a regular file")
        if not any(_is_relative_to(candidate, root) for root in self.allowed_roots):
            raise PermissionError("browser file capability is outside human-trusted folders")
        self._files[key] = candidate

    def resolve(self, ref: str) -> Path:
        key = str(ref or "").strip()
        candidate = self._files.get(key)
        if candidate is None:
            raise PermissionError("browser file is not approved for this task/session")
        if not candidate.is_file():
            raise FileNotFoundError("approved browser file no longer exists")
        return candidate

    def refs(self) -> list[str]:
        return sorted(self._files)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def download_quarantine(session_id: str) -> Path:
    safe = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:24]
    path = _root() / "downloads" / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def evidence_root(session_id: str) -> Path:
    safe = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:24]
    path = _root() / "traces" / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def page_signature(url: str, title: str) -> str:
    material = f"{url}\n{title}".encode("utf-8", errors="replace")
    return hashlib.sha256(material).hexdigest()[:24]


__all__ = [
    "BROWSER_ERROR_CODES",
    "BrowserCheckpoint",
    "BrowserCheckpointStore",
    "BrowserFailure",
    "BrowserFileVault",
    "IRREVERSIBLE_ACTIONS",
    "SAFE_RETRY_ACTIONS",
    "download_quarantine",
    "evidence_root",
    "page_signature",
]
