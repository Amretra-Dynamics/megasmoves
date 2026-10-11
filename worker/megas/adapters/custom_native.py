"""Versioned JSONL bridge for user-owned and future coding agents.

Configure locally (never from a remote task):
  MEGAS_CUSTOM_AGENT_BINARY=/path/to/agent
  MEGAS_CUSTOM_AGENT_ARGS_JSON='["run","--json","{prompt}"]'

The agent writes one JSON object per line using ``megas.native-stream.v1``:
  {"type":"assistant_delta","text":"...","item_id":"...","partial":true}
  {"type":"reasoning_delta","text":"...","item_id":"..."}
  {"type":"tool_started","item_id":"...","title":"...","command":"..."}
  {"type":"tool_output","item_id":"...","output":"..."}
  {"type":"tool_finished","item_id":"...","status":"completed","exit_code":0}
  {"type":"file_change","item_id":"...","changes":[...]}
  {"type":"approval_required","request_id":"...","command":"..."}
  {"type":"turn_completed","status":"completed","provider_session_id":"..."}
"""

from __future__ import annotations

import json
import os

from .base import WorkerEvent, resolve_cli_argv
from .headless_agent import ReadOnlyHeadlessAgentAdapter


PROTOCOL_VERSION = "megas.native-stream.v1"
EVENT_TYPES = {
    "assistant_delta": "assistant_delta",
    "assistant_message": "assistant_message",
    "reasoning_delta": "agent_reasoning",
    "tool_started": "tool_started",
    "tool_output": "tool_output",
    "tool_finished": "tool_finished",
    "plan_update": "plan_update",
    "file_change": "file_change",
    "approval_required": "agent_approval_required",
    "approval_resolved": "agent_approval_resolved",
    "error": "agent_error",
    "turn_started": "agent_turn_started",
    "turn_completed": "agent_turn_completed",
}


def custom_native_json_to_event(payload: dict) -> WorkerEvent:
    native_type = str(payload.get("type") or "").strip().lower()
    event_type = EVENT_TYPES.get(native_type, "agent_activity")
    clean = {
        key: value
        for key, value in payload.items()
        if key not in {"type", "protocol"} and not str(key).lower().endswith("credential")
    }
    clean.update(
        {
            "source": PROTOCOL_VERSION,
            "provider_event": native_type or "event",
            "adapter": "custom_native",
            "agent_type": "custom_native",
        }
    )
    if native_type == "reasoning_delta":
        clean.setdefault("partial", True)
    if native_type == "turn_completed":
        clean.setdefault("final", True)
    return WorkerEvent(event_type, clean)


class CustomNativeAgentAdapter(ReadOnlyHeadlessAgentAdapter):
    name = "custom_native"
    adapter_id = "custom_native"
    supported_range = ">=1"
    binary_env = "MEGAS_CUSTOM_AGENT_BINARY"
    default_binary = ""
    timeout_env = "MEGAS_CUSTOM_AGENT_TIMEOUT_SEC"

    def supports(self, command: dict) -> bool:
        context = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        return str(context.get("agent_type") or command.get("agent_type") or "") == self.name

    def process_args(self, prompt: str, context_pack: dict) -> list[str]:
        binary = os.getenv(self.binary_env, "").strip()
        if not binary:
            raise PermissionError(f"{self.binary_env} must be configured locally")
        try:
            template = json.loads(os.getenv("MEGAS_CUSTOM_AGENT_ARGS_JSON", '["{prompt}"]'))
        except json.JSONDecodeError as exc:
            raise PermissionError("MEGAS_CUSTOM_AGENT_ARGS_JSON must be a JSON string array") from exc
        if not isinstance(template, list) or not all(isinstance(value, str) for value in template):
            raise PermissionError("MEGAS_CUSTOM_AGENT_ARGS_JSON must be a JSON string array")
        args = [
            value.replace("{prompt}", prompt).replace(
                "{provider_session_id}",
                str(context_pack.get("provider_session_id") or ""),
            )
            for value in template
        ]
        return resolve_cli_argv(binary, *args)

    def stream_event(self, stream: str, line: str) -> WorkerEvent:
        if stream != "stdout":
            return super().stream_event(stream, line)
        try:
            payload = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            return super().stream_event(stream, line)
        if not isinstance(payload, dict):
            return super().stream_event(stream, line)
        return custom_native_json_to_event(payload)

    def capabilities(self) -> list[str]:
        return ["read_only", "structured_stream", "session_resume", "custom_native"]
