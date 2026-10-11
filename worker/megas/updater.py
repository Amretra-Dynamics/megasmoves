from __future__ import annotations

import hashlib
from importlib import metadata
import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import __version__
from .adapters.base import subprocess_hide_window_kwargs, windows_subprocess_kwargs
from .config import WorkerConfig


MAX_CLI_ARTIFACT_BYTES = 250 * 1024 * 1024


def platform_id() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "amd64" if machine in {"amd64", "x86_64"} else ""
    operating_system = {"windows": "windows", "linux": "linux", "darwin": "macos"}.get(system, "")
    if not architecture or not operating_system:
        raise RuntimeError(f"Megas updates are not published for {platform.system()} {platform.machine()}.")
    return f"{operating_system}-{architecture}"


def current_version() -> str:
    # Frozen product binary must report the shipped package version, not a
    # leftover editable install metadata from the build machine.
    if getattr(sys, "frozen", False):
        return __version__
    try:
        return metadata.version("megas")
    except metadata.PackageNotFoundError:
        return __version__


def product_install_binary() -> Path | None:
    """Canonical Windows product path from install.ps1 (not PATH order)."""
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    if not local:
        return None
    candidate = Path(local) / "MegasMoves" / "bin" / "megasmoves.exe"
    try:
        if candidate.is_file():
            return candidate.resolve()
    except OSError:
        return None
    return None


def _target_binary() -> Path:
    override = os.getenv("MEGAS_CLI_BINARY_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # Prefer the install.ps1 product location over whatever PATH hit first
    # (pip Scripts\\megas.exe often shadows and never receives updates).
    product = product_install_binary()
    if product is not None:
        return product
    if not getattr(sys, "frozen", False):
        raise RuntimeError("This development checkout is not a standalone Megas CLI build.")
    return Path(sys.executable).resolve()


def repair_windows_product_path(install_bin: Path | None = None) -> list[str]:
    """Put %LOCALAPPDATA%\\MegasMoves\\bin first on the *user* PATH."""
    repaired: list[str] = []
    if os.name != "nt":
        return repaired
    bin_dir = install_bin or (
        product_install_binary().parent if product_install_binary() else None
    )
    if bin_dir is None or not bin_dir.is_dir():
        return repaired
    bin_s = str(bin_dir)
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
        ) as key:
            try:
                raw, _ = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                raw = ""
            parts = [p for p in str(raw or "").split(";") if p and p.strip()]
            # Drop duplicates of product bin, then prepend.
            parts = [p for p in parts if p.rstrip("\\/").lower() != bin_s.rstrip("\\/").lower()]
            new_path = ";".join([bin_s] + parts)
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
            repaired.append(bin_s)
        # Broadcast WM_SETTINGCHANGE so new shells see PATH without logoff.
        try:
            import ctypes

            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None
            )
        except Exception:
            pass
    except Exception:
        pass
    # Session PATH for this process / children.
    os.environ["Path"] = bin_s + ";" + os.environ.get("Path", "")
    # Ensure megas.cmd shim exists.
    shim = bin_dir / "megas.cmd"
    try:
        if not shim.is_file():
            shim.write_text('@echo off\r\n"%~dp0megasmoves.exe" %*\r\n', encoding="ascii")
            repaired.append(str(shim))
    except OSError:
        pass
    return repaired


def _validate_download_url(backend_url: str, download_url: str) -> None:
    backend = urlparse(backend_url)
    download = urlparse(download_url)
    if download.scheme not in {"https", "http"} or not download.netloc:
        raise RuntimeError("Megas update manifest contains an invalid download URL.")
    if download.netloc.lower() != backend.netloc.lower():
        raise RuntimeError("Megas update download must remain on the configured Backend origin.")
    if backend.scheme == "https" and download.scheme != "https":
        raise RuntimeError("Megas update download attempted to downgrade HTTPS.")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clear_macos_quarantine(path: Path) -> None:
    """curl/download can mark the binary quarantined so launchd cannot exec it."""
    if platform.system() != "Darwin":
        return
    for args in (
        ["xattr", "-dr", "com.apple.quarantine", str(path)],
        ["xattr", "-c", str(path)],
    ):
        try:
            subprocess.run(
                args,
                capture_output=True,
                check=False,
                timeout=15,
                **subprocess_hide_window_kwargs(),
            )
        except Exception:
            pass


def repair_cli_path_links(target: Path | None = None) -> list[str]:
    """Ensure ``megas`` is discoverable on common user PATH locations (Unix).

    Returns paths where links were created/refreshed. No-op on Windows (PATH is
    managed by install.ps1).
    """
    repaired: list[str] = []
    if os.name == "nt":
        return repaired
    try:
        binary = (target or _target_binary()).resolve()
    except RuntimeError:
        return repaired
    if not binary.is_file():
        return repaired
    install_bin = binary.parent
    # Product layout: …/bin/megasmoves + sibling symlink megas → megasmoves
    megas_link = install_bin / "megas"
    if not megas_link.exists():
        try:
            if megas_link.is_symlink() or not megas_link.exists():
                megas_link.unlink(missing_ok=True)
            megas_link.symlink_to(binary.name)
            repaired.append(str(megas_link))
        except OSError:
            pass
    home = Path.home()
    dest_dirs = [
        home / ".local" / "bin",
        home / "bin",
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
    ]
    for dest in dest_dirs:
        try:
            if not dest.exists():
                if dest in {home / ".local" / "bin", home / "bin"}:
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    continue
            if not os.access(dest, os.W_OK):
                continue
            for name in ("megas", "megasmoves"):
                link = dest / name
                try:
                    if link.is_symlink() or link.exists():
                        # Refresh only our links (or broken symlinks).
                        if link.is_symlink():
                            try:
                                resolved = link.resolve()
                            except OSError:
                                resolved = None
                            if resolved is not None and resolved != binary and not str(resolved).endswith("megasmoves"):
                                continue
                        elif link.exists() and not link.is_symlink():
                            continue
                        link.unlink(missing_ok=True)
                    link.symlink_to(binary)
                    repaired.append(str(link))
                except OSError:
                    continue
        except OSError:
            continue
    # Persist env helper for ` . ~/.megasmoves/env `
    state_home = Path(os.environ.get("MEGAS_HOME") or (home / ".megasmoves"))
    try:
        state_home.mkdir(parents=True, exist_ok=True)
        env_file = state_home / "env"
        env_file.write_text(f'export PATH="{install_bin}:$PATH"\n', encoding="utf-8")
        repaired.append(str(env_file))
    except OSError:
        pass
    # Shell profiles (idempotent)
    path_line = f'export PATH="{install_bin}:$PATH"'
    for profile_name in (".zprofile", ".zshrc", ".bash_profile", ".bashrc", ".profile"):
        profile = home / profile_name
        try:
            existing = profile.read_text(encoding="utf-8") if profile.is_file() else ""
            if str(install_bin) in existing:
                continue
            with profile.open("a", encoding="utf-8") as handle:
                handle.write(f"\n# Megas CLI\n{path_line}\n")
            repaired.append(str(profile))
        except OSError:
            continue
    return repaired


def _update_log_path() -> Path:
    log_dir = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "MegasMoves" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_dir = Path(tempfile.gettempdir())
    return log_dir / "update-replace.log"


def _append_update_log(message: str) -> None:
    try:
        path = _update_log_path()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{__import__('datetime').datetime.now().isoformat()}] {message}\n")
    except OSError:
        pass


def _apply_pending_download(target: Path) -> bool:
    """If a previous deferred download still sits next to the binary, apply it now."""
    if os.name != "nt":
        return False
    parent = target.parent
    pending = sorted(
        parent.glob(f".{target.name}.*.download"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    if not pending:
        return False
    download = pending[0]
    _append_update_log(f"apply_pending download={download} target={target}")
    try:
        # Best-effort unlock.
        for name in ("megasmoves", "megas"):
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", f"{name}.exe", "/T"],
                    capture_output=True,
                    check=False,
                    timeout=15,
                    **subprocess_hide_window_kwargs(),
                )
            except Exception:
                pass
        # Copy then replace — works more often than Move-Item under AV.
        tmp_live = target.with_suffix(target.suffix + ".new")
        if tmp_live.exists():
            tmp_live.unlink(missing_ok=True)
        import shutil

        shutil.copy2(download, tmp_live)
        os.replace(tmp_live, target)
        download.unlink(missing_ok=True)
        for extra in pending[1:]:
            extra.unlink(missing_ok=True)
        _append_update_log("apply_pending ok")
        repair_windows_product_path(target.parent)
        return True
    except Exception as exc:
        _append_update_log(f"apply_pending failed: {exc}")
        return False


def _schedule_windows_replace(target: Path, downloaded: Path, restart: bool) -> None:
    """Replace the product binary after this process exits (cmd-based, logged).

    Evidence from production: PowerShell Move-Item deferred helpers often never
    ran or failed while handles were open; leftover ``.megasmoves.exe.*.download``
    files remained and ``megas version`` stayed on the old build.
    """
    log_path = _update_log_path()
    _append_update_log(f"schedule replace target={target} download={downloaded} pid={os.getpid()}")
    # cmd.exe batch is more reliable than a hidden PowerShell DETACHED_PROCESS.
    bat = Path(tempfile.gettempdir()) / f"megas-update-{os.getpid()}.cmd"
    # Escape for batch: double % and quote paths.
    t = str(target)
    d = str(downloaded)
    log = str(log_path)
    lines = [
        "@echo off",
        "setlocal EnableExtensions",
        f'set "TARGET={t}"',
        f'set "DOWNLOAD={d}"',
        f'set "LOG={log}"',
        f"set PIDWAIT={os.getpid()}",
        f'set "RESTART={"1" if restart else "0"}"',
        'echo [%DATE% %TIME%] helper start>>"%LOG%"',
        "rem Wait for updater process to exit (unlocks the exe)",
        "set /a N=0",
        ":waitpid",
        "set /a N+=1",
        'tasklist /FI "PID eq %PIDWAIT%" 2>nul | findstr /I "%PIDWAIT%" >nul',
        "if errorlevel 1 goto unlocked",
        "if %N% GEQ 120 (",
        '  echo [%DATE% %TIME%] timeout waiting for pid %PIDWAIT%>>"%LOG%"',
        "  goto fail",
        ")",
        "ping -n 2 127.0.0.1 >nul",
        "goto waitpid",
        ":unlocked",
        'echo [%DATE% %TIME%] updater exited>>"%LOG%"',
        "rem Kill any Worker still locking the product binary",
        "taskkill /F /IM megasmoves.exe /T >nul 2>&1",
        "taskkill /F /IM megas.exe /T >nul 2>&1",
        "ping -n 2 127.0.0.1 >nul",
        "set /a T=0",
        ":retry",
        "set /a T+=1",
        'if not exist "%DOWNLOAD%" (',
        '  echo [%DATE% %TIME%] download missing>>"%LOG%"',
        "  goto fail",
        ")",
        'copy /Y "%DOWNLOAD%" "%TARGET%.new" >nul',
        "if errorlevel 1 goto retry_wait",
        'move /Y "%TARGET%.new" "%TARGET%" >nul',
        "if errorlevel 1 goto retry_wait",
        'del /F /Q "%DOWNLOAD%" >nul 2>&1',
        'echo [%DATE% %TIME%] replaced ok attempt=%T%>>"%LOG%"',
        "goto post",
        ":retry_wait",
        'echo [%DATE% %TIME%] replace attempt=%T% failed>>"%LOG%"',
        "taskkill /F /IM megasmoves.exe /T >nul 2>&1",
        "taskkill /F /IM megas.exe /T >nul 2>&1",
        "if %T% GEQ 40 goto fail",
        "ping -n 2 127.0.0.1 >nul",
        "goto retry",
        ":post",
        'for %%I in ("%TARGET%") do set "TDIR=%%~dpI"',
        'if exist "%TDIR%megas.exe" if /I not "%TARGET%"=="%TDIR%megas.exe" copy /Y "%TARGET%" "%TDIR%megas.exe" >nul 2>&1',
        "set PYINSTALLER_RESET_ENVIRONMENT=1",
        '"%TARGET%" version >>"%LOG%" 2>&1',
        'if "%RESTART%"=="1" (',
        '  echo [%DATE% %TIME%] install-service>>"%LOG%"',
        '  "%TARGET%" worker install-service >>"%LOG%" 2>&1',
        ")",
        'echo [%DATE% %TIME%] done>>"%LOG%"',
        'del /F /Q "%~f0" >nul 2>&1',
        "exit /b 0",
        ":fail",
        'echo [%DATE% %TIME%] FAIL see log>>"%LOG%"',
        'del /F /Q "%~f0" >nul 2>&1',
        "exit /b 1",
    ]
    bat.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    # Fully detach via cmd start so this process can exit and release the lock.
    # Do NOT use DETACHED_PROCESS alone — helpers often never ran on user machines.
    subprocess.Popen(
        ["cmd.exe", "/c", "start", "/b", "", "cmd.exe", "/c", str(bat)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        cwd=str(target.parent),
        **windows_subprocess_kwargs(new_group=True),
    )
    _append_update_log(f"helper launched bat={bat}")


def update_cli(config: WorkerConfig, *, force: bool = False, restart: bool = False) -> dict[str, str | bool]:
    """Download the latest production CLI binary for this machine.

    Returns keys: updated, version, message, deferred (Windows self-replace).
    Runtime reconnect (service/watchdog/Worker) is owned by ``megas update``.
    """
    if not config.is_paired:
        raise RuntimeError("Connect this machine to Megas before checking for updates.")
    target = _target_binary()
    if not target.is_file():
        raise RuntimeError(f"Megas CLI binary was not found at {target}.")
    # Finish any stuck prior download before requesting another.
    if os.name == "nt":
        repair_windows_product_path(target.parent)
        if _apply_pending_download(target):
            _append_update_log("completed stuck download before network update")
    platform_name = platform_id()
    headers = {"X-Megas-Worker-Token": config.worker_token}
    params = {"platform": platform_name, "worker_id": config.worker_id}
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        response = client.get(f"{config.rest_base_url}/api/v1/cli/releases/latest", params=params, headers=headers)
        response.raise_for_status()
        manifest = response.json()
        version = str(manifest.get("version") or "").strip()
        expected_sha = str(manifest.get("sha256") or "").strip().lower()
        expected_size = int(manifest.get("size_bytes") or 0)
        download_url = str(manifest.get("download_url") or "").strip()
        if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}", version):
            raise RuntimeError("Megas update manifest contains an invalid version.")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise RuntimeError("Megas update manifest contains an invalid SHA-256 digest.")
        if expected_size <= 0 or expected_size > MAX_CLI_ARTIFACT_BYTES:
            raise RuntimeError("Megas update artifact size is invalid.")
        _validate_download_url(config.rest_base_url, download_url)
        if (
            version == current_version()
            and not force
            and _sha256_file(target) == expected_sha
        ):
            _clear_macos_quarantine(target)
            repair_cli_path_links(target)
            return {
                "updated": False,
                "deferred": False,
                "version": version,
                "message": (
                    f"Megas binary is already the latest published release ({version}). "
                    "Refreshing autostart and Worker connection…"
                ),
            }

        downloaded = target.with_name(f".{target.name}.{version}.download")
        digest = hashlib.sha256()
        size = 0
        try:
            with client.stream("GET", download_url, headers=headers) as artifact:
                artifact.raise_for_status()
                with downloaded.open("wb") as output:
                    for chunk in artifact.iter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_CLI_ARTIFACT_BYTES or size > expected_size:
                            raise RuntimeError("Megas update download exceeded its declared size.")
                        digest.update(chunk)
                        output.write(chunk)
            if size != expected_size or digest.hexdigest() != expected_sha:
                raise RuntimeError("Megas update checksum verification failed.")
            # Windows: always schedule post-exit replace for product installs —
            # even if os.replace would work, Worker/watchdog often re-locks the file.
            if os.name == "nt":
                repair_windows_product_path(target.parent)
                _schedule_windows_replace(target, downloaded, restart=True)
                return {
                    "updated": True,
                    "deferred": True,
                    "version": version,
                    "target": str(target),
                    "message": (
                        f"Megas {version} downloaded to finish replacing:\n"
                        f"  {target}\n"
                        "Wait 15 seconds, open a *new* terminal, then run:  megas version\n"
                        f"Log: {_update_log_path()}"
                    ),
                }
            os.chmod(downloaded, target.stat().st_mode)
            os.replace(downloaded, target)
            _clear_macos_quarantine(target)
            repair_cli_path_links(target)
            return {
                "updated": True,
                "deferred": False,
                "version": version,
                "message": f"Megas binary updated to {version}.",
            }
        except Exception:
            downloaded.unlink(missing_ok=True)
            raise
