from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
from typing import Iterator

from megas.browser_runtime_manifest import (
    RUNTIME_MANIFEST_NAME,
    load_runtime_manifest,
    verify_runtime_manifest,
)
from megas.operate.gate import is_operate_enabled


PLAYWRIGHT_VERSION = "1.61.0"


@dataclass(frozen=True)
class BrowserCapabilityReport:
    available: bool
    executable: str | None
    browser: str | None
    reasons: tuple[str, ...]
    runtime_root: str | None = None
    dedicated: bool = True
    playwright_version: str = PLAYWRIGHT_VERSION
    runtime_verified: bool = False


def _configured_runtime_root() -> Path:
    configured = os.getenv("MEGAS_BROWSER_RUNTIME_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if platform.system() == "Windows":
        local = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return (local / "Amretra" / "MegasMoves" / "runtime" / "browser").resolve()
    # Non-Windows is retained for tests/development only. The Windows product
    # owns the production browser runtime directory.
    root = Path(os.getenv("MEGAS_HOME", Path.home() / ".megas")).expanduser().resolve()
    return root / "runtime" / "browser"


def _bundled_runtime_root() -> Path | None:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if not bundle_root:
        return None
    candidate = Path(str(bundle_root)).resolve() / "browser-runtime"
    return candidate if (candidate / RUNTIME_MANIFEST_NAME).is_file() else None


@contextmanager
def _runtime_install_lock(parent: Path) -> Iterator[None]:
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / ".browser-runtime.lock"
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _verified_payload(root: Path) -> dict[str, object] | None:
    try:
        return verify_runtime_manifest(root, expected_playwright_version=PLAYWRIGHT_VERSION)
    except (FileNotFoundError, ValueError, OSError):
        return None


def _provision_bundled_runtime(target: Path, bundled: Path) -> Path:
    bundled_manifest = verify_runtime_manifest(
        bundled,
        expected_playwright_version=PLAYWRIGHT_VERSION,
    )
    current = _verified_payload(target)
    if current and current.get("payload_sha256") == bundled_manifest.get("payload_sha256"):
        return target

    parent = target.parent
    with _runtime_install_lock(parent):
        current = _verified_payload(target)
        if current and current.get("payload_sha256") == bundled_manifest.get("payload_sha256"):
            return target

        staging = Path(tempfile.mkdtemp(prefix=".browser-staging-", dir=str(parent))).resolve()
        backup = parent / f".browser-previous-{os.getpid()}"
        try:
            shutil.copytree(bundled, staging, dirs_exist_ok=True, copy_function=shutil.copy2)
            staged = verify_runtime_manifest(
                staging,
                expected_playwright_version=PLAYWRIGHT_VERSION,
            )
            if staged.get("payload_sha256") != bundled_manifest.get("payload_sha256"):
                raise ValueError("staged browser runtime does not match the bundled payload")

            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            if target.exists():
                os.replace(target, backup)
            try:
                os.replace(staging, target)
            except BaseException:
                if backup.exists() and not target.exists():
                    os.replace(backup, target)
                raise
            shutil.rmtree(backup, ignore_errors=True)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if backup.exists() and target.exists():
                shutil.rmtree(backup, ignore_errors=True)

    verified = verify_runtime_manifest(
        target,
        expected_playwright_version=PLAYWRIGHT_VERSION,
    )
    if verified.get("payload_sha256") != bundled_manifest.get("payload_sha256"):
        raise ValueError("installed browser runtime does not match the bundled payload")
    return target


def browser_runtime_root() -> Path:
    target = _configured_runtime_root()
    # An explicitly configured runtime is useful for development and the WinUI
    # extracted-content path. If it already carries a Megas manifest, it is used
    # in-place after integrity verification instead of being copied elsewhere.
    if os.getenv("MEGAS_BROWSER_RUNTIME_DIR", "").strip():
        target.mkdir(parents=True, exist_ok=True)
        if (target / RUNTIME_MANIFEST_NAME).is_file():
            verify_runtime_manifest(target, expected_playwright_version=PLAYWRIGHT_VERSION)
        return target

    bundled = _bundled_runtime_root()
    if bundled is not None:
        return _provision_bundled_runtime(target, bundled)
    target.mkdir(parents=True, exist_ok=True)
    return target


def configure_playwright_runtime() -> Path:
    root = browser_runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    # Playwright itself owns the engine implementation. Megas owns where the
    # validated runtime lives and never silently falls back to daily Chrome.
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
    return root


def find_browser_executable(preferred: str = "chromium") -> Path | None:
    """Return the dedicated Playwright Chromium executable, never system Chrome.

    Production/frozen builds require the Megas runtime manifest. Development
    builds may use a Playwright-installed Chromium only when its executable is
    physically inside the configured Megas runtime root.
    """
    browser = preferred.strip().lower()
    if browser not in {"chromium", "chrome"}:
        return None
    try:
        root = configure_playwright_runtime()
    except (FileNotFoundError, ValueError, OSError):
        return None

    if (root / RUNTIME_MANIFEST_NAME).is_file():
        try:
            verified = verify_runtime_manifest(
                root,
                expected_playwright_version=PLAYWRIGHT_VERSION,
            )
            return Path(str(verified["executable_path"]))
        except (FileNotFoundError, ValueError, OSError, KeyError):
            return None

    if getattr(sys, "frozen", False):
        # A production frozen core must never fall back to an ambient browser.
        return None
    try:
        from playwright.sync_api import sync_playwright
    except (ImportError, ModuleNotFoundError):
        return None
    try:
        with sync_playwright() as playwright:
            candidate = Path(playwright.chromium.executable_path).resolve()
    except Exception:
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def browser_capability_report(preferred: str = "chromium") -> BrowserCapabilityReport:
    reasons: list[str] = []
    if not is_operate_enabled():
        reasons.append("operate_disabled_by_owner")
    try:
        playwright_available = importlib.util.find_spec("playwright.async_api") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        playwright_available = False
    if not playwright_available:
        reasons.append("playwright_runtime_unavailable")

    runtime_verified = False
    try:
        root = configure_playwright_runtime()
        if (root / RUNTIME_MANIFEST_NAME).is_file():
            verify_runtime_manifest(root, expected_playwright_version=PLAYWRIGHT_VERSION)
            runtime_verified = True
        elif getattr(sys, "frozen", False):
            reasons.append("browser_runtime_manifest_missing")
    except (FileNotFoundError, ValueError, OSError):
        root = _configured_runtime_root()
        reasons.append("browser_runtime_integrity_failed")

    executable = find_browser_executable(preferred) if playwright_available else None
    if not executable:
        reasons.append("dedicated_browser_runtime_unavailable")
    return BrowserCapabilityReport(
        available=not reasons,
        executable=str(executable) if executable else None,
        browser="chromium" if executable else None,
        reasons=tuple(dict.fromkeys(reasons)),
        runtime_root=str(root),
        dedicated=True,
        playwright_version=PLAYWRIGHT_VERSION,
        runtime_verified=runtime_verified,
    )


__all__ = [
    "BrowserCapabilityReport",
    "PLAYWRIGHT_VERSION",
    "browser_capability_report",
    "browser_runtime_root",
    "configure_playwright_runtime",
    "find_browser_executable",
]
