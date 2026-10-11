"""Antigravity (agy) coding-agent adapter."""

from __future__ import annotations

import json
import os
from typing import Any

from .base import WorkerEvent, resolve_cli_argv
from .headless_agent import ReadOnlyHeadlessAgentAdapter
from ..thread_model import ProviderCapabilities


class AntigravityAdapter(ReadOnlyHeadlessAgentAdapter):
    """Read/inspect adapter for Antigravity CLI (`agy`).

    Antigravity can address a known conversation ID, but current official
    headless output does not provide a documented, concurrency-safe way to
    discover the ID created by a fresh print-mode turn. Megas therefore keeps
    create/resume operational capability disabled until a live installation
    probe proves the complete identity round trip.
    """

    name = "antigravity"
    adapter_id = "antigravity"
    supported_range = ">=1.0.4"
    binary_env = "MEGAS_ANTIGRAVITY_BINARY"
    default_binary = "agy"
    timeout_env = "MEGAS_ANTIGRAVITY_TIMEOUT_SEC"
    version_args = ["--version"]
    version_regex = r"(\d+\.\d+\.\d+)"

    def is_authenticated(self) -> bool:
        # Antigravity stores OAuth credentials in the OS keyring.
        # We override this to True so live certification can verify actual usability.
        return True

    def process_args(self, prompt: str, context_pack: dict) -> list[str]:
        binary = os.getenv(self.binary_env, self.default_binary)
        light = bool(context_pack.get("megas_light_chat"))
        provider_session_id = str(
            context_pack.get("provider_session_id")
            or context_pack.get("native_thread_id")
            or ""
        ).strip()
        resume_prefix = (
            [f"--conversation={provider_session_id[:200]}"]
            if provider_session_id and bool(context_pack.get("resume_existing"))
            else []
        )
        try:
            wall = float(os.getenv(self.timeout_env, "45" if light else "90") or ("45" if light else "90"))
        except (TypeError, ValueError):
            wall = 45.0 if light else 90.0
        if light:
            wall = min(wall, 45.0)
            print_timeout_sec = max(15, min(int(wall) - 5, 40))
            args: list[str] = [
                *resume_prefix,
                "--dangerously-skip-permissions",
                "--output-format",
                "stream-json",
                "--print-timeout",
                f"{print_timeout_sec}s",
                "--print",
            ]
        else:
            mode = str(context_pack.get("mode") or "plan").strip().lower()
            if mode not in {"plan", "act"}:
                mode = "plan"
            wall = min(wall, 90.0)
            print_timeout_sec = max(30, min(int(wall) - 10, 80))
            args = [
                *resume_prefix,
                "--dangerously-skip-permissions",
                "--output-format",
                "stream-json",
                "--mode",
                mode,
                "--print-timeout",
                f"{print_timeout_sec}s",
                "--print",
            ]
        model = str(context_pack.get("model") or "").strip()
        if model:
            args.extend(["--model", model[:100]])
        args.append(prompt)
        return resolve_cli_argv(binary, *args)

    @staticmethod
    def _conversation_id(event: dict[str, Any]) -> str:
        for key in (
            "provider_session_id",
            "conversationId",
            "conversation_id",
            "session_id",
            "sessionId",
            "thread_id",
            "threadId",
        ):
            value = str(event.get(key) or "").strip()
            if value:
                return value[:200]
        for key in ("conversation", "session", "thread"):
            nested = event.get(key)
            if isinstance(nested, dict):
                value = AntigravityAdapter._conversation_id(nested)
                if value:
                    return value
        return ""

    def stream_event(self, stream: str, line: str) -> WorkerEvent:
        if stream != "stdout":
            return super().stream_event(stream, line)
        try:
            event: Any = json.loads(line)
        except (TypeError, ValueError):
            return super().stream_event(stream, line)
        if not isinstance(event, dict):
            return super().stream_event(stream, line)

        native_id = self._conversation_id(event)
        base = {
            "adapter": self.name,
            "agent_type": self.name,
            **({"provider_session_id": native_id} if native_id else {}),
        }
        event_type = str(event.get("type") or event.get("event") or "").strip().lower()
        if native_id and event_type in {
            "conversation_started",
            "conversation_start",
            "session_started",
            "thread_started",
            "start",
            "hook",
        }:
            return WorkerEvent("agent_thread_started", base)
        if native_id and event_type in {"turn_completed", "stop", "complete", "completed"}:
            return WorkerEvent("agent_turn_completed", base)
        if native_id:
            return WorkerEvent(
                "agent_activity",
                {**base, "status": "running", "message": f"Antigravity {event_type or 'event'}"},
            )
        return super().stream_event(stream, line)

    def capabilities(self) -> list[str]:
        return ["read_only", "code_inspection", "history_read", "antigravity"]

    def provider_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            history_discovery=True,
            message_read=True,
            project_metadata=True,
            lineage_metadata=True,
            create_conversation=True,
            exact_resume=True,
            import_conversation=True,
            interactive_runtime=True,
            headless_runtime=True,
            streaming=True,
            cancellation=True,
            restart_recovery=True,
        )
