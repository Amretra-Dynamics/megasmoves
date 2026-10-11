from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Optional

from megas.adapters.base import prefer_console_free_tool_path, subprocess_hide_window_kwargs


MAX_CHECKPOINT_BYTES = 100 * 1024 * 1024
MAX_CHECKPOINT_EXPANDED_BYTES = 500 * 1024 * 1024
MAX_CHECKPOINT_ENTRIES = 10_000
EXCLUDED_PARTS = {
    ".git",
    "node_modules",
    "venv",
    ".venv",
    ".pytest_cache",
    "build",
    "outputs",
}


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _workspace_files(workspace: Path) -> list[Path]:
    if not (workspace / ".git").exists():
        files = []
        for candidate in workspace.rglob("*"):
            relative = candidate.relative_to(workspace)
            if any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            if candidate.is_file() and not candidate.is_symlink():
                files.append(relative)
        return sorted(set(files))

    commands = [
        ["git", "diff", "--name-only", "-z"],
        ["git", "diff", "--cached", "--name-only", "-z"],
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
    ]
    relative_paths: set[Path] = set()
    for command in commands:
        result = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            check=True,
            timeout=15,
            env=prefer_console_free_tool_path(),
            **subprocess_hide_window_kwargs(),
        )
        for raw_path in result.stdout.split(b"\x00"):
            if not raw_path:
                continue
            text = raw_path.decode("utf-8", errors="strict")
            relative = Path(text)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or any(part in EXCLUDED_PARTS for part in relative.parts)
            ):
                continue
            candidate = (workspace / relative).resolve()
            if (
                _inside(workspace, candidate)
                and candidate.is_file()
                and not candidate.is_symlink()
            ):
                relative_paths.add(relative)
    return sorted(relative_paths)


def save_workspace_checkpoint(workspace_path: str) -> bytes:
    workspace = Path(workspace_path).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError("Checkpoint workspace does not exist.")

    files_to_pack = _workspace_files(workspace)
    total_size = sum(
        (workspace / relative).stat().st_size for relative in files_to_pack
    )
    if (
        len(files_to_pack) > MAX_CHECKPOINT_ENTRIES
        or total_size > MAX_CHECKPOINT_EXPANDED_BYTES
    ):
        raise ValueError("Checkpoint exceeds the allowed entry or expanded-size limit.")

    branch = ""
    if (workspace / ".git").exists():
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            env=prefer_console_free_tool_path(),
            **subprocess_hide_window_kwargs(),
        )
        branch = result.stdout.strip()[:200]

    output = io.BytesIO()
    with tarfile.open(
        fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT
    ) as archive:
        metadata = branch.encode("utf-8")
        info = tarfile.TarInfo(name=".megas_checkpoint_meta")
        info.size = len(metadata)
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(metadata))
        for relative in files_to_pack:
            source = (workspace / relative).resolve()
            archive.add(source, arcname=relative.as_posix(), recursive=False)

    payload = output.getvalue()
    if len(payload) > MAX_CHECKPOINT_BYTES:
        raise ValueError("Compressed checkpoint exceeds the 100 MB limit.")
    return payload


def _safe_member_path(workspace: Path, member: tarfile.TarInfo) -> Path:
    normalized = member.name.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Checkpoint contains an unsafe path.")
    if any(part.lower() == ".git" for part in relative.parts):
        raise ValueError("Checkpoint cannot modify Git metadata.")
    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
        raise ValueError("Checkpoint contains links or device entries.")
    if not (member.isfile() or member.isdir()):
        raise ValueError("Checkpoint contains an unsupported entry type.")
    target = (workspace / Path(*relative.parts)).resolve()
    if not _inside(workspace, target):
        raise ValueError("Checkpoint contains an unsafe path.")
    return target


def restore_workspace_checkpoint(workspace_path: str, payload: bytes) -> Optional[str]:
    if not payload:
        raise ValueError("Checkpoint payload is empty.")
    if len(payload) > MAX_CHECKPOINT_BYTES:
        raise ValueError("Compressed checkpoint exceeds the 100 MB limit.")

    workspace = Path(workspace_path).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    branch: Optional[str] = None
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_CHECKPOINT_ENTRIES:
                raise ValueError("Checkpoint contains too many entries.")
            expanded_size = sum(max(0, member.size) for member in members)
            if expanded_size > MAX_CHECKPOINT_EXPANDED_BYTES:
                raise ValueError("Checkpoint expands beyond the allowed size.")

            seen_paths: set[str] = set()
            metadata_seen = False
            for member in members:
                normalized_path = member.name.replace("\\", "/")
                if normalized_path in seen_paths:
                    raise ValueError("Checkpoint contains duplicate paths.")
                seen_paths.add(normalized_path)
                if member.name == ".megas_checkpoint_meta":
                    if metadata_seen:
                        raise ValueError("Checkpoint metadata is ambiguous.")
                    metadata_seen = True
                    if member.size > 256 or not member.isfile():
                        raise ValueError("Checkpoint metadata is invalid.")
                    stream = archive.extractfile(member)
                    if stream is not None:
                        branch = (
                            stream.read().decode("utf-8", errors="strict").strip()
                            or None
                        )
                    continue

                target = _safe_member_path(workspace, member)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("Checkpoint file content is missing.")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=target.parent, prefix=".megas-restore-", delete=False
                    ) as temporary:
                        temporary_path = Path(temporary.name)
                        shutil.copyfileobj(source, temporary, length=64 * 1024)
                    os.chmod(temporary_path, 0o600)
                    os.replace(temporary_path, target)
                    temporary_path = None
                finally:
                    if temporary_path is not None:
                        temporary_path.unlink(missing_ok=True)
            if not metadata_seen:
                raise ValueError("Checkpoint metadata is missing.")
    except (tarfile.TarError, UnicodeDecodeError) as exc:
        raise ValueError("Checkpoint archive is invalid.") from exc

    return branch
