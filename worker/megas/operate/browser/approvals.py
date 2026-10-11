from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterator
import uuid


APPROVAL_STORE_VERSION = 1
MAX_APPROVAL_ROWS = 512
PENDING_TTL_SECONDS = 10 * 60
MAX_GRANT_TTL_SECONDS = 5 * 60
_VALID_RISKS = frozenset({"sensitive", "destructive"})
_VALID_STATES = frozenset({"pending", "approved", "consumed", "denied", "expired"})


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _root() -> Path:
    configured = os.getenv("MEGAS_HOME", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".megas"
    path = root / "browser" / "approvals"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bounded_id(value: str, name: str, *, limit: int = 128) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise ValueError(f"{name} is missing or invalid")
    if any(ord(char) < 32 for char in text):
        raise ValueError(f"{name} contains control characters")
    return text


def _digest(value: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError("browser action digest must be a SHA-256 hex digest")
    return text


@dataclass(slots=True)
class BrowserApprovalRecord:
    id: str
    browser_session_id: str
    task_id: str
    action_digest: str
    action_type: str
    risk: str
    state: str
    requested_at: str
    request_expires_at: str
    approved_at: str | None = None
    approval_expires_at: str | None = None
    consumed_at: str | None = None
    denied_at: str | None = None

    def validate(self) -> None:
        self.id = _bounded_id(self.id, "approval id")
        self.browser_session_id = _bounded_id(self.browser_session_id, "browser session id")
        self.task_id = _bounded_id(self.task_id, "task id")
        self.action_digest = _digest(self.action_digest)
        self.action_type = _bounded_id(self.action_type, "action type", limit=64)
        self.risk = str(self.risk or "").strip().lower()
        if self.risk not in _VALID_RISKS:
            raise ValueError("browser approval risk is invalid")
        self.state = str(self.state or "").strip().lower()
        if self.state not in _VALID_STATES:
            raise ValueError("browser approval state is invalid")
        _parse(self.requested_at)
        _parse(self.request_expires_at)
        for value in (
            self.approved_at,
            self.approval_expires_at,
            self.consumed_at,
            self.denied_at,
        ):
            if value:
                _parse(value)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BrowserApprovalRecord":
        row = cls(
            id=str(value.get("id") or ""),
            browser_session_id=str(value.get("browser_session_id") or ""),
            task_id=str(value.get("task_id") or ""),
            action_digest=str(value.get("action_digest") or ""),
            action_type=str(value.get("action_type") or ""),
            risk=str(value.get("risk") or ""),
            state=str(value.get("state") or "pending"),
            requested_at=str(value.get("requested_at") or ""),
            request_expires_at=str(value.get("request_expires_at") or ""),
            approved_at=str(value.get("approved_at") or "").strip() or None,
            approval_expires_at=str(value.get("approval_expires_at") or "").strip() or None,
            consumed_at=str(value.get("consumed_at") or "").strip() or None,
            denied_at=str(value.get("denied_at") or "").strip() or None,
        )
        row.validate()
        return row


class BrowserApprovalStore:
    """Cross-process, one-time approval authority for sensitive browser actions.

    MCP/browser processes can request and consume approvals, but only the local
    desktop bridge exposes grant/deny operations. Records contain no credentials,
    page content, form values, file paths, or other secret material.
    """

    def __init__(self, home: Path | None = None) -> None:
        self.root = (home / "browser" / "approvals") if home else _root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "approvals.json"
        self.lock_path = self.root / ".approvals.lock"

    @contextmanager
    def _lock(self, timeout: float = 5.0) -> Iterator[None]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, f"{os.getpid()}\n".encode("ascii"))
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > 30:
                        self.lock_path.unlink()
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("browser approval store is busy")
                time.sleep(0.025)
        try:
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)

    @staticmethod
    def _atomic(path: Path, payload: dict[str, Any]) -> None:
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

    def _load_unlocked(self) -> list[BrowserApprovalRecord]:
        if not self.path.exists():
            return []
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or int(value.get("version") or 0) != APPROVAL_STORE_VERSION:
            raise ValueError("unsupported browser approval store version")
        rows = value.get("approvals")
        if not isinstance(rows, list):
            raise ValueError("browser approval store is malformed")
        parsed = [BrowserApprovalRecord.from_dict(row) for row in rows if isinstance(row, dict)]
        if len(parsed) != len(rows):
            raise ValueError("browser approval store contains malformed records")
        return parsed[-MAX_APPROVAL_ROWS:]

    def _save_unlocked(self, rows: list[BrowserApprovalRecord]) -> None:
        self._atomic(
            self.path,
            {
                "version": APPROVAL_STORE_VERSION,
                "approvals": [row.to_dict() for row in rows[-MAX_APPROVAL_ROWS:]],
            },
        )

    @staticmethod
    def _expire(rows: list[BrowserApprovalRecord], now: datetime) -> None:
        for row in rows:
            if row.state == "pending" and _parse(row.request_expires_at) <= now:
                row.state = "expired"
            elif (
                row.state == "approved"
                and row.approval_expires_at
                and _parse(row.approval_expires_at) <= now
            ):
                row.state = "expired"

    def request(
        self,
        *,
        browser_session_id: str,
        task_id: str,
        action_digest: str,
        action_type: str,
        risk: str,
    ) -> dict[str, Any]:
        session = _bounded_id(browser_session_id, "browser session id")
        task = _bounded_id(task_id, "task id")
        digest = _digest(action_digest)
        action = _bounded_id(action_type, "action type", limit=64)
        selected_risk = str(risk or "").strip().lower()
        if selected_risk not in _VALID_RISKS:
            raise ValueError("browser approval risk is invalid")
        now = _now()
        with self._lock():
            rows = self._load_unlocked()
            self._expire(rows, now)
            existing = next(
                (
                    row
                    for row in reversed(rows)
                    if row.browser_session_id == session
                    and row.action_digest == digest
                    and row.state in {"pending", "approved"}
                ),
                None,
            )
            if existing is not None:
                self._save_unlocked(rows)
                return existing.to_dict()
            row = BrowserApprovalRecord(
                id=f"approval_{uuid.uuid4().hex}",
                browser_session_id=session,
                task_id=task,
                action_digest=digest,
                action_type=action,
                risk=selected_risk,
                state="pending",
                requested_at=_iso(now),
                request_expires_at=_iso(now + timedelta(seconds=PENDING_TTL_SECONDS)),
            )
            rows.append(row)
            self._save_unlocked(rows)
            return row.to_dict()

    def list_pending(self) -> list[dict[str, Any]]:
        now = _now()
        with self._lock():
            rows = self._load_unlocked()
            self._expire(rows, now)
            self._save_unlocked(rows)
            return [
                row.to_dict()
                for row in reversed(rows)
                if row.state in {"pending", "approved"}
            ]

    def grant(self, approval_id: str, *, ttl_seconds: int = 120) -> dict[str, Any]:
        selected = _bounded_id(approval_id, "approval id")
        ttl = max(15, min(MAX_GRANT_TTL_SECONDS, int(ttl_seconds)))
        now = _now()
        with self._lock():
            rows = self._load_unlocked()
            self._expire(rows, now)
            row = next((item for item in rows if item.id == selected), None)
            if row is None:
                raise KeyError(f"unknown browser approval: {selected}")
            if row.state != "pending":
                raise RuntimeError(f"browser approval cannot be granted from state {row.state}")
            if _parse(row.request_expires_at) <= now:
                row.state = "expired"
                self._save_unlocked(rows)
                raise RuntimeError("browser approval request expired")
            row.state = "approved"
            row.approved_at = _iso(now)
            row.approval_expires_at = _iso(now + timedelta(seconds=ttl))
            self._save_unlocked(rows)
            return row.to_dict()

    def deny(self, approval_id: str) -> dict[str, Any]:
        selected = _bounded_id(approval_id, "approval id")
        now = _now()
        with self._lock():
            rows = self._load_unlocked()
            self._expire(rows, now)
            row = next((item for item in rows if item.id == selected), None)
            if row is None:
                raise KeyError(f"unknown browser approval: {selected}")
            if row.state not in {"pending", "approved"}:
                raise RuntimeError(f"browser approval cannot be denied from state {row.state}")
            row.state = "denied"
            row.denied_at = _iso(now)
            self._save_unlocked(rows)
            return row.to_dict()

    def consume(self, *, browser_session_id: str, action_digest: str) -> dict[str, Any] | None:
        session = _bounded_id(browser_session_id, "browser session id")
        digest = _digest(action_digest)
        now = _now()
        with self._lock():
            rows = self._load_unlocked()
            self._expire(rows, now)
            row = next(
                (
                    item
                    for item in reversed(rows)
                    if item.browser_session_id == session
                    and item.action_digest == digest
                    and item.state == "approved"
                ),
                None,
            )
            if row is None:
                self._save_unlocked(rows)
                return None
            if not row.approval_expires_at or _parse(row.approval_expires_at) <= now:
                row.state = "expired"
                self._save_unlocked(rows)
                return None
            row.state = "consumed"
            row.consumed_at = _iso(now)
            self._save_unlocked(rows)
            return {
                "approved": True,
                "action_digest": row.action_digest,
                "expires_at": row.approval_expires_at,
                "approval_id": row.id,
            }


__all__ = [
    "APPROVAL_STORE_VERSION",
    "BrowserApprovalRecord",
    "BrowserApprovalStore",
    "MAX_GRANT_TTL_SECONDS",
    "PENDING_TTL_SECONDS",
]
