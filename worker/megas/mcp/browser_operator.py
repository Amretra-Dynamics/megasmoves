from __future__ import annotations

import asyncio
import atexit
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
import json
import threading
from typing import Any, Coroutine
import uuid

from megas import local_testing
from megas.agent_tasks import AgentTaskStore
from megas.operate.browser.capability import browser_capability_report
from megas.operate.browser.control import BrowserFailure
from megas.operate.browser.runtime import BrowserSessionManager
from megas.operate.gate import is_operate_enabled

from .operator_activity import current_operator_request_context


BROWSER_STATUS_URI = "megas://browser/status"
MAX_BROWSER_CALL_SECONDS = 75.0


def _strict_object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _string(*, max_length: int = 4000, enum: list[str] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "string", "maxLength": max_length}
    if enum is not None:
        value["enum"] = list(enum)
    return value


_SESSION_ID = _string(max_length=128)
_TASK_ID = _string(max_length=128)
_TAB_ID = _string(max_length=128)
_TIMEOUT = {"type": "integer", "minimum": 250, "maximum": 60000}
_TARGET = _strict_object(
    {
        "ref": _string(max_length=128),
        "role": _string(max_length=80),
        "name": _string(max_length=500),
        "label": _string(max_length=500),
        "placeholder": _string(max_length=500),
        "text": _string(max_length=1000),
        "test_id": _string(max_length=256),
    }
)
_POSTCONDITION = _strict_object(
    {
        "url_contains": _string(max_length=1000),
        "text_contains": _string(max_length=1000),
        "target_hidden": {"type": "boolean"},
        "target_visible": {"type": "boolean"},
        "value_equals": _string(max_length=4000),
    }
)


def _tool(
    name: str,
    title: str,
    description: str,
    schema: dict[str, Any],
    *,
    read_only: bool,
    destructive: bool = False,
    open_world: bool = True,
) -> dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": schema,
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "openWorldHint": open_world,
        },
        "securitySchemes": [{"type": "noauth"}],
    }


def browser_tools() -> list[dict[str, Any]]:
    session_only = _strict_object({"browser_session_id": _SESSION_ID}, ["browser_session_id"])
    targeted = {
        "browser_session_id": _SESSION_ID,
        "target": _TARGET,
        "tab_id": _TAB_ID,
        "timeout_ms": _TIMEOUT,
        "postcondition": _POSTCONDITION,
    }
    tools = [
        _tool(
            "browser_status",
            "Browser Status",
            "Show the dedicated Megas Browser runtime capability and sessions owned by this operator process.",
            _strict_object({"browser_session_id": _SESSION_ID}),
            read_only=True,
            open_world=False,
        ),
        _tool(
            "browser_start",
            "Start Browser Session",
            "Start a task-scoped dedicated Playwright browser session. The task must already exist in MegasMoves.",
            _strict_object(
                {
                    "task_id": _TASK_ID,
                    "profile_mode": _string(max_length=16, enum=["ephemeral", "persistent"]),
                    "profile_id": _string(max_length=128),
                    "initial_url": _string(max_length=4000),
                },
                ["task_id"],
            ),
            read_only=False,
        ),
        _tool(
            "browser_open",
            "Open URL",
            "Navigate the dedicated browser to an HTTP(S) URL and verify the resulting destination.",
            _strict_object(
                {
                    "browser_session_id": _SESSION_ID,
                    "url": _string(max_length=4000),
                    "timeout_ms": _TIMEOUT,
                },
                ["browser_session_id", "url"],
            ),
            read_only=False,
        ),
        _tool("browser_observe", "Observe Page", "Return bounded page state and semantic element references.", session_only, read_only=True),
        _tool(
            "browser_click",
            "Click",
            "Click a semantic target or previously observed element reference and prove a state transition.",
            _strict_object({**targeted}, ["browser_session_id", "target"]),
            read_only=False,
        ),
        _tool(
            "browser_fill",
            "Fill Field",
            "Fill a semantic field and verify the resulting value. Password fields remain human/credential flows.",
            _strict_object({**targeted, "value": _string(max_length=20000)}, ["browser_session_id", "target", "value"]),
            read_only=False,
        ),
        _tool(
            "browser_select",
            "Select Option",
            "Select a value on a semantic target and verify the selection.",
            _strict_object({**targeted, "value": _string(max_length=4000)}, ["browser_session_id", "target", "value"]),
            read_only=False,
        ),
        _tool(
            "browser_upload",
            "Upload Approved File",
            "Attach a file already granted to this task/session by opaque file_ref. Arbitrary local paths are never accepted. Sensitive actions require a local human approval.",
            _strict_object({**targeted, "file_ref": _string(max_length=128)}, ["browser_session_id", "target", "file_ref"]),
            read_only=False,
        ),
        _tool(
            "browser_download",
            "Download to Quarantine",
            "Trigger a semantic download into Megas quarantine. Sensitive actions require a local human approval.",
            _strict_object({**targeted}, ["browser_session_id", "target"]),
            read_only=False,
        ),
        _tool("browser_tabs", "List Tabs", "List tabs bound to the current Megas browser session.", session_only, read_only=True),
        _tool(
            "browser_switch",
            "Switch Tab",
            "Switch to a tab that was already observed in the current browser session.",
            _strict_object({"browser_session_id": _SESSION_ID, "tab_id": _TAB_ID}, ["browser_session_id", "tab_id"]),
            read_only=False,
        ),
        _tool(
            "browser_wait",
            "Wait for Evidence",
            "Wait boundedly for an explicit URL/text/visibility/value condition. Fixed sleeps are not exposed.",
            _strict_object(
                {
                    "browser_session_id": _SESSION_ID,
                    "condition": _POSTCONDITION,
                    "timeout_ms": _TIMEOUT,
                },
                ["browser_session_id", "condition"],
            ),
            read_only=True,
        ),
        _tool(
            "browser_screenshot",
            "Screenshot",
            "Capture a bounded JPEG screenshot of the current Megas browser page.",
            _strict_object({"browser_session_id": _SESSION_ID, "full_page": {"type": "boolean"}}, ["browser_session_id"]),
            read_only=True,
        ),
        _tool(
            "browser_takeover_request",
            "Request Human Takeover",
            "Pause agent browser writes while preserving session state for an operator takeover.",
            session_only,
            read_only=False,
        ),
        _tool(
            "browser_takeover_release",
            "Release Human Takeover",
            "Return a paused browser session to agent control. The local operator remains the authority for when this is appropriate.",
            session_only,
            read_only=False,
        ),
        _tool(
            "browser_close",
            "Close Browser Session",
            "Close the dedicated browser session and release its task-scoped runtime resources.",
            session_only,
            read_only=False,
        ),
    ]
    return tools


def _trusted_folders() -> list[str]:
    try:
        snapshot = local_testing.workspace_snapshot()
    except Exception:
        return []
    roots: list[str] = []
    for row in snapshot.get("projects") or []:
        if not isinstance(row, dict):
            continue
        root = str(row.get("root") or "").strip()
        if root and root not in roots:
            roots.append(root)
    return roots


class BrowserServiceHost:
    """Own Playwright on one private event-loop thread for synchronous MCP calls."""

    def __init__(self) -> None:
        self._gate = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._manager: BrowserSessionManager | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        atexit.register(self.shutdown)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self._loop = loop
            self._manager = BrowserSessionManager([])
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            loop.close()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            manager = self._manager
            if manager is not None:
                try:
                    loop.run_until_complete(manager.shutdown())
                except BaseException:
                    pass
            self._manager = None
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                try:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except BaseException:
                    pass
            loop.close()

    def _ensure(self) -> tuple[asyncio.AbstractEventLoop, BrowserSessionManager]:
        with self._gate:
            if self._thread is None or not self._thread.is_alive():
                self._ready.clear()
                self._startup_error = None
                self._thread = threading.Thread(
                    target=self._thread_main,
                    name="MegasBrowserService",
                    daemon=True,
                )
                self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("Megas Browser service did not initialize before its deadline")
        if self._startup_error is not None:
            raise RuntimeError("Megas Browser service failed to initialize") from self._startup_error
        if self._loop is None or self._manager is None:
            raise RuntimeError("Megas Browser service is unavailable")
        return self._loop, self._manager

    def _submit(self, coroutine: Coroutine[Any, Any, Any], *, timeout: float = MAX_BROWSER_CALL_SECONDS) -> Any:
        loop, _manager = self._ensure()
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError("Megas Browser operation exceeded its service deadline") from exc

    async def _start_async(self, command: dict[str, Any], roots: list[str]) -> dict[str, Any]:
        assert self._manager is not None
        self._manager.allowed_folders = list(roots)
        return await self._manager.start(command)

    def start(self, command: dict[str, Any]) -> dict[str, Any]:
        self._ensure()
        return dict(self._submit(self._start_async(command, _trusted_folders())))

    def execute(self, session_id: str, action: dict[str, Any]) -> dict[str, Any]:
        _loop, manager = self._ensure()
        action_id = f"browser_action_{uuid.uuid4().hex}"
        return dict(self._submit(manager.execute(session_id, action_id, action, None)))

    def stop(self, session_id: str) -> dict[str, Any]:
        _loop, manager = self._ensure()
        return dict(self._submit(manager.stop(session_id, "mcp_requested")))

    async def _snapshots_async(self) -> list[dict[str, Any]]:
        assert self._manager is not None
        rows: list[dict[str, Any]] = []
        for session_id in list(self._manager.sessions):
            try:
                rows.append(self._manager.operator_snapshot(session_id))
            except BrowserFailure:
                continue
        return rows

    def snapshots(self) -> list[dict[str, Any]]:
        if self._thread is None or not self._thread.is_alive():
            return []
        return list(self._submit(self._snapshots_async(), timeout=5.0))

    def shutdown(self) -> None:
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None or not thread.is_alive():
            return
        try:
            if self._manager is not None:
                future = asyncio.run_coroutine_threadsafe(self._manager.shutdown(), loop)
                try:
                    future.result(timeout=5.0)
                except BaseException:
                    future.cancel()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5.0)
            self._thread = None
            self._loop = None


def _action_params(arguments: dict[str, Any], *, include_target: bool = False) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if include_target:
        target = arguments.get("target")
        if isinstance(target, dict):
            params["target"] = dict(target)
    for key in ("tab_id", "timeout_ms", "postcondition"):
        if key in arguments:
            params[key] = arguments[key]
    return params


def _safe_failure(failure: BrowserFailure) -> dict[str, Any]:
    evidence = failure.evidence if isinstance(failure.evidence, dict) else {}
    return {
        "code": failure.code,
        "message": str(failure),
        "recoverable": failure.recoverable,
        "human_required": failure.human_required,
        "evidence": {
            "error_code": evidence.get("error_code"),
            "action_type": evidence.get("action_type"),
            "retry_count": evidence.get("retry_count"),
            "screenshot_captured": bool(evidence.get("screenshot")),
            "trace_captured": bool(evidence.get("trace")),
            "console_event_count": len(evidence.get("console") or []),
            "network_event_count": len(evidence.get("network") or []),
        },
    }


def _mcp_result(value: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)}],
        "structuredContent": value,
        "isError": is_error,
    }


@dataclass(slots=True)
class BrowserOperatorCatalog:
    task_store: AgentTaskStore = field(default_factory=AgentTaskStore)
    host: BrowserServiceHost = field(default_factory=BrowserServiceHost)

    def tools(self) -> list[dict[str, Any]]:
        return browser_tools()

    def status(self, session_id: str | None = None) -> dict[str, Any]:
        capability = browser_capability_report()
        sessions = self.host.snapshots()
        if session_id:
            sessions = [row for row in sessions if row.get("browser_session_id") == session_id]
        return {
            "available": capability.available,
            "operate_enabled": is_operate_enabled(),
            "dedicated_runtime": capability.dedicated,
            "browser": capability.browser,
            "runtime_root_configured": bool(capability.runtime_root),
            "reasons": list(capability.reasons),
            "sessions": sessions,
            "session_count": len(sessions),
            "control_plane": "Megas MCP",
            "executor": "Playwright",
        }

    def _execute(self, session_id: str, action_type: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            value = self.host.execute(session_id, {"action_type": action_type, "params": params})
            return _mcp_result(value)
        except BrowserFailure as failure:
            return _mcp_result({"error": _safe_failure(failure)}, is_error=True)

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("browser tool arguments must be an object")
        if name == "browser_status":
            return _mcp_result(self.status(str(arguments.get("browser_session_id") or "").strip() or None))

        if name == "browser_start":
            task_id = str(arguments.get("task_id") or "").strip()
            task = self.task_store.get(task_id)
            profile_mode = str(arguments.get("profile_mode") or "ephemeral").strip().lower()
            if profile_mode not in {"ephemeral", "persistent"}:
                raise ValueError("profile_mode must be ephemeral or persistent")
            profile_id = str(arguments.get("profile_id") or "").strip() or None
            if profile_mode == "persistent" and not profile_id:
                raise ValueError("persistent browser sessions require profile_id")
            request = current_operator_request_context()
            browser_session_id = f"browser_{uuid.uuid4().hex}"
            command_session_id = f"session_{uuid.uuid4().hex}"
            command = {
                "session_id": command_session_id,
                "task_id": task_id,
                "instruction": task.title,
                "agent_id": request.agent if request else None,
                "browser_session": {
                    "id": browser_session_id,
                    "session_id": command_session_id,
                    "task_id": task_id,
                    "agent_id": request.agent if request else None,
                    "browser_type": "chromium",
                    "profile": {"mode": profile_mode, "id": profile_id},
                    # File capabilities are deliberately absent here. They are
                    # granted only by the local human/operator surface.
                    "file_capabilities": [],
                    "objective": task.title,
                },
                "context_pack": {
                    "cognitive_policy": {
                        "browser": {
                            "initial_url": str(arguments.get("initial_url") or "about:blank")
                        }
                    }
                },
            }
            try:
                state = self.host.start(command)
            except BrowserFailure as failure:
                return _mcp_result({"error": _safe_failure(failure)}, is_error=True)
            return _mcp_result(
                {
                    "browser_session_id": browser_session_id,
                    "task_id": task_id,
                    "profile_mode": profile_mode,
                    "state": state,
                }
            )

        session_id = str(arguments.get("browser_session_id") or "").strip()
        if not session_id:
            raise ValueError("browser_session_id is required")
        if name == "browser_open":
            params = {"url": str(arguments.get("url") or "")}
            if "timeout_ms" in arguments:
                params["timeout_ms"] = arguments["timeout_ms"]
            return self._execute(session_id, "navigate", params)
        if name == "browser_observe":
            return self._execute(session_id, "observe", {})
        if name == "browser_click":
            return self._execute(session_id, "click", _action_params(arguments, include_target=True))
        if name == "browser_fill":
            params = _action_params(arguments, include_target=True)
            params["value"] = str(arguments.get("value") or "")
            return self._execute(session_id, "fill_field", params)
        if name == "browser_select":
            params = _action_params(arguments, include_target=True)
            params["value"] = str(arguments.get("value") or "")
            return self._execute(session_id, "select_option", params)
        if name == "browser_upload":
            params = _action_params(arguments, include_target=True)
            params["file_ref"] = str(arguments.get("file_ref") or "")
            return self._execute(session_id, "upload_file", params)
        if name == "browser_download":
            return self._execute(session_id, "download", _action_params(arguments, include_target=True))
        if name == "browser_tabs":
            return self._execute(session_id, "list_tabs", {})
        if name == "browser_switch":
            return self._execute(session_id, "switch_tab", {"tab_id": str(arguments.get("tab_id") or "")})
        if name == "browser_wait":
            params = {"condition": dict(arguments.get("condition") or {})}
            if "timeout_ms" in arguments:
                params["timeout_ms"] = arguments["timeout_ms"]
            return self._execute(session_id, "wait", params)
        if name == "browser_screenshot":
            return self._execute(session_id, "screenshot", {"full_page": bool(arguments.get("full_page", False))})
        if name == "browser_takeover_request":
            return self._execute(session_id, "takeover_request", {})
        if name == "browser_takeover_release":
            # The runtime keeps writes paused while takeover is active. The
            # release command exists in the stable contract, but production UI
            # is expected to be the human decision point.
            return self._execute(session_id, "takeover_release", {})
        if name == "browser_close":
            return _mcp_result(self.host.stop(session_id))
        raise KeyError(name)


__all__ = [
    "BROWSER_STATUS_URI",
    "BrowserOperatorCatalog",
    "BrowserServiceHost",
    "browser_tools",
]
