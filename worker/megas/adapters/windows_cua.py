"""Worker adapter for Windows Computer Use (UI Automation first)."""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

from megas.adapters.base import WorkerEvent
from megas.operate.gate import is_operate_enabled
from megas.operate.windows.capability import (
    CAPABILITY_WINDOWS_INPUT,
    CAPABILITY_WINDOWS_UIA,
    advertised_capability_names,
    simulation_enabled,
)
from megas.operate.windows.session import run_windows_cua_command


class WindowsCuaAdapter:
    name = "windows_cua"

    def __init__(self, allowed_folders: list[str] | None = None) -> None:
        self.allowed_folders = allowed_folders or []

    def supports(self, command: dict) -> bool:
        if not is_operate_enabled():
            return False
        command_type = str(command.get("command_type") or "")
        context = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        agent_type = str(context.get("agent_type") or command.get("agent_type") or "")
        if command_type in {"windows_cua", "desktop_windows_cua", "computer_use_windows"}:
            return True
        if agent_type in {"windows_cua", "desktop.windows.uia", "desktop.windows.input"}:
            return True
        actions = context.get("actions") or command.get("actions")
        return bool(actions) and str(context.get("runtime") or "") == "windows_cua"

    def prepare_context(self, context_pack: dict, workspace: Path, command: dict | None = None) -> Path:
        return workspace

    async def launch(self, command: dict) -> AsyncIterator[WorkerEvent]:
        if not is_operate_enabled():
            raise PermissionError(
                "Megas Operate is disabled on this machine. Run `megas operate enable` locally."
            )
        # Fail closed if capability not advertised and simulation not enabled.
        available = set(advertised_capability_names())
        context = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        if not simulation_enabled() and not context.get("simulation"):
            if CAPABILITY_WINDOWS_UIA not in available and CAPABILITY_WINDOWS_INPUT not in available:
                yield WorkerEvent(
                    "command_started",
                    {
                        "command_id": command.get("command_id"),
                        "session_id": command.get("session_id"),
                        "command": ["windows_cua"],
                    },
                )
                yield WorkerEvent(
                    "evidence",
                    {
                        "evidence_type": "terminal_result",
                        "summary": "Windows CUA capability unavailable (health/certification).",
                        "payload": {
                            "adapter": self.name,
                            "exit_code": 1,
                            "error": "capability_unavailable",
                            "available": sorted(available),
                        },
                    },
                )
                yield WorkerEvent(
                    "command_finished",
                    {"command_id": command.get("command_id"), "exit_code": 1, "duration_ms": 0},
                )
                return

        # Ensure simulation context for non-Windows CI
        if simulation_enabled() or context.get("simulation"):
            context = dict(context)
            context["simulation"] = True
            command = dict(command)
            command["context_pack"] = context

        for item in run_windows_cua_command(command):
            yield WorkerEvent(item["event_type"], item.get("payload") or {})
