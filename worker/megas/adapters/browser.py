from __future__ import annotations

import time
from typing import AsyncIterator

from .base import WorkerEvent
from ..operate.browser.runtime import BrowserSessionManager
from ..operate.gate import is_operate_enabled


class BrowserAdapter:
    name = "browser"
    adapter_id = "browser_playwright"
    contract_version = "1.0"
    status = "available"
    supported_range = ">=1.60.0,<2.0.0"

    def __init__(self, manager: BrowserSessionManager) -> None:
        self.manager = manager

    def supports(self, command: dict) -> bool:
        if not is_operate_enabled():
            return False
        context = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        return str(command.get("command_type") or "") == "browser" or str(context.get("agent_type") or "") == "browser"

    async def launch(self, command: dict) -> AsyncIterator[WorkerEvent]:
        if not is_operate_enabled():
            raise PermissionError(
                "Megas Operate is disabled on this machine. Run `megas operate enable` locally."
            )
        started = time.monotonic()
        browser_session = command.get("browser_session") if isinstance(command.get("browser_session"), dict) else {}
        browser_session_id = str(browser_session.get("id") or "")
        command_id = str(command.get("command_id") or "")
        yield WorkerEvent(
            "command_started",
            {"command_id": command_id, "command": ["browser", "start"], "workdir": "local-browser-runtime"},
        )
        try:
            state = await self.manager.start(command)
            yield WorkerEvent(
                "browser_session_started",
                {"browser_session_id": browser_session_id, **state},
            )
            await self.manager.wait_closed(browser_session_id)
            cleanup = await self.manager.stop(browser_session_id, "session_closed")
            duration_ms = int((time.monotonic() - started) * 1000)
            evidence = {
                "kind": "browser_session",
                "browser_session_id": browser_session_id,
                "browser_type": browser_session.get("browser_type"),
                "profile_id": browser_session.get("profile_id"),
                "started": True,
                "cleanup_ok": bool(cleanup.get("cleanup_ok", True)),
                "duration_ms": duration_ms,
            }
            yield WorkerEvent(
                "browser_session_stopped",
                {"browser_session_id": browser_session_id, "cleanup_ok": bool(cleanup.get("cleanup_ok", True))},
            )
            yield WorkerEvent(
                "evidence",
                {"evidence_type": "evidence_browser_session", "summary": "Browser session completed and cleaned up.", "payload": evidence},
            )
            yield WorkerEvent(
                "evidence",
                {"evidence_type": "terminal_result", "summary": "Browser runtime exited cleanly.", "payload": {"adapter": self.name, "exit_code": 0, "duration_ms": duration_ms}},
            )
            yield WorkerEvent("command_finished", {"command_id": command_id, "exit_code": 0, "duration_ms": duration_ms})
        except BaseException:
            if browser_session_id:
                await self.manager.stop(browser_session_id, "adapter_failed")
            raise
