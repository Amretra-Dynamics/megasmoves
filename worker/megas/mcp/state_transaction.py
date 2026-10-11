from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any


TRANSACTION_VERSION = 1
TRANSACTION_DIR = "transactions"
ACTIVE_JOURNAL = "active_state_transaction.json"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fsync_dir(path: Path) -> None:
    """Best-effort directory durability after rename/unlink operations."""

    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _atomic_bytes(path: Path, value: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, mode)
        except OSError:
            pass
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _atomic_bytes(path, payload)


def _safe_relative(root: Path, target: Path) -> str:
    root = root.resolve()
    target = target.resolve()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"transaction target escapes Megas state root: {target}") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"invalid transaction target: {target}")
    return relative.as_posix()


def _journal_path(root: Path) -> Path:
    return root / ACTIVE_JOURNAL


def _transaction_root(root: Path) -> Path:
    return root / TRANSACTION_DIR


def _read_journal(root: Path) -> dict[str, Any] | None:
    path = _journal_path(root)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Megas active state transaction journal is malformed")
    if int(raw.get("version") or 0) != TRANSACTION_VERSION:
        raise ValueError("unsupported Megas state transaction journal version")
    transaction_id = str(raw.get("transaction_id") or "")
    if not transaction_id or any(ch not in "0123456789abcdef" for ch in transaction_id):
        raise ValueError("Megas state transaction journal has an invalid transaction ID")
    files = raw.get("files")
    if not isinstance(files, list):
        raise ValueError("Megas state transaction journal is missing file records")
    return raw


def _cleanup_transaction(root: Path, transaction_id: str) -> None:
    try:
        _journal_path(root).unlink(missing_ok=True)
    finally:
        shutil.rmtree(_transaction_root(root) / transaction_id, ignore_errors=True)
        _fsync_dir(root)


def recover_pending_transaction(
    root: Path,
    *,
    force: bool = False,
) -> dict[str, Any] | None:
    """Roll back an abandoned multi-file state transaction to its pre-commit snapshot.

    If the recorded owner process is still alive, recovery refuses to interfere unless
    the caller explicitly uses ``force=True``. The force path is reserved for the
    transaction implementation's own synchronous exception rollback.
    """

    root = root.resolve()
    journal = _read_journal(root)
    if journal is None:
        return None
    owner_pid = int(journal.get("owner_pid") or 0)
    if owner_pid and _pid_alive(owner_pid) and not force:
        raise RuntimeError(
            f"Megas state transaction {journal.get('transaction_id')} is still active in process {owner_pid}"
        )

    transaction_id = str(journal["transaction_id"])
    tx_dir = _transaction_root(root) / transaction_id
    restored: list[str] = []
    removed: list[str] = []
    for row in journal["files"]:
        if not isinstance(row, dict):
            raise ValueError("Megas state transaction journal contains a malformed file record")
        relative = str(row.get("path") or "")
        target = root / relative
        if _safe_relative(root, target) != relative:
            raise ValueError("Megas state transaction journal contains an unsafe target path")
        existed = row.get("existed") is True
        backup_name = str(row.get("backup") or "")
        if existed:
            if not backup_name:
                raise ValueError("Megas state transaction journal is missing a required backup")
            backup = tx_dir / backup_name
            if not backup.is_file():
                raise RuntimeError(f"Megas state transaction backup is missing: {backup_name}")
            value = backup.read_bytes()
            expected = str(row.get("before_sha256") or "")
            if expected and _sha256_bytes(value) != expected:
                raise RuntimeError(f"Megas state transaction backup checksum mismatch: {relative}")
            _atomic_bytes(target, value)
            restored.append(relative)
        else:
            target.unlink(missing_ok=True)
            _fsync_dir(target.parent)
            removed.append(relative)

    _cleanup_transaction(root, transaction_id)
    return {
        "recovered": True,
        "transaction_id": transaction_id,
        "purpose": str(journal.get("purpose") or "state-update"),
        "restored": restored,
        "removed": removed,
    }


def apply_state_transaction(
    root: Path,
    replacements: dict[Path, bytes | None],
    *,
    purpose: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically apply a logical multi-file state update with crash rollback evidence.

    The filesystem cannot rename several independent files in one syscall. Megas therefore
    snapshots every target and publishes an fsync'd journal before the first replacement.
    If the process dies before the journal is cleared, the next user-facing invocation
    restores the complete pre-transaction snapshot instead of accepting partial state.
    """

    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    pending = recover_pending_transaction(root)
    if pending is not None:
        # The abandoned operation has already been rolled back. Continue from clean state.
        pass
    if _journal_path(root).exists():
        raise RuntimeError("Megas state transaction journal already exists")
    if not replacements:
        return {"changed": False, "transaction_id": None, "files": []}

    normalized: list[tuple[Path, str, bytes | None]] = []
    seen: set[str] = set()
    for target, value in replacements.items():
        relative = _safe_relative(root, target)
        if relative in seen:
            raise ValueError(f"duplicate transaction target: {relative}")
        seen.add(relative)
        normalized.append((target.resolve(), relative, value))
    normalized.sort(key=lambda row: row[1])

    transaction_id = uuid.uuid4().hex
    tx_dir = _transaction_root(root) / transaction_id
    tx_dir.mkdir(parents=True, exist_ok=False)
    files: list[dict[str, Any]] = []
    try:
        for index, (target, relative, desired) in enumerate(normalized):
            existed = target.is_file()
            before = target.read_bytes() if existed else None
            backup_name = f"before-{index:04d}.bin" if existed else ""
            stage_name = f"after-{index:04d}.bin" if desired is not None else ""
            if before is not None:
                _atomic_bytes(tx_dir / backup_name, before)
            if desired is not None:
                _atomic_bytes(tx_dir / stage_name, desired)
            files.append(
                {
                    "path": relative,
                    "existed": existed,
                    "backup": backup_name,
                    "before_sha256": _sha256_bytes(before) if before is not None else None,
                    "stage": stage_name,
                    "after_sha256": _sha256_bytes(desired) if desired is not None else None,
                    "delete": desired is None,
                }
            )

        journal = {
            "version": TRANSACTION_VERSION,
            "transaction_id": transaction_id,
            "purpose": purpose,
            "owner_pid": os.getpid(),
            "metadata": dict(metadata or {}),
            "files": files,
        }
        _atomic_json(_journal_path(root), journal)
        _fsync_dir(tx_dir)
        _fsync_dir(root)

        try:
            for row in files:
                target = root / str(row["path"])
                if row["delete"]:
                    target.unlink(missing_ok=True)
                    _fsync_dir(target.parent)
                    continue
                stage = tx_dir / str(row["stage"])
                if not stage.is_file():
                    raise RuntimeError(f"Megas transaction stage is missing: {row['path']}")
                value = stage.read_bytes()
                expected = str(row.get("after_sha256") or "")
                if expected and _sha256_bytes(value) != expected:
                    raise RuntimeError(f"Megas transaction stage checksum mismatch: {row['path']}")
                _atomic_bytes(target, value)
        except BaseException:
            # Ordinary Python failures are rolled back synchronously. A hard kill/power loss
            # leaves the journal behind for recover_pending_transaction on the next invocation.
            recover_pending_transaction(root, force=True)
            raise

        _cleanup_transaction(root, transaction_id)
        return {
            "changed": True,
            "transaction_id": transaction_id,
            "files": [str(row["path"]) for row in files],
        }
    except BaseException:
        if not _journal_path(root).exists():
            shutil.rmtree(tx_dir, ignore_errors=True)
        raise


__all__ = [
    "ACTIVE_JOURNAL",
    "TRANSACTION_DIR",
    "TRANSACTION_VERSION",
    "apply_state_transaction",
    "recover_pending_transaction",
]
