from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


# 16 MiB canonical encrypted plaintext becomes ~21.4 MiB after AEAD + base64.
# 32 MiB leaves room for JSON formatting/header overhead while keeping the read
# strictly bounded before json.loads/decryption is allowed to allocate further.
MAX_PORTABLE_IMPORT_FILE_BYTES = 32 * 1024 * 1024


def read_bounded_bytes(
    path: str | os.PathLike[str],
    *,
    max_bytes: int = MAX_PORTABLE_IMPORT_FILE_BYTES,
) -> bytes:
    """Read at most ``max_bytes`` from a local regular file.

    The function checks metadata for fast rejection and also performs a bounded
    max+1 read, so a file that grows after stat still cannot trigger an unbounded
    allocation. Directories and non-regular filesystem objects are rejected.
    """

    candidate = Path(path).expanduser()
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    try:
        stat = candidate.stat()
    except OSError:
        raise
    if not candidate.is_file():
        raise ValueError(f"import path must be a regular file: {candidate}")
    if stat.st_size > max_bytes:
        raise ValueError(
            f"import file exceeds the {max_bytes // (1024 * 1024)} MiB safety limit"
        )
    with candidate.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(
            f"import file exceeds the {max_bytes // (1024 * 1024)} MiB safety limit"
        )
    return data


@contextmanager
def staged_bounded_import(
    path: str | os.PathLike[str],
    *,
    max_bytes: int = MAX_PORTABLE_IMPORT_FILE_BYTES,
) -> Iterator[Path]:
    """Snapshot a bounded import to a private temporary file for downstream parsing.

    This closes the stat/check/use gap at the public CLI boundary: after this
    function snapshots the input, later parsing sees the exact bounded bytes that
    were approved rather than an attacker-replaceable original path.
    """

    data = read_bounded_bytes(path, max_bytes=max_bytes)
    with tempfile.TemporaryDirectory(prefix="megas-import-") as directory:
        staged = Path(directory) / "bundle.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(staged, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        yield staged


__all__ = [
    "MAX_PORTABLE_IMPORT_FILE_BYTES",
    "read_bounded_bytes",
    "staged_bounded_import",
]
