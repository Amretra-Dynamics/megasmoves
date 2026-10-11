"""Cursor Agent CLI adapter using the official headless stream-JSON contract."""

from __future__ import annotations

import json
import os
from typing import Any

from .base import WorkerEvent, resolve_cli_argv
from .headless_agent import ReadOnlyHeadlessAgentAdapter


def _short(value: object, limit: int = 12_000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "\n[TRUNCATED]"


def _content_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = str(item.get("text") or "")
            if text:
                parts.append(text)
    return "".join(parts)


def _tool_shape(value: object) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, dict) or not value:
        return "tool", {}
    key = next(iter(value))
    body = value.get(key)
    return str(key or "tool")[:120], body if isinstance(body, dict) else {}


class CursorCliAdapter(ReadOnlyHeadlessAgentAdapter):
    name = "cursor_cli"
    adapter_id = "cursor_cli"
    contract_version = "1.0"
    supported_range = ">=1.0.0"
    binary_env = "MEGAS_CURSOR_BINARY"
    default_binary = "cursor-agent"
    timeout_env = "MEGAS_CURSOR_TIMEOUT_SEC"
    version_args = ["--version"]
    version_regex = r"(\d+\.\d+\.\d+)"

    def is_authenticated(self) -> bool:
        if os.getenv("CURSOR_API_KEY"):
            return True
        home = os.path.expanduser("~")
        return any(
            os.path.exists(path)
            for path in (
                os.path.join(home, ".cursor"),
                os.path.join(home, ".config", "cursor"),
            )
        )

    def process_args(self, prompt: str, context_pack: dict) -> list[str]:
        binary = os.getenv(self.binary_env, self.default_binary)
        args: list[str] = ["--print", "--output-format", "stream-json"]
        native_id = str(
            context_pack.get("provider_session_id")
            or context_pack.get("cursor_session_id")
            or context_pack.get("native_thread_id")
            or ""
        ).strip()
        if native_id:
            args.extend(["--resume", native_id[:200]])
        model = str(context_pack.get("model") or "").strip()
        if model:
            args.extend(["--model", model[:160]])
        # Deliberately omit --force.  Megas inspection mode must not grant Cursor
        # direct mutation rights even though the provider supports them.
        args.append(prompt)
        return resolve_cli_argv(binary, *args)

    def stream_event(self, stream: str, line: str) -> WorkerEvent:
        if stream != "stdout":
            return super().stream_event(stream, line)
        try:
            event: Any = json.loads(line)
        except (TypeError, ValueError):
            return super().stream_event(stream, line)
        if not isinstance(event, dict):
            return super().stream_event(stream, line)

        event_type = str(event.get("type") or "event").strip().lower()
        subtype = str(event.get("subtype") or "").strip().lower()
        native_id = str(event.get("session_id") or "").strip()[:200]
        base = {
            "adapter": self.name,
            "agent_type": self.name,
            **({"provider_session_id": native_id, "native_id": native_id} if native_id else {}),
        }

        if event_type == "system" and subtype == "init":
            return WorkerEvent(
                "agent_thread_started",
                {
                    **base,
                    "source": "cursor_stream_json",
                    "model": _short(event.get("model"), 200),
                    "permission_mode": _short(event.get("permissionMode"), 80),
                },
            )
        if event_type == "user":
            # The Worker already persists the accepted user instruction.  Keep
            # Cursor's echo as reconciliation evidence, not a second chat row.
            return WorkerEvent(
                "agent_activity",
                {
                    **base,
                    "status": "accepted",
                    "provider_event": "user_echo",
                    "text_hash_source": _short(_content_text(event.get("message")), 2_000),
                    "chat_visible": False,
                },
            )
        if event_type == "assistant":
            text = _short(_content_text(event.get("message")))
            if text:
                return WorkerEvent(
                    "assistant_delta",
                    {**base, "text": text, "final": False, "source": "cursor_stream_json"},
                )
        if event_type == "tool_call":
            tool_name, tool_body = _tool_shape(event.get("tool_call"))
            args = tool_body.get("args") if isinstance(tool_body.get("args"), dict) else {}
            result = tool_body.get("result") if isinstance(tool_body.get("result"), dict) else {}
            payload = {
                **base,
                "item_id": _short(event.get("call_id"), 200),
                "tool_name": tool_name,
                "input": _short(json.dumps(args, ensure_ascii=False, sort_keys=True)),
                "output": _short(json.dumps(result, ensure_ascii=False, sort_keys=True)),
                "source": "cursor_stream_json",
            }
            return WorkerEvent(
                "tool_started" if subtype == "started" else "tool_finished",
                {**payload, "success": subtype == "completed"},
            )
        if event_type == "result":
            if event.get("is_error") is True or subtype not in {"", "success"}:
                return WorkerEvent(
                    "agent_error",
                    {**base, "message": _short(event.get("result") or "Cursor Agent failed.", 2_000)},
                )
            return WorkerEvent(
                "agent_turn_completed",
                {
                    **base,
                    "text": _short(event.get("result")),
                    "duration_ms": event.get("duration_ms"),
                    "duration_api_ms": event.get("duration_api_ms"),
                    "request_id": _short(event.get("request_id"), 200),
                    "final": True,
                    "source": "cursor_stream_json",
                },
            )
        return WorkerEvent(
            "agent_activity",
            {**base, "status": "running", "message": f"Cursor {event_type}"},
        )

    def capabilities(self) -> list[str]:
        return [
            "read_only",
            "code_inspection",
            "structured_stream",
            "session_resume",
            "cursor_cli",
        ]
