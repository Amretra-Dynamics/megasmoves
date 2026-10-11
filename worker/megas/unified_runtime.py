"""One Megas runtime spanning Local, optional cloud, and optional Tailscale access.

`megas` starts this runtime. Local is the baseline and does not require a cloud
account. Cloud connectivity is added when the machine is paired. Tailscale is a
private route exposed by the Local Gateway only after explicit Serve consent;
it is not a separate Worker process or startup mode.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Optional

from megas.client import WorkerRuntime as CloudWorkerRuntime
from megas.config import (
    WorkerConfig,
    default_config_path,
    get_or_create_persistent_machine_id,
    load_config,
    save_config,
)
from megas.local.session_manager import LocalSessionManager


def local_database_path() -> Path:
    configured = str(os.getenv("MEGAS_LOCAL_DB") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return default_config_path().parent / "local.db"


def canonical_local_worker_id(machine_identity: str) -> str:
    identity = str(machine_identity or "").strip()
    if not identity:
        raise ValueError("A persistent machine identity is required")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"wrk_local_{digest}"


def ensure_persistent_machine_identity(config: WorkerConfig) -> str:
    identity = str(
        config.machine_id or get_or_create_persistent_machine_id()
    ).strip()
    if not identity:
        raise RuntimeError("Could not establish a persistent machine identity")
    if config.machine_id != identity:
        config.machine_id = identity
        save_config(config)
    return identity


def requested_worker_id(config: WorkerConfig) -> str:
    """The one Worker identity cloud pairing must preserve for this machine."""
    if config.worker_id:
        return str(config.worker_id)
    return canonical_local_worker_id(ensure_persistent_machine_identity(config))


def _bounded_port(env_name: str, default: int, *, allow_zero: bool) -> int:
    raw = str(os.getenv(env_name) or default).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{env_name} must be an integer") from exc
    minimum = 0 if allow_zero else 1
    if value < minimum or value > 65535:
        raise ValueError(f"{env_name} must be between {minimum} and 65535")
    return value


class UnifiedMegasRuntime:
    """Own one Local Gateway and, when valid, one cloud Worker connection."""

    def __init__(self, config: WorkerConfig) -> None:
        self.initial_config = config
        self.local_manager: Optional[LocalSessionManager] = None
        self.local_worker_id: Optional[str] = None
        self.gateway_port: Optional[int] = None
        self.websocket_port: Optional[int] = None
        self._identity_warning: Optional[str] = None

    async def _start_local(self) -> None:
        # Establish this before SQLite identity resolution. Unpaired Local and later
        # cloud authorization must derive the same requested Worker ID.
        ensure_persistent_machine_identity(self.initial_config)
        manager = LocalSessionManager(local_database_path())
        gateway_port = _bounded_port("MEGAS_LOCAL_PORT", 18765, allow_zero=True)
        websocket_port = _bounded_port("MEGAS_LOCAL_WS_PORT", 0, allow_zero=True)
        try:
            await asyncio.to_thread(
                manager.start_gateway,
                gateway_port,
                websocket_port,
            )
        except Exception:
            await asyncio.to_thread(manager.shutdown)
            raise

        server = manager.gateway_server
        if server is None:
            await asyncio.to_thread(manager.shutdown)
            raise RuntimeError("Local Gateway did not start")
        services = server.services
        self.local_manager = manager
        self.local_worker_id = str(services.worker_id)
        expected = requested_worker_id(self.initial_config)
        if self.local_worker_id != expected:
            await asyncio.to_thread(manager.shutdown)
            self.local_manager = None
            raise RuntimeError(
                "Local Worker identity does not match the persistent machine identity"
            )
        self.gateway_port = int(server.server_port)
        self.websocket_port = services.websocket_port
        print(
            f"Megas Local ready: worker={self.local_worker_id} "
            f"http=127.0.0.1:{self.gateway_port} "
            f"ws=127.0.0.1:{self.websocket_port}",
            flush=True,
        )

    async def _cloud_supervisor(self) -> None:
        """Attach cloud when pairing exists without changing Local authority."""
        last_notice: Optional[str] = None
        while True:
            config = load_config()
            if not config.is_paired:
                notice = "Cloud route not configured; Local remains available."
                if notice != last_notice:
                    print(notice, flush=True)
                    last_notice = notice
                await asyncio.sleep(2.0)
                continue

            if not self.local_worker_id:
                raise RuntimeError("Local Worker identity is unavailable")
            if config.worker_id != self.local_worker_id:
                notice = (
                    "Cloud connection withheld: the paired cloud Worker identity "
                    f"({config.worker_id}) does not match the Local Worker identity "
                    f"({self.local_worker_id}). Re-pair through the identity-continuity "
                    "flow; Megas will not create or advertise a duplicate Worker."
                )
                self._identity_warning = notice
                if notice != last_notice:
                    print(notice, flush=True)
                    last_notice = notice
                await asyncio.sleep(5.0)
                continue

            self._identity_warning = None
            print(
                "Cloud route configured; connecting the same Worker identity.",
                flush=True,
            )
            try:
                await CloudWorkerRuntime(config).start_forever()
            except Exception as exc:
                print(
                    f"Cloud connection unavailable ({exc}); Local runtime remains active.",
                    flush=True,
                )
                await asyncio.sleep(5.0)

    async def start_forever(self) -> None:
        await self._start_local()
        cloud_task = asyncio.create_task(
            self._cloud_supervisor(),
            name="megas-cloud-route-supervisor",
        )
        try:
            await cloud_task
        except asyncio.CancelledError:
            cloud_task.cancel()
            await asyncio.gather(cloud_task, return_exceptions=True)
            raise
        finally:
            manager = self.local_manager
            self.local_manager = None
            if manager is not None:
                await asyncio.to_thread(manager.shutdown)


WorkerRuntime = UnifiedMegasRuntime


__all__ = [
    "UnifiedMegasRuntime",
    "WorkerRuntime",
    "canonical_local_worker_id",
    "ensure_persistent_machine_identity",
    "local_database_path",
    "requested_worker_id",
]
