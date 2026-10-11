from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ..bounded_io import staged_bounded_import
from .bundle import build_bundle, import_bundle, list_rebinding_requirements
from .bundle_crypto import decrypt_bundle, encrypt_bundle, is_encrypted_bundle
from .capabilities import CapabilitySnapshot, CapabilityStore
from .model import MCPProfile, MCPServer
from .profile_requirements import evaluate_profile_requirements
from .protocol import call_tool as protocol_call_tool
from .protocol import probe_server as protocol_probe_server
from .runtime import MCPRuntime
from .secret_release import SecretReleasePolicy
from .state_transaction import recover_pending_transaction
from .store import MCPStore
from .timeline import TimelineStore, contains_redacted, diff_values
from .workspace import activate_profile, active_profile, deactivate_profile


class MegasCoreService:
    """Canonical in-process API for local Megas product surfaces.

    CLI, desktop UI, tests, and future optional adapters should use this facade
    instead of reading Megas JSON files directly. It owns no network service and
    introduces no cloud or model dependency; all state continues to live in the
    normal local MCP store.
    """

    def __init__(self, home: Path | str | None = None) -> None:
        self.store = MCPStore(Path(home) if home is not None else None)
        recovered = recover_pending_transaction(self.store.mcp_dir)
        if recovered is not None:
            self.store.record(
                "state.transaction.recovered",
                transaction_id=recovered["transaction_id"],
                purpose=recovered["purpose"],
                restored=recovered["restored"],
                removed=recovered["removed"],
            )
        self.runtime = MCPRuntime(self.store)
        self.capabilities = CapabilityStore(self.store)
        self.secret_policy = SecretReleasePolicy(self.store)
        self.timeline = TimelineStore(self.store.mcp_dir)
        recovered_calls = self.timeline.recover_incomplete()
        if recovered_calls:
            self.store.record(
                "timeline.recovered",
                tool_call_ids=[call["id"] for call in recovered_calls],
                count=len(recovered_calls),
            )

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        path = path.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _capability_summary(self, server: MCPServer) -> dict[str, Any] | None:
        latest = self.capabilities.latest(server.id)
        return self.capabilities.summary(latest) if latest is not None else None

    def _secret_binding_view(self, binding: Any) -> dict[str, Any]:
        server = self.store.get_server_by_id(binding.server_id)
        return {
            **self.secret_policy.binding_view(binding),
            "server": server.name,
            "available": binding.provider == "env" and binding.reference in os.environ,
        }

    def server_view(self, reference: str) -> dict[str, Any]:
        server = self.store.get_server(reference)
        return {
            **server.to_dict(),
            "runtime": self.runtime.status(server),
            "latest_capability_snapshot": self._capability_summary(server),
            "secret_bindings": [
                self._secret_binding_view(binding)
                for binding in self.store.list_secret_bindings(server.id)
            ],
            "tool_permissions": [
                row
                for row in self.store.list_permissions()
                if row["server_id"] == server.id
            ],
        }

    def list_servers(self) -> list[dict[str, Any]]:
        return [self.server_view(server.id) for server in self.store.list_servers()]

    def health_snapshot(self) -> dict[str, Any]:
        servers = self.store.list_servers()
        runtime_states = [self.runtime.status(server) for server in servers]
        active = active_profile(self.store, self.runtime)
        session = self.timeline.active()
        return {
            "mode": "offline-full-mcp",
            "home": str(self.store.home),
            "network_required": False,
            "sign_in_required": False,
            "configured_servers": len(servers),
            "enabled_servers": sum(server.enabled for server in servers),
            "running_servers": sum(state.get("state") == "running" for state in runtime_states),
            "degraded_servers": sum(state.get("state") == "degraded" for state in runtime_states),
            "tool_permissions": len(self.store.list_permissions()),
            "secret_bindings": len(self.store.list_secret_bindings()),
            "secret_release_permissions": len(self.secret_policy.list()),
            "active_profile_id": active["profile_id"] if active else None,
            "active_profile": active["profile"] if active else None,
            "active_session_id": session.id if session else None,
            "cloud": "optional-not-required",
        }

    def register_server(self, server: MCPServer) -> dict[str, Any]:
        saved = self.store.save_server(server)
        return self.server_view(saved.id)

    def update_server(self, server: MCPServer) -> dict[str, Any]:
        current = self.store.get_server(server.id)
        if current.transport == "stdio":
            state = self.runtime.status(current)
            if state.get("state") in {"running", "degraded"}:
                self.runtime.stop(current)
        saved = self.store.save_server(server)
        return self.server_view(saved.id)

    def remove_server(self, reference: str) -> dict[str, Any]:
        server = self.store.get_server(reference)
        if server.transport == "stdio":
            self.runtime.stop(server)
        for binding in self.store.list_secret_bindings(server.id):
            self.secret_policy.revoke(server.id, binding.target)
        self.store.delete_server(server.id)
        return {"removed": server.name, "server_id": server.id}

    def set_server_enabled(self, reference: str, enabled: bool) -> dict[str, Any]:
        server = self.store.get_server(reference)
        if not enabled and server.transport == "stdio":
            self.runtime.stop(server)
        server.enabled = bool(enabled)
        self.store.save_server(server)
        return self.server_view(server.id)

    def start_server(self, reference: str, *, timeout: float = 8.0) -> dict[str, Any]:
        server = self.store.get_server(reference)
        self.secret_policy.prepare_server(server)
        return self.runtime.start(server, timeout=timeout)

    def stop_server(self, reference: str) -> dict[str, Any]:
        return self.runtime.stop(self.store.get_server(reference))

    def restart_server(self, reference: str, *, timeout: float = 8.0) -> dict[str, Any]:
        server = self.store.get_server(reference)
        self.secret_policy.prepare_server(server)
        self.runtime.stop(server)
        return self.runtime.start(server, timeout=timeout)

    def doctor(self, reference: str) -> dict[str, Any]:
        server = self.store.get_server(reference)
        result = self.runtime.doctor(server)
        bindings = self.store.list_secret_bindings(server.id)
        denied = [
            binding.target
            for binding in bindings
            if not self.secret_policy.is_allowed(binding)
        ]
        result["secret_bindings"] = [self._secret_binding_view(binding) for binding in bindings]
        if denied:
            result["healthy"] = False
            result.setdefault("problems", []).append(
                "secret release not granted: " + ", ".join(sorted(denied))
            )
        return result

    def probe_server(self, reference: str, *, timeout: float = 5.0) -> dict[str, Any]:
        server = self.store.get_server(reference)
        if not server.enabled:
            raise RuntimeError(f"{server.name} is disabled")
        prepared = self.secret_policy.prepare_server(server)
        if server.transport == "stdio" and self.runtime.status(server).get("state") == "running":
            probe = self.runtime.probe(server, timeout=timeout)
            source = "managed-runtime"
        else:
            probe = protocol_probe_server(prepared, timeout=timeout)
            source = "ephemeral-session" if server.transport == "stdio" else f"remote-{server.transport}"
        snapshot, diff, reused = self.capabilities.capture(server, probe)
        return {
            "server_id": server.id,
            "server": server.name,
            "source": source,
            "snapshot": {
                "id": snapshot.id,
                "fingerprint": snapshot.fingerprint,
                "observed_at": snapshot.observed_at,
                "reused": reused,
                "diff": diff,
            },
            **probe,
        }

    def capability_history(self, server_reference: str) -> list[dict[str, Any]]:
        server = self.store.get_server(server_reference)
        return [
            {
                **self.capabilities.summary(snapshot),
                "snapshot": snapshot.to_dict(),
            }
            for snapshot in self.capabilities.list(server.id)
        ]

    def capability_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        for server in self.store.list_servers():
            for snapshot in self.capabilities.list(server.id):
                if snapshot.id == snapshot_id:
                    return {
                        **self.capabilities.summary(snapshot),
                        "snapshot": snapshot.to_dict(),
                    }
        raise KeyError(f"unknown capability snapshot: {snapshot_id}")

    def list_permissions(self, server_reference: str | None = None) -> list[dict[str, str]]:
        rows = self.store.list_permissions()
        if server_reference is None:
            return rows
        server = self.store.get_server(server_reference)
        return [row for row in rows if row["server_id"] == server.id]

    def grant_tool(self, server_reference: str, tool: str) -> dict[str, Any]:
        server = self.store.get_server(server_reference)
        snapshot = self.capabilities.latest(server.id)
        if snapshot is None:
            raise RuntimeError(
                f"no inspected capability snapshot for {server.name}; probe the server first"
            )
        known = {str(item.get("name")) for item in snapshot.tools if item.get("name")}
        if tool not in known:
            raise ValueError(f"tool {tool} is not present in the latest capabilities for {server.name}")
        return self.store.grant_tool(server.id, tool)

    def cancel_tool_call(
        self,
        tool_call_id: str,
        *,
        reason: str = "cancel requested",
        source: str = "core-service",
    ) -> dict[str, Any]:
        call = self.timeline.get_tool_call(tool_call_id)
        call, changed = self.timeline.request_cancel(call.id, reason=reason, source=source)
        server = self.store.get_server_by_id(call.server_id)
        hard_termination_guaranteed = False
        runtime_stopped = False
        if call.status == "running" and call.source == "managed-runtime" and server.transport == "stdio":
            stopped = self.runtime.stop(server, timeout=0.25)
            runtime_stopped = bool(stopped.get("changed", False))
            hard_termination_guaranteed = True
        self.store.record(
            "tool.cancel.requested",
            tool_call_id=call.id,
            server_id=server.id,
            server=server.name,
            tool=call.tool,
            source=source,
            changed=changed,
            hard_termination_guaranteed=hard_termination_guaranteed,
            runtime_stopped=runtime_stopped,
        )
        current = self.timeline.get_tool_call(call.id)
        return {
            "tool_call_id": current.id,
            "server_id": current.server_id,
            "server": server.name,
            "tool": current.tool,
            "source": current.source,
            "cancel_requested": changed or current.cancel_requested_at is not None,
            "hard_termination_guaranteed": hard_termination_guaranteed,
            "runtime_stopped": runtime_stopped,
            "state": current.status,
            "reason": current.cancel_reason or reason,
        }

    def revoke_tool(self, server_reference: str, tool: str) -> dict[str, Any]:
        server = self.store.get_server(server_reference)
        # Persist deny first. Every new dispatch sees the revoked authority before
        # we begin terminating calls that were already admitted.
        revoked = self.store.revoke_tool(server.id, tool)
        active_calls = self.timeline.active_tool_calls(server_id=server.id, tool=tool)
        cancellations: list[dict[str, Any]] = []
        managed_stdio_calls = []
        for call in active_calls:
            call, changed = self.timeline.request_cancel(
                call.id,
                reason="permission revoked while tool call was active",
                source="permission-revocation",
            )
            managed = call.source == "managed-runtime" and server.transport == "stdio"
            if managed:
                managed_stdio_calls.append(call)
            cancellations.append(
                {
                    "tool_call_id": call.id,
                    "cancel_requested": changed or call.cancel_requested_at is not None,
                    "hard_termination_guaranteed": managed,
                    "runtime_stopped": False,
                    "state": call.status,
                    "reason": call.cancel_reason,
                }
            )

        runtime_stopped = False
        if managed_stdio_calls:
            stopped = self.runtime.stop(server, timeout=0.25)
            runtime_stopped = bool(stopped.get("changed", False))
            for item in cancellations:
                if item["hard_termination_guaranteed"]:
                    item["runtime_stopped"] = runtime_stopped

        for call in active_calls:
            self.timeline.append_event(
                call.session_id,
                "permission.revoked",
                {
                    "server_id": server.id,
                    "server": server.name,
                    "tool": tool,
                    "revoked": revoked,
                    "tool_call_id": call.id,
                    "hard_termination_guaranteed": (
                        call.source == "managed-runtime" and server.transport == "stdio"
                    ),
                    "runtime_stopped": runtime_stopped,
                },
                correlation_id=call.correlation_id,
                causation_id=call.id,
                source="permission-revocation",
            )
        self.store.record(
            "permission.revoked",
            server_id=server.id,
            server=server.name,
            tool=tool,
            revoked=revoked,
            active_call_ids=[call.id for call in active_calls],
            managed_runtime_stopped=runtime_stopped,
        )
        return {
            "server_id": server.id,
            "server": server.name,
            "tool": tool,
            "revoked": revoked,
            "active_calls_affected": len(cancellations),
            "active_call_cancellations": cancellations,
            "managed_runtime_stopped": runtime_stopped,
        }

    def bind_env_secret(self, server_reference: str, target: str, source: str) -> dict[str, Any]:
        server = self.store.get_server(server_reference)
        if server.transport != "stdio":
            raise ValueError("environment SecretBindings are supported only for stdio MCP servers")
        state = self.runtime.status(server)
        if state.get("state") in {"running", "degraded"}:
            self.runtime.stop(server)
        binding = self.store.bind_env_secret(server.id, target, source)
        # Re-evaluating prunes release authority if the binding fingerprint changed.
        self.secret_policy.list(server.id)
        return self._secret_binding_view(binding)

    def unbind_secret(self, server_reference: str, target: str) -> bool:
        server = self.store.get_server(server_reference)
        state = self.runtime.status(server)
        if server.transport == "stdio" and state.get("state") in {"running", "degraded"}:
            self.runtime.stop(server)
        self.secret_policy.revoke(server.id, target)
        return self.store.unbind_secret(server.id, target)

    def grant_secret_release(self, server_reference: str, target: str) -> dict[str, str]:
        return self.secret_policy.grant(server_reference, target)

    def revoke_secret_release(self, server_reference: str, target: str) -> bool:
        server = self.store.get_server(server_reference)
        changed = self.secret_policy.revoke(server.id, target)
        if changed and server.transport == "stdio":
            state = self.runtime.status(server)
            if state.get("state") in {"running", "degraded"}:
                self.runtime.stop(server)
        return changed

    def list_secret_bindings(self, server_reference: str | None = None) -> list[dict[str, Any]]:
        return [
            self._secret_binding_view(binding)
            for binding in self.store.list_secret_bindings(server_reference)
        ]

    def list_profiles(self) -> list[dict[str, Any]]:
        current = active_profile(self.store, self.runtime)
        active_id = current["profile_id"] if current else None
        result: list[dict[str, Any]] = []
        for profile in self.store.list_profiles():
            result.append(
                {
                    **profile.to_dict(),
                    "servers": self.store.profile_server_names(profile),
                    "active": profile.id == active_id,
                    "requirements": evaluate_profile_requirements(
                        self.store,
                        profile,
                        secret_policy=self.secret_policy,
                    ),
                }
            )
        return result

    def profile_view(self, reference: str) -> dict[str, Any]:
        profile = self.store.get_profile(reference)
        current = active_profile(self.store, self.runtime)
        return {
            **profile.to_dict(),
            "servers": self.store.profile_server_names(profile),
            "active": bool(current and current["profile_id"] == profile.id),
            "requirements": evaluate_profile_requirements(
                self.store,
                profile,
                secret_policy=self.secret_policy,
            ),
        }

    def create_profile(self, profile: MCPProfile) -> dict[str, Any]:
        saved = self.store.save_profile(profile)
        return self.profile_view(saved.id)

    def delete_profile(self, reference: str) -> dict[str, Any]:
        profile = self.store.get_profile(reference)
        current = active_profile(self.store, self.runtime)
        if current is not None and current["profile_id"] == profile.id:
            raise RuntimeError(
                f"profile {profile.name} is active; deactivate it before deletion"
            )
        self.store.delete_profile(profile.id)
        return {"deleted": profile.name, "profile_id": profile.id}

    def activate_profile(self, reference: str) -> dict[str, Any]:
        return activate_profile(self.store, self.runtime, reference)

    def active_profile(self) -> dict[str, Any] | None:
        return active_profile(self.store, self.runtime)

    def deactivate_profile(self, *, keep_running: bool = False) -> dict[str, Any]:
        return deactivate_profile(self.store, self.runtime, keep_running=keep_running)

    def start_session(
        self,
        *,
        profile_reference: str | None = None,
        label: str = "",
        make_active: bool = True,
    ) -> dict[str, Any]:
        profile_id: str | None = None
        if profile_reference is not None:
            profile_id = self.store.get_profile(profile_reference).id
        else:
            current = active_profile(self.store, self.runtime)
            if current is not None:
                profile_id = str(current["profile_id"])
        session = self.timeline.start_session(
            profile_id=profile_id,
            label=label,
            source="core-service",
            make_active=make_active,
        )
        self.store.record("session.started", session_id=session.id, profile_id=profile_id)
        return session.to_dict()

    def end_session(self, session_id: str, *, state: str = "completed") -> dict[str, Any]:
        ended = self.timeline.end_session(session_id, state=state)
        self.store.record("session.ended", session_id=ended.id, state=ended.state)
        return ended.to_dict()

    def list_sessions(self) -> list[dict[str, Any]]:
        return [session.to_dict() for session in self.timeline.list_sessions()]

    def session_view(self, session_id: str) -> dict[str, Any]:
        session = self.timeline.get_session(session_id)
        return {
            **session.to_dict(),
            "events": self.timeline.events(session_id, limit=100000),
            "tool_calls": self.timeline.tool_calls(session_id, limit=100000),
        }

    def session_events(self, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return self.timeline.events(session_id, limit=limit)

    def session_tool_calls(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.timeline.tool_calls(session_id, limit=limit)

    def tool_call_view(self, tool_call_id: str) -> dict[str, Any]:
        return self.timeline.get_tool_call(tool_call_id).to_dict()

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.history(limit)

    def rebinding_requirements(self) -> list[dict[str, Any]]:
        return list_rebinding_requirements(self.store)

    def export_bundle(self) -> dict[str, Any]:
        return build_bundle(self.store)

    def export_to_path(
        self,
        path: Path | str,
        *,
        passphrase: str | None = None,
    ) -> dict[str, Any]:
        bundle = build_bundle(self.store)
        value: dict[str, Any] = bundle
        encrypted = passphrase is not None
        if passphrase is not None:
            value = encrypt_bundle(bundle, passphrase)
        target = Path(path).expanduser()
        self._atomic_json(target, value)
        manifest = bundle["manifest"]
        self.store.record(
            "bundle.portable_exported",
            path=str(target),
            bundle_version=bundle["version"],
            content_sha256=manifest["content_sha256"],
            encrypted=encrypted,
        )
        return {
            "exported": str(target),
            "bundle_version": bundle["version"],
            "content_sha256": manifest["content_sha256"],
            "encrypted": encrypted,
            **manifest["counts"],
        }

    def import_bundle(
        self,
        bundle: dict[str, Any],
        *,
        replace: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return import_bundle(self.store, bundle, replace=replace, dry_run=dry_run)

    def import_from_path(
        self,
        path: Path | str,
        *,
        passphrase: str | None = None,
        replace: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        source = Path(path).expanduser()
        # Snapshot a bounded regular file before parsing. The staged copy closes
        # the stat/check/use gap and gives desktop/CLI the same import boundary.
        with staged_bounded_import(source) as staged:
            raw = json.loads(staged.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("bundle must be a JSON object")
        encrypted = is_encrypted_bundle(raw)
        if encrypted:
            if passphrase is None:
                raise ValueError("encrypted bundle requires a passphrase")
            raw = decrypt_bundle(raw, passphrase)
        elif passphrase is not None:
            raise ValueError("a passphrase was supplied but the bundle is not encrypted")
        result = import_bundle(
            self.store,
            raw,
            replace=replace,
            dry_run=dry_run,
        )
        return {"path": str(source), "encrypted": encrypted, **result}

    def invoke_tool(
        self,
        server_reference: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        timeout: float = 30.0,
        session_id: str | None = None,
        replay_of: str | None = None,
    ) -> dict[str, Any]:
        server = self.store.get_server(server_reference)
        if not server.enabled:
            raise RuntimeError(f"{server.name} is disabled")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be a JSON object")

        # Probe immediately before dispatch so an upgrade invalidates stale grants.
        probe = self.probe_server(server.id, timeout=min(timeout, 10.0))
        if not self.store.is_tool_allowed(server.id, tool):
            raise PermissionError(
                f"tool denied by local policy: {server.name}/{tool}; explicit owner grant required"
            )

        prepared = self.secret_policy.prepare_server(server)
        if session_id is not None:
            session = self.timeline.get_session(session_id)
            implicit = False
        else:
            session = self.timeline.active()
            if session is None:
                session = self.timeline.start_session(
                    label=f"call:{server.name}/{tool}",
                    source="core-service-implicit",
                    make_active=False,
                )
                implicit = True
            else:
                implicit = False

        correlation_id = f"corr_{uuid.uuid4().hex}"
        managed = server.transport == "stdio" and self.runtime.status(server).get("state") == "running"
        source = (
            "managed-runtime"
            if managed
            else "ephemeral-session"
            if server.transport == "stdio"
            else f"remote-{server.transport}"
        )
        call = self.timeline.begin_tool_call(
            session.id,
            server_id=server.id,
            server=server.name,
            tool=tool,
            arguments=arguments,
            source=source,
            correlation_id=correlation_id,
            replay_of=replay_of,
        )
        try:
            # Revocation can race with probing/session setup. Re-check after the
            # call is durable and visible so a concurrent revoke can either stop
            # this dispatch or mark/terminate the now-visible active call.
            if not self.store.is_tool_allowed(server.id, tool):
                self.timeline.request_cancel(
                    call.id,
                    reason="permission revoked before tool dispatch",
                    source="permission-revocation",
                )
                raise PermissionError(
                    f"tool denied by local policy: {server.name}/{tool}; permission was revoked before dispatch"
                )
            result = (
                self.runtime.call_tool(server, tool, arguments, timeout=timeout)
                if managed
                else protocol_call_tool(prepared, tool, arguments, timeout=timeout)
            )
            completed = self.timeline.complete_tool_call(call, result=result)
            self.store.record(
                "tool.called",
                session_id=session.id,
                tool_call_id=completed.id,
                correlation_id=correlation_id,
                replay_of=replay_of,
                server_id=server.id,
                server=server.name,
                tool=tool,
                is_error=bool(result.get("is_error", False)),
                source=source,
                duration_ms=completed.duration_ms,
            )
            if implicit:
                self.timeline.end_session(
                    session.id,
                    state="failed" if bool(result.get("is_error", False)) else "completed",
                )
            return {
                **result,
                "session_id": session.id,
                "tool_call_id": completed.id,
                "correlation_id": correlation_id,
                "replay_of": replay_of,
                "source": source,
                "capability_snapshot": probe["snapshot"],
            }
        except Exception as exc:
            self.timeline.complete_tool_call(call, error=exc)
            if implicit:
                current = self.timeline.get_session(session.id)
                if current.state == "active":
                    self.timeline.end_session(session.id, state="failed")
            raise

    def replay_tool_call(
        self,
        tool_call_id: str,
        *,
        arguments: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        original = self.timeline.get_tool_call(tool_call_id)
        if original.status == "running":
            raise RuntimeError(f"tool call {original.id} is still running and cannot be replayed")
        if arguments is None:
            if contains_redacted(original.arguments):
                raise RuntimeError(
                    "the original tool arguments contain redacted values; replacement arguments are required"
                )
            replay_arguments = dict(original.arguments)
        else:
            if not isinstance(arguments, dict):
                raise ValueError("replacement replay arguments must be a JSON object")
            replay_arguments = arguments

        result = self.invoke_tool(
            original.server_id,
            original.tool,
            replay_arguments,
            timeout=timeout,
            replay_of=original.id,
        )
        current = self.timeline.get_tool_call(str(result["tool_call_id"]))
        before = original.result if original.result is not None else {"error": original.error}
        after = current.result if current.result is not None else {"error": current.error}
        output_diff = diff_values(before, after)
        self.timeline.append_event(
            current.session_id,
            "tool.replayed",
            {
                "original_tool_call_id": original.id,
                "new_tool_call_id": current.id,
                "output_diff": output_diff,
                "changed": bool(output_diff),
            },
            correlation_id=current.correlation_id,
            causation_id=original.id,
            source="core-service",
        )
        self.store.record(
            "tool.replayed",
            original_tool_call_id=original.id,
            new_tool_call_id=current.id,
            session_id=current.session_id,
            changed=bool(output_diff),
        )
        return {
            "replay_of": original.id,
            "session_id": current.session_id,
            "tool_call_id": current.id,
            "changed": bool(output_diff),
            "output_diff": output_diff,
            "result": result,
        }


__all__ = ["MegasCoreService"]
