from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
import errno
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterator
import uuid


TASK_STORE_VERSION = 1
TASK_STATES = frozenset(
    {
        "open",
        "assigned",
        "running",
        "waiting_approval",
        "waiting_capacity",
        "recovering",
        "paused",
        "failed",
        "completed",
        "cancelled",
    }
)
MAX_TASKS = 2000
MAX_ASSIGNMENT_HISTORY = 64


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _home() -> Path:
    configured = os.environ.get("MEGAS_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".megas"


def _task_id() -> str:
    return f"task_{uuid.uuid4().hex}"


@dataclass(slots=True)
class TaskRecord:
    title: str
    id: str = field(default_factory=_task_id)
    project: str | None = None
    action_id: str | None = None
    state: str = "open"
    assigned_client: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    assignment_generation: int = 0
    assignment_history: list[dict[str, Any]] = field(default_factory=list)
    description: str = ""
    priority: str = "medium"
    due_date: str | None = None
    depends_on: list[str] = field(default_factory=list)
    completion_note: str = ""
    project_authority_scope: str | None = None
    continuity: dict[str, Any] = field(default_factory=dict)
    shared_context: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.action_id is not None and (not isinstance(self.action_id, str) or not self.action_id.startswith("action_") or len(self.action_id) > 80):
            raise ValueError("invalid action id")
        if not isinstance(self.shared_context, dict) or len(json.dumps(self.shared_context).encode("utf-8")) > 64 * 1024:
            raise ValueError("invalid or oversized shared task context")
        if not isinstance(self.continuity, dict) or len(json.dumps(self.continuity).encode("utf-8")) > 128 * 1024:
            raise ValueError("invalid or oversized task continuity record")
        if self.project_authority_scope is not None and (not isinstance(self.project_authority_scope, str) or not self.project_authority_scope or len(self.project_authority_scope) > 128):
            raise ValueError("invalid project authority scope")
        if self.priority not in {"low", "medium", "high", "urgent"}:
            raise ValueError("invalid task priority")
        if len(self.description) > 16000 or len(self.completion_note) > 4000:
            raise ValueError("task details exceed the supported limit")
        if self.due_date is not None:
            from datetime import date
            if date.fromisoformat(self.due_date).isoformat() != self.due_date:
                raise ValueError("due_date must be YYYY-MM-DD")
        if len(self.depends_on) > 32 or len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("task dependencies must be unique and bounded")
        self.title = self.title.strip()
        if not self.id.startswith("task_") or len(self.id) > 80:
            raise ValueError("invalid task id")
        if not self.title or len(self.title) > 4000:
            raise ValueError("task title must be between 1 and 4000 characters")
        if self.project is not None:
            self.project = self.project.strip() or None
            if self.project is not None and len(self.project) > 256:
                raise ValueError("task project exceeds 256 characters")
        if self.state not in TASK_STATES:
            raise ValueError("invalid task state")
        if self.assigned_client is not None:
            self.assigned_client = self.assigned_client.strip().lower() or None
            if self.assigned_client is not None and len(self.assigned_client) > 64:
                raise ValueError("assigned client exceeds 64 characters")
        if self.assignment_generation < 0:
            raise ValueError("assignment generation cannot be negative")
        if len(self.assignment_history) > MAX_ASSIGNMENT_HISTORY:
            self.assignment_history = self.assignment_history[-MAX_ASSIGNMENT_HISTORY:]

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskRecord":
        row = cls(
            shared_context=value.get("shared_context", {}),
            continuity=value.get("continuity", {}),
            description=str(value.get("description") or ""),
            priority=str(value.get("priority") or "medium"),
            due_date=value.get("due_date"),
            depends_on=list(value.get("depends_on") or []),
            completion_note=str(value.get("completion_note") or ""),
            id=str(value.get("id") or ""),
            title=str(value.get("title") or ""),
            project=str(value.get("project") or "").strip() or None,
            action_id=value.get("action_id"),
            project_authority_scope=value.get("project_authority_scope"),
            state=str(value.get("state") or "open"),
            assigned_client=str(value.get("assigned_client") or "").strip() or None,
            created_at=str(value.get("created_at") or _now()),
            updated_at=str(value.get("updated_at") or _now()),
            assignment_generation=int(value.get("assignment_generation") or 0),
            assignment_history=[
                dict(item)
                for item in (value.get("assignment_history") or [])
                if isinstance(item, dict)
            ],
        )
        row.validate()
        return row


class AgentTaskStore:
    """Durable Megas-owned task/agent binding state.

    A task is independent from an agent. Assignment changes only participation;
    it never deletes or replaces the task. This is the local authoritative
    binding used by the Windows operator and embedded MCP projections.
    """

    def __init__(self, home: Path | None = None) -> None:
        root = home or _home()
        self.root = root / "mcp" / "tasks"
        self.path = self.root / "tasks.json"
        self.lock_path = self.root / ".tasks.lock"
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _lock(self, *, timeout: float = 5.0) -> Iterator[None]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
                finally:
                    os.close(fd)
                break
            except (FileExistsError, PermissionError) as exc:
                # Windows can report access denied while another writer's
                # exclusive lock file is being opened or deleted. Retry only
                # those Windows contention errors, with the same deadline.
                if isinstance(exc, PermissionError) and not (
                    os.name == "nt" and (
                        getattr(exc, "winerror", None) in {5, 32, 33}
                        or (getattr(exc, "winerror", None) is None and exc.errno == errno.EACCES)
                    )
                ):
                    raise
                try:
                    if time.time() - self.lock_path.stat().st_mtime > 30:
                        self.lock_path.unlink()
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("agent task store is busy")
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

    def _load_unlocked(self) -> list[TaskRecord]:
        if not self.path.exists():
            return []
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or int(value.get("version") or 0) != TASK_STORE_VERSION:
            raise ValueError("unsupported agent task store version")
        rows = value.get("tasks")
        if not isinstance(rows, list):
            raise ValueError("agent task store is malformed")
        tasks = [TaskRecord.from_dict(row) for row in rows if isinstance(row, dict)]
        if len(tasks) != len(rows):
            raise ValueError("agent task store contains malformed records")
        if len(tasks) > MAX_TASKS:
            raise ValueError("agent task store exceeds the supported task limit")
        ids = [task.id for task in tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("agent task store contains duplicate task ids")
        return tasks

    def _save_unlocked(self, tasks: list[TaskRecord]) -> None:
        if len(tasks) > MAX_TASKS:
            raise ValueError("agent task store exceeds the supported task limit")
        # Project metadata shares this authority and must survive GUI task edits.
        document = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self._atomic(
            self.path,
            {
                **document,
                "version": TASK_STORE_VERSION,
                "tasks": [task.to_dict() for task in tasks],
                "projects": document.get("projects", []),
            },
        )

    def list(self, *, include_terminal: bool = True) -> list[dict[str, Any]]:
        tasks = self._load_unlocked()
        if not include_terminal:
            tasks = [task for task in tasks if task.state not in {"completed", "cancelled"}]
        return [
            task.to_dict()
            for task in sorted(tasks, key=lambda item: item.updated_at, reverse=True)
        ]

    def get(self, task_id: str) -> TaskRecord:
        selected = str(task_id or "").strip()
        for task in self._load_unlocked():
            if task.id == selected:
                return task
        raise KeyError(f"unknown task: {selected}")

    def create(self, title: str, *, project: str | None = None) -> dict[str, Any]:
        task = TaskRecord(title=str(title or ""), project=project)
        task.validate()
        with self._lock():
            tasks = self._load_unlocked()
            if len(tasks) >= MAX_TASKS:
                # Retain every active task and prune only the oldest terminal rows.
                tasks.sort(key=lambda item: item.updated_at)
                while len(tasks) >= MAX_TASKS:
                    index = next(
                        (
                            i
                            for i, item in enumerate(tasks)
                            if item.state in {"completed", "cancelled"}
                        ),
                        None,
                    )
                    if index is None:
                        raise RuntimeError("agent task capacity is full with active tasks")
                    tasks.pop(index)
            tasks.append(task)
            self._save_unlocked(tasks)
        return task.to_dict()

    def assign(self, task_id: str, client: str) -> dict[str, Any]:
        selected_client = str(client or "").strip().lower()
        if not selected_client or len(selected_client) > 64:
            raise ValueError("agent client is required")
        with self._lock():
            tasks = self._load_unlocked()
            task = next((item for item in tasks if item.id == task_id), None)
            if task is None:
                raise KeyError(f"unknown task: {task_id}")
            if task.state in {"completed", "cancelled"}:
                raise RuntimeError("terminal tasks cannot be reassigned")
            previous = task.assigned_client
            task.assigned_client = selected_client
            task.assignment_generation += 1
            task.state = "assigned"
            task.updated_at = _now()
            task.assignment_history.append(
                {
                    "at": task.updated_at,
                    "generation": task.assignment_generation,
                    "from": previous,
                    "to": selected_client,
                    "event": "assigned" if previous is None else "reassigned",
                }
            )
            task.validate()
            self._save_unlocked(tasks)
            return task.to_dict()

    def unassign(self, task_id: str) -> dict[str, Any]:
        with self._lock():
            tasks = self._load_unlocked()
            task = next((item for item in tasks if item.id == task_id), None)
            if task is None:
                raise KeyError(f"unknown task: {task_id}")
            previous = task.assigned_client
            task.assigned_client = None
            task.assignment_generation += 1
            if task.state not in {"completed", "cancelled"}:
                task.state = "open"
            task.updated_at = _now()
            task.assignment_history.append(
                {
                    "at": task.updated_at,
                    "generation": task.assignment_generation,
                    "from": previous,
                    "to": None,
                    "event": "unassigned",
                }
            )
            task.validate()
            self._save_unlocked(tasks)
            return task.to_dict()

    def set_state(self, task_id: str, state: str) -> dict[str, Any]:
        selected = str(state or "").strip().lower()
        if selected not in TASK_STATES:
            raise ValueError("invalid task state")
        with self._lock():
            tasks = self._load_unlocked()
            task = next((item for item in tasks if item.id == task_id), None)
            if task is None:
                raise KeyError(f"unknown task: {task_id}")
            task.state = selected
            task.updated_at = _now()
            task.validate()
            self._save_unlocked(tasks)
            return task.to_dict()


__all__ = ["AgentTaskStore", "TaskRecord", "TASK_STATES", "TASK_STORE_VERSION"]
