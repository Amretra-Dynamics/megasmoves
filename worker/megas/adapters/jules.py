"""Jules adapter backed by the official sessions and activities REST API.

The local ``jules`` executable is still discovered and version-bound for the
installation certificate, but conversation authority comes from Jules session
resources and activity IDs. Terminal prose is never treated as native history.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from typing import Any, AsyncIterator

from .base import WorkerEvent, resolve_cli_argv
from .headless_agent import ReadOnlyHeadlessAgentAdapter
from .jules_control_plane import (
    JulesApiError,
    JulesControlPlane,
    UrllibJulesTransport,
    normalize_session_name,
)


_TERMINAL_STATES = {"COMPLETED", "FAILED"}
_WAITING_STATES = {"AWAITING_PLAN_APPROVAL", "AWAITING_USER_FEEDBACK", "PAUSED"}


def _short(value: object, limit: int = 12_000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "\n[TRUNCATED]"


def _prompt_digest(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _activity_id(activity: dict[str, Any]) -> str:
    return str(activity.get("name") or activity.get("id") or "").strip()


def _activity_event(activity: dict[str, Any], *, session_name: str) -> WorkerEvent:
    item_id = _activity_id(activity)[:300]
    base = {
        "adapter": "jules_cli",
        "agent_type": "jules_cli",
        "provider_session_id": session_name,
        "native_id": session_name,
        "item_id": item_id,
        "created_at": str(activity.get("createTime") or "")[:100],
        "source": "jules_rest_activity",
    }
    agent_message = activity.get("agentMessaged")
    if isinstance(agent_message, dict):
        text = _short(agent_message.get("agentMessage"))
        return WorkerEvent("assistant_message", {**base, "text": text, "final": True})
    user_message = activity.get("userMessaged")
    if isinstance(user_message, dict):
        text = _short(user_message.get("userMessage"), 2_000)
        return WorkerEvent(
            "agent_activity",
            {
                **base,
                "status": "accepted",
                "provider_event": "user_message_persisted",
                "prompt_sha256": _prompt_digest(text) if text else "",
                "chat_visible": False,
            },
        )
    plan_generated = activity.get("planGenerated")
    if isinstance(plan_generated, dict):
        plan = plan_generated.get("plan") if isinstance(plan_generated.get("plan"), dict) else {}
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        return WorkerEvent(
            "plan_update",
            {
                **base,
                "plan_id": _short(plan.get("id"), 200),
                "steps": [
                    {
                        "id": _short(step.get("id"), 200),
                        "title": _short(step.get("title"), 1_000),
                        "description": _short(step.get("description"), 4_000),
                        "index": step.get("index"),
                    }
                    for step in steps[:200]
                    if isinstance(step, dict)
                ],
            },
        )
    progress = activity.get("progressUpdated")
    if isinstance(progress, dict):
        return WorkerEvent(
            "agent_activity",
            {
                **base,
                "status": "running",
                "message": _short(progress.get("title") or activity.get("description"), 1_000),
                "detail": _short(progress.get("description"), 4_000),
            },
        )
    if isinstance(activity.get("sessionCompleted"), dict):
        return WorkerEvent("agent_turn_completed", {**base, "final": True})
    failed = activity.get("sessionFailed")
    if isinstance(failed, dict):
        return WorkerEvent(
            "agent_error",
            {**base, "message": _short(failed.get("reason") or "Jules session failed.", 2_000)},
        )
    if isinstance(activity.get("planApproved"), dict):
        approved = activity.get("planApproved") or {}
        return WorkerEvent(
            "agent_activity",
            {**base, "status": "running", "message": "Jules plan approved", "plan_id": _short(approved.get("planId"), 200)},
        )
    return WorkerEvent(
        "agent_activity",
        {
            **base,
            "status": "running",
            "message": _short(activity.get("description") or "Jules activity"),
            "chat_visible": False,
        },
    )


class JulesCliAdapter(ReadOnlyHeadlessAgentAdapter):
    name = "jules_cli"
    adapter_id = "jules_cli"
    contract_version = "1.0"
    supported_range = ">=0.1.0"
    binary_env = "MEGAS_JULES_BINARY"
    default_binary = "jules"
    timeout_env = "MEGAS_JULES_TIMEOUT_SEC"
    version_args = ["version"]
    version_regex = r"(\d+\.\d+\.\d+)"

    def is_authenticated(self) -> bool:
        return bool(os.getenv("JULES_API_KEY", "").strip())

    def process_args(self, prompt: str, context_pack: dict) -> list[str]:
        # Kept for discovery/diagnostics only. Native execution is API-backed.
        binary = os.getenv(self.binary_env, self.default_binary)
        return resolve_cli_argv(binary, "remote", "list")

    def _control_plane(self) -> JulesControlPlane:
        api_key = os.getenv("JULES_API_KEY", "").strip()
        if not api_key:
            raise PermissionError("JULES_API_KEY_REQUIRED")
        try:
            timeout = float(os.getenv("MEGAS_JULES_HTTP_TIMEOUT_SEC", "30") or "30")
        except ValueError:
            timeout = 30.0
        return JulesControlPlane(UrllibJulesTransport(api_key, timeout_sec=timeout))

    async def launch(self, command: dict) -> AsyncIterator[WorkerEvent]:
        context_pack = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        prompt = self.prompt(command, context_pack)
        session_id = str(command.get("session_id") or "")
        command_id = str(command.get("command_id") or "")
        native_value = str(
            context_pack.get("provider_session_id")
            or context_pack.get("jules_session_id")
            or context_pack.get("native_thread_id")
            or ""
        ).strip()
        resumed = bool(native_value)
        try:
            timeout_sec = float(
                command.get("timeout_sec")
                or context_pack.get("timeout_sec")
                or os.getenv(self.timeout_env, "900")
            )
        except (TypeError, ValueError):
            timeout_sec = 900.0
        timeout_sec = min(max(timeout_sec, 30.0), 3600.0)
        poll_sec = min(max(float(os.getenv("MEGAS_JULES_POLL_SEC", "2") or "2"), 0.5), 15.0)
        plane = self._control_plane()

        yield WorkerEvent(
            "command_started",
            {
                "command_id": command_id,
                "command": ["jules", "api", "sessions"],
                "session_id": session_id,
                "adapter": self.name,
                "agent_type": self.name,
                "transport": "jules_rest_v1alpha",
            },
        )

        known_activity_ids: set[str] = set()
        prompt_persisted = False
        expected_prompt_hash = _prompt_digest(prompt)
        try:
            if resumed:
                native_name = normalize_session_name(native_value)
                existing = await asyncio.to_thread(plane.get_session, native_name)
                if str(existing.get("name") or "") != native_name:
                    raise RuntimeError("JULES_EXACT_RESUME_IDENTITY_MISMATCH")
                before = await asyncio.to_thread(plane.list_activities, native_name)
                known_activity_ids = {_activity_id(item) for item in before if _activity_id(item)}
                await asyncio.to_thread(plane.send_message, native_name, prompt)
            else:
                created = await asyncio.to_thread(
                    plane.create_session,
                    prompt=prompt,
                    title=str(context_pack.get("title") or "Megas")[:120],
                    source=str(context_pack.get("jules_source") or ""),
                    starting_branch=str(context_pack.get("starting_branch") or ""),
                    require_plan_approval=bool(context_pack.get("require_plan_approval", True)),
                )
                native_name = normalize_session_name(created.get("name") or created.get("id"))
                prompt_persisted = str(created.get("prompt") or "") == prompt
        except JulesApiError as exc:
            code = "ACCEPTANCE_AMBIGUOUS_REQUIRES_RECONCILIATION" if exc.ambiguous else "JULES_API_REQUEST_FAILED"
            yield WorkerEvent(
                "agent_error",
                {
                    "adapter": self.name,
                    "agent_type": self.name,
                    "message": f"{code}: {_short(exc, 1_500)}",
                    "acceptance_ambiguous": bool(exc.ambiguous),
                    "retry_safe": False if exc.ambiguous else True,
                },
            )
            yield WorkerEvent(
                "command_finished",
                {"command_id": command_id, "adapter": self.name, "exit_code": 1},
            )
            return

        yield WorkerEvent(
            "agent_thread_started",
            {
                "adapter": self.name,
                "agent_type": self.name,
                "source": "jules_rest_session",
                "thread_id": native_name,
                "provider_session_id": native_name,
                "native_id": native_name,
                "resumed": resumed,
            },
        )

        started = time.monotonic()
        terminal_state = ""
        while time.monotonic() - started < timeout_sec:
            try:
                activities = await asyncio.to_thread(plane.list_activities, native_name)
                for activity in activities:
                    item_id = _activity_id(activity)
                    if not item_id or item_id in known_activity_ids:
                        continue
                    known_activity_ids.add(item_id)
                    user_message = activity.get("userMessaged")
                    if isinstance(user_message, dict):
                        text = str(user_message.get("userMessage") or "")
                        if _prompt_digest(text) == expected_prompt_hash:
                            prompt_persisted = True
                    yield _activity_event(activity, session_name=native_name)

                session = await asyncio.to_thread(plane.get_session, native_name)
                terminal_state = str(session.get("state") or "").upper()
                if terminal_state in _TERMINAL_STATES:
                    break
                if terminal_state == "AWAITING_PLAN_APPROVAL":
                    yield WorkerEvent(
                        "approval_request",
                        {
                            "adapter": self.name,
                            "agent_type": self.name,
                            "provider_session_id": native_name,
                            "text": "Jules generated a plan and is waiting for approval.",
                            "approval_type": "jules_plan",
                        },
                    )
                    break
                if terminal_state == "AWAITING_USER_FEEDBACK":
                    yield WorkerEvent(
                        "agent_question",
                        {
                            "adapter": self.name,
                            "agent_type": self.name,
                            "provider_session_id": native_name,
                            "text": "Jules is waiting for user feedback.",
                        },
                    )
                    break
                if terminal_state in _WAITING_STATES:
                    break
            except JulesApiError as exc:
                yield WorkerEvent(
                    "agent_activity",
                    {
                        "adapter": self.name,
                        "agent_type": self.name,
                        "provider_session_id": native_name,
                        "status": "reconciling",
                        "message": _short(exc, 1_000),
                    },
                )
            await asyncio.sleep(poll_sec)

        if not prompt_persisted:
            yield WorkerEvent(
                "agent_error",
                {
                    "adapter": self.name,
                    "agent_type": self.name,
                    "provider_session_id": native_name,
                    "message": "NATIVE_PERSISTENCE_NOT_PROVED: Jules user activity was not observed.",
                    "expected_prompt_sha256": expected_prompt_hash,
                },
            )
            yield WorkerEvent(
                "command_finished",
                {
                    "command_id": command_id,
                    "adapter": self.name,
                    "provider_session_id": native_name,
                    "exit_code": 1,
                },
            )
            return

        exit_code = 1 if terminal_state == "FAILED" else 0
        yield WorkerEvent(
            "command_finished",
            {
                "command_id": command_id,
                "adapter": self.name,
                "provider_session_id": native_name,
                "native_id": native_name,
                "exit_code": exit_code,
                "provider_state": terminal_state or "IN_PROGRESS",
                "native_persistence_proved": True,
            },
        )

    def capabilities(self) -> list[str]:
        return [
            "cloud_native",
            "structured_stream",
            "session_list",
            "session_read",
            "session_resume",
            "native_reconciliation",
            "jules_cli",
        ]
