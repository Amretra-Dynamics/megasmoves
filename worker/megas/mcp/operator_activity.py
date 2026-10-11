from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Iterator

from .store import MCPStore
from .timeline import _pid_alive


SAFE_ID_FIELDS = (
    "workspace_id",
    "project_id",
    "product_id",
    "screen_id",
    "schema_id",
    "workflow_id",
    "version_id",
    "token_set_id",
    "component_definition_id",
    "change_set_id",
    "task_id",
    "browser_session_id",
)
MAX_ACTIVITY_LABEL = 128
OPERATION_RECOVERY_SCAN_LIMIT = 10_000
_OPERATION_TERMINAL_SUFFIXES = (
    ".operation.completed",
    ".operation.failed",
    ".operation.interrupted",
)
_PROCESS_INSTANCE_ID = f"proc_{uuid.uuid4().hex}"
_RECOVERY_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class OperatorRequestContext:
    transport: str = "mcp"
    agent: str | None = None
    agent_version: str | None = None


_REQUEST_CONTEXT: ContextVar[OperatorRequestContext | None] = ContextVar(
    "megas_operator_request_context",
    default=None,
)
_OPERATION_ID_CONTEXT: ContextVar[str | None] = ContextVar(
    "megas_operator_operation_id",
    default=None,
)


def _safe_label(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    # Client names/versions are untrusted MCP metadata. Keep only printable,
    # single-line bounded labels suitable for a local structured activity row.
    text = " ".join(text.split())
    return text[:MAX_ACTIVITY_LABEL]


@contextmanager
def operator_request_context(
    *,
    transport: str,
    client_info: dict[str, Any] | None = None,
) -> Iterator[OperatorRequestContext]:
    info = client_info if isinstance(client_info, dict) else {}
    context = OperatorRequestContext(
        transport=_safe_label(transport) or "mcp",
        agent=_safe_label(info.get("name")),
        agent_version=_safe_label(info.get("version")),
    )
    token = _REQUEST_CONTEXT.set(context)
    try:
        yield context
    finally:
        _REQUEST_CONTEXT.reset(token)


def current_operator_request_context() -> OperatorRequestContext | None:
    return _REQUEST_CONTEXT.get()


def current_operator_operation_id() -> str | None:
    """Return the operation currently executing inside the Megas MCP operator.

    The value is deliberately a process-local correlation identifier, not an
    authentication credential. It is used only as bounded local correlation
    metadata and never contains an MCP request body.
    """

    return _OPERATION_ID_CONTEXT.get()


def _safe_identity_fields(arguments: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in SAFE_ID_FIELDS:
        raw = arguments.get(key)
        if raw is None:
            continue
        value = str(raw).strip()
        if value and len(value) <= 512:
            values[key] = value
    return values


def _started_process_is_alive(row: dict[str, Any]) -> bool:
    raw_pid = row.get("process_pid")
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    return _pid_alive(pid)


@contextmanager
def _operator_recovery_lock(store: MCPStore) -> Iterator[None]:
    """Serialize history reconciliation across threads and Megas processes.

    The lock file itself may persist, but ownership is an OS file lock rather
    than a PID marker. Windows/POSIX release the lock automatically if the
    process dies, so crash recovery cannot be permanently blocked by a stale
    lock record.
    """

    lock_path = store.history_path.with_name(store.history_path.name + ".recovery.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def recover_interrupted_operator_operations(
    store: MCPStore,
    *,
    limit: int = OPERATION_RECOVERY_SCAN_LIMIT,
) -> list[str]:
    """Close dead-process started-without-terminal operator evidence.

    Recovery never retries or guesses the external side effect. It only appends an
    explicit ``*.operation.interrupted`` terminal fact using fields that were already
    safe enough to persist in the original started event. Operations owned by a live
    Megas process are deliberately left alone so simultaneous stdio/HTTP operators do
    not misclassify each other's active work as crashed.
    """

    rows = store.history(max(1, min(int(limit), OPERATION_RECOVERY_SCAN_LIMIT)))
    started: dict[str, dict[str, Any]] = {}
    terminal: set[str] = set()
    for row in rows:
        operation_id = str(row.get("operation_id") or "").strip()
        event = str(row.get("event") or "")
        if not operation_id:
            continue
        if event.endswith(".operation.started"):
            started[operation_id] = row
        elif event.endswith(_OPERATION_TERMINAL_SUFFIXES):
            terminal.add(operation_id)

    interrupted: list[str] = []
    for operation_id, row in started.items():
        if operation_id in terminal or _started_process_is_alive(row):
            continue
        event = str(row.get("event") or "")
        if not event.endswith(".operation.started"):
            continue
        prefix = event[: -len(".operation.started")]
        fields: dict[str, Any] = {
            "operation_id": operation_id,
            "tool": _safe_label(row.get("tool")) or "unknown",
            "recovery_reason": "operator_restart",
            "started_at": str(row.get("at") or "")[:128],
        }
        try:
            process_pid = int(row.get("process_pid"))
        except (TypeError, ValueError):
            process_pid = 0
        if process_pid > 0:
            fields["process_pid"] = process_pid
        process_instance = _safe_label(row.get("process_instance"))
        if process_instance:
            fields["process_instance"] = process_instance
        for key in ("transport", "agent", "agent_version"):
            value = _safe_label(row.get(key))
            if value:
                fields[key] = value
        for key in SAFE_ID_FIELDS:
            raw = row.get(key)
            if raw is None:
                continue
            value = str(raw).strip()
            if value and len(value) <= 512:
                fields[key] = value
        store.record(f"{prefix}.operation.interrupted", **fields)
        interrupted.append(operation_id)
    return interrupted


def reconcile_operator_activity_once(store: MCPStore) -> list[str]:
    """Reconcile dead-process evidence under a cross-process recovery lock.

    The historical name is retained for compatibility. The operation is now
    deliberately safe to call repeatedly: each scan re-reads terminal evidence,
    so a sibling operator that crashes after this process started is reconciled
    on the next startup/operation without producing duplicate terminal rows.
    """

    with _RECOVERY_LOCK:
        with _operator_recovery_lock(store):
            return recover_interrupted_operator_operations(store)


@dataclass(slots=True)
class OperatorOperation:
    store: MCPStore
    tool: str
    arguments: dict[str, Any]
    transport: str = "mcp"
    operation_id: str = ""
    started: float = 0.0
    _operation_token: Token[str | None] | None = field(default=None, init=False, repr=False)

    def _prefix(self) -> str:
        return (
            "workspace"
            if self.tool.startswith("workspace_")
            else "browser"
            if self.tool.startswith("browser_")
            else "operator"
        )

    def _context_fields(self) -> dict[str, str]:
        current = current_operator_request_context()
        fields = {"transport": current.transport if current else self.transport}
        if current and current.agent:
            fields["agent"] = current.agent
        if current and current.agent_version:
            fields["agent_version"] = current.agent_version
        return fields

    def _process_fields(self) -> dict[str, Any]:
        return {
            "process_pid": os.getpid(),
            "process_instance": _PROCESS_INSTANCE_ID,
        }

    def __enter__(self) -> "OperatorOperation":
        # Reconcile evidence from a previous dead process before admitting new
        # work. Live sibling operators sharing MEGAS_HOME are not interrupted.
        reconcile_operator_activity_once(self.store)
        self.operation_id = f"op_{uuid.uuid4().hex}"
        self.started = time.monotonic()
        self._operation_token = _OPERATION_ID_CONTEXT.set(self.operation_id)
        self.store.record(
            f"{self._prefix()}.operation.started",
            operation_id=self.operation_id,
            tool=self.tool,
            **self._process_fields(),
            **self._context_fields(),
            **_safe_identity_fields(self.arguments),
        )
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        token = self._operation_token
        self._operation_token = None
        if token is not None:
            _OPERATION_ID_CONTEXT.reset(token)

    def complete(self, result: dict[str, Any]) -> None:
        duration_ms = max(0, int((time.monotonic() - self.started) * 1000))
        self.store.record(
            f"{self._prefix()}.operation.completed",
            operation_id=self.operation_id,
            tool=self.tool,
            duration_ms=duration_ms,
            is_error=bool(result.get("isError", result.get("is_error", False))),
            **self._process_fields(),
            **self._context_fields(),
            **_safe_identity_fields(self.arguments),
        )

    def fail(self, exc: BaseException) -> None:
        duration_ms = max(0, int((time.monotonic() - self.started) * 1000))
        # Persist the exception type only. Third-party exception messages can
        # contain response bodies or echoed credentials and are not log-safe.
        self.store.record(
            f"{self._prefix()}.operation.failed",
            operation_id=self.operation_id,
            tool=self.tool,
            duration_ms=duration_ms,
            exception_type=type(exc).__name__,
            **self._process_fields(),
            **self._context_fields(),
            **_safe_identity_fields(self.arguments),
        )


def attach_operation_metadata(result: dict[str, Any], operation_id: str) -> dict[str, Any]:
    value = dict(result)
    meta = value.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
    else:
        meta = dict(meta)
    meta["megas/operationId"] = operation_id
    value["_meta"] = meta
    return value


__all__ = [
    "MAX_ACTIVITY_LABEL",
    "OPERATION_RECOVERY_SCAN_LIMIT",
    "OperatorOperation",
    "OperatorRequestContext",
    "SAFE_ID_FIELDS",
    "attach_operation_metadata",
    "current_operator_operation_id",
    "current_operator_request_context",
    "operator_request_context",
    "reconcile_operator_activity_once",
    "recover_interrupted_operator_operations",
]
