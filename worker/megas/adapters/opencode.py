"""OpenCode adapter — HTTP control plane first, CLI fallback before submission only.

A known native ``ses_*`` is resumed exactly. If that session cannot be proven,
Megas reports the failure instead of silently creating a replacement thread.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
from typing import Any, AsyncIterator

from megas.policy import resolve_allowed_workdir

from .base import WorkerEvent, prefer_console_free_tool_path, resolve_cli_argv, which_with_path
from .headless_agent import ReadOnlyHeadlessAgentAdapter
from .opencode_control_plane import (
    OpenCodeControlPlane,
    load_bindings,
    messages_to_thread_payload,
    parse_model_ref,
    resolve_bound_session,
    save_binding,
)

SUPPORTED_OPENCODE_VERSIONS = frozenset({"1.18.5", "1.18.10"})


def _short(value: Any, limit: int = 12_000) -> str:
    return str(value or "")[:limit]


def _use_http_plane() -> bool:
    raw = os.getenv("MEGAS_OPENCODE_TRANSPORT", "cli").strip().lower()
    return raw in {"http", "serve", "control_plane"}


class OpenCodeAdapter(ReadOnlyHeadlessAgentAdapter):
    name = "opencode"
    adapter_id = "opencode"
    supported_range = ">=1.18.0"
    binary_env = "MEGAS_OPENCODE_BINARY"
    default_binary = "opencode"
    timeout_env = "MEGAS_OPENCODE_TIMEOUT_SEC"
    version_args = ["--version"]
    version_regex = r"(\d+\.\d+\.\d+)"

    def health_status(self) -> str:
        """OpenCode health check that never fails the entire worker.
        
        Returns "available" if the binary is installed, even if the configured
        model endpoint is unreachable. This ensures one failing agent (e.g., a
        stale offline GPU endpoint) doesn't disrupt all other agents on the system.
        
        The endpoint reachability is treated as a warning, not a hard failure.
        """
        base_status = super().health_status()
        if base_status != "available":
            return base_status

        # OpenCode binary is installed and runnable - that's the core requirement.
        # Config issues are non-fatal; the agent can still work with different models.
        config_path = Path.home() / ".config" / "opencode" / "opencode.json"
        if not config_path.is_file():
            # No config is fine - agent will use defaults
            return "available"

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            # Config read failure is a warning, not a blocker - agent can still run
            return "available"

        model = config.get("model")
        if not model or not isinstance(model, str):
            return "available"

        provider_name = model.split("/")[0] if "/" in model else None
        if not provider_name:
            return "available"

        providers = config.get("provider") or {}
        provider_config = providers.get(provider_name) or {}
        options = provider_config.get("options") or {}
        base_url = options.get("baseURL")

        # Optional endpoint health check - failure here is informational only.
        # We log but don't block the agent from being available.
        if base_url and isinstance(base_url, str):
            import urllib.parse
            import socket
            try:
                parsed = urllib.parse.urlparse(base_url)
                host = parsed.hostname
                port = parsed.port
                if not port:
                    port = 443 if parsed.scheme == "https" else 80
                if host:
                    # Quick connectivity check with short timeout
                    with socket.create_connection((host, port), timeout=1.5):
                        pass
            except Exception as e:
                # Endpoint unreachable - log it but don't fail the agent.
                # The user can still use OpenCode with other models/providers.
                import sys
                print(
                    f"[OpenCode] Warning: Configured endpoint {base_url} is unreachable: {type(e).__name__}. "
                    f"Agent remains available; use a different model or fix the endpoint.",
                    file=sys.stderr,
                    flush=True,
                )
                # Return "available" - the agent binary works, just the endpoint doesn't
                return "available"

        return "available"

    def discover_executable(self) -> Path | None:
        env_path = prefer_console_free_tool_path(os.environ).get("PATH", "")
        for candidate in (os.getenv(self.binary_env, "").strip(), "opencode"):
            if not candidate:
                continue
            if os.path.isabs(candidate) or ("/" in candidate or "\\" in candidate):
                path = Path(candidate)
                if path.is_file():
                    return path
            resolved = which_with_path(candidate, env_path)
            if resolved:
                return Path(resolved)
        return None

    def process_args(self, prompt: str, context_pack: dict) -> list[str]:
        binary = str(self.discover_executable() or os.getenv(self.binary_env, self.default_binary))
        args: list[str] = ["run", "--format", "json"]
        model = str(context_pack.get("model") or "").strip()
        if not model:
            model = "opencode/nemotron-3-ultra-free"
        if model:
            if isinstance(context_pack.get("model"), dict):
                parsed = parse_model_ref(context_pack.get("model"))
                if parsed:
                    model = f"{parsed['providerID']}/{parsed['modelID']}"
            args.extend(["--model", model[:160]])
        native_agent = str(context_pack.get("native_agent") or "").strip()
        if native_agent:
            args.extend(["--agent", native_agent[:100]])
        session_id = str(
            context_pack.get("provider_session_id")
            or context_pack.get("opencode_session_id")
            or ""
        ).strip()
        if session_id:
            args.extend(["--session", session_id[:200]])
        variant = str(context_pack.get("model_variant") or "").strip()
        if variant:
            args.extend(["--variant", variant[:80]])
        if context_pack.get("show_thinking") is True:
            args.append("--thinking")
        args.append(prompt)
        return resolve_cli_argv(binary, *args)

    def stream_event(self, stream: str, line: str) -> WorkerEvent:
        if stream != "stdout":
            return super().stream_event(stream, line)
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            return super().stream_event(stream, line)
        if not isinstance(event, dict):
            return super().stream_event(stream, line)

        event_type = str(event.get("type") or "").strip().lower()
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        base = {
            "adapter": self.name,
            "agent_type": self.name,
            "provider_session_id": str(event.get("sessionID") or "")[:200],
            "item_id": str(part.get("id") or "")[:200],
        }
        if event_type == "text":
            return WorkerEvent(
                "agent_message",
                {**base, "text": _short(part.get("text") or event.get("text"))},
            )
        if event_type == "reasoning":
            return WorkerEvent(
                "reasoning",
                {**base, "text": _short(part.get("text") or event.get("text"))},
            )
        if event_type == "tool_use":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = str(state.get("status") or "completed").lower()
            return WorkerEvent(
                "tool_finished",
                {
                    **base,
                    "tool_name": str(part.get("tool") or "tool")[:120],
                    "status": status,
                    "success": status == "completed",
                    "output": _short(state.get("output") or state.get("error")),
                },
            )
        if event_type in {"step_start", "step_finish"}:
            return WorkerEvent(
                "agent_activity",
                {
                    **base,
                    "status": "running" if event_type == "step_start" else "completed",
                    "message": (
                        "OpenCode step started"
                        if event_type == "step_start"
                        else "OpenCode step finished"
                    ),
                },
            )
        if event_type == "error":
            error = event.get("error")
            if isinstance(error, dict):
                data = error.get("data") if isinstance(error.get("data"), dict) else {}
                message = data.get("message") or error.get("name") or "OpenCode reported an error."
            else:
                message = error or "OpenCode reported an error."
            return WorkerEvent("agent_error", {**base, "message": _short(message, 2_000)})
        return WorkerEvent(
            "agent_activity",
            {**base, "status": "running", "message": f"OpenCode {event_type or 'event'}"},
        )

    def capabilities(self) -> list[str]:
        return ["read_only", "code_inspection", "structured_stream", "session_resume", "opencode"]

    def _resolve_native_session_id(self, command: dict, context_pack: dict) -> str:
        for key in (
            "provider_session_id",
            "opencode_session_id",
            "native_thread_id",
            "native_id",
        ):
            value = str(context_pack.get(key) or command.get(key) or "").strip()
            if value.startswith("ses_"):
                return value
        megas_session_id = str(
            command.get("session_id") or context_pack.get("session_id") or ""
        ).strip()
        bound = resolve_bound_session(megas_session_id=megas_session_id)
        if bound.startswith("ses_"):
            return bound
        megas_thread_id = str(context_pack.get("megas_thread_id") or "").strip()
        if megas_thread_id:
            row = load_bindings().get(megas_thread_id)
            if isinstance(row, dict):
                session_id = str(row.get("provider_session_id") or "").strip()
                if session_id.startswith("ses_"):
                    return session_id
        return ""

    async def launch(self, command: dict) -> AsyncIterator[WorkerEvent]:
        context_pack = (
            command.get("context_pack")
            if isinstance(command.get("context_pack"), dict)
            else {}
        )
        if context_pack.get("write_mode") is True:
            raise PermissionError(f"{self.name} write mode is not certified by Megas.")

        if not _use_http_plane() or os.getenv("MEGAS_AGENT_DRY_RUN", "").lower() in {
            "1",
            "true",
            "yes",
        }:
            async for event in super().launch(command):
                yield event
            return

        binary = self.discover_executable()
        if binary is None:
            async for event in super().launch(command):
                yield event
            return

        workspace = resolve_allowed_workdir(
            str(command.get("workdir") or context_pack.get("workspace_path") or "") or None,
            self.allowed_folders,
        )
        session_id = str(command.get("session_id") or "")
        command_id = str(command.get("command_id") or "")
        prompt = self.prompt(command, context_pack)
        try:
            timeout_sec = float(
                command.get("timeout_sec")
                or context_pack.get("timeout_sec")
                or os.getenv(self.timeout_env, "300")
            )
        except (TypeError, ValueError):
            timeout_sec = 300.0
        timeout_sec = min(max(timeout_sec, 30.0), 3600.0)

        requested_native_id = self._resolve_native_session_id(command, context_pack)
        native_id = ""
        native_session_selected = False
        prompt_may_have_been_submitted = False

        yield WorkerEvent(
            "command_started",
            {
                "command_id": command_id,
                "command": [self.default_binary, "serve", "http"],
                "session_id": session_id,
                "adapter": self.name,
                "agent_type": self.name,
                "transport": "http",
                "workspace": str(workspace),
            },
        )
        yield WorkerEvent(
            "terminal_stdout",
            {
                "text": "[opencode] HTTP control plane…",
                "adapter": self.name,
                "agent_type": self.name,
                "phase": "waiting_for_model",
            },
        )

        try:
            plane = OpenCodeControlPlane(str(binary), directory=workspace)
            handle = await asyncio.to_thread(plane.ensure)
            live_version = str(handle.version or "").strip()
            if live_version and live_version not in SUPPORTED_OPENCODE_VERSIONS:
                allow_any = os.getenv("MEGAS_OPENCODE_ALLOW_ANY_VERSION", "").lower() in {
                    "1",
                    "true",
                    "yes",
                }
                if not allow_any:
                    raise RuntimeError(
                        f"PROVIDER_VERSION_UNSUPPORTED: opencode {live_version} "
                        f"(allowlist={sorted(SUPPORTED_OPENCODE_VERSIONS)})"
                    )

            native_id = requested_native_id
            resumed = bool(native_id)
            if native_id:
                try:
                    await asyncio.to_thread(plane.get_session, native_id)
                except Exception as exc:
                    raise RuntimeError(
                        f"OPENCODE_EXACT_RESUME_UNAVAILABLE: {native_id}: {exc}"
                    ) from exc
            else:
                created = await asyncio.to_thread(
                    plane.create_session,
                    title=str(context_pack.get("title") or "Megas")[:120],
                )
                native_id = str(created.get("id") or "").strip()

            if not native_id.startswith("ses_"):
                raise RuntimeError("OpenCode session id missing ses_ prefix")
            native_session_selected = True
            await asyncio.to_thread(
                plane.wait_until_idle,
                native_id,
                timeout_sec=min(30.0, timeout_sec),
            )

            save_binding(
                megas_session_id=session_id,
                provider_session_id=native_id,
                worker_id=str(context_pack.get("worker_id") or ""),
            )
            megas_thread_id = str(context_pack.get("megas_thread_id") or "").strip()
            if megas_thread_id:
                save_binding(
                    megas_session_id=megas_thread_id,
                    provider_session_id=native_id,
                    worker_id=str(context_pack.get("worker_id") or ""),
                )

            yield WorkerEvent(
                "agent_thread_started",
                {
                    "adapter": self.name,
                    "agent_type": self.name,
                    "source": "opencode_control_plane",
                    "thread_id": native_id,
                    "provider_session_id": native_id,
                    "resumed": resumed,
                },
            )
            yield WorkerEvent(
                "agent_activity",
                {
                    "adapter": self.name,
                    "agent_type": self.name,
                    "status": "running",
                    "message": (
                        f"OpenCode resumed {native_id}"
                        if resumed
                        else f"OpenCode created {native_id}"
                    ),
                    "provider_session_id": native_id,
                    "native_id": native_id,
                    "resumed": resumed,
                    "version": handle.version,
                },
            )

            prompt_may_have_been_submitted = True
            message_id = await asyncio.to_thread(
                plane.prompt_async,
                native_id,
                text=prompt,
                model=context_pack.get("model"),
                agent=str(context_pack.get("native_agent") or ""),
                variant=str(context_pack.get("model_variant") or ""),
            )
            save_binding(
                megas_session_id=session_id,
                provider_session_id=native_id,
                message_id=message_id,
                worker_id=str(context_pack.get("worker_id") or ""),
            )

            messages, assistant_text = await asyncio.to_thread(
                plane.wait_for_assistant_after,
                native_id,
                user_message_id=message_id,
                timeout_sec=timeout_sec,
            )

            user_ids: list[str] = []
            for item in messages:
                info = item.get("info") if isinstance(item.get("info"), dict) else item
                if str((info or {}).get("role") or "").lower() == "user":
                    user_ids.append(str((info or {}).get("id") or ""))
            if message_id not in user_ids:
                yield WorkerEvent(
                    "agent_error",
                    {
                        "adapter": self.name,
                        "message": (
                            "NATIVE_PERSISTENCE_TIMEOUT: exact OpenCode user message ID "
                            f"was not found: {message_id}"
                        ),
                        "provider_session_id": native_id,
                        "expected_message_id": message_id,
                    },
                )
                yield WorkerEvent(
                    "evidence",
                    {
                        "evidence_type": "terminal_result",
                        "summary": "OpenCode native persistence timeout",
                        "payload": {
                            "adapter": self.name,
                            "command": "serve http",
                            "workdir": str(workspace),
                            "exit_code": 1,
                            "assistant_answer": "",
                            "provider_session_id": native_id,
                            "native_id": native_id,
                        },
                    },
                )
                yield WorkerEvent(
                    "command_finished",
                    {
                        "command_id": command_id,
                        "exit_code": 1,
                        "adapter": self.name,
                        "provider_session_id": native_id,
                        "native_id": native_id,
                    },
                )
                return

            if assistant_text.startswith("[opencode error]"):
                yield WorkerEvent(
                    "agent_error",
                    {
                        "adapter": self.name,
                        "agent_type": self.name,
                        "message": _short(assistant_text, 2_000),
                        "provider_session_id": native_id,
                        "native_id": native_id,
                    },
                )
                yield WorkerEvent(
                    "agent_message",
                    {
                        "adapter": self.name,
                        "agent_type": self.name,
                        "text": _short(assistant_text),
                        "provider_session_id": native_id,
                        "native_id": native_id,
                    },
                )
            elif assistant_text:
                yield WorkerEvent(
                    "agent_message",
                    {
                        "adapter": self.name,
                        "agent_type": self.name,
                        "text": _short(assistant_text),
                        "provider_session_id": native_id,
                        "native_id": native_id,
                    },
                )
            else:
                empty_message = (
                    "[opencode] no assistant response was correlated to the exact "
                    f"user message {message_id}."
                )
                yield WorkerEvent(
                    "agent_error",
                    {
                        "adapter": self.name,
                        "message": empty_message,
                        "provider_session_id": native_id,
                        "native_id": native_id,
                    },
                )
                yield WorkerEvent(
                    "agent_message",
                    {
                        "adapter": self.name,
                        "agent_type": self.name,
                        "text": empty_message,
                        "provider_session_id": native_id,
                        "native_id": native_id,
                    },
                )

            thread_payload = messages_to_thread_payload(
                session_id=native_id,
                messages=messages,
                title=str(context_pack.get("title") or ""),
            )
            yield WorkerEvent(
                "agent_activity",
                {
                    "adapter": self.name,
                    "agent_type": self.name,
                    "status": "completed",
                    "message": "OpenCode turn synced",
                    "provider_session_id": native_id,
                    "native_id": native_id,
                    "thread_sync": thread_payload,
                    "message_count": len(thread_payload.get("messages") or []),
                    "user_message_id": message_id,
                    "resumed": resumed,
                },
            )
            failed = (not assistant_text) or assistant_text.startswith("[opencode error]")
            yield WorkerEvent(
                "evidence",
                {
                    "evidence_type": "terminal_result",
                    "summary": f"OpenCode turn completed (exit {'1' if failed else '0'})",
                    "payload": {
                        "adapter": self.name,
                        "command": "serve http",
                        "workdir": str(workspace),
                        "exit_code": 1 if failed else 0,
                        "assistant_answer": assistant_text,
                        "provider_session_id": native_id,
                        "native_id": native_id,
                    },
                },
            )
            yield WorkerEvent(
                "command_finished",
                {
                    "command_id": command_id,
                    "exit_code": 1 if failed else 0,
                    "adapter": self.name,
                    "agent_type": self.name,
                    "provider_session_id": native_id,
                    "native_id": native_id,
                    "transport": "http",
                    "resumed": resumed,
                },
            )
        except Exception as exc:
            if requested_native_id or native_session_selected or prompt_may_have_been_submitted:
                identity_payload = (
                    {
                        "provider_session_id": native_id,
                        "native_id": native_id,
                    }
                    if native_session_selected and native_id
                    else {
                        "requested_provider_session_id": requested_native_id,
                    }
                )
                yield WorkerEvent(
                    "agent_error",
                    {
                        "adapter": self.name,
                        "agent_type": self.name,
                        "message": _short(exc, 2_000),
                        "retry_safe": False,
                        **identity_payload,
                    },
                )
                yield WorkerEvent(
                    "evidence",
                    {
                        "evidence_type": "terminal_result",
                        "summary": f"OpenCode exception: {exc}",
                        "payload": {
                            "adapter": self.name,
                            "command": "serve http",
                            "workdir": str(workspace),
                            "exit_code": 1,
                            "assistant_answer": "",
                            **identity_payload,
                        },
                    },
                )
                yield WorkerEvent(
                    "command_finished",
                    {
                        "command_id": command_id,
                        "exit_code": 1,
                        "adapter": self.name,
                        "retry_safe": False,
                        **identity_payload,
                    },
                )
                return

            yield WorkerEvent(
                "agent_activity",
                {
                    "adapter": self.name,
                    "agent_type": self.name,
                    "status": "running",
                    "message": f"OpenCode HTTP path failed before session creation ({exc}); CLI fallback",
                },
            )
            async for event in super().launch(command):
                yield event
