"""Cline CLI adapter using Cline's current headless NDJSON contract."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from .base import (
    WorkerEvent,
    prefer_console_free_tool_path,
    resolve_cli_argv,
    subprocess_hide_window_kwargs,
)
from .headless_agent import ReadOnlyHeadlessAgentAdapter


def _short(value: Any, limit: int = 12_000) -> str:
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value or "")
    else:
        text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "\n[TRUNCATED]"


def _event_body(raw: dict[str, Any]) -> dict[str, Any]:
    nested = raw.get("event")
    return nested if isinstance(nested, dict) else raw


def _native_id(raw: dict[str, Any], body: dict[str, Any]) -> str:
    for source in (raw, body):
        for key in (
            "taskId",
            "task_id",
            "sessionId",
            "session_id",
            "provider_session_id",
        ):
            value = str(source.get(key) or "").strip()
            if value:
                return value[:200]
    return ""


def _text(body: dict[str, Any]) -> str:
    for key in (
        "text",
        "outputText",
        "content",
        "message",
        "result",
        "output",
        "description",
    ):
        value = body.get(key)
        if value not in (None, "", [], {}):
            return _short(value)
    return ""


def _error_code(raw: dict[str, Any], body: dict[str, Any]) -> str:
    for source in (body, raw):
        for key in ("code", "error_code", "errorCode", "reason"):
            value = str(source.get(key) or "").strip().lower()
            if value:
                return value[:160]
        error = source.get("error")
        if isinstance(error, dict):
            value = str(
                error.get("code")
                or error.get("error_code")
                or error.get("type")
                or ""
            ).strip().lower()
            if value:
                return value[:160]
    return ""


_LIMIT_CODES = {
    "insufficient_credits",
    "usage_limit_exceeded",
    "rate_limit_exceeded",
    "quota_exceeded",
    "out_of_credits",
    "out_of_quota",
}


class ClineAdapter(ReadOnlyHeadlessAgentAdapter):
    name = "cline"
    adapter_id = "cline"
    contract_version = "1.1"
    # Exact task continuation with --taskId + --continue is documented in the
    # current 3.x CLI line. Older binaries remain detected but uncertifiable.
    supported_range = ">=3.0.3,<4.0.0"
    binary_env = "MEGAS_CLINE_BINARY"
    default_binary = "cline"
    timeout_env = "MEGAS_CLINE_TIMEOUT_SEC"
    version_args = ["--version"]
    version_regex = r"(\d+\.\d+\.\d+)"
    _resume_style_cache: dict[str, str] = {}

    def _resume_args(self, session_id: str) -> list[str]:
        """Resolve the exact installed Cline session-selection contract.

        Current Cline documentation exposes ``--id <session-id>``. Early 3.x
        releases exposed ``--taskId <id> --continue``. We inspect the installed
        binary instead of silently selecting the wrong conversation or starting
        a replacement session. Tests/certification may pin an observed style via
        MEGAS_CLINE_RESUME_STYLE.
        """
        forced = os.getenv("MEGAS_CLINE_RESUME_STYLE", "").strip().lower()
        if forced in {"id", "session_id"}:
            return ["--id", session_id[:200]]
        if forced in {"task", "task_id", "legacy"}:
            return ["--taskId", session_id[:200], "--continue"]

        executable = self.discover_executable()
        if executable is None:
            raise RuntimeError(
                "CLINE_EXACT_RESUME_UNAVAILABLE: installed CLI help could not be inspected"
            )
        key = str(executable.resolve())
        cached = type(self)._resume_style_cache.get(key)
        if cached == "id":
            return ["--id", session_id[:200]]
        if cached == "task":
            return ["--taskId", session_id[:200], "--continue"]
        try:
            result = subprocess.run(
                resolve_cli_argv(str(executable), "--help"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3.0,
                check=False,
                env=prefer_console_free_tool_path(dict(os.environ)),
                **subprocess_hide_window_kwargs(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"CLINE_EXACT_RESUME_UNAVAILABLE: help probe failed: {exc}"
            ) from exc
        help_text = f"{result.stdout}\n{result.stderr}".lower()
        if "--id <session-id>" in help_text or "--id <session" in help_text:
            type(self)._resume_style_cache[key] = "id"
            return ["--id", session_id[:200]]
        if "--taskid" in help_text and "--continue" in help_text:
            type(self)._resume_style_cache[key] = "task"
            return ["--taskId", session_id[:200], "--continue"]
        raise RuntimeError(
            "CLINE_EXACT_RESUME_UNAVAILABLE: installed CLI exposes no certified exact-resume flag"
        )

    def process_args(self, prompt: str, context_pack: dict) -> list[str]:
        binary = os.getenv(self.binary_env, self.default_binary)
        args: list[str] = [
            "--json",
            # The Local inspect path is intentionally non-YOLO. In headless
            # mode Cline denies tools that require approval rather than opening
            # an invisible prompt or mutating the user's real workspace.
            "--auto-approve",
            "false",
        ]
        mode = str(context_pack.get("mode") or "plan").strip().lower()
        if mode != "act":
            args.append("--plan")
        model = str(context_pack.get("model") or "").strip()
        if model:
            args.extend(["--model", model[:160]])
        thinking = str(
            context_pack.get("reasoning_effort")
            or context_pack.get("thinking")
            or ""
        ).strip().lower()
        if thinking in {"none", "low", "medium", "high", "xhigh"}:
            args.extend(["--thinking", thinking])
        provider = str(context_pack.get("provider") or "").strip()
        if provider:
            args.extend(["--provider", provider[:100]])
        session_id = str(
            context_pack.get("provider_session_id")
            or context_pack.get("native_thread_id")
            or context_pack.get("task_id")
            or ""
        ).strip()
        if session_id:
            args.extend(self._resume_args(session_id))
        retries = context_pack.get("max_retries")
        try:
            retry_count = (
                min(max(int(retries), 0), 10)
                if retries is not None
                else None
            )
        except (TypeError, ValueError):
            retry_count = None
        if retry_count is not None:
            args.extend(["--retries", str(retry_count)])
        args.append(prompt)
        return resolve_cli_argv(binary, *args)

    def stream_event(self, stream: str, line: str) -> WorkerEvent:
        if stream != "stdout":
            return super().stream_event(stream, line)
        try:
            raw = json.loads(line)
        except (TypeError, ValueError):
            return super().stream_event(stream, line)
        if not isinstance(raw, dict):
            return super().stream_event(stream, line)

        body = _event_body(raw)
        outer_type = str(raw.get("type") or "event").strip().lower()
        event_type = str(
            body.get("type")
            or body.get("event")
            or body.get("kind")
            or outer_type
            or "event"
        ).strip().lower()
        say = str(body.get("say") or "").strip().lower()
        ask = str(body.get("ask") or "").strip().lower()
        subtype = say or ask or event_type
        text = _text(body)
        reasoning = _short(
            body.get("reasoning")
            or body.get("thinking")
            or body.get("thought")
            or ""
        )
        partial = bool(body.get("partial") is True or raw.get("partial") is True)
        native_id = _native_id(raw, body)
        base: dict[str, Any] = {
            "adapter": self.name,
            "agent_type": self.name,
            "partial": partial,
            "provider_event": subtype,
            "timestamp_ms": body.get("ts") or raw.get("ts"),
        }
        if native_id:
            base.update(
                {
                    "provider_session_id": native_id,
                    "native_id": native_id,
                    "task_id": native_id,
                }
            )

        if native_id and subtype in {
            "session_started",
            "task_started",
            "initialized",
            "init",
            "start",
        }:
            return WorkerEvent(
                "agent_thread_started",
                {**base, "thread_id": native_id},
            )

        code = _error_code(raw, body)
        combined_error = f"{code} {text}".lower()
        if code in _LIMIT_CODES or any(
            marker in combined_error
            for marker in (
                "insufficient credits",
                "usage limit",
                "rate limit",
                "quota exceeded",
                "out of credits",
            )
        ):
            return WorkerEvent(
                "agent_limit_state",
                {
                    **base,
                    "state": "limited",
                    "reason": code or "provider_limit_reached",
                    "remaining_percent": None,
                    "reset_at": None,
                    "exact_percentage": False,
                    "message": text,
                },
            )

        if reasoning or subtype in {"reasoning", "thinking", "thought"}:
            return WorkerEvent(
                "reasoning_delta" if partial else "reasoning",
                {**base, "text": reasoning or text},
            )

        if outer_type == "agent_event" and event_type in {
            "usage",
            "token_usage",
            "metrics",
        }:
            return WorkerEvent(
                "agent_usage",
                {
                    **base,
                    "usage": body.get("usage") or body.get("tokens") or body,
                },
            )

        if ask or event_type in {
            "ask",
            "question",
            "approval_request",
            "user_input_required",
        }:
            return WorkerEvent(
                "agent_question",
                {
                    **base,
                    "text": text,
                    "question_type": ask or event_type or "approval",
                },
            )

        toolish = {
            "tool",
            "tool_use",
            "tool_call",
            "command",
            "execute_command",
            "run_command",
            "file_edit",
            "write_to_file",
            "replace_in_file",
        }
        if subtype in toolish or event_type in toolish:
            status = str(body.get("status") or "").strip().lower()
            finished = (
                not partial
                and status not in {"started", "running", "pending"}
            )
            return WorkerEvent(
                "tool_finished" if finished else "tool_started",
                {
                    **base,
                    "tool_name": str(
                        body.get("name")
                        or body.get("tool_name")
                        or say
                        or event_type
                        or "tool"
                    )[:120],
                    "tool_call_id": str(
                        body.get("tool_call_id")
                        or body.get("toolUseId")
                        or body.get("id")
                        or ""
                    )[:300],
                    "input": body.get("input") or body.get("arguments"),
                    "output": text,
                    "status": status or ("completed" if finished else "running"),
                    "success": finished and status not in {"failed", "error"},
                },
            )

        if event_type in {"error", "failed", "run_error"} or raw.get(
            "error"
        ):
            return WorkerEvent(
                "agent_error",
                {
                    **base,
                    "message": text or _short(raw.get("error"), 2_000),
                    "error_code": code or "cline_error",
                    "fatal": True,
                },
            )

        if subtype in {
            "completion_result",
            "result",
            "final",
            "completed",
            "task_completed",
        } or event_type in {"result", "completed", "task_completed"}:
            return WorkerEvent(
                "agent_turn_completed",
                {**base, "text": text, "final": True},
            )

        if text:
            return WorkerEvent(
                "assistant_message",
                {**base, "text": text, "final": not partial},
            )
        return WorkerEvent(
            "agent_activity",
            {
                **base,
                "status": "running",
                "message": f"Cline {subtype or outer_type}",
            },
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
            "cline",
        ]
