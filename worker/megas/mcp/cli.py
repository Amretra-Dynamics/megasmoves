from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path
from typing import Any

from .bundle import (
    build_bundle,
    import_bundle as import_portable_bundle,
    list_rebinding_requirements,
)
from .bundle_crypto import decrypt_bundle, encrypt_bundle, is_encrypted_bundle
from .capabilities import CapabilityStore
from .discovery import DiscoveryEngine
from .model import MCPProfile, MCPServer
from .profile_requirements import (
    assert_profile_requirements,
    evaluate_profile_requirements,
    parse_requirement_pairs,
)
from .protocol import call_tool, probe_server
from .runtime import MCPRuntime
from .secret_release import SecretReleasePolicy
from .state_transaction import recover_pending_transaction
from .store import MCPStore
from .timeline import TimelineStore, ToolCall, contains_redacted, diff_values
from .workspace import activate_profile, active_profile, deactivate_profile


def _package_version() -> str:
    try:
        return distribution_version("megas")
    except PackageNotFoundError:
        return "0+unknown"


def _pairs(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"expected KEY=VALUE, got: {value}")
        key, val = value.split("=", 1)
        if not key.strip():
            raise ValueError("key must not be empty")
        out[key.strip()] = val
    return out


def _json_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed


def _emit(value: Any, machine: bool) -> None:
    if machine:
        print(json.dumps(value, indent=2, sort_keys=True, default=str))
    elif isinstance(value, list):
        if not value:
            print("(none)")
        for item in value:
            print(json.dumps(item, sort_keys=True) if isinstance(item, (dict, list)) else item)
    elif isinstance(value, dict):
        for key, val in value.items():
            print(f"{key}: {json.dumps(val, sort_keys=True) if isinstance(val, (dict, list)) else val}")
    else:
        print(value)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
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


def _required_env(name: str | None, *, purpose: str) -> str:
    if not name:
        raise ValueError(f"{purpose} requires an environment-variable name")
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{purpose} environment variable is not set: {name}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="megas",
        description="Offline-full local MCP control, inspection, and tool execution.",
    )
    parser.add_argument("--json", action="store_true")
    top = parser.add_subparsers(dest="product_command")
    top.add_parser("status")
    top.add_parser("version")

    mcp = top.add_parser("mcp")
    actions = mcp.add_subparsers(dest="mcp_command", required=True)
    actions.add_parser("list")
    actions.add_parser(
        "rebind-required",
        help="list imported SecretBinding requirements still unresolved on this machine",
    )

    add = actions.add_parser("add")
    add.add_argument("name")
    target = add.add_mutually_exclusive_group(required=True)
    target.add_argument("--command", dest="server_command")
    target.add_argument("--url")
    add.add_argument("--transport", choices=("stdio", "http", "sse", "streamable-http"))
    add.add_argument("--arg", action="append", default=[])
    add.add_argument("--cwd")
    add.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="persist non-secret configuration only",
    )
    add.add_argument(
        "--env-ref",
        action="append",
        default=[],
        metavar="TARGET=SOURCE",
        help="bind secret/config target to a machine-local environment variable",
    )
    add.add_argument("--description", default="")
    add.add_argument("--disabled", action="store_true")

    discover = actions.add_parser("discover")
    discover.add_argument("--path", action="append", default=[])
    discover.add_argument("--no-common", action="store_true")
    discover.add_argument("--accept", metavar="FINGERPRINT")
    discover.add_argument("--yes", action="store_true", help="confirm the selected import")

    for name in (
        "show",
        "enable",
        "disable",
        "start",
        "stop",
        "restart",
        "doctor",
        "probe",
        "logs",
    ):
        command = actions.add_parser(name)
        command.add_argument("name")
        if name == "probe":
            command.add_argument("--timeout", type=float, default=5.0)
        if name == "logs":
            command.add_argument("--lines", type=int, default=100)

    remove = actions.add_parser("remove")
    remove.add_argument("name")
    remove.add_argument("--yes", action="store_true", help="confirm permanent removal")

    capabilities = actions.add_parser("capabilities")
    capabilities.add_argument("name")
    capabilities.add_argument("--history", action="store_true")

    secret_bindings = actions.add_parser("secret-bindings")
    secret_bindings.add_argument("name", nargs="?")

    bind_env = actions.add_parser("bind-env")
    bind_env.add_argument("name")
    bind_env.add_argument("target")
    bind_env.add_argument("source")

    unbind_secret = actions.add_parser("unbind-secret")
    unbind_secret.add_argument("name")
    unbind_secret.add_argument("target")

    allow_secret = actions.add_parser("allow-secret")
    allow_secret.add_argument("name")
    allow_secret.add_argument("target")

    deny_secret = actions.add_parser("deny-secret")
    deny_secret.add_argument("name")
    deny_secret.add_argument("target")

    permissions = actions.add_parser("permissions")
    permissions.add_argument("name", nargs="?")

    allow = actions.add_parser("allow")
    allow.add_argument("name")
    allow.add_argument("tool")

    deny = actions.add_parser("deny")
    deny.add_argument("name")
    deny.add_argument("tool")

    call = actions.add_parser("call")
    call.add_argument("name")
    call.add_argument("tool")
    call.add_argument("--arguments", default="{}", metavar="JSON")
    call.add_argument("--timeout", type=float, default=30.0)
    call.add_argument("--replay-of", help=argparse.SUPPRESS)

    profiles = top.add_parser("profile")
    profile_actions = profiles.add_subparsers(dest="profile_command", required=True)
    profile_actions.add_parser("list")
    create = profile_actions.add_parser("create")
    create.add_argument("name")
    create.add_argument("servers", nargs="+")
    create.add_argument("--description", default="")
    create.add_argument(
        "--health-policy",
        choices=("required", "best-effort"),
        default="required",
        help="required blocks activation on failed health checks; best-effort reports degradation",
    )
    create.add_argument(
        "--startup-timeout",
        type=float,
        default=8.0,
        metavar="SECONDS",
        help="per-server startup/activation timeout, greater than 0 and at most 300 seconds",
    )
    create.add_argument(
        "--project",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="repeatable deterministic project metadata stored with the profile",
    )
    create.add_argument(
        "--require-tool",
        action="append",
        default=[],
        metavar="SERVER=TOOL",
        help=(
            "repeatable required tool permission; this records a requirement only and never grants it"
        ),
    )
    create.add_argument(
        "--require-secret",
        action="append",
        default=[],
        metavar="SERVER=TARGET",
        help=(
            "repeatable required machine SecretBinding; this records a requirement only and never binds/releases it"
        ),
    )
    for name in ("show", "start", "stop", "requirements"):
        command = profile_actions.add_parser(name)
        command.add_argument("name")
    activate = profile_actions.add_parser(
        "activate",
        help="preflight and make a profile the active workspace context",
    )
    activate.add_argument("name")
    profile_actions.add_parser(
        "active",
        help="show the active workspace context",
    )
    deactivate = profile_actions.add_parser(
        "deactivate",
        help="clear the active workspace and stop local runtimes by default",
    )
    deactivate.add_argument(
        "--keep-running",
        action="store_true",
        help="clear workspace context without stopping its local stdio servers",
    )
    delete = profile_actions.add_parser("delete")
    delete.add_argument("name")
    delete.add_argument("--yes", action="store_true", help="confirm permanent deletion")

    sessions = top.add_parser("session")
    session_actions = sessions.add_subparsers(dest="session_command", required=True)
    session_actions.add_parser("list")
    session_actions.add_parser("recover")
    session_start = session_actions.add_parser("start")
    session_start.add_argument("--profile")
    session_start.add_argument("--label", default="")
    for name in ("show", "end", "events", "calls"):
        command = session_actions.add_parser(name)
        command.add_argument("session_id", nargs="?")
        if name in {"events", "calls"}:
            command.add_argument("--limit", type=int, default=200 if name == "events" else 100)
    replay = session_actions.add_parser("replay")
    replay.add_argument("tool_call_id")
    replay.add_argument("--arguments", metavar="JSON")
    replay.add_argument("--timeout", type=float, default=30.0)
    cancel = session_actions.add_parser("cancel")
    cancel.add_argument("tool_call_id")
    cancel.add_argument("--reason", default="user requested cancellation")

    history = top.add_parser("history")
    history.add_argument("--limit", type=int, default=100)

    export = top.add_parser(
        "export",
        help="write a checksum-backed portable bundle",
    )
    export.add_argument("path")
    export.add_argument(
        "--encrypt-passphrase-env",
        metavar="ENV_NAME",
        help=(
            "environment variable containing the bundle passphrase; the value is never written to the bundle or command line"
        ),
    )

    imp = top.add_parser(
        "import",
        help="validate/import a plain or authenticated-encrypted portable bundle",
    )
    imp.add_argument("path")
    imp.add_argument("--replace", action="store_true")
    imp.add_argument("--dry-run", action="store_true")
    imp.add_argument(
        "--passphrase-env",
        metavar="ENV_NAME",
        help="environment variable containing the passphrase for an encrypted bundle",
    )
    return parser


def _probe_and_capture(
    server: MCPServer,
    store: MCPStore,
    runtime: MCPRuntime,
    *,
    timeout: float,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    prepared = SecretReleasePolicy(store).prepare_server(server)
    if server.transport == "stdio" and runtime.status(server)["state"] == "running":
        result = runtime.probe(server, timeout=timeout)
        source = "managed-runtime"
    else:
        result = probe_server(prepared, timeout=timeout)
        source = "ephemeral-session" if server.transport == "stdio" else f"remote-{server.transport}"
    snapshot, diff, reused = CapabilityStore(store).capture(server, result)
    evidence = {
        "id": snapshot.id,
        "fingerprint": snapshot.fingerprint,
        "observed_at": snapshot.observed_at,
        "reused": reused,
        "diff": diff,
    }
    store.record(
        "server.probed",
        server_id=server.id,
        server=server.name,
        source=source,
        snapshot_id=snapshot.id,
    )
    return result, source, evidence


def _profile_view(store: MCPStore, profile: MCPProfile) -> dict[str, Any]:
    return {**profile.to_dict(), "servers": store.profile_server_names(profile)}


def _secret_binding_view(
    store: MCPStore,
    binding: Any,
    policy: SecretReleasePolicy | None = None,
) -> dict[str, Any]:
    policy = policy or SecretReleasePolicy(store)
    server = store.get_server_by_id(binding.server_id)
    return {
        **policy.binding_view(binding),
        "server": server.name,
        "available": binding.provider == "env" and binding.reference in os.environ,
    }


def _session_or_active(timeline: TimelineStore, session_id: str | None) -> Any:
    if session_id:
        return timeline.get_session(session_id)
    active = timeline.active()
    if active is None:
        raise RuntimeError("no active session; provide a session ID or start a session")
    return active


def _execution_session(
    timeline: TimelineStore,
    *,
    label: str,
) -> tuple[Any, bool]:
    active = timeline.active()
    if active is not None:
        return active, False
    return timeline.start_session(label=label, source="cli-implicit", make_active=False), True


def _cancel_tool_call(
    timeline: TimelineStore,
    store: MCPStore,
    runtime: MCPRuntime,
    call: ToolCall,
    *,
    reason: str,
    source: str,
) -> dict[str, Any]:
    call, changed = timeline.request_cancel(call.id, reason=reason, source=source)
    server = store.get_server_by_id(call.server_id)
    hard_termination_guaranteed = False
    runtime_stopped = False
    if call.status == "running" and call.source == "managed-runtime" and server.transport == "stdio":
        stopped = runtime.stop(server, timeout=0.25)
        runtime_stopped = bool(stopped.get("changed", False))
        hard_termination_guaranteed = True
    return {
        "tool_call_id": call.id,
        "server_id": call.server_id,
        "server": server.name,
        "tool": call.tool,
        "source": call.source,
        "cancel_requested": changed or call.cancel_requested_at is not None,
        "hard_termination_guaranteed": hard_termination_guaranteed,
        "runtime_stopped": runtime_stopped,
        "state": call.status,
        "reason": call.cancel_reason or reason,
    }


def _stop_running_stdio(runtime: MCPRuntime, server: MCPServer) -> bool:
    if server.transport != "stdio":
        return False
    state = runtime.status(server)
    if state.get("state") not in {"running", "degraded"}:
        return False
    return bool(runtime.stop(server).get("changed", False))


def _run(args: argparse.Namespace, store: MCPStore, runtime: MCPRuntime) -> Any:
    timeline = TimelineStore(store.mcp_dir)
    secret_policy = SecretReleasePolicy(store)
    recovered_calls = timeline.recover_incomplete()
    if recovered_calls:
        store.record(
            "timeline.recovered",
            tool_call_ids=[call["id"] for call in recovered_calls],
            count=len(recovered_calls),
        )

    if args.product_command in {None, "status"}:
        servers = store.list_servers()
        runtime_states = [runtime.status(server) for server in servers]
        active_session = timeline.active()
        workspace = active_profile(store, runtime)
        return {
            "product": "Megas",
            "package_version": _package_version(),
            "mode": "offline-full-mcp",
            "home": str(store.home),
            "network_required": False,
            "sign_in_required": False,
            "configured_servers": len(servers),
            "enabled_servers": sum(1 for server in servers if server.enabled),
            "running_servers": sum(1 for state in runtime_states if state["state"] == "running"),
            "tool_permissions": len(store.list_permissions()),
            "secret_bindings": len(store.list_secret_bindings()),
            "secret_release_permissions": len(secret_policy.list()),
            "active_profile": workspace["profile_id"] if workspace else None,
            "active_session": active_session.id if active_session else None,
            "recovered_incomplete_calls": len(recovered_calls),
            "remote_mcp_optional": True,
            "cloud": "optional-not-required",
        }

    if args.product_command == "version":
        return {
            "product": "Megas",
            "package_version": _package_version(),
            "mode": "offline-full-mcp",
        }

    if args.product_command == "session":
        action = args.session_command
        if action == "list":
            return [session.to_dict() for session in timeline.list_sessions()]
        if action == "recover":
            return {
                "recovered": recovered_calls,
                "count": len(recovered_calls),
            }
        if action == "start":
            selected_profile: MCPProfile | None = None
            if args.profile:
                selected_profile = store.get_profile(args.profile)
            else:
                workspace = active_profile(store, runtime)
                if workspace is not None:
                    selected_profile = store.get_profile(str(workspace["profile_id"]))
            profile_id: str | None = None
            if selected_profile is not None:
                assert_profile_requirements(
                    store,
                    selected_profile,
                    secret_policy=secret_policy,
                )
                profile_id = selected_profile.id
            session = timeline.start_session(
                profile_id=profile_id,
                label=args.label,
                source="cli",
                make_active=True,
            )
            store.record(
                "session.started",
                session_id=session.id,
                profile_id=profile_id,
            )
            return session.to_dict()
        if action == "replay":
            original = timeline.get_tool_call(args.tool_call_id)
            if original.status == "running":
                raise RuntimeError(
                    f"tool call {original.id} is still running and cannot be replayed"
                )
            if args.arguments is None:
                if contains_redacted(original.arguments):
                    raise RuntimeError(
                        "the original tool arguments contain redacted values; provide replacement JSON with --arguments"
                    )
                replay_arguments = original.arguments
            else:
                replay_arguments = _json_object(args.arguments)

            server = store.get_server_by_id(original.server_id)
            replay_args = _parser().parse_args(
                [
                    "mcp",
                    "call",
                    server.id,
                    original.tool,
                    "--arguments",
                    json.dumps(replay_arguments, separators=(",", ":"), sort_keys=True),
                    "--timeout",
                    str(args.timeout),
                    "--replay-of",
                    original.id,
                ]
            )
            replay_result = _run(replay_args, store, runtime)
            new_call = timeline.get_tool_call(str(replay_result["tool_call_id"]))
            before = original.result if original.result is not None else {"error": original.error}
            after = new_call.result if new_call.result is not None else {"error": new_call.error}
            output_diff = diff_values(before, after)
            timeline.append_event(
                new_call.session_id,
                "tool.replayed",
                {
                    "original_tool_call_id": original.id,
                    "new_tool_call_id": new_call.id,
                    "output_diff": output_diff,
                    "changed": bool(output_diff),
                },
                correlation_id=new_call.correlation_id,
                causation_id=original.id,
                source="cli",
            )
            store.record(
                "tool.replayed",
                original_tool_call_id=original.id,
                new_tool_call_id=new_call.id,
                session_id=new_call.session_id,
                changed=bool(output_diff),
            )
            return {
                "replay_of": original.id,
                "session_id": new_call.session_id,
                "tool_call_id": new_call.id,
                "changed": bool(output_diff),
                "output_diff": output_diff,
                "result": replay_result,
            }
        if action == "cancel":
            call = timeline.get_tool_call(args.tool_call_id)
            result = _cancel_tool_call(
                timeline,
                store,
                runtime,
                call,
                reason=args.reason,
                source="cli",
            )
            store.record(
                "tool.cancel.requested",
                tool_call_id=call.id,
                server_id=call.server_id,
                tool=call.tool,
                hard_termination_guaranteed=result["hard_termination_guaranteed"],
            )
            return result

        session = _session_or_active(timeline, args.session_id)
        if action == "show":
            return {
                **session.to_dict(),
                "events": len(timeline.events(session.id, limit=100000)),
                "tool_calls": len(timeline.tool_calls(session.id, limit=100000)),
            }
        if action == "end":
            ended = timeline.end_session(session.id)
            store.record("session.ended", session_id=ended.id, state=ended.state)
            return ended.to_dict()
        if action == "events":
            return timeline.events(session.id, limit=args.limit)
        if action == "calls":
            return timeline.tool_calls(session.id, limit=args.limit)

    if args.product_command == "mcp":
        action = args.mcp_command
        capability_store = CapabilityStore(store)
        if action == "list":
            return [
                {
                    "id": server.id,
                    "name": server.name,
                    "transport": server.transport,
                    "enabled": server.enabled,
                    "state": runtime.status(server)["state"],
                    "target": server.command if server.transport == "stdio" else server.url,
                }
                for server in store.list_servers()
            ]
        if action == "rebind-required":
            return list_rebinding_requirements(store)
        if action == "add":
            inferred_transport = "stdio" if args.server_command else "http"
            transport = args.transport or inferred_transport
            if args.server_command and transport != "stdio":
                raise ValueError("--command requires --transport stdio")
            if args.url and transport == "stdio":
                raise ValueError("--url requires a remote transport")
            try:
                existing = store.get_server(args.name)
            except KeyError:
                existing = None
            if existing is not None:
                _stop_running_stdio(runtime, existing)
            server = MCPServer(
                name=args.name,
                transport=transport,
                command=args.server_command,
                args=list(args.arg),
                url=args.url,
                cwd=args.cwd,
                env=_pairs(args.env),
                env_refs=_pairs(args.env_ref),
                enabled=not args.disabled,
                description=args.description,
            )
            store.save_server(server)
            return {"saved": server.to_dict()}
        if action == "discover":
            engine = DiscoveryEngine(store)
            result = engine.scan(args.path, include_common=not args.no_common)
            preview = engine.preview(result)
            response: dict[str, Any] = {
                "scanned_files": result.scanned_files,
                "candidates": preview,
                "errors": [error.to_dict() for error in result.errors],
                "imported": None,
            }
            if args.accept:
                if not args.yes:
                    raise ValueError(
                        "discovery import is explicit; review the preview then repeat with --accept FINGERPRINT --yes"
                    )
                selected = next(
                    (row for row in preview if row.get("fingerprint") == args.accept),
                    None,
                )
                if selected and selected.get("action") == "update":
                    discovered_server = selected.get("server") or {}
                    existing_name = str(discovered_server.get("name") or "")
                    if existing_name:
                        try:
                            _stop_running_stdio(runtime, store.get_server(existing_name))
                        except KeyError:
                            pass
                saved = engine.accept(result, args.accept)
                response["imported"] = {
                    "id": saved.id,
                    "name": saved.name,
                    "source": saved.source,
                }
            return response
        if action == "secret-bindings":
            return [
                _secret_binding_view(store, binding, secret_policy)
                for binding in store.list_secret_bindings(args.name)
            ]
        if action == "bind-env":
            server = store.get_server(args.name)
            if server.transport != "stdio":
                raise ValueError(
                    "environment SecretBindings are currently supported only for stdio MCP servers"
                )
            _stop_running_stdio(runtime, server)
            binding = store.bind_env_secret(server.id, args.target, args.source)
            # A changed binding fingerprint automatically invalidates an old grant.
            secret_policy.list(server.id)
            return {"saved": _secret_binding_view(store, binding, secret_policy)}
        if action == "unbind-secret":
            server = store.get_server(args.name)
            _stop_running_stdio(runtime, server)
            release_revoked = secret_policy.revoke(server.id, args.target)
            return {
                "server_id": server.id,
                "server": server.name,
                "target": args.target,
                "release_revoked": release_revoked,
                "removed": store.unbind_secret(server.id, args.target),
            }
        if action == "allow-secret":
            server = store.get_server(args.name)
            grant = secret_policy.grant(server.id, args.target)
            active = timeline.active()
            if active is not None:
                timeline.append_event(
                    active.id,
                    "secret.release.granted",
                    {
                        "permission_id": grant["id"],
                        "server_id": server.id,
                        "server": server.name,
                        "target": args.target,
                    },
                    source="cli",
                )
            return {"granted": grant, "server": server.name}
        if action == "deny-secret":
            server = store.get_server(args.name)
            revoked = secret_policy.revoke(server.id, args.target)
            runtime_stopped = _stop_running_stdio(runtime, server) if revoked else False
            active = timeline.active()
            if active is not None:
                timeline.append_event(
                    active.id,
                    "secret.release.revoked",
                    {
                        "server_id": server.id,
                        "server": server.name,
                        "target": args.target,
                        "revoked": revoked,
                        "runtime_stopped": runtime_stopped,
                    },
                    source="cli",
                )
            return {
                "server_id": server.id,
                "server": server.name,
                "target": args.target,
                "revoked": revoked,
                "runtime_stopped": runtime_stopped,
            }
        if action == "permissions":
            grants = store.list_permissions()
            if args.name:
                server = store.get_server(args.name)
                grants = [row for row in grants if row["server_id"] == server.id]
            return grants
        if action == "allow":
            server = store.get_server(args.name)
            snapshot = capability_store.latest(server.id)
            if snapshot is None:
                raise RuntimeError(
                    f"no inspected capability snapshot for {server.name}; run 'megas mcp probe {server.name}' first"
                )
            known_tools = {str(item.get("name")) for item in snapshot.tools if item.get("name")}
            if args.tool not in known_tools:
                raise ValueError(
                    f"tool {args.tool} is not present in the latest inspected capabilities for {server.name}"
                )
            grant = store.grant_tool(server.id, args.tool)
            active = timeline.active()
            if active is not None:
                timeline.append_event(
                    active.id,
                    "permission.granted",
                    {
                        "permission_id": grant["id"],
                        "server_id": server.id,
                        "server": server.name,
                        "tool": args.tool,
                    },
                    source="cli",
                )
            return {"granted": grant, "snapshot_id": snapshot.id}
        if action == "deny":
            server = store.get_server(args.name)
            revoked = store.revoke_tool(server.id, args.tool)
            active_calls = timeline.active_tool_calls(server_id=server.id, tool=args.tool)
            cancellations = [
                _cancel_tool_call(
                    timeline,
                    store,
                    runtime,
                    call,
                    reason="permission revoked while tool call was active",
                    source="permission-revocation",
                )
                for call in active_calls
            ]
            active = timeline.active()
            if active is not None:
                timeline.append_event(
                    active.id,
                    "permission.revoked",
                    {
                        "server_id": server.id,
                        "server": server.name,
                        "tool": args.tool,
                        "revoked": revoked,
                        "active_calls": [item["tool_call_id"] for item in cancellations],
                    },
                    source="cli",
                )
            return {
                "server_id": server.id,
                "server": server.name,
                "tool": args.tool,
                "revoked": revoked,
                "active_call_cancellations": cancellations,
            }

        server = store.get_server(args.name)
        if action == "show":
            latest = capability_store.latest(server.id)
            return {
                **server.to_dict(),
                "runtime": runtime.status(server),
                "secret_bindings": [
                    _secret_binding_view(store, binding, secret_policy)
                    for binding in store.list_secret_bindings(server.id)
                ],
                "latest_capability_snapshot": capability_store.summary(latest) if latest else None,
            }
        if action == "remove":
            if not args.yes:
                raise ValueError(
                    f"removing MCP server {server.name} deletes its registry record, local grants, and secret bindings; repeat with --yes"
                )
            if server.transport == "stdio":
                runtime.stop(server)
            for binding in store.list_secret_bindings(server.id):
                secret_policy.revoke(server.id, binding.target)
            store.delete_server(server.id)
            return {"removed": server.name, "server_id": server.id}
        if action in {"enable", "disable"}:
            if action == "disable" and server.transport == "stdio":
                runtime.stop(server)
            server.enabled = action == "enable"
            store.save_server(server)
            return {"id": server.id, "name": server.name, "enabled": server.enabled}
        if action == "start":
            secret_policy.prepare_server(server)
            return runtime.start(server)
        if action == "stop":
            return runtime.stop(server)
        if action == "restart":
            secret_policy.prepare_server(server)
            return runtime.restart(server)
        if action == "doctor":
            result = runtime.doctor(server)
            bindings = store.list_secret_bindings(server.id)
            denied = [binding.target for binding in bindings if not secret_policy.is_allowed(binding)]
            result["secret_bindings"] = [
                _secret_binding_view(store, binding, secret_policy) for binding in bindings
            ]
            if denied:
                result["healthy"] = False
                result.setdefault("problems", []).append(
                    "secret release not granted: " + ", ".join(sorted(denied))
                )
            return result
        if action == "logs":
            return runtime.tail_logs(server, args.lines)
        if action == "capabilities":
            snapshots = capability_store.list(server.id)
            if args.history:
                return [capability_store.summary(snapshot) for snapshot in snapshots]
            latest = snapshots[0] if snapshots else None
            if latest is None:
                return {
                    "server_id": server.id,
                    "server": server.name,
                    "snapshot": None,
                    "next_action": f"megas mcp probe {server.name}",
                }
            return {**capability_store.summary(latest), "snapshot": latest.to_dict()}
        if action == "probe":
            if not server.enabled:
                raise RuntimeError(f"{server.name} is disabled")
            session, implicit = _execution_session(
                timeline,
                label=f"probe:{server.name}",
            )
            correlation_id = f"corr_{uuid.uuid4().hex}"
            timeline.append_event(
                session.id,
                "protocol.probe.requested",
                {
                    "server_id": server.id,
                    "server": server.name,
                    "transport": server.transport,
                },
                correlation_id=correlation_id,
                source="cli",
            )
            try:
                result, source, evidence = _probe_and_capture(
                    server,
                    store,
                    runtime,
                    timeout=args.timeout,
                )
                timeline.append_event(
                    session.id,
                    "protocol.probe.response",
                    {
                        "server_id": server.id,
                        "server": server.name,
                        "source": source,
                        "snapshot_id": evidence["id"],
                        "fingerprint": evidence["fingerprint"],
                        "protocol_version": result.get("protocol_version"),
                    },
                    correlation_id=correlation_id,
                    source=source,
                )
                if implicit:
                    timeline.end_session(session.id, state="completed")
                return {
                    "id": server.id,
                    "name": server.name,
                    "session_id": session.id,
                    "source": source,
                    "snapshot": evidence,
                    **result,
                }
            except Exception as exc:
                timeline.append_event(
                    session.id,
                    "protocol.probe.failed",
                    {
                        "server_id": server.id,
                        "server": server.name,
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    },
                    correlation_id=correlation_id,
                    source="cli",
                )
                if implicit:
                    timeline.end_session(session.id, state="failed")
                raise
        if action == "call":
            if not server.enabled:
                raise RuntimeError(f"{server.name} is disabled")

            session, implicit = _execution_session(
                timeline,
                label=f"call:{server.name}/{args.tool}",
            )
            correlation_id = f"corr_{uuid.uuid4().hex}"
            try:
                _result, probe_source, evidence = _probe_and_capture(
                    server,
                    store,
                    runtime,
                    timeout=min(args.timeout, 10.0),
                )
                timeline.append_event(
                    session.id,
                    "capability.checked",
                    {
                        "server_id": server.id,
                        "server": server.name,
                        "tool": args.tool,
                        "snapshot_id": evidence["id"],
                        "fingerprint": evidence["fingerprint"],
                        "source": probe_source,
                    },
                    correlation_id=correlation_id,
                    source=probe_source,
                )
                allowed = store.is_tool_allowed(server.id, args.tool)
                timeline.append_event(
                    session.id,
                    "permission.decision",
                    {
                        "server_id": server.id,
                        "server": server.name,
                        "tool": args.tool,
                        "allowed": allowed,
                    },
                    correlation_id=correlation_id,
                    source="local-policy",
                )
                if not allowed:
                    raise RuntimeError(
                        f"tool denied by local policy: {server.name}/{args.tool}; "
                        f"inspect and run 'megas mcp allow {server.name} {args.tool}' first"
                    )

                arguments = _json_object(args.arguments)
                prepared = secret_policy.prepare_server(server)
                managed = server.transport == "stdio" and runtime.status(server)["state"] == "running"
                source = (
                    "managed-runtime"
                    if managed
                    else (
                        "ephemeral-session"
                        if server.transport == "stdio"
                        else f"remote-{server.transport}"
                    )
                )
                tool_call = timeline.begin_tool_call(
                    session.id,
                    server_id=server.id,
                    server=server.name,
                    tool=args.tool,
                    arguments=arguments,
                    source=source,
                    correlation_id=correlation_id,
                    replay_of=getattr(args, "replay_of", None),
                )
                try:
                    if managed:
                        result = runtime.call_tool(
                            server,
                            args.tool,
                            arguments,
                            timeout=args.timeout,
                        )
                    else:
                        result = call_tool(
                            prepared,
                            args.tool,
                            arguments,
                            timeout=args.timeout,
                        )
                except Exception as exc:
                    completed_error = timeline.complete_tool_call(tool_call, error=exc)
                    if implicit and completed_error.status == "cancelled":
                        timeline.end_session(session.id, state="cancelled")
                    raise

                completed_call = timeline.complete_tool_call(tool_call, result=result)
                store.record(
                    "tool.called",
                    session_id=session.id,
                    tool_call_id=completed_call.id,
                    correlation_id=correlation_id,
                    replay_of=completed_call.replay_of,
                    server_id=server.id,
                    server=server.name,
                    tool=args.tool,
                    is_error=bool(result.get("is_error", False)),
                    source=source,
                    duration_ms=completed_call.duration_ms,
                )
                if implicit:
                    timeline.end_session(
                        session.id,
                        state="failed" if bool(result.get("is_error", False)) else "completed",
                    )
                return {
                    **result,
                    "server_id": server.id,
                    "session_id": session.id,
                    "tool_call_id": completed_call.id,
                    "correlation_id": correlation_id,
                    "replay_of": completed_call.replay_of,
                    "source": source,
                }
            except Exception:
                if implicit:
                    current = timeline.get_session(session.id)
                    if current.state == "active":
                        timeline.end_session(session.id, state="failed")
                raise

    if args.product_command == "profile":
        action = args.profile_command
        if action == "list":
            return [_profile_view(store, profile) for profile in store.list_profiles()]
        if action == "create":
            member_ids = [store.get_server(reference).id for reference in args.servers]
            profile = MCPProfile(
                args.name,
                server_ids=member_ids,
                description=args.description,
                health_policy=args.health_policy,
                startup_timeout_seconds=args.startup_timeout,
                project_metadata=_pairs(args.project),
                tool_permission_requirements=parse_requirement_pairs(
                    store,
                    args.require_tool,
                    member_server_ids=member_ids,
                    kind="tool",
                ),
                secret_binding_requirements=parse_requirement_pairs(
                    store,
                    args.require_secret,
                    member_server_ids=member_ids,
                    kind="secret",
                ),
            )
            store.save_profile(profile)
            return {"saved": _profile_view(store, profile)}
        if action == "active":
            current = active_profile(store, runtime)
            return current if current is not None else {"active": None}
        if action == "activate":
            return activate_profile(store, runtime, args.name)
        if action == "deactivate":
            return deactivate_profile(store, runtime, keep_running=args.keep_running)

        profile = store.get_profile(args.name)
        if action == "show":
            return _profile_view(store, profile)
        if action == "requirements":
            return {
                "profile_id": profile.id,
                "profile": profile.name,
                **evaluate_profile_requirements(store, profile, secret_policy=secret_policy),
            }
        if action == "delete":
            current = active_profile(store, runtime)
            if current is not None and current["profile_id"] == profile.id:
                raise RuntimeError(
                    f"profile {profile.name} is active; run 'megas profile deactivate' before deleting it"
                )
            if not args.yes:
                raise ValueError(
                    f"deleting profile {profile.name} removes the local profile record; repeat with --yes"
                )
            store.delete_profile(profile.id)
            return {"deleted": profile.name, "profile_id": profile.id}
        if action == "stop":
            current = active_profile(store, runtime)
            if current is not None and current["profile_id"] == profile.id:
                return deactivate_profile(store, runtime)
        if action in {"start", "stop"}:
            if action == "start":
                assert_profile_requirements(
                    store,
                    profile,
                    secret_policy=secret_policy,
                )
            results: list[dict[str, Any]] = []
            server_ids = profile.startup_order if action == "start" else list(reversed(profile.startup_order))
            for server_id in server_ids:
                server = store.get_server_by_id(server_id)
                if server.transport == "stdio":
                    if action == "start":
                        secret_policy.prepare_server(server)
                        results.append(
                            runtime.start(
                                server,
                                timeout=profile.startup_timeout_seconds,
                            )
                        )
                    else:
                        results.append(runtime.stop(server))
                else:
                    results.append(runtime.status(server))
            store.record(
                f"profile.{action}",
                profile_id=profile.id,
                profile=profile.name,
                startup_timeout_seconds=profile.startup_timeout_seconds,
            )
            return results

    if args.product_command == "history":
        return store.history(args.limit)

    if args.product_command == "export":
        bundle = build_bundle(store)
        value_to_write: dict[str, Any] = bundle
        encrypted = False
        if args.encrypt_passphrase_env:
            passphrase = _required_env(
                args.encrypt_passphrase_env,
                purpose="encrypted bundle export passphrase",
            )
            value_to_write = encrypt_bundle(bundle, passphrase)
            encrypted = True
        path = Path(args.path).expanduser()
        _atomic_write_json(path, value_to_write)
        manifest = bundle["manifest"]
        return {
            "exported": str(path),
            "bundle_version": bundle["version"],
            "content_sha256": manifest["content_sha256"],
            "encrypted": encrypted,
            **manifest["counts"],
        }

    if args.product_command == "import":
        path = Path(args.path).expanduser()
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("bundle must be a JSON object")
        encrypted = is_encrypted_bundle(raw)
        if encrypted:
            passphrase = _required_env(
                args.passphrase_env,
                purpose="encrypted bundle import passphrase",
            )
            raw = decrypt_bundle(raw, passphrase)
        elif args.passphrase_env:
            raise ValueError(
                "--passphrase-env was supplied but the input is not an encrypted Megas bundle"
            )
        result = import_portable_bundle(
            store,
            raw,
            replace=args.replace,
            dry_run=args.dry_run,
        )
        return {"path": str(path), "encrypted": encrypted, **result}

    raise ValueError("unknown command")


def _recover_state(store: MCPStore) -> dict[str, Any] | None:
    recovered = recover_pending_transaction(store.mcp_dir)
    if recovered is not None:
        store.record(
            "state.transaction.recovered",
            transaction_id=recovered["transaction_id"],
            purpose=recovered["purpose"],
            restored=recovered["restored"],
            removed=recovered["removed"],
        )
    return recovered


def main(argv: list[str] | None = None) -> None:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    if actual_argv and actual_argv[0] == "--runtime-worker":
        from .runtime_worker import main as runtime_worker_main

        raise SystemExit(runtime_worker_main(actual_argv[1:]))

    parser = _parser()
    args = parser.parse_args(actual_argv)
    store = MCPStore()
    runtime = MCPRuntime(store)
    try:
        _recover_state(store)
        _emit(_run(args, store, runtime), args.json)
    except (
        KeyError,
        ValueError,
        RuntimeError,
        TimeoutError,
        PermissionError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        if args.json:
            _emit({"error": type(exc).__name__, "message": str(exc)}, True)
        else:
            print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


__all__ = ["main"]
