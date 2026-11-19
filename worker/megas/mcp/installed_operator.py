"""Expose selected native capabilities through the local public MCP server."""

from __future__ import annotations

import json
import os
from importlib.resources import files
from pathlib import Path
import sys

from megas.mcp.model import MCPServer
from megas.mcp.native_capabilities import NATIVE_TOOL_NAMES
from megas.mcp.protocol import StdioMCPClient
from megas.mcp.validation import validate_tool_arguments
from megas.operate.gate import is_operate_enabled
from megas.operate.window_identity import matches_window_inventory

WINDOW_NAMES = frozenset({
    "linux_desktop_status", "linux_ui_windows", "linux_ui_inspect_window", "linux_ui_agent_window",
})


def installed_linux_core() -> Path | None:
    """Return the installed Linux launcher, independent of the command search path."""
    if sys.platform != "linux":
        return None
    command = Path.home() / ".local/share/MegasMoves/bin/core"
    if not command.exists() and Path("/usr/lib/megasmoves/core/payload.json").is_file():
        command = Path("/usr/bin/megas")
    return command if command.is_file() and os.access(command, os.X_OK) else None


def _installed_server():
    if sys.platform == "linux":
        command = installed_linux_core()
        if command is None:
            return None
        args = ["serve"]
    elif sys.platform == "darwin":
        suffix = Path("MegasMoves.app/Contents/MacOS/MegasMoves.Core")
        command = Path("/Applications") / suffix
        if not command.exists():
            command = Path.home() / "Applications" / suffix
        args = ["--mcp-stdio"]
    elif sys.platform == "win32":
        from megas.updater import product_install_binary
        command = product_install_binary()
        if command is None:
            return None
        try:
            with command.open("rb") as executable:
                if executable.read(2) != b"MZ":
                    return None
            receipt = json.loads(command.with_suffix(".release.json").read_text(encoding="utf-8"))
            if not isinstance(receipt, dict) or receipt.get("native_ui") != "WinUI 3" or receipt.get("embedded_megas_mcp") is not True:
                return None
        except (OSError, ValueError):
            return None
        args = ["serve"]
    else:
        return None
    if not command.is_file() or not os.access(command, os.X_OK):
        return None
    return MCPServer(name="megas-native", command=str(command), args=args)


def _error(reason):
    body = {"ok": False, "reason": reason}
    return {"isError": True, "structuredContent": body, "content": [{"type": "text", "text": json.dumps(body)}]}


class InstalledOperator:
    """Keep observation handles and client attribution on the same native session."""

    def __init__(self):
        self.server = _installed_server()
        self.definitions = {}
        self.client = None
        self.client_info = {}
        self.live_tools = {}
        if self.server is not None:
            allowed = NATIVE_TOOL_NAMES | WINDOW_NAMES if sys.platform == "linux" else NATIVE_TOOL_NAMES
            catalog = files(__package__).joinpath("native_tool_schemas.json")
            self.definitions = {row["name"]: row for row in json.loads(catalog.read_text("utf-8"))
                                if row["name"] in allowed}

    def initialize(self, params):
        info = params.get("clientInfo") if isinstance(params, dict) else None
        info = info if isinstance(info, dict) else {}
        labels = {}
        for key in ("name", "version"):
            value = info.get(key)
            if isinstance(value, str) and value:
                labels[key] = " ".join(value.split())[:64]
        if self.client_info != labels:
            self.close()
            self.client_info = labels

    def _connect(self):
        if self.client is not None:
            return
        client = StdioMCPClient(self.server, timeout=25, client_info=self.client_info)
        try:
            client.__enter__()
            rows = client.request("tools/list").get("tools", [])
            self.live_tools = {row["name"]: row for row in rows
                               if isinstance(row, dict) and isinstance(row.get("inputSchema"), dict)
                               and row.get("name") in self.definitions}
            self.client = client
        except BaseException:
            client.close()
            raise

    def call(self, name, arguments):
        if name not in self.definitions:
            raise LookupError("unknown native capability")
        validate_tool_arguments(name, self.definitions[name]["inputSchema"], arguments)
        if name in WINDOW_NAMES and name != "linux_desktop_status" and not is_operate_enabled():
            return _error("operate_disabled_by_owner")
        try:
            self._connect()
        except (OSError, RuntimeError, ValueError):
            return _error("installed_operator_unavailable")
        definition = self.live_tools.get(name)
        if definition is None:
            return _error("installed_operator_tool_unavailable")
        validate_tool_arguments(name, definition["inputSchema"], arguments)
        try:
            response = self.client.request("tools/call", {"name": name, "arguments": arguments})
            if name == "linux_ui_windows" and not response.get("isError"):
                body = response.get("structuredContent")
                if not isinstance(body, dict) or not matches_window_inventory(body, arguments):
                    return _error("native_window_inventory_unverified")
            return response
        except (OSError, RuntimeError, ValueError):
            # An interrupted write may already have taken effect. Rediscovery is
            # permitted on the next request; replay is always the caller's decision.
            self.close()
            return _error("installed_operator_result_unconfirmed")

    def close(self):
        if self.client is not None:
            self.client.close()
        self.client = None
        self.live_tools = {}
