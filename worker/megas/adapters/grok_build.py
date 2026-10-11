"""Grok Build CLI adapter using the official headless streaming contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from typing import Any

from .base import WorkerEvent, is_runnable_native_binary, resolve_cli_argv
from .headless_agent import ReadOnlyHeadlessAgentAdapter


def _short(value: object, limit: int = 12_000) -> str:
    return str(value or "")[:limit]


def _native_id(payload: dict[str, Any]) -> str:
    for key in (
        "session_id",
        "sessionId",
        "provider_session_id",
        "thread_id",
        "threadId",
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            return value[:200]
    session = payload.get("session")
    if isinstance(session, dict):
        return _native_id(session)
    return ""


class GrokBuildAdapter(ReadOnlyHeadlessAgentAdapter):
    name = "grok_build"
    adapter_id = "grok_build"
    contract_version = "1.1"
    supported_range = ">=0.1.0"
    binary_env = "MEGAS_GROK_BINARY"
    default_binary = "grok"
    timeout_env = "MEGAS_GROK_TIMEOUT_SEC"
    version_args = ["version"]
    version_regex = r"(\d+\.\d+\.\d+)"

    def discover_executable(self) -> Path | None:
        home = Path.home()
        candidates = [
            os.getenv(self.binary_env, "").strip(),
            str(home / ".grok" / "bin" / "grok.exe"),
            "grok.exe",
            "grok-build.exe",
            "grok",
            "grok-build",
            str(home / ".grok" / "bin" / "grok"),
            str(home / ".local" / "bin" / "grok"),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            which = shutil.which(candidate)
            path = Path(which) if which else Path(candidate)
            try:
                if path.is_file() and is_runnable_native_binary(path):
                    return path
            except OSError:
                continue
        return None

    def process_args(self, prompt: str, context_pack: dict) -> list[str]:
        binary = str(
            self.discover_executable()
            or os.getenv(self.binary_env, self.default_binary)
        )
        args: list[str] = [
            "--no-auto-update",
            "-p",
            prompt,
            "--output-format",
            "streaming-json",
            "--no-alt-screen",
        ]
        model = str(context_pack.get("model") or "").strip()
        if model:
            args.extend(["--model", model[:160]])
        effort = str(
            context_pack.get("reasoning_effort")
            or context_pack.get("effort")
            or ""
        ).strip()
        if effort:
            args.extend(["--effort", effort[:50]])
        session_id = str(
            context_pack.get("provider_session_id")
            or context_pack.get("grok_session_id")
            or context_pack.get("native_thread_id")
            or ""
        ).strip()
        if session_id:
            args.extend(["--resume", session_id[:200]])
        return resolve_cli_argv(binary, *args)

    def stream_event(self, stream: str, line: str) -> WorkerEvent:
        if stream != "stdout":
            return super().stream_event(stream, line)
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            return super().stream_event(stream, line)
        if not isinstance(payload, dict):
            return super().stream_event(stream, line)

        kind = str(payload.get("type") or payload.get("event") or "event").lower()
        subtype = str(payload.get("subtype") or "").lower()
        content = payload.get("content")
        text = _short(
            payload.get("text")
            or payload.get("delta")
            or payload.get("result")
            or (content.get("text") if isinstance(content, dict) else "")
        )
        native_id = _native_id(payload)
        base = {
            "adapter": self.name,
            "agent_type": self.name,
            **({
                "provider_session_id": native_id,
                "native_id": native_id,
            } if native_id else {}),
            "provider_event": subtype or kind,
        }

        if native_id and (
            kind in {"system", "session", "session_started", "init"}
            or subtype in {"init", "session_started"}
        ):
            return WorkerEvent(
                "agent_thread_started",
                {**base, "thread_id": native_id},
            )
        if "reason" in kind or "thinking" in kind:
            return WorkerEvent("reasoning_delta", {**base, "text": text})
        if "tool" in kind:
            tool = payload.get("tool")
            tool_name = (
                tool.get("name") if isinstance(tool, dict) else payload.get("name")
            )
            status = str(payload.get("status") or subtype or "completed").lower()
            return WorkerEvent(
                "tool_finished" if status in {"completed", "success", "done"} else "tool_started",
                {
                    **base,
                    "tool_name": str(tool_name or "tool")[:120],
                    "output": text,
                    "status": status,
                    "success": status in {"completed", "success", "done"},
                },
            )
        if kind in {"error", "failed"} or payload.get("is_error") is True:
            return WorkerEvent(
                "agent_error",
                {
                    **base,
                    "message": text or _short(payload.get("error"), 2_000),
                    "fatal": True,
                },
            )
        if kind in {"result", "complete", "completed"}:
            return WorkerEvent(
                "agent_turn_completed",
                {**base, "text": text, "final": True},
            )
        if text:
            return WorkerEvent("agent_message_delta", {**base, "text": text})
        return WorkerEvent(
            "agent_activity",
            {**base, "message": f"Grok Build {subtype or kind}"},
        )

    def capabilities(self) -> list[str]:
        return [
            "read_only",
            "code_inspection",
            "structured_stream",
            "session_list",
            "session_read",
            "session_resume",
            "native_reconciliation",
            "limit_state",
            "grok_build",
        ]
