from __future__ import annotations

from dataclasses import dataclass

from .store import MCPStore


@dataclass(frozen=True, slots=True)
class ToolPermission:
    server: str
    tool: str

    @property
    def key(self) -> str:
        return f"{self.server}:{self.tool}"


class PermissionStore:
    """Compatibility facade over the canonical MCPStore permission engine.

    `MCPStore` owns the only persisted permission representation.  This facade is
    retained for callers that imported PermissionStore during the migration, but
    it must never maintain a second permissions.json schema.
    """

    def __init__(self, store: MCPStore | None = None) -> None:
        self.store = store or MCPStore()

    def list(self) -> list[ToolPermission]:
        return [
            ToolPermission(server=row["server"], tool=row["tool"])
            for row in self.store.list_permissions()
        ]

    def is_allowed(self, server: str, tool: str) -> bool:
        return self.store.is_tool_allowed(server, tool)

    def grant(self, server: str, tool: str) -> ToolPermission:
        row = self.store.grant_tool(server, tool)
        return ToolPermission(server=row["server"], tool=row["tool"])

    def revoke(self, server: str, tool: str) -> bool:
        return self.store.revoke_tool(server, tool)
