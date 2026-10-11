import io
import json
import tempfile
import unittest
from pathlib import Path

from megas_community.core import TrustStore
from megas_community.mcp import dispatch, serve


class McpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TrustStore(Path(self.tmp.name) / "config")

    def test_initialize_and_tools(self):
        self.assertIn("serverInfo", dispatch("initialize", {}, self.store))
        self.assertTrue(
            {"operator_status", "workspace_projects", "workspace_check",
             "megas_action_list", "megas_action_create", "browser_start", "browser_open"}
            .issubset({tool["name"] for tool in dispatch("tools/list", {}, self.store)["tools"]})
        )

    def test_readonly(self):
        with self.assertRaises(ValueError):
            dispatch("tools/call", {"name": "workspace_check", "arguments": {"workspace_id": "../../etc"}}, self.store)
        with self.assertRaises(ValueError):
            dispatch("tools/call", {"name": "workspace_add", "arguments": {"path": "/tmp"}}, self.store)

    def test_stdio_rpc(self):
        input_data = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "operator_status", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "do_anything", "arguments": {}}},
        ]
        output = io.StringIO()
        serve(store=self.store, stdin=io.StringIO("".join(json.dumps(x) + "\n" for x in input_data)), stdout=output)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["result"]["serverInfo"]["name"], "megasmoves")
        self.assertEqual(rows[1]["result"]["isError"], False)
        self.assertIn("error", rows[2])


if __name__ == "__main__":
    unittest.main()
