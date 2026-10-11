from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any
import uuid

from .mcp.store import MCPStore


@dataclass(slots=True)
class LocalTestingOperation:
    """Record one typed Local Testing operation in canonical Megas MCP Activity.

    The recorder persists only bounded identifiers and outcome metadata. Command
    output, file contents, ADB serials and other potentially sensitive payloads
    stay in their typed response path and are never copied into Activity here.
    """

    action: str
    project: str | None = None
    target_id: str | None = None
    service: str | None = None
    store: MCPStore = field(default_factory=MCPStore)
    operation_id: str = field(default_factory=lambda: f"op_local_{uuid.uuid4().hex}")
    started: float = 0.0

    @staticmethod
    def _bounded(value: Any, limit: int = 512) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        return " ".join(text.split())[:limit]

    def _identity(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "operation_id": self.operation_id,
            "source": "Runtime",
            "action": self._bounded(self.action, 64),
        }
        project = self._bounded(self.project)
        target_id = self._bounded(self.target_id)
        service = self._bounded(self.service)
        if project:
            row["project"] = project
        if target_id:
            row["target_id"] = target_id
        if service:
            row["service"] = service
        return row

    def __enter__(self) -> "LocalTestingOperation":
        self.started = time.monotonic()
        self.store.record(
            "runtime.local_testing.operation.started",
            severity="info",
            **self._identity(),
        )
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_val is not None:
            self.fail(exc_val)

    def complete(self, result: dict[str, Any]) -> dict[str, Any]:
        duration_ms = max(0, int((time.monotonic() - self.started) * 1000))
        ok = bool(result.get("ok", True))
        fields: dict[str, Any] = {
            **self._identity(),
            "severity": "success" if ok else "error",
            "duration_ms": duration_ms,
            "ok": ok,
        }
        stage = self._bounded(result.get("stage"), 64)
        kind = self._bounded(result.get("kind"), 64)
        if stage:
            fields["stage"] = stage
        if kind:
            fields["kind"] = kind
        for key in ("returncode", "crash_anr_count", "line_limit"):
            value = result.get(key)
            if isinstance(value, (bool, int, float)):
                fields[key] = value
        self.store.record("runtime.local_testing.operation.completed", **fields)
        value = dict(result)
        value["operation_id"] = self.operation_id
        value["duration_ms"] = duration_ms
        return value

    def fail(self, exc: BaseException) -> None:
        duration_ms = max(0, int((time.monotonic() - self.started) * 1000))
        self.store.record(
            "runtime.local_testing.operation.failed",
            severity="error",
            duration_ms=duration_ms,
            exception_type=type(exc).__name__,
            **self._identity(),
        )


__all__ = ["LocalTestingOperation"]
