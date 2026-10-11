"""Shared cross-platform process-group lifecycle helpers for agent adapters."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from typing import Any, Optional

from .base import subprocess_hide_window_kwargs, windows_subprocess_kwargs


def agent_process_group_kwargs() -> dict[str, Any]:
    """Spawn a child in a separately terminable process group/session."""
    if os.name == "nt":
        return windows_subprocess_kwargs(new_group=True)
    return {"start_new_session": True}


async def terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    timeout: float = 3.0,
) -> None:
    if process.returncode is not None:
        return
    pid = process.pid
    if os.name == "nt":
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(timeout, 10.0),
            check=False,
            **subprocess_hide_window_kwargs(),
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        return

    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        await process.wait()
        return
    if pgid != pid:
        # Never signal a process group we did not explicitly create for this child.
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def terminate_process_tree_by_pid(
    pid: int,
    *,
    expected_create_time: Optional[float],
    timeout: float = 3.0,
) -> bool:
    """Terminate a detached process only after PID/create-time verification."""
    try:
        import psutil
    except ImportError:
        return False

    try:
        process = psutil.Process(pid)
        actual_create_time = process.create_time()
        if expected_create_time is None or abs(actual_create_time - expected_create_time) > 0.01:
            return False
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return True
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return True
    except (psutil.AccessDenied, OSError):
        return False

    if os.name == "nt":
        result = await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(timeout, 10.0),
            check=False,
            **subprocess_hide_window_kwargs(),
        )
        return result.returncode == 0

    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return True
    if pgid != pid:
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return True


def process_create_time(pid: int) -> Optional[float]:
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except (ImportError, OSError, ValueError):
        return None
    except Exception:
        return None
