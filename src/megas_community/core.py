"""Explicitly trusted local workspace discovery.

Only the human CLI can add/remove roots. The MCP transport is read-only.
No subprocesses, arbitrary paths, network calls, or remote code execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any


MAX_MANIFEST_BYTES = 256 * 1024


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_manifest(root: Path, filename: str) -> dict[str, Any] | None:
    candidate = root / filename
    try:
        resolved = candidate.resolve(strict=True)
        if not _is_within(resolved, root) or not resolved.is_file():
            return None
        if resolved.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        with resolved.open("rb") as stream:
            content = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(content) > MAX_MANIFEST_BYTES:
            return None
        parsed = tomllib.loads(content.decode("utf-8")) if filename.endswith(".toml") else json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        return None


def inspect_workspace(root: Path) -> dict[str, Any]:
    """Inspect recognized metadata from one previously approved root."""
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("trusted workspace is no longer a directory")
    npm = _read_manifest(root, "package.json")
    python = _read_manifest(root, "pyproject.toml")
    kinds = []
    if npm is not None:
        kinds.append("node")
    if python is not None:
        kinds.append("python")
    scripts = npm.get("scripts", {}) if isinstance(npm, dict) else {}
    scripts = scripts if isinstance(scripts, dict) else {}
    safe_scripts = sorted(k for k in scripts if k in {"start", "dev", "test", "build"} and isinstance(k, str))
    return {
        "path": str(root),
        "name": root.name,
        "stack": kinds,
        "git": (root / ".git").exists(),
        "node_scripts": safe_scripts,
        "note": "Metadata inspection only; no commands were run.",
    }


class TrustStore:
    """Persistent, explicitly approved paths, stored outside project directories."""

    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home is not None else Path.home() / ".megasmoves-community"
        self.config = self.home / "trusted-workspaces.json"

    def _load(self) -> list[dict[str, str]]:
        if self.config.is_symlink():
            raise PermissionError("refusing symlinked trust configuration")
        try:
            if self.config.stat().st_size > MAX_MANIFEST_BYTES:
                raise ValueError("trust configuration too large")
            value = json.loads(self.config.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        if not isinstance(value, dict) or value.get("format") != 1 or not isinstance(value.get("roots"), list):
            raise ValueError("invalid trust configuration")
        roots = value["roots"]
        if len(roots) > 100 or any(
            not isinstance(x, dict)
            or not isinstance(x.get("id"), str)
            or not isinstance(x.get("path"), str)
            for x in roots
        ):
            raise ValueError("invalid trust configuration entries")
        return roots

    def _save(self, roots: list[dict[str, str]]) -> None:
        if self.config.is_symlink():
            raise PermissionError("refusing symlinked trust configuration")
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.home.chmod(0o700)
        fd, tmp = tempfile.mkstemp(prefix=".trust-", dir=self.home)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"format": 1, "roots": roots}, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.config)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def add(self, raw_path: str) -> dict[str, str]:
        if not raw_path or not raw_path.strip():
            raise ValueError("workspace path is required")
        root = Path(raw_path).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace must be an existing directory")
        record = {"id": hashlib.sha256(str(root).encode()).hexdigest()[:16], "path": str(root)}
        roots = self._load()
        if any(item["id"] == record["id"] for item in roots):
            return record
        if len(roots) >= 100:
            raise ValueError("workspace limit reached")
        roots.append(record)
        self._save(roots)
        return record

    def list(self) -> list[dict[str, str]]:
        return list(self._load())

    def remove(self, workspace_id: str) -> bool:
        roots = self._load()
        remaining = [r for r in roots if r["id"] != workspace_id]
        if len(remaining) == len(roots):
            return False
        self._save(remaining)
        return True

    def check(self, workspace_id: str) -> dict[str, Any]:
        for record in self._load():
            if record["id"] == workspace_id:
                root = Path(record["path"])
                if not root.is_dir():
                    raise ValueError("trusted workspace no longer exists")
                # Re-resolve the stored path so symlink retargeting can't
                # silently transfer trust to another location.
                if str(root.resolve(strict=True)) != record["path"]:
                    raise PermissionError("trusted workspace path has changed")
                return {"id": workspace_id, **inspect_workspace(root)}
        raise ValueError("workspace is not trusted")
