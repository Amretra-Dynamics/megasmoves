"""Devin adapter backed by the official v3 sessions/messages API.

The local ``devin`` binary remains part of the installation fingerprint, while
conversation authority comes only from Devin session and message resources.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from typing import Any, AsyncIterator

from .base import WorkerEvent, resolve_cli_argv
from .devin_control_plane import (
    DevinApiError,
    DevinControlPlane,
    UrllibDevinTransport,
    normalize_devin_id,
)
from .headless_agent import ReadOnlyHeadlessAgentAdapter


_TERMINAL = {"exit", "error"}
_WAITING = {"waiting_for_user", "waiting_for_approval"}
_LIMIT_REASONS = {
    "usage_limit_exceeded",
    "out_of_credits",
    "out_of_quota",
    "no_quota_allocation",
    "payment_declined",
    "org_usage_limit_exceeded",
    "total_session_limit_exceeded",
}


def _short(value: object, limit: int = 12_000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "\n[TRUNCATED]"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _message_id(item: dict[str, Any]) -> str:
    return str(item.get("event_id") or item.get("id") or "").strip()


def _message_event(item: dict[str, Any], *, session_id: str) -> WorkerEvent:
    source = str(item.get("source") or "").strip().lower()
    text = _short(item.get("message"))
    base = {
        "adapter": "devin_cli",
        "agent_type": "devin_cli",
        "provider_session_id": session_id,
        "native_id": session_id,
        "item_id": _message_id(item)[:300],
        "created_at": item.get("created_at"),
        "source": "devin_v3_messages",
    }
    if source in {"user", "human", "service_user"}:
        return WorkerEvent(
            "agent_activity",
            {
                **base,
                "status": "accepted",
                "provider_event": "user_message_persisted",
                "prompt_sha256": _digest(text) if text else "",
                "chat_visible": False,
            },
        )
    return WorkerEvent(
        "assistant_message",
        {**base, "text": text, "final": True},
    )


class DevinCliAdapter(ReadOnlyHeadlessAgentAdapter):
    name = "devin_cli"
    adapter_id = "devin_cli"
    contract_version = "1.0"
    supported_range = ">=1.0.0"
    binary_env = "MEGAS_DEVIN_BINARY"
    default_binary = "devin"
    timeout_env = "MEGAS_DEVIN_TIMEOUT_SEC"
    version_args = ["--version"]
    version_regex = r"(\d+\.\d+\.\d+)"

    def is_authenticated(self) -> bool:
        return bool(
            os.getenv("DEVIN_API_KEY", "").strip()
            and os.getenv("DEVIN_ORG_ID", "").strip()
        )

    def process_args(self, prompt: str, context_pack: dict) -> list[str]:
        # Diagnostics only. Native execution is API-backed.
        binary = os.getenv(self.binary_env, self.default_binary)
        return resolve_cli_argv(binary, "--version")

    def _control_plane(self) -> DevinControlPlane:
        token = os.getenv("DEVIN_API_KEY", "").strip()
        org_id = os.getenv("DEVIN_ORG_ID", "").strip()
        if not token:
            raise PermissionError("DEVIN_API_KEY_REQUIRED")
        if not org_id:
            raise PermissionError("DEVIN_ORG_ID_REQUIRED")
        try:
            timeout = float(os.getenv("MEGAS_DEVIN_HTTP_TIMEOUT_SEC", "30") or "30")
        except ValueError:
            timeout = 30.0
        return DevinControlPlane(
            UrllibDevinTransport(token, timeout_sec=timeout),
            org_id,
        )

    async def launch(self, command: dict) -> AsyncIterator[WorkerEvent]:
        context_pack = (
            command.get("context_pack")
            if isinstance(command.get("context_pack"), dict)
            else {}
        )
        prompt = self.prompt(command, context_pack)
        command_id = str(command.get("command_id") or "")
        megas_session_id = str(command.get("session_id") or "")
        native_value = str(
            context_pack.get("provider_session_id")
            or context_pack.get("devin_session_id")
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
        try:
            poll_sec = float(os.getenv("MEGAS_DEVIN_POLL_SEC", "2") or "2")
        except ValueError:
            poll_sec = 2.0
        poll_sec = min(max(poll_sec, 0.5), 15.0)
        plane = self._control_plane()

        yield WorkerEvent(
            "command_started",
            {
                "command_id": command_id,
                "command": ["devin", "api", "v3", "sessions"],
                "session_id": megas_session_id,
                "adapter": self.name,
                "agent_type": self.name,
                "transport": "devin_v3",
            },
        )

        known_message_ids: set[str] = set()
        prompt_persisted = False
        expected_prompt_hash = _digest(prompt)
        try:
            if resumed:
                native_id = normalize_devin_id(native_value)
                existing = await asyncio.to_thread(plane.get_session, native_id)
                if normalize_devin_id(existing.get("session_id")) != native_id:
                    raise RuntimeError("DEVIN_EXACT_RESUME_IDENTITY_MISMATCH")
                before = await asyncio.to_thread(plane.list_messages, native_id)
                known_message_ids = {
                    _message_id(item) for item in before if _message_id(item)
                }
                await asyncio.to_thread(plane.send_message, native_id, prompt)
            else:
                repos_raw = context_pack.get("devin_repos")
                repos = (
                    [str(item) for item in repos_raw if str(item).strip()]
                    if isinstance(repos_raw, list)
                    else []
                )
                max_acu_raw = context_pack.get("max_acu_limit")
                try:
                    max_acu = int(max_acu_raw) if max_acu_raw is not None else None
                except (TypeError, ValueError):
                    max_acu = None
                created = await asyncio.to_thread(
                    plane.create_session,
                    prompt=prompt,
                    title=str(context_pack.get("title") or "Megas")[:200],
                    repos=repos,
                    max_acu_limit=max_acu,
                )
                native_id = normalize_devin_id(created.get("session_id"))
        except DevinApiError as exc:
            code = (
                "ACCEPTANCE_AMBIGUOUS_REQUIRES_RECONCILIATION"
                if exc.ambiguous
                else "DEVIN_API_REQUEST_FAILED"
            )
            yield WorkerEvent(
                "agent_error",
                {
                    "adapter": self.name,
                    "agent_type": self.name,
                    "message": f"{code}: {_short(exc, 1_500)}",
                    "acceptance_ambiguous": bool(exc.ambiguous),
                    "retry_safe": False if exc.ambiguous else True,
                    "fatal": True,
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
                "source": "devin_v3_session",
                "thread_id": native_id,
                "provider_session_id": native_id,
                "native_id": native_id,
                "resumed": resumed,
            },
        )

        started = time.monotonic()
        status = ""
        status_detail = ""
        while time.monotonic() - started < timeout_sec:
            try:
                messages = await asyncio.to_thread(plane.list_messages, native_id)
                for item in messages:
                    item_id = _message_id(item)
                    if not item_id or item_id in known_message_ids:
                        continue
                    known_message_ids.add(item_id)
                    text = str(item.get("message") or "")
                    source = str(item.get("source") or "").strip().lower()
                    if source in {"user", "human", "service_user"} and _digest(text) == expected_prompt_hash:
                        prompt_persisted = True
                    yield _message_event(item, session_id=native_id)

                session = await asyncio.to_thread(plane.get_session, native_id)
                if normalize_devin_id(session.get("session_id")) != native_id:
                    raise RuntimeError("DEVIN_EXACT_RESUME_IDENTITY_MISMATCH")
                status = str(session.get("status") or "").strip().lower()
                status_detail = str(session.get("status_detail") or "").strip().lower()
                if status_detail in _LIMIT_REASONS:
                    yield WorkerEvent(
                        "agent_limit_state",
                        {
                            "adapter": self.name,
                            "agent_type": self.name,
                            "provider_session_id": native_id,
                            "state": "limited",
                            "reason": status_detail,
                            "remaining_percent": None,
                            "reset_at": None,
                            "exact_percentage": False,
                        },
                    )
                    break
                if status in _TERMINAL or status_detail == "finished":
                    break
                if status_detail in _WAITING:
                    event_type = (
                        "approval_request"
                        if status_detail == "waiting_for_approval"
                        else "agent_question"
                    )
                    yield WorkerEvent(
                        event_type,
                        {
                            "adapter": self.name,
                            "agent_type": self.name,
                            "provider_session_id": native_id,
                            "text": (
                                "Devin is waiting for approval."
                                if status_detail == "waiting_for_approval"
                                else "Devin is waiting for user input."
                            ),
                        },
                    )
                    break
            except DevinApiError as exc:
                yield WorkerEvent(
                    "agent_activity",
                    {
                        "adapter": self.name,
                        "agent_type": self.name,
                        "provider_session_id": native_id,
                        "status": "reconciling",
                        "message": _short(exc, 1_000),
                    },
                )
            await asyncio.sleep(poll_sec)

        # Re-read once after the provider reached a stable state.
        try:
            final_messages = await asyncio.to_thread(plane.list_messages, native_id)
            for item in final_messages:
                text = str(item.get("message") or "")
                source = str(item.get("source") or "").strip().lower()
                if source in {"user", "human", "service_user"} and _digest(text) == expected_prompt_hash:
                    prompt_persisted = True
        except DevinApiError:
            pass

        if not prompt_persisted:
            yield WorkerEvent(
                "agent_error",
                {
                    "adapter": self.name,
                    "agent_type": self.name,
                    "provider_session_id": native_id,
                    "message": "NATIVE_PERSISTENCE_NOT_PROVED: Devin user message was not observed.",
                    "expected_prompt_sha256": expected_prompt_hash,
                    "fatal": True,
                },
            )
            yield WorkerEvent(
                "command_finished",
                {
                    "command_id": command_id,
                    "adapter": self.name,
                    "provider_session_id": native_id,
                    "exit_code": 1,
                },
            )
            return

        exit_code = 1 if status == "error" else 0
        yield WorkerEvent(
            "command_finished",
            {
                "command_id": command_id,
                "adapter": self.name,
                "provider_session_id": native_id,
                "native_id": native_id,
                "exit_code": exit_code,
                "provider_status": status,
                "provider_status_detail": status_detail,
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
            "provider_cancellation",
            "limit_state",
            "devin_cli",
        ]
