"""Owner-side inspection and pairing for the Megas Local runtime.

The CLI runs on the same OS account that owns ``MEGAS_HOME``. It reads the
authoritative Local SQLite journal directly instead of treating the Cloud Worker
token as a Local device credential. Browser/device access is granted only through
one-time challenges created here by the Worker owner.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any

from megas.config import load_config
from megas.local.identity import worker_installation_id
from megas.local.pairing import PairingChallengeStore
from megas.local.security import LocalSecurityPolicy
from megas.local.store import LocalStore
from megas.local.tailscale import TailscaleServeService
from megas.unified_runtime import local_database_path, requested_worker_id


class LocalRuntimeUnavailable(RuntimeError):
    pass


class LocalRuntimeInspector:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = (db_path or local_database_path()).expanduser().resolve()

    def _store(self) -> LocalStore:
        if not self.db_path.is_file():
            raise LocalRuntimeUnavailable(
                f"Megas Local database is not available at {self.db_path}. "
                "Start Megas on this machine first."
            )
        return LocalStore(self.db_path)

    @staticmethod
    def _bounded_port(value: Any, label: str) -> int:
        try:
            port = int(value)
        except (TypeError, ValueError) as exc:
            raise LocalRuntimeUnavailable(f"Stored {label} port is invalid.") from exc
        if port < 1 or port > 65535:
            raise LocalRuntimeUnavailable(f"Stored {label} port is out of range.")
        return port

    def _gateway_port(self, store: LocalStore) -> int:
        configured = store.get_setting("gateway_port")
        value = configured.get("port") if isinstance(configured, dict) else configured
        if value in (None, ""):
            value = os.getenv("MEGAS_LOCAL_PORT") or 18765
        return self._bounded_port(value, "Local Gateway")

    def _websocket_port(self, store: LocalStore) -> int:
        configured = store.get_setting("websocket_port")
        value = configured.get("port") if isinstance(configured, dict) else None
        if value in (None, ""):
            raise LocalRuntimeUnavailable(
                "Megas Local event streaming is not ready. Start or restart Megas on this machine."
            )
        return self._bounded_port(value, "Local WebSocket")

    @staticmethod
    def _gateway_health(port: int) -> dict[str, Any]:
        import ssl
        for scheme in ("https", "http"):
            request = urllib.request.Request(
                f"{scheme}://127.0.0.1:{port}/api/local/v1/health",
                method="GET",
                headers={"Accept": "application/json"},
            )
            ssl_ctx = ssl._create_unverified_context() if scheme == "https" else None
            try:
                with urllib.request.urlopen(request, timeout=2.0, context=ssl_ctx) as response:
                    raw = response.read(64 * 1024)
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                    if isinstance(payload, dict):
                        return payload
            except Exception:
                continue
        return {"status": "offline", "error": "unreachable"}

    @staticmethod
    def _runtime_identity(store: LocalStore) -> tuple[str, str]:
        identity = store.get_setting("local_worker_identity")
        if not isinstance(identity, dict):
            raise LocalRuntimeUnavailable(
                "Megas Local identity is not initialized. Start Megas on this machine first."
            )
        worker_id = str(identity.get("worker_id") or "").strip()
        worker_name = str(identity.get("worker_name") or "").strip()
        if not worker_id or not worker_name:
            raise LocalRuntimeUnavailable("Stored Megas Local identity is incomplete.")
        return worker_id, worker_name

    @staticmethod
    def _pair_uri(payload: dict[str, Any]) -> str:
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return f"megas://pair?payload={urllib.parse.quote(encoded, safe='')}"

    def status(self) -> dict[str, Any]:
        store = self._store()
        try:
            port = self._gateway_port(store)
            health = self._gateway_health(port)
            threads = store.list_threads()
            active_sessions = store.get_active_sessions()
            database = store.get_status()
        finally:
            store.close()

        config = load_config()
        expected_worker_id = requested_worker_id(config)
        durable_worker_ids = sorted({thread.worker_id for thread in threads})
        identity_consistent = not durable_worker_ids or durable_worker_ids == [expected_worker_id]
        return {
            "status": (
                "ok"
                if health.get("status") == "ok"
                and database.get("status") == "ok"
                and identity_consistent
                else "degraded"
            ),
            "database_path": str(self.db_path),
            "database": database,
            "gateway": health,
            "gateway_port": port,
            "worker_id": expected_worker_id,
            "installation_id": worker_installation_id(),
            "durable_worker_ids": durable_worker_ids,
            "identity_consistent": identity_consistent,
            "cloud_route_configured": bool(config.is_paired),
            "active_session_count": len(active_sessions),
            "thread_count": len(threads),
        }

    def create_pairing_payload(
        self,
        *,
        ttl_seconds: int = 300,
        include_tailscale: bool = True,
    ) -> dict[str, Any]:
        if ttl_seconds < 30 or ttl_seconds > 600:
            raise ValueError("Pairing TTL must be between 30 and 600 seconds.")

        store = self._store()
        try:
            gateway_port = self._gateway_port(store)
            websocket_port = self._websocket_port(store)
            health = self._gateway_health(gateway_port)
            if health.get("status") != "ok":
                raise LocalRuntimeUnavailable(
                    "Megas Local Gateway is not reachable. Start Megas before creating a pairing code."
                )
            worker_id, worker_name = self._runtime_identity(store)
            installation_id = worker_installation_id()
            allowed_scopes = [
                "conversation:read",
                "conversation:execute",
                "conversation:stream",
            ]
            challenge_id, secret = PairingChallengeStore(store).create_challenge(
                worker_id,
                {
                    "worker_id": worker_id,
                    "installation_id": installation_id,
                    "allowed_scopes": allowed_scopes,
                    "security_context": "Worker-owner-issued Megas Local pairing",
                },
                ttl_seconds=ttl_seconds,
            )

            use_https = os.environ.get("MEGAS_LOCAL_TLS") == "1"
            scheme = "https" if use_https else "http"
            ws_scheme = "wss" if use_https else "ws"

            target_host = os.environ.get("MEGAS_LOCAL_IP") or "127.0.0.1"
            label = f"{worker_name} · This computer" if target_host == "127.0.0.1" else f"{worker_name} · {target_host}"

            endpoints: list[dict[str, Any]] = [
                {
                    "endpoint_id": f"{installation_id}:{worker_id}:direct",
                    "route": "direct",
                    "label": label,
                    "api_base_url": f"{scheme}://{target_host}:{gateway_port}",
                    "stream_base_url": f"{ws_scheme}://{target_host}:{websocket_port}",
                    "same_machine_only": target_host == "127.0.0.1",
                    "verified_at": time.time(),
                }
            ]

            tailscale_status: dict[str, Any] = {
                "included": False,
                "reason": "not_requested" if not include_tailscale else "not_configured",
            }
            if include_tailscale:
                status = TailscaleServeService(store, LocalSecurityPolicy()).status()
                state = status.get("state") if isinstance(status, dict) else None
                if (
                    status.get("configured") is True
                    and status.get("drift") is False
                    and isinstance(state, dict)
                    and isinstance(state.get("verified_api_url"), str)
                    and isinstance(state.get("verified_stream_url"), str)
                ):
                    endpoints.append(
                        {
                            "endpoint_id": f"{installation_id}:{worker_id}:tailscale",
                            "route": "tailscale",
                            "label": f"{worker_name} · Tailscale",
                            "api_base_url": state["verified_api_url"],
                            "stream_base_url": state["verified_stream_url"],
                            "same_machine_only": False,
                            "verified_at": float(state.get("verified_at") or time.time()),
                        }
                    )
                    tailscale_status = {"included": True, "reason": "verified"}
                elif status.get("configured") is True:
                    tailscale_status = {"included": False, "reason": "serve_drift"}

            stored_tunnel = store.get_setting("tunnel_state")
            saved_url = stored_tunnel.get("url") if isinstance(stored_tunnel, dict) else None
            tunnel_url = (os.environ.get("MEGAS_TUNNEL_URL") or os.environ.get("MEGAS_REMOTE_URL") or saved_url or "").strip()
            if tunnel_url:
                tunnel_api = tunnel_url.rstrip("/")
                tunnel_stream = tunnel_api.replace("https://", "wss://").replace("http://", "ws://")
                endpoints.append(
                    {
                        "endpoint_id": f"{installation_id}:{worker_id}:tunnel",
                        "route": "tunnel",
                        "label": f"{worker_name} · Remote 5G Tunnel",
                        "api_base_url": tunnel_api,
                        "stream_base_url": tunnel_stream,
                        "same_machine_only": False,
                        "verified_at": time.time(),
                    }
                )

            expires_at = time.time() + ttl_seconds
            payload = {
                "schema_version": "megas.local-pairing.v1",
                "protocol_version": 1,
                "installation_id": installation_id,
                "worker_id": worker_id,
                "worker_name": worker_name,
                "challenge_id": challenge_id,
                "secret": secret,
                "allowed_scopes": allowed_scopes,
                "issued_at": time.time(),
                "expires_at": expires_at,
                "endpoints": endpoints,
            }
            return {
                "pairing": payload,
                "pair_uri": self._pair_uri(payload),
                "tailscale": tailscale_status,
            }
        finally:
            store.close()

    def list_workspaces(self) -> list[dict[str, Any]]:
        store = self._store()
        try:
            return [asdict(item) for item in store.list_workspaces()]
        finally:
            store.close()

    def list_threads(self) -> list[dict[str, Any]]:
        store = self._store()
        try:
            return [asdict(item) for item in store.list_threads()]
        finally:
            store.close()

    def replay_events(
        self,
        live_session_id: str,
        *,
        after_seq: int = -1,
        limit: int = 100,
    ) -> dict[str, Any]:
        store = self._store()
        try:
            session, thread = store.get_session_scope(live_session_id)
            replay = store.replay_events(
                live_session_id,
                after_seq=after_seq,
                limit=limit,
                worker_id=thread.worker_id,
                megas_thread_id=thread.megas_thread_id,
            )
            return {
                **replay,
                "events": [asdict(event) for event in replay["events"]],
                "session": asdict(session),
                "thread": asdict(thread),
            }
        finally:
            store.close()


__all__ = ["LocalRuntimeInspector", "LocalRuntimeUnavailable"]
