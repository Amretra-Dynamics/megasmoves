import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from megas.mcp.standalone_operator import MegasMCP, serve
from megas.package_entrypoint import main


class MegasDistributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.environment = patch.dict(os.environ, {"MEGAS_HOME": str(Path(self.tmp.name) / ".megas")})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_same_operator_exposes_real_megas_capabilities(self):
        operator = MegasMCP()
        self.addCleanup(operator.close)
        tools = {item["name"] for item in operator.dispatch("tools/list", {})["tools"]}
        self.assertIn("megas_action_create", tools)
        self.assertIn("browser_start", tools)
        self.assertIn("workspace_projects", tools)
        self.assertIn("operator_status", tools)

    def test_stdio_mcp_initialize_and_tool_listing(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        output = io.StringIO()
        serve(input_stream=io.StringIO("".join(json.dumps(q) + "\n" for q in requests)),
              output_stream=output)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["result"]["serverInfo"]["name"], "MegasMoves")
        self.assertTrue(messages[1]["result"]["tools"])

    def test_megas_cli_actions(self):
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(main(["actions", "create", "Local automation", "--path", self.tmp.name]), 0)
        created = json.loads(output.getvalue())
        self.assertTrue(created["id"].startswith("action_"))
        listed = io.StringIO()
        with patch("sys.stdout", listed):
            self.assertEqual(main(["actions", "list"]), 0)
        self.assertEqual(len(json.loads(listed.getvalue())["actions"]), 1)

    def test_no_duplicate_console_command(self):
        from importlib.metadata import entry_points
        matching = {ep.name for ep in entry_points(group="console_scripts") if ep.name in {"megas", "megasmoves"}}
        self.assertEqual(matching, {"megas"})


if __name__ == "__main__":
    unittest.main()
