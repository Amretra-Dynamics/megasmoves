"""Prevent accidental private-product coupling in the standalone public edition.

This static regression check supplements, but does not replace, history
and secret scans, dependency audits, and human source review.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PRODUCT_MARKER = "chitra" + "agni"
TEXT_SUFFIXES = frozenset(
    {".py", ".md", ".html", ".css", ".js", ".ts", ".tsx", ".json", ".toml", ".yml", ".yaml", ".txt"}
)
SKIP_DIRS = frozenset({".git", ".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist"})


class PublicReleaseBoundaryTests(unittest.TestCase):
    def test_no_excluded_product_paths_or_references(self):
        unwanted = []
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if any(part in SKIP_DIRS for part in relative.parts):
                continue
            if path.suffix not in TEXT_SUFFIXES and path.name not in {"LICENSE", ".gitignore"}:
                continue
            normal_path = re.sub(r"[\s._/-]+", "", str(relative).casefold())
            if EXCLUDED_PRODUCT_MARKER in normal_path:
                unwanted.append(str(relative))
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            normal_content = re.sub(r"[\s._/-]+", "", content.casefold())
            if EXCLUDED_PRODUCT_MARKER in normal_content:
                unwanted.append(str(relative))
        self.assertEqual(unwanted, [], "Disallowed private integration found in public source: " + repr(unwanted))


if __name__ == "__main__":
    unittest.main()
