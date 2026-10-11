import importlib.metadata as md
import unittest


class PackagingTests(unittest.TestCase):
    def test_distribution_identity(self):
        dist = md.distribution("megasmoves")
        self.assertEqual(dist.metadata["Name"].lower(), "megasmoves")

    def test_only_megas_console_command(self):
        entries = {e.name: e.value for e in md.entry_points(group="console_scripts") if e.name in {"megas", "megasmoves"}}
        self.assertEqual(entries, {"megas": "megas_community.cli:main"})


if __name__ == "__main__":
    unittest.main()
