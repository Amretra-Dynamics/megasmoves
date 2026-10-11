from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


RUNTIME_MANIFEST_NAME = "megas-browser-runtime.json"
RUNTIME_MANIFEST_VERSION = 1
MAX_RUNTIME_FILES = 20_000
MAX_RUNTIME_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_RUNTIME_RELATIVE_PATH = 600


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _runtime_files(root: Path) -> list[Path]:
    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.name != RUNTIME_MANIFEST_NAME
    ]
    files.sort(key=lambda path: path.relative_to(root).as_posix().lower())
    if not files:
        raise ValueError("dedicated browser runtime is empty")
    if len(files) > MAX_RUNTIME_FILES:
        raise ValueError("dedicated browser runtime exceeds the file-count safety bound")
    return files


def _safe_relative(root: Path, path: Path) -> str:
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    if not relative or len(relative) > MAX_RUNTIME_RELATIVE_PATH:
        raise ValueError("browser runtime contains an invalid relative path")
    if relative.startswith("/") or relative.startswith("../") or "/../" in f"/{relative}/":
        raise ValueError("browser runtime relative path escapes its root")
    return relative


def build_runtime_manifest(
    root: Path,
    *,
    playwright_version: str,
    executable: Path,
) -> dict[str, Any]:
    root = root.resolve()
    executable = executable.resolve()
    executable_relative = _safe_relative(root, executable)
    rows: list[dict[str, Any]] = []
    aggregate = hashlib.sha256()
    total_size = 0
    executable_sha256 = ""

    for path in _runtime_files(root):
        relative = _safe_relative(root, path)
        size = path.stat().st_size
        digest = sha256_file(path)
        total_size += size
        aggregate.update(relative.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(str(size).encode("ascii"))
        aggregate.update(b"\0")
        aggregate.update(digest.encode("ascii"))
        aggregate.update(b"\n")
        rows.append({"path": relative, "size_bytes": size, "sha256": digest})
        if relative == executable_relative:
            executable_sha256 = digest

    if not executable_sha256:
        raise ValueError("dedicated browser executable is not part of the runtime payload")

    return {
        "format": "megas-browser-runtime",
        "version": RUNTIME_MANIFEST_VERSION,
        "playwright_version": str(playwright_version),
        "browser": "chromium",
        "dedicated": True,
        "daily_browser_attachment": False,
        "file_count": len(rows),
        "size_bytes": total_size,
        "payload_sha256": aggregate.hexdigest(),
        "executable": executable_relative,
        "executable_sha256": executable_sha256,
        "files": rows,
    }


def write_runtime_manifest(
    root: Path,
    *,
    playwright_version: str,
    executable: Path,
) -> dict[str, Any]:
    manifest = build_runtime_manifest(
        root,
        playwright_version=playwright_version,
        executable=executable,
    )
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_RUNTIME_MANIFEST_BYTES:
        raise ValueError("dedicated browser runtime manifest exceeds its size bound")
    path = root / RUNTIME_MANIFEST_NAME
    path.write_bytes(encoded)
    return manifest


def load_runtime_manifest(root: Path) -> dict[str, Any]:
    path = root / RUNTIME_MANIFEST_NAME
    if not path.is_file():
        raise FileNotFoundError("dedicated browser runtime manifest is missing")
    if path.stat().st_size <= 0 or path.stat().st_size > MAX_RUNTIME_MANIFEST_BYTES:
        raise ValueError("dedicated browser runtime manifest has an invalid size")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("dedicated browser runtime manifest must be an object")
    if value.get("format") != "megas-browser-runtime":
        raise ValueError("dedicated browser runtime manifest format is invalid")
    if int(value.get("version") or 0) != RUNTIME_MANIFEST_VERSION:
        raise ValueError("dedicated browser runtime manifest version is unsupported")
    if value.get("browser") != "chromium" or value.get("dedicated") is not True:
        raise ValueError("dedicated browser runtime manifest identity is invalid")
    if value.get("daily_browser_attachment") is not False:
        raise ValueError("browser runtime manifest may not permit daily-browser attachment")
    return value


def verify_runtime_manifest(
    root: Path,
    *,
    expected_playwright_version: str | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    manifest = load_runtime_manifest(root)
    if expected_playwright_version is not None and str(manifest.get("playwright_version") or "") != expected_playwright_version:
        raise ValueError("dedicated browser runtime Playwright version does not match the product pin")

    rows = manifest.get("files")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_RUNTIME_FILES:
        raise ValueError("dedicated browser runtime manifest file list is invalid")

    expected: dict[str, tuple[int, str]] = {}
    aggregate = hashlib.sha256()
    total_size = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("dedicated browser runtime manifest contains a malformed file row")
        relative = str(row.get("path") or "")
        if not relative or len(relative) > MAX_RUNTIME_RELATIVE_PATH or relative in expected:
            raise ValueError("dedicated browser runtime manifest contains an invalid file path")
        candidate = (root / Path(relative)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("dedicated browser runtime manifest path escapes the runtime root") from exc
        if not candidate.is_file():
            raise FileNotFoundError("dedicated browser runtime payload is incomplete")
        raw_size = row.get("size_bytes")
        expected_size = int(raw_size) if raw_size is not None else -1
        expected_hash = str(row.get("sha256") or "")
        if expected_size < 0 or len(expected_hash) != 64:
            raise ValueError("dedicated browser runtime manifest file metadata is invalid")
        actual_size = candidate.stat().st_size
        if actual_size != expected_size:
            raise ValueError("dedicated browser runtime file size verification failed")
        actual_hash = sha256_file(candidate)
        if actual_hash != expected_hash:
            raise ValueError("dedicated browser runtime file hash verification failed")
        expected[relative] = (expected_size, expected_hash)
        total_size += actual_size
        aggregate.update(relative.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(str(actual_size).encode("ascii"))
        aggregate.update(b"\0")
        aggregate.update(actual_hash.encode("ascii"))
        aggregate.update(b"\n")

    actual_paths = {
        _safe_relative(root, path)
        for path in _runtime_files(root)
    }
    if actual_paths != set(expected):
        raise ValueError("dedicated browser runtime contains files outside its signed build manifest")
    if total_size != int(manifest.get("size_bytes") or -1):
        raise ValueError("dedicated browser runtime total size verification failed")
    if aggregate.hexdigest() != str(manifest.get("payload_sha256") or ""):
        raise ValueError("dedicated browser runtime aggregate verification failed")

    executable_relative = str(manifest.get("executable") or "")
    executable = (root / Path(executable_relative)).resolve()
    try:
        executable.relative_to(root)
    except ValueError as exc:
        raise ValueError("dedicated browser executable escapes the runtime root") from exc
    if not executable.is_file():
        raise FileNotFoundError("dedicated browser executable is missing")
    if sha256_file(executable) != str(manifest.get("executable_sha256") or ""):
        raise ValueError("dedicated browser executable verification failed")

    return {**manifest, "verified": True, "root": str(root), "executable_path": str(executable)}


__all__ = [
    "MAX_RUNTIME_FILES",
    "MAX_RUNTIME_MANIFEST_BYTES",
    "RUNTIME_MANIFEST_NAME",
    "RUNTIME_MANIFEST_VERSION",
    "build_runtime_manifest",
    "load_runtime_manifest",
    "sha256_file",
    "verify_runtime_manifest",
    "write_runtime_manifest",
]
