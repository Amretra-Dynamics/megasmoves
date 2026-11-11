"""Regression checks for direct-install channel promotion, including interrupted retries."""
import json
from pathlib import Path
from shutil import copyfile
import tempfile
import unittest
from unittest.mock import patch

import update_preview_feed as feed


class PreviewFeedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for subtree in ("marketing/public", "docs"):
            for rel in (feed.CATALOG, *feed.FEEDS):
                src = feed.ROOT / subtree / rel
                if src.exists():
                    dest = self.root / subtree / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    copyfile(src, dest)
        current = json.loads((self.root / "docs" / feed.FEEDS[0]).read_text())
        code = current["versionCode"] + 1
        self.tag = f"preview-2026-11-11-android-{code}"
        self.name = f"megas-moves-debug-v1.0.{code}.apk"
        self.url = f"https://github.com/{feed.REPO}/releases/download/{self.tag}/{self.name}"
        self.new = dict(versionCode=code, versionName=f"1.0.{code}", sizeBytes=3, sha256="a" * 64,
                        apkUrl=self.url, builtAt="2026-11-11", minSdk=24, targetSdk=36)

    def test_projections_retries_and_downgrade_guard(self):
        release_date = "2026-11-11"
        old_path = self.root / "docs" / feed.CATALOG
        old_bytes = old_path.read_bytes()
        others = [row for row in json.loads(old_bytes)["artifacts"] if row["title"] != "Android APK"]
        feed.update(self.root, self.new, release_date)
        old_path.write_bytes(old_bytes)  # Simulate an interrupted checkout update before staging.
        feed.update(self.root, self.new, release_date)
        for subtree in ("marketing/public", "docs"):
            for rel in feed.FEEDS:
                manifest = json.loads((self.root / subtree / rel).read_text())
                self.assertEqual(manifest["apk_url"], self.url)
                self.assertEqual(manifest["sha256"], self.new["sha256"])
                self.assertEqual(manifest["signingCertificateSha256"], feed.DIRECT_SIGNER)
            rows = json.loads((self.root / subtree / feed.CATALOG).read_text())["artifacts"]
            self.assertEqual(others, [r for r in rows if r["title"] != "Android APK"])
            self.assertEqual(next(r for r in rows if r["title"] == "Android APK")["href"], self.url)
        for wrong in (dict(self.new, versionCode=1), dict(self.new, sha256="c" * 64)):
            with self.assertRaises(ValueError):
                feed.update(self.root, wrong, release_date)

    def test_rejects_other_signer_wrong_package_and_wrong_digest(self):
        apk = self.root / self.name
        apk.write_bytes(b"APK")
        digest = feed.hashlib.sha256(apk.read_bytes()).hexdigest()
        asset = dict(name=self.name, browser_download_url=self.url, size=3, state="uploaded",
                     digest="sha256:" + digest)
        with self.assertRaisesRegex(ValueError, "digest"):
            feed.verify_apk(apk, dict(asset, digest="sha256:" + "0" * 64), self.tag, self.root)
        with patch.object(feed, "run", return_value="Signer #1 certificate SHA-256 digest: " + "a" * 64):
            with self.assertRaisesRegex(ValueError, "signer"):
                feed.verify_apk(apk, asset, self.tag, self.root)
        with patch.object(feed, "run", side_effect=[
                "Signer #1 certificate SHA-256 digest: " + feed.DIRECT_SIGNER,
                "package: name='wrong.package' versionCode='1' versionName='1'"]):
            with self.assertRaisesRegex(ValueError, "package"):
                feed.verify_apk(apk, asset, self.tag, self.root)

    def test_original_public_updater_size_ceiling(self):
        apk = self.root / self.name
        limit = 150 * 1024 * 1024
        asset = dict(name=self.name, browser_download_url=self.url, state="uploaded",
                     size=limit, digest="sha256:" + "d" * 64)
        with apk.open("wb") as stream:
            stream.truncate(limit)
        badging = (f"package: name='{feed.PACKAGE}' versionCode='{self.new['versionCode']}' "
                   f"versionName='{self.new['versionName']}'\nsdkVersion:'24'\ntargetSdkVersion:'36'")
        with patch.object(feed.hashlib, "file_digest") as digest, patch.object(feed, "run") as tool:
            digest.return_value.hexdigest.return_value = "d" * 64
            tool.side_effect = ["Signer #1 certificate SHA-256 digest: " + feed.DIRECT_SIGNER, badging]
            self.assertEqual(feed.verify_apk(apk, asset, self.tag, self.root)["sizeBytes"], limit)
            with apk.open("ab") as stream:
                stream.write(b"x")
            with self.assertRaisesRegex(ValueError, "size"):
                feed.verify_apk(apk, dict(asset, size=limit + 1), self.tag, self.root)
            self.assertEqual(digest.call_count, 1)


if __name__ == "__main__":
    unittest.main()
