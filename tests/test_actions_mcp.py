import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from megas_community.actions import MegasActions
from megas_community.cli import main
from megas_community.core import TrustStore
from megas_community.mcp import dispatch, serve


class ActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home_patch = patch.dict(os.environ, {"MEGAS_HOME": str(Path(self.tmp.name) / "megas")})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.path = Path(self.tmp.name)

    def test_standalone_megas_action_without_external_account(self):
        action = MegasActions().call("megas_action_create", {"name": "Test",
            "description": "Local action", "paths": [str(self.path)]})["structuredContent"]
        self.assertTrue(action["id"].startswith("action_"))
        listing = MegasActions().call("megas_action_list", {})["structuredContent"]["actions"]
        self.assertEqual(listing[0]["name"], "Test")

    def test_mcp_exposes_action_and_browser_by_default(self):
        tools = {item["name"] for item in dispatch("tools/list", {}, TrustStore())["tools"]}
        self.assertIn("megas_action_list", tools)
        self.assertIn("megas_action_create", tools)
        self.assertIn("browser_start", tools)
        self.assertIn("browser_open", tools)
        result = dispatch("tools/call", {"name": "browser_status", "arguments": {}}, TrustStore())
        self.assertTrue(json.loads(result["content"][0]["text"])["enabled"])

    def test_connect_runs_real_mcp_stdio(self):
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        stdin, stdout = io.StringIO(json.dumps(rpc) + "\n"), io.StringIO()
        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            self.assertEqual(main(["connect"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["result"]["serverInfo"]["name"], "megasmoves")

    def test_action_cli(self):
        capture = io.StringIO()
        with patch("sys.stdout", capture):
            self.assertEqual(main(["actions", "create", "Work", "--path", str(self.path)]), 0)
        value = json.loads(capture.getvalue())
        self.assertEqual(value["name"], "Work")
        listing = io.StringIO()
        with patch("sys.stdout", listing):
            self.assertEqual(main(["actions", "list"]), 0)
        self.assertEqual(len(json.loads(listing.getvalue())["actions"]), 1)


if __name__ == "__main__":
    unittest.main()
