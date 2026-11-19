"""Expose the signed-in owner's registered MCP tools inside the embedded operator."""

import hashlib
import json
from pathlib import Path

from . import action_skills
from .mcp.capabilities import CapabilityStore
from .mcp.gateway import tool_contract_fingerprint
from .mcp.protocol import _summarize_tool, call_tool
from .mcp.secret_release import SecretReleasePolicy
from .mcp.store import MCPStore
from .operator_paths import operator_home


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plugin_home(owner, home=None):
    return (Path(home) if home is not None else operator_home()) / "plugins" / _digest(owner)


class CustomMCPPlugins:
    def __init__(self, *, current_owner, home=None):
        self.current_owner = current_owner
        self.owner = current_owner()
        if not isinstance(self.owner, str) or not self.owner.strip() or len(self.owner) > 128:
            raise PermissionError("Verified owner sign-in required")
        self.store = MCPStore(plugin_home(self.owner, home))

    def manage(self, arguments):
        """Human CLI entry for registering servers and granting individual tools."""
        from .mcp.cli import main

        if self.current_owner() != self.owner:
            raise PermissionError("Megas account changed")
        main(["--json", "mcp", *arguments], home=self.store.home, owner_plugin=True)

    def discover(self):
        if self.current_owner() != self.owner:
            raise PermissionError("Megas account changed")
        snapshots = CapabilityStore(self.store)
        permissions = self.store.list_permissions()
        if not permissions:
            return {}
        grants = {(g["server_id"], g["tool"]): g["id"] for g in permissions}
        result = {}
        for server in self.store.list_servers():
            if not server.enabled or not any(sid == server.id for sid, _ in grants):
                continue
            snapshot = snapshots.latest(server.id)
            if snapshot is None:
                continue
            target = _digest([server.transport, server.command, server.args, server.url,
                              server.cwd, server.env, server.env_refs])
            for spec in snapshot.tools:
                tool = spec["name"]
                permission = grants.get((server.id, tool))
                if permission is None:
                    continue
                name = "mcp_" + _digest([server.id, tool])[:32]
                hints = spec.get("annotations") or {}
                result[name] = {
                    "name": name, "server": server.name, "tool": tool,
                    "description": str(spec.get("description") or "")[:1500],
                    "inputSchema": spec["input_schema"],
                    "read_only": hints.get("readOnlyHint") is True and not hints.get("destructiveHint"),
                    "grant": {"server_id": server.id, "permission_id": permission,
                              "target": target, "fingerprint": tool_contract_fingerprint(spec)},
                }
        if self.current_owner() != self.owner:
            raise PermissionError("Megas account changed")
        return result

    def call(self, name, arguments, grant):
        def authorize():
            if self.current_owner() != self.owner:
                raise PermissionError("Megas account changed")
            entry = self.discover().get(name)
            if not entry or entry["grant"] != grant:
                raise PermissionError("Custom MCP permission or server changed")
            return entry

        entry = authorize()
        server = self.store.get_server_by_id(grant["server_id"])
        policy = SecretReleasePolicy(self.store)
        prepared = policy.prepare_server(server)
        prepared.source = "owner-plugin"

        def verify(spec):
            if tool_contract_fingerprint(_summarize_tool(spec)) != grant["fingerprint"]:
                raise PermissionError("Custom MCP tool changed; probe it and review its grant")
            authorize()
            policy.prepare_server(self.store.get_server_by_id(server.id))

        result = call_tool(prepared, entry["tool"], arguments, timeout=15, verify=verify)
        authorize()
        content = result.get("content") or []
        structured = result.get("structured_content")
        self.store.record("plugin.called", server_id=server.id, tool=entry["tool"],
                          is_error=bool(result["is_error"]))
        return {"content": content, "structuredContent": structured if structured is not None else {"content": content},
                "isError": bool(result["is_error"])}


def direct_tool():
    return {
        "name": "megas_plugin_call",
        "description": "Call one tool on a custom MCP server added and granted by the current Megas user. Find names and schemas with megas_skill_capabilities. A call may change the external service.",
        "inputSchema": action_skills._object({
            "name": {"type": "string", "pattern": "^mcp_[0-9a-f]{32}$"},
            "arguments": {"type": "object", "maxProperties": 64},
        }, ["name", "arguments"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    }


def call_direct(args, plugins):
    from .mcp.validation import validate_tool_arguments
    validate_tool_arguments("megas_plugin_call", direct_tool()["inputSchema"], args)
    entry = plugins.discover().get(args["name"])
    if entry is None:
        raise PermissionError("Custom MCP tool is not granted for this owner")
    return plugins.call(args["name"], args["arguments"], entry["grant"])
