from __future__ import annotations

import ctypes
import hashlib
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .secret_policy import is_sensitive_env_key


TIMELINE_VERSION = 1
SEQUENCE_STATE_VERSION = 1
SEQUENCE_ANCHOR_BYTES = 4096
REDACTED = "[REDACTED]"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id(kind: str) -> str:
    return f"{kind}_{uuid.uuid4().hex}"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_sensitive_field(key: str) -> bool:
    normalized = key.strip().replace("-", "_")
    return is_sensitive_env_key(normalized) or normalized.lower() in {
        "authorization",
        "proxy_authorization",
        "set_cookie",
        "cookie",
    }


def _scalar_strings(value: Any) -> set[str]:
    values: set[str] = set()
    if isinstance(value, str):
        if value:
            values.add(value)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        values.add(str(value))
    elif isinstance(value, dict):
        for item in value.values():
            values.update(_scalar_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            values.update(_scalar_strings(item))
    return values


def _sensitive_values(value: Any) -> set[str]:
    values: set[str] = set()
    if isinstance(value, dict):
        for raw_key, item in value.items():
            if _is_sensitive_field(str(raw_key)):
                values.update(_scalar_strings(item))
            else:
                values.update(_sensitive_values(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            values.update(_sensitive_values(item))
    return values


def contains_redacted(value: Any) -> bool:
    if value == REDACTED:
        return True
    if isinstance(value, dict):
        return any(contains_redacted(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_redacted(item) for item in value)
    return False


def diff_values(before: Any, after: Any, *, path: str = "$") -> list[dict[str, Any]]:
    """Return a deterministic structural diff suitable for replay evidence."""

    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            child = f"{path}.{key}"
            if key not in before:
                changes.append({"path": child, "before": None, "after": after[key]})
            elif key not in after:
                changes.append({"path": child, "before": before[key], "after": None})
            else:
                changes.extend(diff_values(before[key], after[key], path=child))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        changes = []
        size = max(len(before), len(after))
        for index in range(size):
            child = f"{path}[{index}]"
            if index >= len(before):
                changes.append({"path": child, "before": None, "after": after[index]})
            elif index >= len(after):
                changes.append({"path": child, "before": before[index], "after": None})
            else:
                changes.extend(diff_values(before[index], after[index], path=child))
        return changes
    if before != after:
        return [{"path": path, "before": before, "after": after}]
    return []


def redact(
    value: Any,
    *,
    path: str = "$",
    secret_values: set[str] | None = None,
) -> tuple[Any, list[str]]:
    """Redact structured secret fields and known secret values before persistence."""

    secrets = secret_values or set()
    paths: list[str] = []
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if _is_sensitive_field(key):
                clean[key] = REDACTED
                paths.append(child_path)
                continue
            cleaned, child_paths = redact(
                item,
                path=child_path,
                secret_values=secrets,
            )
            clean[key] = cleaned
            paths.extend(child_paths)
        return clean, paths
    if isinstance(value, list):
        clean_list: list[Any] = []
        for index, item in enumerate(value):
            cleaned, child_paths = redact(
                item,
                path=f"{path}[{index}]",
                secret_values=secrets,
            )
            clean_list.append(cleaned)
            paths.extend(child_paths)
        return clean_list, paths
    if isinstance(value, tuple):
        cleaned, child_paths = redact(
            list(value),
            path=path,
            secret_values=secrets,
        )
        return cleaned, child_paths
    if isinstance(value, str) and secrets:
        cleaned = value
        for secret in sorted((item for item in secrets if item), key=len, reverse=True):
            cleaned = cleaned.replace(secret, REDACTED)
        if cleaned != value:
            return cleaned, [path]
    return value, paths


@dataclass(slots=True)
class Session:
    id: str = field(default_factory=lambda: _id("ses"))
    started_at: str = field(default_factory=_now)
    ended_at: str | None = None
    state: str = "active"
    profile_id: str | None = None
    label: str = ""
    source: str = "cli"
    schema_version: int = TIMELINE_VERSION

    def validate(self) -> None:
        if not self.id or self.state not in {"active", "completed", "failed", "cancelled"}:
            raise ValueError("invalid session")
        if self.schema_version != TIMELINE_VERSION:
            raise ValueError(f"unsupported session schema version: {self.schema_version}")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        session = cls(
            id=str(data["id"]),
            started_at=str(data["started_at"]),
            ended_at=str(data["ended_at"]) if data.get("ended_at") else None,
            state=str(data.get("state") or "active"),
            profile_id=str(data["profile_id"]) if data.get("profile_id") else None,
            label=str(data.get("label") or ""),
            source=str(data.get("source") or "cli"),
            schema_version=int(data.get("schema_version") or TIMELINE_VERSION),
        )
        session.validate()
        return session


@dataclass(slots=True)
class Event:
    session_id: str
    sequence: int
    type: str
    data: dict[str, Any]
    correlation_id: str
    id: str = field(default_factory=lambda: _id("evt"))
    at: str = field(default_factory=_now)
    causation_id: str | None = None
    source: str = "cli"
    redacted: bool = False
    redaction_paths: list[str] = field(default_factory=list)
    schema_version: int = TIMELINE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ToolCall:
    session_id: str
    server_id: str
    server: str
    tool: str
    arguments: dict[str, Any]
    correlation_id: str
    source: str
    id: str = field(default_factory=lambda: _id("call"))
    started_at: str = field(default_factory=_now)
    ended_at: str | None = None
    duration_ms: int | None = None
    status: str = "running"
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    redacted: bool = False
    redaction_paths: list[str] = field(default_factory=list)
    schema_version: int = TIMELINE_VERSION
    owner_pid: int = field(default_factory=os.getpid)
    replay_of: str | None = None
    cancel_requested_at: str | None = None
    cancel_reason: str | None = None
    _secret_values: list[str] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("_secret_values", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolCall":
        return cls(
            session_id=str(data["session_id"]),
            server_id=str(data["server_id"]),
            server=str(data.get("server") or ""),
            tool=str(data["tool"]),
            arguments=dict(data.get("arguments") or {}),
            correlation_id=str(data["correlation_id"]),
            source=str(data.get("source") or "cli"),
            id=str(data["id"]),
            started_at=str(data["started_at"]),
            ended_at=str(data["ended_at"]) if data.get("ended_at") else None,
            duration_ms=int(data["duration_ms"]) if data.get("duration_ms") is not None else None,
            status=str(data.get("status") or "running"),
            result=dict(data["result"]) if isinstance(data.get("result"), dict) else None,
            error=dict(data["error"]) if isinstance(data.get("error"), dict) else None,
            redacted=bool(data.get("redacted", False)),
            redaction_paths=[str(v) for v in (data.get("redaction_paths") or [])],
            schema_version=int(data.get("schema_version") or TIMELINE_VERSION),
            owner_pid=int(data.get("owner_pid") or 0),
            replay_of=str(data["replay_of"]) if data.get("replay_of") else None,
            cancel_requested_at=(
                str(data["cancel_requested_at"]) if data.get("cancel_requested_at") else None
            ),
            cancel_reason=str(data["cancel_reason"]) if data.get("cancel_reason") else None,
        )


class TimelineStore:
    def __init__(self, mcp_dir: Path) -> None:
        self.root = mcp_dir / "timeline"
        self.calls_dir = self.root / "tool_calls"
        self.sessions_path = self.root / "sessions.json"
        self.events_path = self.root / "events.jsonl"
        self.active_path = self.root / "active_session.json"
        self.lock_path = self.root / ".timeline.lock"
        self.sequence_state_path = self.root / "sequence_state.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.calls_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    @contextmanager
    def _lock(self, *, timeout: float = 5.0) -> Iterator[None]:
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
                    raise TimeoutError("timeline store is busy")
                time.sleep(0.025)
        try:
            yield
        finally:
            try:
                self.lock_path.unlink()
            except OSError:
                pass

    def _load_sessions(self) -> list[Session]:
        if not self.sessions_path.exists():
            return []
        raw = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("sessions"), list):
            raise ValueError("timeline sessions store is malformed")
        return [Session.from_dict(row) for row in raw["sessions"] if isinstance(row, dict)]

    def _save_sessions(self, sessions: list[Session]) -> None:
        self._atomic_json(
            self.sessions_path,
            {
                "version": TIMELINE_VERSION,
                "sessions": [session.to_dict() for session in sessions],
            },
        )

    def list_sessions(self) -> list[Session]:
        return sorted(self._load_sessions(), key=lambda item: item.started_at, reverse=True)

    def get_session(self, session_id: str) -> Session:
        for session in self._load_sessions():
            if session.id == session_id:
                return session
        raise KeyError(f"unknown session: {session_id}")

    def active(self) -> Session | None:
        if not self.active_path.exists():
            return None
        raw = json.loads(self.active_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not raw.get("session_id"):
            return None
        try:
            session = self.get_session(str(raw["session_id"]))
        except KeyError:
            self.active_path.unlink(missing_ok=True)
            return None
        if session.state != "active":
            self.active_path.unlink(missing_ok=True)
            return None
        return session

    def start_session(
        self,
        *,
        profile_id: str | None = None,
        label: str = "",
        source: str = "cli",
        make_active: bool = True,
    ) -> Session:
        with self._lock():
            if make_active and self.active() is not None:
                raise RuntimeError("an active session already exists; end it before starting another")
            sessions = self._load_sessions()
            session = Session(profile_id=profile_id, label=label, source=source)
            sessions.append(session)
            self._save_sessions(sessions)
            if make_active:
                self._atomic_json(
                    self.active_path,
                    {"version": TIMELINE_VERSION, "session_id": session.id},
                )
        self.append_event(
            session.id,
            "session.started",
            {"profile_id": profile_id, "label": label},
            correlation_id=session.id,
            source=source,
        )
        return session

    def end_session(self, session_id: str, *, state: str = "completed") -> Session:
        if state not in {"completed", "failed", "cancelled"}:
            raise ValueError("session end state must be completed, failed, or cancelled")
        changed = False
        with self._lock():
            sessions = self._load_sessions()
            target = next((session for session in sessions if session.id == session_id), None)
            if target is None:
                raise KeyError(f"unknown session: {session_id}")
            if target.state == "active":
                target.state = state
                target.ended_at = _now()
                self._save_sessions(sessions)
                changed = True
            if self.active_path.exists():
                try:
                    active = json.loads(self.active_path.read_text(encoding="utf-8"))
                except Exception:
                    active = {}
                if active.get("session_id") == target.id:
                    self.active_path.unlink(missing_ok=True)
        if changed:
            self.append_event(
                target.id,
                "session.ended",
                {"state": target.state},
                correlation_id=target.id,
                source=target.source,
            )
        return target

    def _sequence_anchor(self, size: int) -> str:
        digest = hashlib.sha256()
        if size <= 0 or not self.events_path.exists():
            return digest.hexdigest()
        start = max(0, size - SEQUENCE_ANCHOR_BYTES)
        with self.events_path.open("rb") as handle:
            handle.seek(start)
            remaining = size - start
            while remaining > 0:
                block = handle.read(min(remaining, 64 * 1024))
                if not block:
                    break
                digest.update(block)
                remaining -= len(block)
        return digest.hexdigest()

    @staticmethod
    def _merge_sequence_row(sequences: dict[str, int], row: Any) -> None:
        if not isinstance(row, dict):
            return
        session_id = str(row.get("session_id") or "").strip()
        if not session_id:
            return
        try:
            sequence = int(row.get("sequence") or 0)
        except (TypeError, ValueError):
            return
        if sequence > sequences.get(session_id, 0):
            sequences[session_id] = sequence

    def _read_sequence_rows(self, *, offset: int = 0) -> dict[str, int]:
        sequences: dict[str, int] = {}
        if not self.events_path.exists():
            return sequences
        with self.events_path.open("rb") as handle:
            handle.seek(max(0, offset))
            for raw_line in handle:
                try:
                    row = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                self._merge_sequence_row(sequences, row)
        return sequences

    def _save_sequence_state_unlocked(self, sequences: dict[str, int]) -> dict[str, Any]:
        if self.events_path.exists():
            stat = self.events_path.stat()
            size = int(stat.st_size)
            mtime_ns = int(stat.st_mtime_ns)
        else:
            size = 0
            mtime_ns = 0
        state: dict[str, Any] = {
            "version": SEQUENCE_STATE_VERSION,
            "events_size": size,
            "events_mtime_ns": mtime_ns,
            "tail_sha256": self._sequence_anchor(size),
            "sequences": {key: int(value) for key, value in sorted(sequences.items())},
        }
        self._atomic_json(self.sequence_state_path, state)
        return state

    def _rebuild_sequence_state_unlocked(self) -> dict[str, Any]:
        """Rebuild the non-authoritative sequence cache from canonical JSONL."""

        return self._save_sequence_state_unlocked(self._read_sequence_rows())

    def _load_sequence_state_unlocked(self) -> dict[str, Any] | None:
        if not self.sequence_state_path.is_file():
            return None
        try:
            raw = json.loads(self.sequence_state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or int(raw.get("version") or 0) != SEQUENCE_STATE_VERSION:
                return None
            events_size = int(raw.get("events_size") or 0)
            if events_size < 0:
                return None
            sequences_raw = raw.get("sequences")
            if not isinstance(sequences_raw, dict):
                return None
            sequences: dict[str, int] = {}
            for raw_key, raw_value in sequences_raw.items():
                key = str(raw_key).strip()
                value = int(raw_value)
                if not key or value < 0:
                    return None
                sequences[key] = value
            tail_sha256 = str(raw.get("tail_sha256") or "")
            if len(tail_sha256) != 64:
                return None
            return {
                "version": SEQUENCE_STATE_VERSION,
                "events_size": events_size,
                "events_mtime_ns": int(raw.get("events_mtime_ns") or 0),
                "tail_sha256": tail_sha256,
                "sequences": sequences,
            }
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def _sync_sequence_state_unlocked(self) -> dict[str, Any]:
        """Synchronize a rebuildable sequence projection without rescanning history."""

        state = self._load_sequence_state_unlocked()
        if self.events_path.exists():
            current_size = int(self.events_path.stat().st_size)
        else:
            current_size = 0

        if state is None:
            return self._rebuild_sequence_state_unlocked()

        stored_size = int(state["events_size"])
        if stored_size > current_size:
            return self._rebuild_sequence_state_unlocked()

        # The tail hash proves that bytes already incorporated into the cache
        # still match canonical JSONL. A rewrite at the same or larger size
        # therefore rebuilds instead of silently trusting stale counters.
        if self._sequence_anchor(stored_size) != state["tail_sha256"]:
            return self._rebuild_sequence_state_unlocked()

        if stored_size == current_size:
            return state

        sequences = dict(state["sequences"])
        delta = self._read_sequence_rows(offset=stored_size)
        for session_id, sequence in delta.items():
            if sequence > sequences.get(session_id, 0):
                sequences[session_id] = sequence
        return self._save_sequence_state_unlocked(sequences)

    def _next_sequence_unlocked(self, session_id: str) -> int:
        state = self._sync_sequence_state_unlocked()
        return int(state["sequences"].get(session_id, 0)) + 1

    def append_event(
        self,
        session_id: str,
        event_type: str,
        data: dict[str, Any],
        *,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        source: str = "cli",
    ) -> Event:
        self.get_session(session_id)
        clean_data, paths = redact(data)
        assert isinstance(clean_data, dict)
        with self._lock():
            event = Event(
                session_id=session_id,
                sequence=self._next_sequence_unlocked(session_id),
                type=event_type,
                data=clean_data,
                correlation_id=correlation_id or _id("corr"),
                causation_id=causation_id,
                source=source,
                redacted=bool(paths),
                redaction_paths=paths,
            )
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(self.events_path, 0o600)
            except OSError:
                pass
            # Canonical JSONL is fsynced first. If the process dies before this
            # cache update, the next append parses only the newly grown tail.
            self._sync_sequence_state_unlocked()
        return event

    def events(self, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        self.get_session(session_id)
        rows: list[dict[str, Any]] = []
        if not self.events_path.exists():
            return rows
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("session_id") == session_id:
                rows.append(row)
        return rows[-max(1, limit):]

    def begin_tool_call(
        self,
        session_id: str,
        *,
        server_id: str,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        source: str,
        correlation_id: str | None = None,
        replay_of: str | None = None,
    ) -> ToolCall:
        self.get_session(session_id)
        secret_values = _sensitive_values(arguments)
        clean_args, paths = redact(arguments, secret_values=secret_values)
        assert isinstance(clean_args, dict)
        call = ToolCall(
            session_id=session_id,
            server_id=server_id,
            server=server,
            tool=tool,
            arguments=clean_args,
            correlation_id=correlation_id or _id("corr"),
            source=source,
            redacted=bool(paths),
            redaction_paths=paths,
            replay_of=replay_of,
            _secret_values=sorted(secret_values, key=len, reverse=True),
        )
        self._atomic_json(self.calls_dir / f"{call.id}.json", call.to_dict())
        self.append_event(
            session_id,
            "tool.requested",
            {
                "tool_call_id": call.id,
                "replay_of": replay_of,
                "server_id": server_id,
                "server": server,
                "tool": tool,
                "arguments": clean_args,
            },
            correlation_id=call.correlation_id,
            causation_id=replay_of,
            source=source,
        )
        return call

    def get_tool_call(self, call_id: str) -> ToolCall:
        path = self.calls_dir / f"{call_id}.json"
        if not path.is_file():
            raise KeyError(f"unknown tool call: {call_id}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"tool call record is malformed: {call_id}")
        call = ToolCall.from_dict(raw)
        if call.id != call_id:
            raise ValueError(f"tool call identity mismatch: {call_id}")
        return call

    def active_tool_calls(
        self,
        *,
        server_id: str | None = None,
        tool: str | None = None,
    ) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for path in sorted(self.calls_dir.glob("call_*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                call = ToolCall.from_dict(raw) if isinstance(raw, dict) else None
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue
            if call is None or call.status != "running":
                continue
            if server_id is not None and call.server_id != server_id:
                continue
            if tool is not None and call.tool != tool:
                continue
            calls.append(call)
        return calls

    def request_cancel(
        self,
        call_id: str,
        *,
        reason: str,
        source: str = "cli",
    ) -> tuple[ToolCall, bool]:
        reason = reason.strip() or "cancel requested"
        changed = False
        with self._lock():
            call = self.get_tool_call(call_id)
            if call.status == "running" and call.cancel_requested_at is None:
                call.cancel_requested_at = _now()
                call.cancel_reason = reason
                self._atomic_json(self.calls_dir / f"{call.id}.json", call.to_dict())
                changed = True
        if changed:
            self.append_event(
                call.session_id,
                "tool.cancel.requested",
                {
                    "tool_call_id": call.id,
                    "server_id": call.server_id,
                    "server": call.server,
                    "tool": call.tool,
                    "reason": reason,
                },
                correlation_id=call.correlation_id,
                causation_id=call.id,
                source=source,
            )
        return call, changed

    def complete_tool_call(
        self,
        call: ToolCall,
        *,
        result: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> ToolCall:
        ended = _now()
        start_dt = datetime.fromisoformat(call.started_at)
        end_dt = datetime.fromisoformat(ended)
        call.ended_at = ended
        call.duration_ms = max(0, int((end_dt - start_dt).total_seconds() * 1000))
        known_secrets = set(call._secret_values)
        event_type: str
        event_data: dict[str, Any]

        with self._lock():
            persisted = self.get_tool_call(call.id)
            if persisted.cancel_requested_at:
                call.cancel_requested_at = persisted.cancel_requested_at
                call.cancel_reason = persisted.cancel_reason

            if error is not None:
                clean_error, paths = redact(
                    {"type": type(error).__name__, "message": str(error)},
                    secret_values=known_secrets,
                )
                assert isinstance(clean_error, dict)
                call.status = "cancelled" if call.cancel_requested_at else "error"
                call.error = clean_error
                call.redacted = call.redacted or bool(paths)
                call.redaction_paths = sorted(set([*call.redaction_paths, *paths]))
                event_type = "tool.cancelled" if call.status == "cancelled" else "tool.failed"
                event_data = {
                    "tool_call_id": call.id,
                    "status": call.status,
                    "duration_ms": call.duration_ms,
                    "reason": call.cancel_reason,
                    "error": clean_error,
                }
            else:
                clean_result, paths = redact(
                    result or {},
                    secret_values=known_secrets,
                )
                assert isinstance(clean_result, dict)
                call.status = "error" if bool((result or {}).get("is_error", False)) else "completed"
                call.result = clean_result
                call.redacted = call.redacted or bool(paths)
                call.redaction_paths = sorted(set([*call.redaction_paths, *paths]))
                event_type = "tool.response"
                event_data = {
                    "tool_call_id": call.id,
                    "status": call.status,
                    "duration_ms": call.duration_ms,
                    "result": clean_result,
                }
            self._atomic_json(self.calls_dir / f"{call.id}.json", call.to_dict())

        self.append_event(
            call.session_id,
            event_type,
            event_data,
            correlation_id=call.correlation_id,
            causation_id=call.id,
            source=call.source,
        )
        call._secret_values.clear()
        return call

    def recover_incomplete(self) -> list[dict[str, Any]]:
        """Recover calls whose owning Megas process disappeared mid-execution."""

        recovered: list[dict[str, Any]] = []
        for path in sorted(self.calls_dir.glob("call_*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                call = ToolCall.from_dict(raw) if isinstance(raw, dict) else None
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue
            if call is None or call.status != "running" or _pid_alive(call.owner_pid):
                continue

            ended = _now()
            start_dt = datetime.fromisoformat(call.started_at)
            end_dt = datetime.fromisoformat(ended)
            call.ended_at = ended
            call.duration_ms = max(0, int((end_dt - start_dt).total_seconds() * 1000))
            if call.cancel_requested_at:
                call.status = "cancelled"
                call.error = {
                    "type": "CancelledExecution",
                    "message": "Megas recovered a tool call after cancellation terminated its owning process.",
                }
                event_type = "tool.cancelled"
            else:
                call.status = "error"
                call.error = {
                    "type": "InterruptedExecution",
                    "message": "Megas recovered an incomplete tool call after its owning process exited.",
                }
                event_type = "tool.interrupted"
            self._atomic_json(path, call.to_dict())
            self.append_event(
                call.session_id,
                event_type,
                {
                    "tool_call_id": call.id,
                    "server_id": call.server_id,
                    "server": call.server,
                    "tool": call.tool,
                    "owner_pid": call.owner_pid,
                    "duration_ms": call.duration_ms,
                    "reason": call.cancel_reason,
                    "recovered": True,
                },
                correlation_id=call.correlation_id,
                causation_id=call.id,
                source="recovery",
            )
            recovered.append(call.to_dict())

            session = self.get_session(call.session_id)
            if session.state == "active" and session.source == "cli-implicit":
                self.end_session(
                    session.id,
                    state="cancelled" if call.status == "cancelled" else "failed",
                )
        return recovered

    def tool_calls(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        self.get_session(session_id)
        calls: list[ToolCall] = []
        for path in self.calls_dir.glob("call_*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict) and raw.get("session_id") == session_id:
                calls.append(ToolCall.from_dict(raw))
        calls.sort(key=lambda item: item.started_at)
        return [call.to_dict() for call in calls[-max(1, limit):]]


__all__ = [
    "Event",
    "Session",
    "TimelineStore",
    "ToolCall",
    "contains_redacted",
    "diff_values",
    "redact",
]
