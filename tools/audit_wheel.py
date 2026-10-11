"""Fail closed if a MegasMoves wheel includes unexpected content.

No wheel contents, credentials, or personal data are printed to CI logs.
"""
from __future__ import annotations

import configparser
from email.parser import Parser
import sys
from pathlib import Path
from zipfile import ZipFile


def check(dist: Path) -> int:
    wheels = sorted(dist.glob("*.whl"))
    if len(wheels) != 1:
        print("Package gate failed: expected exactly one MegasMoves wheel.")
        return 1

    with ZipFile(wheels[0]) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos if not info.is_dir()]
        if len(names) != len(set(names)):
            print("Package gate failed: duplicate archive entries.")
            return 1
        metadata_dirs = {
            name.split("/", 1)[0] for name in names
            if ".dist-info/" in name
        }
        if len(metadata_dirs) != 1 or not next(iter(metadata_dirs)).startswith("megasmoves-"):
            print("Package gate failed: unexpected distribution metadata.")
            return 1
        dist_info = next(iter(metadata_dirs))
        for info in infos:
            name = info.filename
            if (name.startswith("/") or "\\" in name or ".." in Path(name).parts or
                    (not name.startswith("megas_community/") and
                     not name.startswith(dist_info + "/")) or
                    info.file_size > 2_000_000):
                print("Package gate failed: unexpected archive path, type, or size.")
                return 1
            if Path(name).suffix.lower() in {".env", ".pem", ".p12", ".pfx", ".jks", ".db", ".sqlite", ".key", ".exe"}:
                print("Package gate failed: forbidden file type.")
                return 1
        required = {dist_info + "/METADATA", dist_info + "/entry_points.txt"}
        if not required.issubset(set(names)):
            print("Package gate failed: package metadata or console scripts missing.")
            return 1
        metadata = Parser().parsestr(archive.read(dist_info + "/METADATA").decode("utf-8"))
        if metadata.get("Name", "").lower() != "megasmoves":
            print("Package gate failed: PyPI distribution name mismatch.")
            return 1
        cfg = configparser.ConfigParser()
        cfg.read_string(archive.read(dist_info + "/entry_points.txt").decode("utf-8"))
        console = dict(cfg.items("console_scripts")) if cfg.has_section("console_scripts") else {}
        if console != {"megas": "megas_community.cli:main"}:
            print("Package gate failed: unexpected entrypoints: only the megas console command is allowed.")
            return 1
    print("PASS: verified wheel scope and the megas console entrypoint; no additional runtime files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(check(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dist")))
