import json
import tempfile
import unittest
from pathlib import Path

from megas_community.core import TrustStore, inspect_workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.store = TrustStore(self.base / "settings")

    def test_trust_lifecycle(self):
        added = self.store.add(str(self.project))
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.store.add(str(self.project)), added)
        self.assertEqual(self.store.check(added["id"])["name"], "project")
        self.assertTrue(self.store.remove(added["id"]))
        self.assertFalse(self.store.remove(added["id"]))
        with self.assertRaises(ValueError):
            self.store.check(added["id"])

    def test_no_arbitrary_workspace_access(self):
        with self.assertRaises(ValueError):
            self.store.check("../../etc")
        with self.assertRaises(ValueError):
            self.store.add(str(self.project / "missing"))

    def test_bounded_metadata_only(self):
        (self.project / "package.json").write_text(json.dumps({
            "name": "sample",
            "scripts": {"dev": "run", "test": "run", "dangerous": "ignored"},
        }))
        result = inspect_workspace(self.project)
        self.assertEqual(result["stack"], ["node"])
        self.assertEqual(result["node_scripts"], ["dev", "test"])
        self.assertIn("no commands were run", result["note"])

    def test_large_manifest_ignored(self):
        (self.project / "package.json").write_text("x" * (256 * 1024 + 1))
        self.assertEqual(inspect_workspace(self.project)["stack"], [])

    def test_symlinked_manifest_outside_root_ignored(self):
        outside = self.base / "outside.json"
        outside.write_text('{"scripts":{"build":"secret"}}')
        try:
            (self.project / "package.json").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertEqual(inspect_workspace(self.project)["stack"], [])

    def test_malformed_config_fails_closed(self):
        self.store.home.mkdir()
        self.store.config.write_text('{"format":1,"roots":"bad"}')
        with self.assertRaises(ValueError):
            self.store.list()


if __name__ == "__main__":
    unittest.main()
