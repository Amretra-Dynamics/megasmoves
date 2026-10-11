"""Small, injectable client for the official Jules REST session/activity API."""

from __future__ import annotations

import json
import socket
from typing import Any, Protocol
from urllib import error, parse, request


JULES_API_ROOT = "https://jules.googleapis.com/v1alpha"


class JulesApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous


class JulesTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, object] | None = None,
    ) -> dict[str, Any]: ...


class UrllibJulesTransport:
    def __init__(self, api_key: str, *, timeout_sec: float = 30.0) -> None:
        key = str(api_key or "").strip()
        if not key:
            raise ValueError("JULES_API_KEY is required")
        self._api_key = key
        self._timeout_sec = min(max(float(timeout_sec), 1.0), 120.0)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        normalized_path = "/" + str(path or "").lstrip("/")
        url = JULES_API_ROOT + normalized_path
        if query:
            encoded = parse.urlencode(
                {key: value for key, value in query.items() if value not in (None, "")}
            )
            if encoded:
                url += "?" + encoded
        payload = None
        headers = {
            "Accept": "application/json",
            "X-Goog-Api-Key": self._api_key,
            "User-Agent": "Megas-Worker/Jules-Adapter",
        }
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=payload, headers=headers, method=method.upper())
        try:
            with request.urlopen(req, timeout=self._timeout_sec) as response:
                raw = response.read()
        except error.HTTPError as exc:
            detail = ""
            try:
                parsed = json.loads(exc.read().decode("utf-8", errors="replace"))
                if isinstance(parsed, dict):
                    error_body = parsed.get("error")
                    if isinstance(error_body, dict):
                        detail = str(error_body.get("message") or error_body.get("status") or "")
            except Exception:
                detail = ""
            raise JulesApiError(
                detail or f"Jules API returned HTTP {exc.code}",
                status=int(exc.code),
                ambiguous=False,
            ) from exc
        except (error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            # A POST may have reached Jules before the connection failed. The
            # adapter must reconcile, never blindly submit the same prompt again.
            ambiguous = method.upper() == "POST"
            raise JulesApiError(
                f"Jules API transport failed: {type(exc).__name__}",
                ambiguous=ambiguous,
            ) from exc
        if not raw:
            return {}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise JulesApiError("Jules API returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise JulesApiError("Jules API returned a non-object response")
        return decoded


def normalize_session_name(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith("sessions/") and text.count("/") == 1:
        return text
    if text and "/" not in text:
        return f"sessions/{text}"
    raise ValueError("Invalid Jules session identity")


class JulesControlPlane:
    def __init__(self, transport: JulesTransport) -> None:
        self._transport = transport

    def create_session(
        self,
        *,
        prompt: str,
        title: str = "Megas",
        source: str = "",
        starting_branch: str = "",
        require_plan_approval: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "prompt": str(prompt),
            "title": str(title or "Megas")[:120],
            "requirePlanApproval": bool(require_plan_approval),
        }
        source_name = str(source or "").strip()
        if source_name:
            source_context: dict[str, Any] = {"source": source_name}
            branch = str(starting_branch or "").strip()
            if branch:
                source_context["githubRepoContext"] = {"startingBranch": branch[:240]}
            payload["sourceContext"] = source_context
        session = self._transport.request("POST", "/sessions", body=payload)
        name = normalize_session_name(session.get("name") or session.get("id"))
        if str(session.get("name") or name) != name:
            raise JulesApiError("Jules create returned an inconsistent session identity")
        return {**session, "name": name}

    def get_session(self, session: object) -> dict[str, Any]:
        name = normalize_session_name(session)
        result = self._transport.request("GET", f"/{name}")
        returned = normalize_session_name(result.get("name") or result.get("id"))
        if returned != name:
            raise JulesApiError("Jules get returned the wrong session identity")
        return {**result, "name": name}

    def send_message(self, session: object, prompt: str) -> None:
        name = normalize_session_name(session)
        self._transport.request(
            "POST",
            f"/{name}:sendMessage",
            body={"prompt": str(prompt)},
        )

    def list_sessions(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        token = ""
        rows: list[dict[str, Any]] = []
        while True:
            payload = self._transport.request(
                "GET",
                "/sessions",
                query={"pageSize": min(max(int(page_size), 1), 100), "pageToken": token},
            )
            items = payload.get("sessions")
            if isinstance(items, list):
                rows.extend(item for item in items if isinstance(item, dict))
            token = str(payload.get("nextPageToken") or "")
            if not token:
                return rows

    def list_activities(self, session: object, *, page_size: int = 100) -> list[dict[str, Any]]:
        name = normalize_session_name(session)
        token = ""
        rows: list[dict[str, Any]] = []
        while True:
            payload = self._transport.request(
                "GET",
                f"/{name}/activities",
                query={"pageSize": min(max(int(page_size), 1), 100), "pageToken": token},
            )
            items = payload.get("activities")
            if isinstance(items, list):
                rows.extend(item for item in items if isinstance(item, dict))
            token = str(payload.get("nextPageToken") or "")
            if not token:
                rows.sort(key=lambda item: (str(item.get("createTime") or ""), str(item.get("name") or "")))
                return rows
