from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any


ACTIVITY_INDEX_VERSION = 1
MAX_INDEX_RECORD_BYTES = 4 * 1024 * 1024
ANCHOR_BYTES = 4096
_BUSY_TIMEOUT_MS = 3000

Projector = Callable[[dict[str, Any], int], Iterable[dict[str, Any]]]


class ActivityIndex:
    """Rebuildable SQLite projection over canonical Megas Activity JSONL stores.

    The JSONL history/timeline files remain authoritative. This database contains
    only redacted Activity projections plus source offsets and may be deleted or
    rebuilt at any time. It exists to keep Activity responsive as the canonical
    event history grows beyond 100,000 records.
    """

    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, mcp_dir: Path) -> None:
        self.root = mcp_dir / "cache"
        self.path = self.root / "activity-v1.sqlite3"
        self.root.mkdir(parents=True, exist_ok=True)
        with self._locks_guard:
            self._lock = self._locks.setdefault(str(self.path.resolve()), threading.RLock())
        self._ensure_schema_with_recovery()

    def sync(
        self,
        *,
        history_path: Path,
        timeline_path: Path,
        history_projector: Projector,
        timeline_projector: Projector,
    ) -> None:
        with self._lock:
            with self._connect() as connection:
                self._sync_source(
                    connection,
                    origin="history",
                    path=history_path,
                    projector=history_projector,
                )
                self._sync_source(
                    connection,
                    origin="timeline",
                    path=timeline_path,
                    projector=timeline_projector,
                )

    def query(
        self,
        *,
        limit: int,
        sources: set[str] | None = None,
        errors_only: bool = False,
        query: str = "",
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 5000))
        clauses: list[str] = []
        parameters: list[Any] = []

        if sources:
            ordered = sorted(sources)
            clauses.append("source IN (" + ",".join("?" for _ in ordered) + ")")
            parameters.extend(ordered)
        if errors_only:
            clauses.append("severity = 'error'")
        needle = query.strip().lower()
        if needle:
            clauses.append("search_text LIKE ? ESCAPE '\\'")
            parameters.append("%" + self._escape_like(needle) + "%")

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            "SELECT stream,row_id,at,source,severity,event,operation_id,project,"
            "session_id,tool_call_id,server,tool,duration_ms,message,details_json "
            "FROM activity" + where + " ORDER BY at DESC,row_id DESC LIMIT ?"
        )
        parameters.append(bounded_limit)

        with self._lock:
            with self._connect() as connection:
                rows = connection.execute(sql, parameters).fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                details = json.loads(row[14]) if row[14] else {}
            except json.JSONDecodeError:
                details = {}
            result.append(
                {
                    "id": row[1],
                    "at": row[2],
                    "source": row[3],
                    "severity": row[4],
                    "event": row[5],
                    "operation_id": row[6],
                    "project": row[7],
                    "session_id": row[8],
                    "tool_call_id": row[9],
                    "server": row[10],
                    "tool": row[11],
                    "duration_ms": row[12],
                    "message": row[13],
                    "details": details if isinstance(details, dict) else {},
                    "stream": row[0],
                }
            )
        return result

    def count(self) -> int:
        with self._lock:
            with self._connect() as connection:
                return int(connection.execute("SELECT COUNT(*) FROM activity").fetchone()[0])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_MS / 1000.0)
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA temp_store=MEMORY")
        return connection

    def _ensure_schema_with_recovery(self) -> None:
        with self._lock:
            try:
                self._ensure_schema()
            except sqlite3.DatabaseError:
                # The projection has no authority. A corrupt cache is safer to
                # discard than to prevent the local operator from starting.
                self._discard_database_files()
                self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_info(
                    singleton INTEGER NOT NULL PRIMARY KEY CHECK(singleton = 1),
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_info(singleton, version)
                VALUES(1, 1)
                ON CONFLICT(singleton) DO NOTHING;

                CREATE TABLE IF NOT EXISTS source_offsets(
                    origin TEXT NOT NULL PRIMARY KEY,
                    path TEXT NOT NULL,
                    offset INTEGER NOT NULL,
                    anchor TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS activity(
                    origin TEXT NOT NULL,
                    stream TEXT NOT NULL,
                    row_id TEXT NOT NULL,
                    at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    event TEXT NOT NULL,
                    operation_id TEXT,
                    project TEXT,
                    session_id TEXT,
                    tool_call_id TEXT,
                    server TEXT,
                    tool TEXT,
                    duration_ms REAL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    PRIMARY KEY(origin, row_id)
                );

                CREATE INDEX IF NOT EXISTS idx_activity_at
                    ON activity(at DESC, row_id DESC);
                CREATE INDEX IF NOT EXISTS idx_activity_source_at
                    ON activity(source, at DESC, row_id DESC);
                CREATE INDEX IF NOT EXISTS idx_activity_severity_at
                    ON activity(severity, at DESC, row_id DESC);
                CREATE INDEX IF NOT EXISTS idx_activity_operation
                    ON activity(operation_id);
                CREATE INDEX IF NOT EXISTS idx_activity_session
                    ON activity(session_id);
                """
            )
            version = int(
                connection.execute(
                    "SELECT version FROM schema_info WHERE singleton = 1"
                ).fetchone()[0]
            )
            if version != ACTIVITY_INDEX_VERSION:
                raise sqlite3.DatabaseError(
                    f"unsupported Activity index schema {version}"
                )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _sync_source(
        self,
        connection: sqlite3.Connection,
        *,
        origin: str,
        path: Path,
        projector: Projector,
    ) -> None:
        path = path.resolve()
        state = connection.execute(
            "SELECT path,offset,anchor,size,mtime_ns FROM source_offsets WHERE origin = ?",
            (origin,),
        ).fetchone()

        if not path.exists():
            with connection:
                connection.execute("DELETE FROM activity WHERE origin = ?", (origin,))
                connection.execute("DELETE FROM source_offsets WHERE origin = ?", (origin,))
            return
        if not path.is_file():
            raise ValueError(f"Activity index source is not a regular file: {path}")

        stat = path.stat()
        offset = int(state[1]) if state else 0
        reset = state is None or str(path) != str(state[0]) or offset > stat.st_size
        if not reset and offset > 0:
            reset = self._anchor(path, offset) != str(state[2] or "")
        if reset:
            with connection:
                connection.execute("DELETE FROM activity WHERE origin = ?", (origin,))
                connection.execute("DELETE FROM source_offsets WHERE origin = ?", (origin,))
            offset = 0

        pending: list[tuple[Any, ...]] = []
        safe_offset = offset
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                line_start = handle.tell()
                raw = handle.readline(MAX_INDEX_RECORD_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_INDEX_RECORD_BYTES and not raw.endswith(b"\n"):
                    # Consume the rest of an oversized record but never index it.
                    while raw and not raw.endswith(b"\n"):
                        raw = handle.readline(MAX_INDEX_RECORD_BYTES + 1)
                    safe_offset = handle.tell()
                    continue
                if not raw.endswith(b"\n"):
                    # A writer may be between write and newline. Leave the offset
                    # at the start of this record so the next sync retries it.
                    handle.seek(line_start)
                    break
                safe_offset = handle.tell()
                if not raw.strip() or len(raw) > MAX_INDEX_RECORD_BYTES:
                    continue
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(value, dict):
                    continue
                for projected in projector(value, line_start):
                    pending.append(self._db_row(origin, projected))
                    if len(pending) >= 2000:
                        self._insert_rows(connection, pending)
                        pending.clear()

        if pending:
            self._insert_rows(connection, pending)

        final_stat = path.stat()
        anchor = self._anchor(path, safe_offset)
        with connection:
            connection.execute(
                """
                INSERT INTO source_offsets(origin,path,offset,anchor,size,mtime_ns)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(origin) DO UPDATE SET
                    path=excluded.path,
                    offset=excluded.offset,
                    anchor=excluded.anchor,
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns
                """,
                (
                    origin,
                    str(path),
                    safe_offset,
                    anchor,
                    final_stat.st_size,
                    final_stat.st_mtime_ns,
                ),
            )

    @staticmethod
    def _insert_rows(connection: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
        with connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO activity(
                    origin,stream,row_id,at,source,severity,event,operation_id,
                    project,session_id,tool_call_id,server,tool,duration_ms,
                    message,details_json,search_text
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )

    @staticmethod
    def _db_row(origin: str, row: dict[str, Any]) -> tuple[Any, ...]:
        row_id = str(row.get("id") or "").strip()
        if not row_id:
            raise ValueError("Activity projection row requires an id")
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        searchable = " ".join(
            str(row.get(key) or "")
            for key in (
                "source",
                "severity",
                "event",
                "operation_id",
                "project",
                "session_id",
                "server",
                "tool",
                "message",
            )
        ).lower()
        return (
            origin,
            str(row.get("stream") or origin),
            row_id,
            str(row.get("at") or ""),
            str(row.get("source") or "MCP"),
            str(row.get("severity") or "info"),
            str(row.get("event") or "activity"),
            row.get("operation_id"),
            row.get("project"),
            row.get("session_id"),
            row.get("tool_call_id"),
            row.get("server"),
            row.get("tool"),
            row.get("duration_ms"),
            str(row.get("message") or row.get("event") or "activity"),
            json.dumps(details, separators=(",", ":"), sort_keys=True, default=str),
            searchable,
        )

    @staticmethod
    def _anchor(path: Path, offset: int) -> str:
        if offset <= 0:
            return ""
        start = max(0, offset - ANCHOR_BYTES)
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(offset - start)
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _discard_database_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(self.path) + suffix).unlink(missing_ok=True)
            except OSError:
                pass


__all__ = ["ACTIVITY_INDEX_VERSION", "ActivityIndex"]
