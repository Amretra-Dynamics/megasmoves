from __future__ import annotations

import json
import os
from typing import Any

from .base import WorkerEvent, resolve_cli_argv
from .headless_agent import ReadOnlyHeadlessAgentAdapter


class ClaudeCodeAdapter(ReadOnlyHeadlessAgentAdapter):
    name = "claude_code"
    adapter_id = "claude_code"
    supported_range = ">=0.1.0"
    binary_env = "MEGAS_CLAUDE_BINARY"
    default_binary = "claude"
    timeout_env = "MEGAS_CLAUDE_TIMEOUT_SEC"

    def is_authenticated(self) -> bool:
        if os.getenv("ANTHROPIC_API_KEY"):
            return True
        home = os.path.expanduser("~")
        config_paths = [
            os.path.join(home, ".claude.json"),
            os.path.join(home, ".claude", "config.json"),
            os.path.join(home, ".config", "claude", "config.json"),
        ]
        for config_path in config_paths:
            if os.path.isfile(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("sessionKey") or data.get("oauthAccount") or data.get("apiKey"):
                        return True
                except Exception:
                    pass
        return False

    def process_args(self, prompt: str, context_pack: dict) -> list[str]:
        binary = os.getenv(self.binary_env, self.default_binary)
        try:
            max_turns = int(context_pack.get("max_turns") or 8)
        except (TypeError, ValueError):
            max_turns = 8
        max_turns = min(max(max_turns, 1), 30)
        args = [
            "--permission-mode",
            "plan",
            "--disallowedTools",
            "Edit",
            "Write",
            "NotebookEdit",
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            str(max_turns),
        ]
        model = str(context_pack.get("model") or "").strip()
        if model:
            args.extend(["--model", model[:100]])
        session_id = str(context_pack.get("provider_session_id") or "").strip()
        if session_id:
            args.extend(["--resume", session_id[:200]])
        return resolve_cli_argv(binary, *args, "-p", prompt)

    def stream_event(self, stream: str, line: str) -> WorkerEvent:
        if stream != "stdout":
            return super().stream_event(stream, line)
        try:
            event: Any = json.loads(line)
        except (TypeError, ValueError):
            return super().stream_event(stream, line)
        if not isinstance(event, dict):
            return super().stream_event(stream, line)
        kind = str(event.get("type") or "event").lower()
        base = {
            "adapter": self.name,
            "agent_type": self.name,
            "provider_session_id": str(event.get("session_id") or "")[:200],
        }
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        blocks = message.get("content") if isinstance(message.get("content"), list) else []
        texts = [
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        tools = [
            block
            for block in blocks
            if isinstance(block, dict) and block.get("type") in {"tool_use", "tool_result"}
        ]
        if texts:
            return WorkerEvent(
                "assistant_message",
                {**base, "text": "\n".join(texts)[:12000], "final": kind == "result"},
            )
        if tools:
            tool = tools[-1]
            return WorkerEvent(
                "tool_output" if tool.get("type") == "tool_result" else "tool_started",
                {
                    **base,
                    "tool_name": str(tool.get("name") or "tool")[:120],
                    "input": str(tool.get("input") or "")[:12000],
                    "output": str(tool.get("content") or "")[:12000],
                },
            )
        if kind == "result":
            if event.get("is_error"):
                return WorkerEvent(
                    "agent_error",
                    {**base, "message": str(event.get("result") or "Claude Code failed.")[:2000]},
                )
            return WorkerEvent(
                "agent_turn_completed",
                {**base, "usage": event.get("usage") or {}, "text": str(event.get("result") or "")[:12000]},
            )
        return WorkerEvent(
            "agent_activity",
            {**base, "status": "running", "message": f"Claude Code {kind}"},
        )
