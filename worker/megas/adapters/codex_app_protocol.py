"""Codex app-server notification to WorkerEvent mapping."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import WorkerEvent
from ..provider_limit_state import normalize_codex_rate_limits


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_text(item) for item in value)))
    if isinstance(value, dict):
        for key in ("text", "delta", "summary", "content", "message"):
            result = _text(value.get(key))
            if result:
                return result
    return ""


def codex_app_server_message_to_events(
    message: dict[str, Any],
    *,
    redact_text: Callable[[str], str] = lambda value: value,
) -> list[WorkerEvent]:
    method = str(message.get("method") or "")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    common = {
        "source": "codex_app_server",
        "codex_method": method,
        "thread_id": str(params.get("threadId") or ""),
        "turn_id": str(params.get("turnId") or ""),
        "item_id": str(params.get("itemId") or ""),
    }

    if method == "account/rateLimits/updated":
        snapshot = normalize_codex_rate_limits(params, method=method)
        return [WorkerEvent("provider_limit_snapshot", snapshot)] if snapshot else []
    if method == "thread/started":
        thread = params.get("thread") if isinstance(params.get("thread"), dict) else {}
        thread_id = str(thread.get("id") or "")
        return [
            WorkerEvent(
                "agent_thread_started",
                {
                    **common,
                    "thread_id": thread_id,
                    "provider_session_id": thread_id,
                },
            )
        ] if thread_id else []
    if method == "turn/started":
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        return [WorkerEvent("agent_turn_started", {**common, "status": turn.get("status")})]
    if method == "turn/completed":
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        status = str(turn.get("status") or "completed")
        event_type = "agent_error" if status == "failed" else "agent_turn_completed"
        return [WorkerEvent(event_type, {**common, "status": status, "turn": turn})]
    if method == "turn/diff/updated":
        return [
            WorkerEvent(
                "file_change",
                {**common, "summary": "Working tree diff updated", "diff": params.get("diff")},
            )
        ]
    if method == "turn/plan/updated":
        return [
            WorkerEvent(
                "plan_update",
                {**common, "steps": params.get("plan") or params.get("steps") or []},
            )
        ]
    if method == "item/agentMessage/delta":
        return [
            WorkerEvent(
                "assistant_delta",
                {**common, "text": redact_text(str(params.get("delta") or "")), "partial": True},
            )
        ]
    if method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
        return [
            WorkerEvent(
                "agent_reasoning",
                {**common, "text": redact_text(str(params.get("delta") or "")), "partial": True},
            )
        ]
    if method == "item/commandExecution/outputDelta":
        return [
            WorkerEvent(
                "tool_output",
                {
                    **common,
                    "tool_type": "command",
                    "title": "Command execution",
                    "output": redact_text(str(params.get("delta") or "")),
                    "partial": True,
                },
            )
        ]
    if method == "item/fileChange/outputDelta":
        return [
            WorkerEvent(
                "file_change",
                {**common, "diff": redact_text(str(params.get("delta") or "")), "partial": True},
            )
        ]
    if method == "item/fileChange/patchUpdated":
        return [
            WorkerEvent(
                "file_change",
                {**common, "changes": params.get("changes") or [], "partial": True},
            )
        ]
    if method == "item/plan/delta":
        return [
            WorkerEvent(
                "plan_update",
                {**common, "text": redact_text(str(params.get("delta") or "")), "partial": True},
            )
        ]
    if method in {"item/started", "item/completed"}:
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        item_type = str(item.get("type") or "activity")
        payload = {
            **common,
            "item_id": str(item.get("id") or common["item_id"]),
            "item_type": item_type,
            "status": item.get("status"),
            "final": method == "item/completed",
        }
        if item_type == "agentMessage":
            text = redact_text(_text(item))
            return [WorkerEvent("assistant_message", {**payload, "text": text})] if text else []
        if item_type == "reasoning":
            text = redact_text(_text(item.get("summary") or item.get("content")))
            return [WorkerEvent("agent_reasoning", {**payload, "text": text})] if text else []
        if item_type == "commandExecution":
            event_type = "tool_started" if method == "item/started" else "tool_finished"
            return [
                WorkerEvent(
                    event_type,
                    {
                        **payload,
                        "tool_type": "command",
                        "title": "Command execution",
                        "command": redact_text(str(item.get("command") or "")),
                        "cwd": str(item.get("cwd") or ""),
                        "output": redact_text(str(item.get("aggregatedOutput") or "")),
                        "exit_code": item.get("exitCode"),
                        "duration_ms": item.get("durationMs"),
                        "command_actions": item.get("commandActions") or [],
                    },
                )
            ]
        if item_type == "fileChange":
            return [WorkerEvent("file_change", {**payload, "changes": item.get("changes") or []})]
        if item_type in {"mcpToolCall", "collabToolCall", "webSearch", "imageView"}:
            event_type = "tool_started" if method == "item/started" else "tool_finished"
            return [
                WorkerEvent(
                    event_type,
                    {
                        **payload,
                        "tool_type": item_type,
                        "title": str(item.get("tool") or item.get("query") or item_type),
                        "input": item.get("arguments") or item.get("query"),
                        "output": item.get("result") or item.get("results") or item.get("error"),
                    },
                )
            ]
        if item_type in {"plan", "todoList"}:
            return [
                WorkerEvent(
                    "plan_update",
                    {
                        **payload,
                        "text": redact_text(_text(item)),
                        "steps": item.get("items") or item.get("steps") or [],
                    },
                )
            ]
        return [WorkerEvent("agent_activity", {**payload, "item": item})]
    if method == "serverRequest/resolved":
        return [
            WorkerEvent(
                "approval_response",
                {**common, "request_id": params.get("requestId"), "status": "resolved"},
            )
        ]
    if method == "error":
        return [WorkerEvent("agent_error", {**common, "message": redact_text(_text(params))})]
    return []
