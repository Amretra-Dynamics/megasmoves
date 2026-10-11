"""HTTP client for Megas Backend operations APIs."""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from megas.config import WorkerConfig, load_config
from megas.contracts.generated.monitor_incidents_v1 import (
    MONITOR_INCIDENT_ACKNOWLEDGE_ROUTE,
    MONITOR_INCIDENT_ASSIGN_ROUTE,
    MONITOR_INCIDENT_DETAIL_ROUTE,
    MONITOR_INCIDENT_LIST_ROUTE,
    MONITOR_INCIDENT_MITIGATE_ROUTE,
    MONITOR_INCIDENT_REOPEN_ROUTE,
    MONITOR_INCIDENT_RESOLVE_ROUTE,
    MONITOR_INCIDENT_SUPPRESS_ROUTE,
    parse_MonitorIncidentDetailResponseV1,
    parse_MonitorIncidentListResponseV1,
)

from .errors import (
    AuthenticationError,
    CapabilityUnavailableError,
    CliError,
    NetworkError,
    RequestTimeoutError,
    map_http_status,
)
from .output import redact_value
from .user_credentials import load_user_token


class OpsApiClient:
    """Authenticated REST client for user-facing and ops APIs."""

    def __init__(
        self,
        *,
        api_url: str,
        token: str | None = None,
        timeout: float = 30.0,
        worker_config: WorkerConfig | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.token = (token if token is not None else load_user_token()).strip()
        self.timeout = timeout
        self.worker_config = (
            worker_config if worker_config is not None else load_config()
        )

    def _headers(
        self, *, worker: bool = False, extra: dict[str, str] | None = None
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "megas-cli/ops",
        }
        if worker:
            worker_token = self.worker_config.worker_token
            if not worker_token:
                raise AuthenticationError(
                    "Worker is not paired.",
                    hint="Run `megas connect` or `megas workers pair` first.",
                )
            headers["X-Megas-Worker-Token"] = worker_token
        else:
            if not self.token:
                raise AuthenticationError(
                    "No user API token configured.",
                    hint="Run `megas auth login --token <firebase-id-token>` or set MEGAS_TOKEN.",
                )
            headers["Authorization"] = f"Bearer {self.token}"
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        worker: bool = False,
        allow_statuses: set[int] | None = None,
        timeout: float | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                url = f"{url}?{urlencode(filtered, doseq=True)}"
        try:
            with httpx.Client(timeout=timeout or self.timeout) as client:
                response = client.request(
                    method.upper(),
                    url,
                    headers=self._headers(worker=worker, extra=extra_headers),
                    json=json_body,
                )
        except httpx.TimeoutException as exc:
            raise RequestTimeoutError(
                f"Request timed out: {method.upper()} {path}"
            ) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(
                f"Network error contacting Megas Backend: {exc}"
            ) from exc

        if allow_statuses and response.status_code in allow_statuses:
            return self._parse_body(response)

        if response.status_code >= 400:
            message = self._error_message(response)
            raise map_http_status(
                response.status_code,
                message,
                details={"status": response.status_code, "path": path},
            )
        return self._parse_body(response)

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)

    def try_paths(
        self,
        method: str,
        paths: list[str],
        *,
        capability: str,
        **kwargs: Any,
    ) -> Any:
        """Try multiple API paths for forward/backward compatibility."""
        last_error: Exception | None = None
        saw_404 = False
        for path in paths:
            try:
                return self.request(method, path, **kwargs)
            except CliError as exc:
                last_error = exc
                status = (exc.details or {}).get("status")
                if status == 404:
                    saw_404 = True
                    continue
                if status in {401, 403}:
                    raise
                # 501/405 etc → try next
                if status in {400, 404, 405, 501, 503}:
                    continue
                raise
        if saw_404 or last_error:
            raise CapabilityUnavailableError(
                capability,
                hint=(
                    f"Tried paths: {', '.join(paths)}. "
                    "This feature may require a backend release that is not deployed yet."
                ),
            ) from last_error
        raise CapabilityUnavailableError(capability)

    def healthz(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=min(5.0, self.timeout)) as client:
                response = client.get(f"{self.api_url}/healthz")
                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, dict) else {"status": "ok", "raw": data}
        except httpx.TimeoutException as exc:
            raise RequestTimeoutError("Health check timed out.") from exc
        except httpx.HTTPError as exc:
            raise NetworkError(f"Backend health check failed: {exc}") from exc

    def readyz(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=min(10.0, self.timeout)) as client:
                response = client.get(f"{self.api_url}/readyz")
                if response.status_code >= 400:
                    return {"status": "not_ready", "http_status": response.status_code}
                data = response.json()
                return data if isinstance(data, dict) else {"status": "ready"}
        except httpx.HTTPError as exc:
            return {"status": "unreachable", "error": str(exc)}

    @staticmethod
    def _parse_body(response: httpx.Response) -> Any:
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except json.JSONDecodeError:
            return {"raw": response.text}

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            text = response.text.strip()
            return text or f"HTTP {response.status_code}"
        if isinstance(payload, dict):
            detail = (
                payload.get("detail") or payload.get("error") or payload.get("message")
            )
            if isinstance(detail, dict):
                return json.dumps(redact_value(detail), default=str)
            if detail:
                return str(detail)
        return f"HTTP {response.status_code}"


# ---------------------------------------------------------------------------
# Domain helpers — thin wrappers around known backend contracts
# ---------------------------------------------------------------------------


def list_workers(
    client: OpsApiClient, *, limit: int | None = None
) -> list[dict[str, Any]]:
    data = client.get("/api/v1/workers")
    rows = (
        data
        if isinstance(data, list)
        else data.get("workers") or data.get("items") or []
    )
    if limit is not None:
        rows = rows[:limit]
    return [row if isinstance(row, dict) else {"id": row} for row in rows]


def get_worker(client: OpsApiClient, worker_id: str) -> dict[str, Any]:
    data = client.get(f"/api/v1/workers/{worker_id}")
    return data if isinstance(data, dict) else {"id": worker_id, "raw": data}


def list_sessions(client: OpsApiClient, *, limit: int = 50) -> list[dict[str, Any]]:
    data = client.get("/api/v1/sessions", params={"limit": limit})
    rows = (
        data
        if isinstance(data, list)
        else data.get("sessions") or data.get("items") or []
    )
    return [row if isinstance(row, dict) else {"id": row} for row in rows]


def get_session(client: OpsApiClient, session_id: str) -> dict[str, Any]:
    data = client.get(f"/api/v1/sessions/{session_id}")
    return data if isinstance(data, dict) else {"id": session_id, "raw": data}


def session_events(
    client: OpsApiClient, session_id: str, *, after_seq: int = 0, limit: int = 200
) -> dict[str, Any]:
    data = client.get(
        f"/api/v1/sessions/{session_id}/events",
        params={"after_seq": after_seq, "limit": limit},
    )
    return data if isinstance(data, dict) else {"events": data}


def create_session(
    client: OpsApiClient,
    *,
    instruction: str,
    agent_type: str = "",
    workdir: str = "",
    preferred_worker_id: str = "",
    source: str = "cli",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    extra_headers: dict[str, str] | None = None
    if idempotency_key:
        extra_headers = {"Idempotency-Key": idempotency_key}
    body: dict[str, Any] = {
        "instruction": instruction,
        "source": source,
        "agent_type": agent_type or "",
        "workdir": workdir or "",
    }
    if preferred_worker_id:
        body["preferred_worker_id"] = preferred_worker_id
    data = client.post(
        "/api/v1/sessions",
        json_body=body,
        extra_headers=extra_headers,
    )
    return data if isinstance(data, dict) else {"raw": data}


def cancel_session(
    client: OpsApiClient, session_id: str, *, reason: str = "user_cancelled"
) -> dict[str, Any]:
    data = client.post(
        f"/api/v1/sessions/{session_id}/cancel",
        json_body={"reason": reason},
    )
    return data if isinstance(data, dict) else {"success": True, "raw": data}


def close_session(client: OpsApiClient, session_id: str) -> dict[str, Any]:
    data = client.post(f"/api/v1/sessions/{session_id}/close")
    return data if isinstance(data, dict) else {"success": True, "raw": data}


def list_evidence(
    client: OpsApiClient, *, session_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": limit}
    if session_id:
        params["session_id"] = session_id
    data = client.get("/api/v1/evidence", params=params)
    rows = (
        data
        if isinstance(data, list)
        else data.get("evidence") or data.get("items") or []
    )
    return [row if isinstance(row, dict) else {"id": row} for row in rows]


def list_monitor_incidents(client: OpsApiClient, **params: Any) -> dict[str, Any]:
    data = client.get(MONITOR_INCIDENT_LIST_ROUTE, params=params)
    return dict(parse_MonitorIncidentListResponseV1(data))


def get_monitor_incident(client: OpsApiClient, incident_id: str) -> dict[str, Any]:
    path = MONITOR_INCIDENT_DETAIL_ROUTE.replace(
        "{incident_id}", quote(incident_id, safe="")
    )
    data = client.get(path)
    return dict(parse_MonitorIncidentDetailResponseV1(data))


def incident_action(
    client: OpsApiClient,
    incident_id: str,
    action: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action_routes = {
        "acknowledge": MONITOR_INCIDENT_ACKNOWLEDGE_ROUTE,
        "assign": MONITOR_INCIDENT_ASSIGN_ROUTE,
        "mute": MONITOR_INCIDENT_SUPPRESS_ROUTE,
        "suppress": MONITOR_INCIDENT_SUPPRESS_ROUTE,
        "mitigate": MONITOR_INCIDENT_MITIGATE_ROUTE,
        "resolve": MONITOR_INCIDENT_RESOLVE_ROUTE,
        "reopen": MONITOR_INCIDENT_REOPEN_ROUTE,
    }
    try:
        route = action_routes[action]
    except KeyError as exc:
        raise ValueError(f"Unsupported Monitor incident action: {action}") from exc
    path = route.replace("{incident_id}", quote(incident_id, safe=""))
    data = client.post(path, json_body=body or {})
    return dict(parse_MonitorIncidentDetailResponseV1(data))


def monitor_telemetry_events(client: OpsApiClient, **params: Any) -> Any:
    return client.try_paths(
        "GET",
        ["/api/v1/monitor/telemetry/events", "/api/v1/monitor/events"],
        capability="monitor.events",
        params=params,
    )


def monitor_health(client: OpsApiClient) -> Any:
    return client.try_paths(
        "GET",
        ["/api/v1/monitor/telemetry/health", "/api/v1/monitor/health", "/readyz"],
        capability="monitor.health",
    )


def create_browser_session(
    client: OpsApiClient,
    *,
    url: str | None = None,
    browser: str = "chrome",
    profile_id: str | None = None,
) -> dict[str, Any]:
    data = client.post(
        "/api/v1/browsers/sessions",
        json_body={
            "browser_type": browser,
            "engine_type": "playwright",
            "initial_url": url or "about:blank",
            "profile_id": profile_id,
        },
        extra_headers={"Idempotency-Key": f"cli-browser-{uuid.uuid4()}"},
    )
    return dict(data.get("browser_session") or data)


def list_operate_sessions(client: OpsApiClient) -> list[dict[str, Any]]:
    data = client.get("/api/v1/browsers/sessions")
    rows = data.get("browser_sessions") or []
    return [row if isinstance(row, dict) else {"id": row} for row in rows]


def get_browser_session(client: OpsApiClient, session_id: str) -> dict[str, Any]:
    data = client.get(f"/api/v1/browsers/sessions/{quote(session_id, safe='')}")
    return dict(data.get("browser_session") or data)


def stop_browser_session(client: OpsApiClient, session_id: str) -> dict[str, Any]:
    return dict(client.post(f"/api/v1/browsers/sessions/{quote(session_id, safe='')}/terminate"))


def emergency_stop_session(client: OpsApiClient, session_id: str) -> dict[str, Any]:
    return client.try_paths(
        "POST",
        [
            f"/api/v1/console/operate/sessions/{session_id}/emergency-stop",
            f"/api/v1/operate/sessions/{session_id}/emergency-stop",
            f"/api/v1/sessions/{session_id}/close",
        ],
        capability="operate.emergency_stop",
        json_body={},
    )


def list_security_findings(client: OpsApiClient) -> list[dict[str, Any]]:
    data = client.try_paths(
        "GET",
        [
            "/api/v1/console/secure/findings",
            "/api/v1/security/findings",
            "/api/v1/secure/findings",
        ],
        capability="security.findings",
    )
    rows = (
        data
        if isinstance(data, list)
        else data.get("findings") or data.get("items") or []
    )
    return [row if isinstance(row, dict) else {"id": row} for row in rows]


def security_remediation_action(
    client: OpsApiClient,
    remediation_id: str,
    action: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return client.try_paths(
        "POST",
        [
            f"/api/v1/console/secure/remediations/{remediation_id}/{action}",
            f"/api/v1/security/remediations/{remediation_id}/{action}",
            f"/api/v1/console/approvals/{remediation_id}/decision",
        ],
        capability=f"security.remediation.{action}",
        json_body=body or {"decision": action, "remediation_id": remediation_id},
    )


def list_notifications(client: OpsApiClient) -> list[dict[str, Any]]:
    data = client.try_paths(
        "GET",
        [
            "/api/v1/notifications",
            "/api/v1/console/notifications",
            "/api/v1/monitor/incidents/domain-events/outbox",
        ],
        capability="notifications",
        params={"limit": 50},
    )
    rows = (
        data
        if isinstance(data, list)
        else data.get("notifications") or data.get("events") or data.get("items") or []
    )
    return [row if isinstance(row, dict) else {"id": row} for row in rows]


def acknowledge_notification(
    client: OpsApiClient, notification_id: str
) -> dict[str, Any]:
    return client.try_paths(
        "POST",
        [
            f"/api/v1/notifications/{notification_id}/acknowledge",
            f"/api/v1/console/notifications/{notification_id}/acknowledge",
        ],
        capability="notifications.acknowledge",
        json_body={},
    )


__all__ = [
    "OpsApiClient",
    "acknowledge_notification",
    "cancel_session",
    "close_session",
    "create_session",
    "emergency_stop_session",
    "get_monitor_incident",
    "get_session",
    "get_worker",
    "incident_action",
    "list_evidence",
    "list_monitor_incidents",
    "list_notifications",
    "list_operate_sessions",
    "list_security_findings",
    "list_sessions",
    "list_workers",
    "monitor_health",
    "monitor_telemetry_events",
    "security_remediation_action",
    "session_events",
]
