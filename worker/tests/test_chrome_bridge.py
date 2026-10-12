"""Contract tests for the opt-in Chrome Native Messaging bridge."""
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from megas.chrome_bridge import HOST_NAME, ChromeHost, _atomic_json, validate_command, install


class ChromeContractTests(unittest.TestCase):
    def test_commands_are_allowlisted_and_typed(self):
        self.assertEqual(validate_command({"action": "tabs"}), {"action": "tabs"})
        self.assertEqual(
            validate_command({"action": "fill", "tabId": 3, "selector": "#q", "value": "example"}),
            {"action": "fill", "tabId": 3, "selector": "#q", "value": "example"},
        )
        invalid = [
            {"action": "eval", "script": "alert(1)"},
            {"action": "observe", "tabId": True},
            {"action": "observe", "tabId": -1},
            {"action": "click", "tabId": 4, "selector": ""},
            {"action": "fill", "tabId": 4, "selector": "input", "value": 123},
            {"action": "navigate", "tabId": 4, "url": "file:///etc/passwd"},
            {"action": "navigate", "tabId": 4, "url": "https://user:pass@example.com"},
            {"action": "tabs", "unexpected": "data"},
        ]
        for case in invalid:
            with self.subTest(case=case), self.assertRaises(ValueError):
                validate_command(case)

    def test_host_resolves_pending_message_by_id(self):
        host = ChromeHost()
        event = __import__("threading").Event()
        payload = {}
        host.pending["x"] = (event, payload)
        host.accept({"kind": "result", "id": "wrong", "ok": True})
        self.assertFalse(event.is_set())
        host.accept({"kind": "result", "id": "x", "ok": True, "result": {"tabs": []}})
        self.assertTrue(event.is_set())
        self.assertEqual(payload["result"], {"tabs": []})

    def test_install_generates_only_exact_origin(self):
        with tempfile.TemporaryDirectory() as d, patch("megas.chrome_bridge.bridge_root", return_value=Path(d)):
            if __import__("sys").platform not in {"win32", "darwin"}:
                with patch("pathlib.Path.home", return_value=Path(d)):
                    manifest = install("abcdefghijklmnopabcdefghijklmnop")
                    content = __import__("json").loads(manifest.read_text())
                    self.assertEqual(content["name"], HOST_NAME)
                    self.assertEqual(content["allowed_origins"], [
                        "chrome-extension://abcdefghijklmnopabcdefghijklmnop/"
                    ])
                    self.assertNotIn("*", str(content["allowed_origins"]))
                    self.assertTrue(Path(content["path"]).is_file())

    def test_install_rejects_fake_id(self):
        with self.assertRaises(ValueError):
            install("*")

    def test_discovery_write(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "state.json"
            _atomic_json(f, {"port": 12345})
            self.assertEqual(__import__("json").loads(f.read_text())["port"], 12345)


if __name__ == "__main__":
    unittest.main()
