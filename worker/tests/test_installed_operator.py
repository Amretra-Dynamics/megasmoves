import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from megas.mcp.model import MCPServer
from megas.mcp.standalone_operator import MegasMCP
from megas.operate.window_identity import matches_window_inventory, window_ref


FIXTURE = """
import json, pathlib, sys
journal = pathlib.Path(sys.argv[1])
for line in sys.stdin:
    request = json.loads(line)
    method, params = request["method"], request.get("params", {})
    with journal.open("a") as target:
        target.write(json.dumps([method, params]) + "\\n")
    if method == "initialize":
        result = {"protocolVersion": params["protocolVersion"], "capabilities": {"tools": {}}}
    elif method == "tools/list":
        result = {"tools": [{"name": name, "inputSchema": {"type": "object"}}
                            for name in ("linux_ui_agent_window", "megas_skill_list", "private_method")]}
    elif method == "tools/call":
        if params["name"] == "linux_ui_agent_window":
            sys.exit(1)
        result = {"isError": False, "content": [], "structuredContent": {"ok": True}}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


class NativeBridgeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.journal = root / "journal"
        fixture = root / "fixture_core.py"
        fixture.write_text(FIXTURE)
        server = MCPServer(name="fixture", command=sys.executable, args=["-I", "-B", str(fixture), str(self.journal)])
        self.enterContext(patch("megas.mcp.installed_operator._installed_server", return_value=server))
        self.enterContext(patch("megas.mcp.installed_operator.sys.platform", "linux"))
        self.operator = MegasMCP()
        self.addCleanup(self.operator.close)

    def call(self, name, **args):
        return self.operator.dispatch("tools/call", {"name": name, "arguments": args})

    def test_list_and_disabled_operate_leave_core_stopped(self):
        self.operator.dispatch("initialize", {"clientInfo": {"name": "Codex"}})
        tools = {row["name"] for row in self.operator.dispatch("tools/list", {})["tools"]}
        self.assertTrue({"linux_ui_agent_window", "megas_skill_list"} <= tools)
        self.assertNotIn("private_method", tools)
        with patch("megas.mcp.installed_operator.is_operate_enabled", return_value=False):
            denied = self.call("linux_ui_windows")["structuredContent"]
            self.assertEqual(denied["reason"], "operate_disabled_by_owner")
        with self.assertRaises(ValueError):
            self.call("linux_ui_agent_window", command="unexpected")
        self.assertFalse(self.journal.exists())

    def test_checked_install_preserves_handshake_and_refuses_old_and_unlisted_tools(self):
        self.operator.dispatch("initialize", {"clientInfo": {"name": "openai-mcp", "version": "1", "extra": "omit"}})
        self.assertTrue(self.call("megas_skill_list")["structuredContent"]["ok"])
        with patch("megas.mcp.installed_operator.is_operate_enabled", return_value=True):
            result = self.call("linux_ui_windows")["structuredContent"]
        self.assertEqual(result["reason"], "installed_operator_tool_unavailable")
        with self.assertRaises(LookupError):
            self.call("private_method")
        lines = [json.loads(line) for line in self.journal.read_text().splitlines()]
        self.assertEqual(lines[0][1]["clientInfo"], {"name": "openai-mcp", "version": "1"})
        self.assertEqual([params["name"] for method, params in lines if method == "tools/call"], ["megas_skill_list"])

    def test_lost_native_reply_never_replays_a_window_creation(self):
        with patch("megas.mcp.installed_operator.is_operate_enabled", return_value=True):
            response = self.call("linux_ui_agent_window")
        self.assertEqual(response["structuredContent"]["reason"], "installed_operator_result_unconfirmed")
        self.assertEqual(self.journal.read_text().count('"tools/call"'), 1)

    def test_without_install_local_tools_remain_available(self):
        with patch("megas.mcp.installed_operator._installed_server", return_value=None):
            operator = MegasMCP()
        self.addCleanup(operator.close)
        tools = {row["name"] for row in operator.dispatch("tools/list", {})["tools"]}
        self.assertIn("megas_action_list", tools)
        self.assertNotIn("linux_ui_windows", tools)


class WindowReceiptTests(unittest.TestCase):
    def test_window_id_reuse_and_duplicate_titles_do_not_retarget_receipts(self):
        session = {"session_id": "session", "display": ":77", "session_type": "x11"}
        row = {"index": 0, "window_id": 6, "process_id": 7, "process_created": 2.0, "title": "Same title"}
        ref = window_ref(session, row)
        row["window_ref"] = ref
        body = {"result": {"windows": [row]}, "desktop_session": session}
        self.assertTrue(matches_window_inventory(body, {"window_ref": ref}))
        self.assertFalse(matches_window_inventory(body, {"agent_window_id": "a" * 32}))
        row["process_created"] = 3
        self.assertFalse(matches_window_inventory(body, {"window_ref": ref}))
