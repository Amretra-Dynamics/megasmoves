"""
RuntimeRefreshAdapter — Worker-side handler for runtime surface refresh commands.

The backend dispatches a runtime_refresh command over WebSocket when a user
requests a surface refresh. This adapter executes scope-specific cleanup
on the worker machine and reports results back.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import AsyncIterator

from .base import windows_creationflags, windows_subprocess_kwargs,  WorkerEvent, BaseAgentAdapter
from ..policy import redact


class RuntimeRefreshAdapter(BaseAgentAdapter):
    name = "runtime_refresh"
    adapter_id = "runtime_refresh"
    contract_version = "1.0"
    status = "discovered"
    supported_range = ">=0.0.0"

    def __init__(self, allowed_folders: list[str]) -> None:
        super().__init__(allowed_folders)

    def supports(self, command: dict) -> bool:
        command_type = str(command.get("command_type") or "")
        return command_type == "runtime_refresh"

    def prepare_context(self, context_pack: dict, workspace: Path, command: dict | None = None) -> Path:
        return workspace

    async def launch(self, command: dict) -> AsyncIterator[WorkerEvent]:
        session_id = str(command.get("session_id") or "")
        command_id = str(command.get("command_id") or "")
        refresh_id = str(command.get("refresh_id") or "")
        runtime_id = str(command.get("runtime_id") or "")
        requested_scopes = command.get("requested_scopes") or ["full_surface"]
        reason = str(command.get("reason") or "Runtime refresh")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", runtime_id):
            raise ValueError("Runtime refresh identity is invalid.")
        if not isinstance(requested_scopes, list) or not all(
            isinstance(scope, str) for scope in requested_scopes
        ):
            raise ValueError("Runtime refresh scopes must be a list of names.")

        started = time.time()

        yield WorkerEvent("refresh_started", {
            "refresh_id": refresh_id,
            "runtime_id": runtime_id,
            "session_id": session_id,
            "requested_scopes": requested_scopes,
            "reason": reason,
        })

        # Expand full_surface into all individual scopes
        if "full_surface" in requested_scopes:
            expanded_scopes = ["runtime", "network_ip", "browser_profile", "cookies_session", "storage", "dns"]
        else:
            expanded_scopes = list(requested_scopes)

        completed_scopes: list[str] = []
        failed_scopes: list[dict] = []

        scope_handlers = {
            "runtime": lambda: self._refresh_runtime(runtime_id),
            "network_ip": lambda: self._refresh_network_ip(runtime_id),
            "browser_profile": lambda: self._refresh_browser_profile(runtime_id),
            "cookies_session": lambda: self._refresh_cookies_session(runtime_id),
            "storage": lambda: self._refresh_storage(runtime_id),
            "dns": self._refresh_dns,
        }

        for scope in expanded_scopes:
            handler = scope_handlers.get(scope)
            if handler is None:
                failed_scopes.append({"scope": scope, "error": f"Unknown scope: {scope}"})
                yield WorkerEvent("refresh_scope_failed", {
                    "refresh_id": refresh_id,
                    "scope": scope,
                    "error": f"Unknown scope: {scope}",
                })
                continue

            try:
                result = await handler()
                completed_scopes.append(scope)
                yield WorkerEvent("refresh_scope_completed", {
                    "refresh_id": refresh_id,
                    "scope": scope,
                    "details": result,
                })
            except Exception as exc:
                error_msg = redact(str(exc))
                failed_scopes.append({"scope": scope, "error": error_msg})
                yield WorkerEvent("refresh_scope_failed", {
                    "refresh_id": refresh_id,
                    "scope": scope,
                    "error": error_msg,
                })

        duration_ms = int((time.time() - started) * 1000)
        success = len(failed_scopes) == 0

        # Emit evidence
        yield WorkerEvent("evidence", {
            "evidence_type": "evidence_runtime_refresh_executed",
            "summary": f"Runtime refresh {'completed' if success else 'partially failed'}: {reason}",
            "payload": {
                "refresh_id": refresh_id,
                "runtime_id": runtime_id,
                "requested_scopes": requested_scopes,
                "expanded_scopes": expanded_scopes,
                "completed_scopes": completed_scopes,
                "failed_scopes": failed_scopes,
                "duration_ms": duration_ms,
                "success": success,
            },
        })

        yield WorkerEvent("refresh_completed", {
            "refresh_id": refresh_id,
            "runtime_id": runtime_id,
            "session_id": session_id,
            "completed_scopes": completed_scopes,
            "failed_scopes": failed_scopes,
            "duration_ms": duration_ms,
            "success": success,
        })

    # ------------------------------------------------------------------
    # Scope handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _remove_owned_path(item: Path) -> bool:
        if not item.exists() and not item.is_symlink():
            return False
        if item.is_symlink() or item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)
        else:
            raise RuntimeError(f"Unsupported runtime path type: {item}")
        if item.exists() or item.is_symlink():
            raise RuntimeError(f"Runtime path could not be removed: {item}")
        return True

    @staticmethod
    async def _communicate_bounded(
        process: asyncio.subprocess.Process, timeout: float = 15
    ) -> tuple[bytes, bytes]:
        try:
            return await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise RuntimeError("Runtime refresh subprocess timed out.") from exc

    async def _refresh_runtime(self, runtime_id: str) -> dict:
        """Clear only the ephemeral directory owned by this runtime."""
        cleaned_paths: list[str] = []
        item = Path(tempfile.gettempdir()) / f"megas-runtime-{runtime_id}"
        if await asyncio.to_thread(self._remove_owned_path, item):
            cleaned_paths.append(str(item))
        return {"cleaned_paths": cleaned_paths[:50], "count": len(cleaned_paths)}

    async def _refresh_network_ip(self, runtime_id: str) -> dict:
        """Flush cached IP info. Actual DNS flush handled by _refresh_dns."""
        cache_dir = Path(tempfile.gettempdir()) / f"megas-network-cache-{runtime_id}"
        cleaned = int(await asyncio.to_thread(self._remove_owned_path, cache_dir))
        return {"flushed_network_cache": True, "cleaned": cleaned}

    async def _refresh_browser_profile(self, runtime_id: str) -> dict:
        """Remove browser profile lock files."""
        cleaned = 0
        item = Path(tempfile.gettempdir()) / f"megas-browser-{runtime_id}"
        if item.exists():
            if not item.is_dir() or item.is_symlink():
                raise RuntimeError("Browser runtime profile path is invalid.")
            locks = [*item.glob("*.lock"), *item.glob("SingletonLock")]
            for lock in locks:
                if await asyncio.to_thread(self._remove_owned_path, lock):
                    cleaned += 1
        return {"browser_locks_cleared": cleaned}

    async def _refresh_cookies_session(self, runtime_id: str) -> dict:
        """Clear session cookie stores."""
        item = Path(tempfile.gettempdir()) / f"megas-cookies-{runtime_id}"
        cleaned = int(await asyncio.to_thread(self._remove_owned_path, item))
        return {"cookie_stores_cleared": cleaned}

    async def _refresh_storage(self, runtime_id: str) -> dict:
        """Clear ephemeral storage directories."""
        item = Path(tempfile.gettempdir()) / f"megas-storage-{runtime_id}"
        cleaned = int(await asyncio.to_thread(self._remove_owned_path, item))
        return {"storage_dirs_cleared": cleaned}

    async def _refresh_dns(self) -> dict:
        """Flush system DNS cache."""
        import subprocess
        import platform as plat

        system = plat.system().lower()
        try:
            if system == "windows":
                proc = await asyncio.create_subprocess_exec(
                    "ipconfig", "/flushdns",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **windows_subprocess_kwargs(new_group=False),
                )
                stdout, stderr = await self._communicate_bounded(proc)
            elif system == "darwin":
                proc = await asyncio.create_subprocess_exec(
                    "dscacheutil", "-flushcache",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **windows_subprocess_kwargs(new_group=False),
                )
                stdout, stderr = await self._communicate_bounded(proc)
            elif system == "linux":
                # Try systemd-resolve first, fall back to resolvectl
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "resolvectl", "flush-caches",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        **windows_subprocess_kwargs(new_group=False),
                    )
                    stdout, stderr = await self._communicate_bounded(proc)
                except FileNotFoundError:
                    proc = await asyncio.create_subprocess_exec(
                        "systemd-resolve", "--flush-caches",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        **windows_subprocess_kwargs(new_group=False),
                    )
                    stdout, stderr = await self._communicate_bounded(proc)
            else:
                raise RuntimeError(f"DNS refresh is unsupported on platform: {system}")
            if proc.returncode != 0:
                detail = redact(stderr.decode(errors="replace").strip())[:300]
                raise RuntimeError(detail or f"DNS refresh exited with code {proc.returncode}")
            return {"dns_flushed": True, "platform": system}
        except Exception as exc:
            raise RuntimeError(f"DNS refresh failed on {system}: {redact(str(exc))}") from exc
