from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from ..bounded_io import read_bounded_bytes
from .timeline import TIMELINE_VERSION, Session, TimelineStore, ToolCall, redact


TIMELINE_ARCHIVE_FORMAT = "megas-timeline-archive"
TIMELINE_ARCHIVE_VERSION = 1
MAX_TIMELINE_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_TIMELINE_RECORD_BYTES = 4 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_TERMINAL_SESSIONS = 5000


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _atomic_bytes(path: Path, value: bytes) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
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


def _safe_export_row(value: dict[str, Any]) -> dict[str, Any]:
    cleaned, _paths = redact(value)
    if not isinstance(cleaned, dict):
        raise ValueError("timeline export record must remain a JSON object after redaction")
    return cleaned


def _iter_event_rows(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    if not path.is_file():
        raise ValueError(f"timeline events path is not a regular file: {path}")
    with path.open("rb") as handle:
        for raw in handle:
            if len(raw) > MAX_TIMELINE_RECORD_BYTES:
                raise ValueError("timeline event record exceeds the 4 MiB safety limit")
            if not raw.strip():
                continue
            try:
                row = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("timeline event store contains malformed JSON") from exc
            if not isinstance(row, dict):
                raise ValueError("timeline event store contains a non-object record")
            yield row


def _tool_call_rows(timeline: TimelineStore) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(timeline.calls_dir.glob("call_*.json")):
        raw = read_bounded_bytes(path, max_bytes=MAX_TIMELINE_RECORD_BYTES)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"tool-call record is malformed: {path.name}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"tool-call record must be a JSON object: {path.name}")
        call = ToolCall.from_dict(value)
        if path.name != f"{call.id}.json":
            raise ValueError(f"tool-call file identity mismatch: {path.name}")
        rows.append(_safe_export_row(call.to_dict()))
    rows.sort(key=lambda row: (str(row.get("session_id") or ""), str(row.get("started_at") or ""), str(row.get("id") or "")))
    return rows


def build_timeline_archive(
    timeline: TimelineStore,
    *,
    session_ids: Iterable[str] | None = None,
    max_bytes: int = MAX_TIMELINE_ARCHIVE_BYTES,
) -> dict[str, Any]:
    """Build a deterministic redacted archive from persisted timeline state.

    The export never reads external secrets. It re-applies structured redaction
    before serialization and enforces a final size ceiling so exporting a large
    history cannot allocate an unbounded artifact.
    """

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    requested = None if session_ids is None else {str(value).strip() for value in session_ids if str(value).strip()}
    with timeline._lock():
        sessions = timeline._load_sessions()
        by_id = {session.id: session for session in sessions}
        if requested is not None:
            unknown = sorted(requested - set(by_id))
            if unknown:
                raise KeyError("unknown timeline session(s): " + ", ".join(unknown))
            selected = requested
        else:
            selected = set(by_id)

        session_rows = [
            _safe_export_row(session.to_dict())
            for session in sessions
            if session.id in selected
        ]
        session_rows.sort(key=lambda row: (str(row.get("started_at") or ""), str(row.get("id") or "")))

        event_rows = [
            _safe_export_row(row)
            for row in _iter_event_rows(timeline.events_path)
            if str(row.get("session_id") or "") in selected
        ]
        event_rows.sort(
            key=lambda row: (
                str(row.get("session_id") or ""),
                int(row.get("sequence") or 0),
                str(row.get("id") or ""),
            )
        )

        call_rows = [
            row
            for row in _tool_call_rows(timeline)
            if str(row.get("session_id") or "") in selected
        ]

    payload = {
        "timeline_version": TIMELINE_VERSION,
        "sessions": session_rows,
        "events": event_rows,
        "tool_calls": call_rows,
    }
    digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    archive = {
        "format": TIMELINE_ARCHIVE_FORMAT,
        "version": TIMELINE_ARCHIVE_VERSION,
        "manifest": {
            "content_sha256": digest,
            "counts": {
                "sessions": len(session_rows),
                "events": len(event_rows),
                "tool_calls": len(call_rows),
            },
        },
        "payload": payload,
    }
    encoded = _canonical_bytes(archive)
    if len(encoded) > max_bytes:
        raise ValueError(
            f"timeline archive exceeds the {max_bytes // (1024 * 1024)} MiB export safety limit"
        )
    return archive


def write_timeline_archive(
    timeline: TimelineStore,
    path: Path | str,
    *,
    session_ids: Iterable[str] | None = None,
    max_bytes: int = MAX_TIMELINE_ARCHIVE_BYTES,
) -> dict[str, Any]:
    archive = build_timeline_archive(
        timeline,
        session_ids=session_ids,
        max_bytes=max_bytes,
    )
    encoded = json.dumps(
        archive,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8") + b"\n"
    if len(encoded) > max_bytes:
        raise ValueError(
            f"timeline archive exceeds the {max_bytes // (1024 * 1024)} MiB export safety limit"
        )
    target = Path(path).expanduser()
    _atomic_bytes(target, encoded)
    return {
        "path": str(target),
        "content_sha256": archive["manifest"]["content_sha256"],
        **archive["manifest"]["counts"],
    }


@dataclass(frozen=True, slots=True)
class TimelineRetentionPolicy:
    max_age_days: int = DEFAULT_RETENTION_DAYS
    max_terminal_sessions: int = DEFAULT_MAX_TERMINAL_SESSIONS

    def validate(self) -> None:
        if not (1 <= int(self.max_age_days) <= 36500):
            raise ValueError("timeline max_age_days must be between 1 and 36500")
        if not (1 <= int(self.max_terminal_sessions) <= 1_000_000):
            raise ValueError("timeline max_terminal_sessions must be between 1 and 1000000")


def plan_timeline_retention(
    timeline: TimelineStore,
    policy: TimelineRetentionPolicy,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return terminal sessions eligible for deletion; never selects live work."""

    policy.validate()
    current = (now or _now()).astimezone(UTC)
    cutoff = current - timedelta(days=policy.max_age_days)
    sessions = timeline.list_sessions()
    running_session_ids = {call.session_id for call in timeline.active_tool_calls()}
    active = timeline.active()
    active_id = active.id if active is not None else None

    terminal = [session for session in sessions if session.state != "active"]
    terminal.sort(key=lambda session: (_parse_time(session.ended_at or session.started_at), session.id), reverse=True)
    over_limit_ids = {
        session.id
        for session in terminal[policy.max_terminal_sessions :]
    }

    candidates: list[Session] = []
    protected: list[dict[str, Any]] = []
    for session in sessions:
        if session.state == "active" or session.id == active_id:
            protected.append({"session_id": session.id, "reason": "active_session"})
            continue
        if session.id in running_session_ids:
            protected.append({"session_id": session.id, "reason": "running_tool_call"})
            continue
        ended = _parse_time(session.ended_at or session.started_at)
        if ended < cutoff or session.id in over_limit_ids:
            candidates.append(session)

    candidates.sort(key=lambda session: (_parse_time(session.ended_at or session.started_at), session.id))
    return {
        "policy": {
            "max_age_days": policy.max_age_days,
            "max_terminal_sessions": policy.max_terminal_sessions,
        },
        "cutoff": cutoff.isoformat(),
        "candidate_session_ids": [session.id for session in candidates],
        "candidate_count": len(candidates),
        "protected": protected,
        "mutated": False,
    }


def prune_timeline(
    timeline: TimelineStore,
    policy: TimelineRetentionPolicy,
    *,
    confirm: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Prune only terminal timeline data selected by an explicit retention policy.

    The operation is dry-run unless ``confirm=True``. Visible session state is
    removed first via atomic files; stale call files are deleted afterward. A
    crash can therefore leave unreachable orphan data, never a retained session
    whose referenced call was deleted. The next prune also garbage-collects such
    orphan records.
    """

    plan = plan_timeline_retention(timeline, policy, now=now)
    if not confirm or not plan["candidate_session_ids"]:
        return plan

    delete_ids = set(plan["candidate_session_ids"])
    with timeline._lock():
        sessions = timeline._load_sessions()
        live_sessions = {session.id for session in sessions if session.state == "active"}
        running_session_ids = {call.session_id for call in timeline.active_tool_calls()}
        unsafe = sorted(delete_ids & (live_sessions | running_session_ids))
        if unsafe:
            raise RuntimeError(
                "timeline retention refused because selected sessions became active: "
                + ", ".join(unsafe)
            )

        retained = [session for session in sessions if session.id not in delete_ids]
        retained_ids = {session.id for session in retained}
        timeline._save_sessions(retained)

        event_lines: list[bytes] = []
        if timeline.events_path.exists():
            for row in _iter_event_rows(timeline.events_path):
                if str(row.get("session_id") or "") in retained_ids:
                    event_lines.append(_canonical_bytes(row) + b"\n")
        _atomic_bytes(timeline.events_path, b"".join(event_lines))

        deleted_call_ids: list[str] = []
        for path in sorted(timeline.calls_dir.glob("call_*.json")):
            raw = read_bounded_bytes(path, max_bytes=MAX_TIMELINE_RECORD_BYTES)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"tool-call record is malformed: {path.name}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"tool-call record must be an object: {path.name}")
            session_id = str(value.get("session_id") or "")
            # Also clean orphan call files left by a previously interrupted prune.
            if session_id not in retained_ids:
                call_id = str(value.get("id") or path.stem)
                path.unlink(missing_ok=True)
                deleted_call_ids.append(call_id)

    return {
        **plan,
        "mutated": True,
        "deleted_session_ids": sorted(delete_ids),
        "deleted_call_ids": sorted(deleted_call_ids),
    }


__all__ = [
    "DEFAULT_MAX_TERMINAL_SESSIONS",
    "DEFAULT_RETENTION_DAYS",
    "MAX_TIMELINE_ARCHIVE_BYTES",
    "TIMELINE_ARCHIVE_FORMAT",
    "TIMELINE_ARCHIVE_VERSION",
    "TimelineRetentionPolicy",
    "build_timeline_archive",
    "plan_timeline_retention",
    "prune_timeline",
    "write_timeline_archive",
]
