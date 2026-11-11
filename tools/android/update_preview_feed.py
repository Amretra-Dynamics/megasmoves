"""Update the public direct-install feeds from a published, signed GitHub APK."""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.request import urlopen

REPO = "Amretra-Dynamics/megasmoves"
PACKAGE = "com.amretra.megasmoves"
# SHA-256 of the public Android signing certificate used by the direct-install channel.
DIRECT_SIGNER = "9c71b2c39a4a0b44d642d852407e6786d489a9e334ecec4e298f414dd8e096f8"
MAX_BYTES = 200 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[2]
CATALOG = Path("downloads.json")
FEEDS = (
    Path("mobile/releases/latest.json"),
    Path("api/v1/mobile/latest-version/index.html"),
    Path("v1/mobile/latest-version/index.html"),
    Path("api/v1/update/check/index.html"),
)


def run(*argv):
    return subprocess.check_output(argv, text=True)


def parse_tag(tag):
    match = re.fullmatch(r"preview-(\d{4}-\d{2}-\d{2})-android-([1-9][0-9]{0,9})", tag)
    if not match:
        raise ValueError("Expected a preview-YYYY-MM-DD-android-CODE tag")
    day, code = match.groups()
    return date.fromisoformat(day).isoformat(), int(code)


def verify_apk(apk, asset, tag, build_tools):
    built_at, code = parse_tag(tag)
    version = f"1.0.{code}"
    name = f"megas-moves-debug-v{version}.apk"
    url = f"https://github.com/{REPO}/releases/download/{tag}/{name}"
    size = apk.stat().st_size
    if (asset.get("name") != name or asset.get("state") != "uploaded"
            or asset.get("browser_download_url") != url or asset.get("size") != size
            or not 0 < size <= MAX_BYTES):
        raise ValueError("Uploaded release asset name, URL or size differs from the preview contract")
    with apk.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if asset.get("digest") != "sha256:" + digest:
        raise ValueError("APK bytes do not match the published release asset digest")
    certs = run(str(build_tools / "apksigner"), "verify", "--print-certs", str(apk))
    signers = re.findall(r"^Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]{64})$", certs, re.M)
    if [signer.lower() for signer in signers] != [DIRECT_SIGNER]:
        raise ValueError("APK signer differs from the direct-install channel")
    info = run(str(build_tools / "aapt"), "dump", "badging", str(apk))
    identity = re.search(r"^package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']+)'", info, re.M)
    if not identity or identity.groups() != (PACKAGE, str(code), version):
        raise ValueError("APK package or embedded version differs from the release tag")
    sdk = re.search(r"^sdkVersion:'(\d+)'$", info, re.M)
    target = re.search(r"^targetSdkVersion:'(\d+)'$", info, re.M)
    if not sdk or not target:
        raise ValueError("APK has no numeric minimum or target SDK")
    return dict(versionCode=code, versionName=version, sizeBytes=size, sha256=digest,
                apkUrl=url, builtAt=built_at, minSdk=int(sdk[1]), targetSdk=int(target[1]))


def verify_progress(code, digest, new):
    if code > new["versionCode"]:
        raise ValueError("Refusing to roll back a newer direct-install release")
    if code == new["versionCode"] and digest != new["sha256"]:
        raise ValueError("A published version code already identifies a different APK")


def update(root, new, published_at):
    """Plan and validate every projection before modifying the working tree."""
    source = root / "marketing/public"
    dirs = [source, root / "docs"]
    canonical = json.loads((source / FEEDS[0]).read_text(encoding="utf-8"))
    notes = f"Megas Moves Android {new['versionName']} direct-install preview."
    feed = dict(canonical)  # Preserve the channel's existing support and opt-in settings.
    feed.update(new, latest_version_name=new["versionName"], latest_version_code=new["versionCode"],
                apk_url=new["apkUrl"], releaseNotes=notes, release_notes=notes,
                signingCertificateSha256=DIRECT_SIGNER)
    if canonical.get("packageName") != PACKAGE or canonical.get("channel") != "preview":
        raise ValueError("Unexpected direct-install feed")
    outputs = {}
    for directory in dirs:
        for rel in FEEDS:
            path = directory / rel
            if path.exists():
                prior = json.loads(path.read_text(encoding="utf-8"))
                if prior["packageName"] != PACKAGE or prior["channel"] != "preview":
                    raise ValueError("Unexpected Android feed")
                verify_progress(prior["versionCode"], prior["sha256"], new)
            outputs[path] = feed
        path = directory / CATALOG
        catalog = json.loads(path.read_text(encoding="utf-8"))
        rows = [row for row in catalog["artifacts"] if row.get("title") == "Android APK"]
        if len(rows) != 1 or catalog["channel"] != "preview":
            raise ValueError("Unexpected public APK catalog")
        row = rows[0]
        version = re.fullmatch(r"1\.0\.([0-9]+)", row["version"])
        if not version:
            raise ValueError("Unexpected public Android catalog version")
        verify_progress(int(version[1]), row["sha256"], new)
        row.update(version=new["versionName"], size_bytes=new["sizeBytes"], sha256=new["sha256"],
                   filename=new["apkUrl"].rsplit("/", 1)[1], href=new["apkUrl"], built_at=new["builtAt"],
                   source_updated_at=None, source_revision=None, status="Signed Android direct-install preview",
                   source_note="Verified APK from the Megas Moves Android preview release.")
        catalog["published_at"] = max(catalog["published_at"], published_at)
        outputs[path] = catalog
    if outputs[source / CATALOG] != outputs[root / "docs" / CATALOG]:
        raise ValueError("The two download catalogs have unrelated differences")
    for path, value in outputs.items():
        body = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
        if not path.exists() or path.read_text(encoding="utf-8") != body:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             delete=False) as pending:
                pending.write(body)
            replacement = Path(pending.name)
            replacement.chmod(0o644)
            replacement.replace(path)


def verify_live(tag, root=ROOT):
    """Wait for Pages to serve the exact catalog and feed bytes as JSON at each public path."""
    _, code = parse_tag(tag)
    docs = root / "docs"
    manifest = json.loads((docs / FEEDS[0]).read_text(encoding="utf-8"))
    expected_url = f"https://github.com/{REPO}/releases/download/{tag}/megas-moves-debug-v1.0.{code}.apk"
    if manifest["versionCode"] != code or manifest["apkUrl"] != expected_url:
        raise ValueError("Checked-out feed does not match requested release")
    catalog = json.loads((docs / CATALOG).read_text(encoding="utf-8"))
    pending = {str(path).removesuffix("/index.html"): manifest for path in FEEDS}
    pending[str(CATALOG)] = catalog
    deadline = time.monotonic() + 600
    while pending:
        for route, expected in list(pending.items()):
            url = f"https://megasmoves.io/{route}?release={code}"
            try:
                with urlopen(url, timeout=10) as response:
                    body = response.read(512_001)
                if len(body) <= 512_000 and json.loads(body) == expected:
                    del pending[route]
            except (OSError, ValueError):
                pass  # Pages may serve the previous export while its new build propagates.
        if pending and time.monotonic() >= deadline:
            raise RuntimeError("Public Android feed verification timed out: " + ", ".join(pending))
        if pending:
            time.sleep(10)
    print(f"Public catalog and all direct Android update feeds verified for {tag}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--build-tools", type=Path, help="Android SDK build-tools directory")
    parser.add_argument("--apk", type=Path, help="Verify an existing copy instead of downloading the asset again")
    parser.add_argument("--verify-live", action="store_true", help="Compare the deployed feeds with this checkout")
    args = parser.parse_args()
    if args.verify_live:
        verify_live(args.tag)
        return
    if args.build_tools is None:
        parser.error("--build-tools is required to stage an APK")
    _, code = parse_tag(args.tag)
    release = json.loads(run("gh", "api", f"repos/{REPO}/releases/tags/{args.tag}"))
    if release["draft"] or not release["prerelease"] or release["tag_name"] != args.tag:
        raise ValueError("Publish a direct-install preview release before promoting its APK")
    name = f"megas-moves-debug-v1.0.{code}.apk"
    assets = [asset for asset in release["assets"] if asset["name"] == name]
    if len(assets) != 1 or not 0 < assets[0]["size"] <= MAX_BYTES:
        raise ValueError("Expected one bounded universal APK in the preview release")
    with tempfile.TemporaryDirectory() as temp:
        apk = args.apk
        if apk is None:
            run("gh", "release", "download", args.tag, "--repo", REPO, "--pattern", name, "--dir", temp)
            apk = Path(temp) / name
        verified = verify_apk(apk, assets[0], args.tag, args.build_tools)
    update(ROOT, verified, date.fromisoformat(release["published_at"][:10]).isoformat())
    print(f"Verified {name} ({verified['sizeBytes']} bytes, SHA-256 {verified['sha256']}).")


if __name__ == "__main__":
    main()
