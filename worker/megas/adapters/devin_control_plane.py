"""Small, dependency-free client for Devin's official v3 session API."""

from __future__ import annotations

from dataclasses import dataclass
import json
import socket
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEVIN_API_ROOT = "https://api.devin.ai/v3"


class DevinTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, object] | None = None,
        body: Mapping[str, object] | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class DevinApiError(RuntimeError):
    message: str
    status_code: int | None = None
    ambiguous: bool = False

    def __str__(self) -> str:
        return self.message


def normalize_devin_id(value: object) -> str:
    session_id = str(value or "").strip()
    if not session_id.startswith("devin-"):
        raise ValueError("DEVIN_SESSION_ID_INVALID")
    if len(session_id) > 200 or any(ch in session_id for ch in "/?#\\"):
        raise ValueError("DEVIN_SESSION_ID_INVALID")
    return session_id


def normalize_org_id(value: object) -> str:
    org_id = str(value or "").strip()
    if not org_id.startswith("org-"):
        raise ValueError("DEVIN_ORG_ID_INVALID")
    if len(org_id) > 200 or any(ch in org_id for ch in "/?#\\"):
        raise ValueError("DEVIN_ORG_ID_INVALID")
    return org_id


class UrllibDevinTransport:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEVIN_API_ROOT,
        timeout_sec: float = 30.0,
    ) -> None:
        self.token = str(token or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = min(max(float(timeout_sec), 1.0), 120.0)
        if not self.token:
            raise ValueError("DEVIN_API_KEY_REQUIRED")

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, object] | None = None,
        body: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        verb = str(method or "GET").upper()
        url = self.base_url + "/" + path.lstrip("/")
        if query:
            pairs: list[tuple[str, str]] = []
            for key, value in query.items():
                if value is None:
                    continue
                if isinstance(value, (list, tuple)):
                    pairs.extend((str(key), str(item)) for item in value)
                else:
                    pairs.append((str(key), str(value)))
            if pairs:
                url += "?" + urlencode(pairs)
        payload = (
            json.dumps(dict(body), separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        )
        request = Request(
            url,
            data=payload,
            method=verb,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Megas-Worker/adapter-devin-v1",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_sec) as response:
                raw = response.read()
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:2_000]
            except Exception:
                detail = ""
            raise DevinApiError(
                f"DEVIN_HTTP_{exc.code}: {detail or exc.reason}",
                status_code=int(exc.code),
                ambiguous=False,
            ) from exc
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            # A transport failure after POST bytes may have reached Devin. Never
            # silently retry a create/send operation under a new Megas turn.
            raise DevinApiError(
                f"DEVIN_TRANSPORT_ERROR: {type(exc).__name__}",
                ambiguous=verb in {"POST", "DELETE"},
            ) from exc
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DevinApiError("DEVIN_RESPONSE_NOT_JSON") from exc
        if not isinstance(parsed, dict):
            raise DevinApiError("DEVIN_RESPONSE_SHAPE_INVALID")
        return parsed


class DevinControlPlane:
    def __init__(self, transport: DevinTransport, org_id: str) -> None:
        self.transport = transport
        self.org_id = normalize_org_id(org_id)

    @property
    def sessions_path(self) -> str:
        return f"organizations/{self.org_id}/sessions"

    def list_sessions(self, *, limit: int = 200) -> list[dict[str, Any]]:
        remaining = min(max(int(limit), 1), 5_000)
        cursor: str | None = None
        items: list[dict[str, Any]] = []
        while remaining > 0:
            page_size = min(remaining, 200)
            response = self.transport.request(
                "GET",
                self.sessions_path,
                query={"first": page_size, "after": cursor},
            )
            page = response.get("items")
            if not isinstance(page, list):
                raise DevinApiError("DEVIN_SESSION_LIST_SHAPE_INVALID")
            for item in page:
                if isinstance(item, dict):
                    items.append(item)
            remaining = limit - len(items)
            if not response.get("has_next_page") or not response.get("end_cursor"):
                break
            cursor = str(response["end_cursor"])
        return items[:limit]

    def get_session(self, session_id: str) -> dict[str, Any]:
        normalized = normalize_devin_id(session_id)
        response = self.transport.request(
            "GET", f"{self.sessions_path}/{normalized}"
        )
        if normalize_devin_id(response.get("session_id")) != normalized:
            raise DevinApiError("DEVIN_EXACT_SESSION_ID_MISMATCH")
        return response

    def list_messages(
        self,
        session_id: str,
        *,
        limit: int = 5_000,
    ) -> list[dict[str, Any]]:
        normalized = normalize_devin_id(session_id)
        remaining = min(max(int(limit), 1), 20_000)
        cursor: str | None = None
        items: list[dict[str, Any]] = []
        while remaining > 0:
            page_size = min(remaining, 200)
            response = self.transport.request(
                "GET",
                f"{self.sessions_path}/{normalized}/messages",
                query={"first": page_size, "after": cursor},
            )
            page = response.get("items")
            if not isinstance(page, list):
                raise DevinApiError("DEVIN_MESSAGE_LIST_SHAPE_INVALID")
            for item in page:
                if isinstance(item, dict):
                    items.append(item)
            remaining = limit - len(items)
            if not response.get("has_next_page") or not response.get("end_cursor"):
                break
            cursor = str(response["end_cursor"])
        return items[:limit]

    def create_session(
        self,
        *,
        prompt: str,
        title: str = "Megas",
        repos: list[str] | None = None,
        max_acu_limit: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, object] = {
            "prompt": str(prompt),
            "title": str(title or "Megas")[:200],
            "tags": ["megas"],
        }
        if repos:
            body["repos"] = [str(item) for item in repos[:20] if str(item).strip()]
        if max_acu_limit is not None:
            body["max_acu_limit"] = min(max(int(max_acu_limit), 1), 100)
        response = self.transport.request("POST", self.sessions_path, body=body)
        normalize_devin_id(response.get("session_id"))
        return response

    def send_message(self, session_id: str, message: str) -> dict[str, Any]:
        normalized = normalize_devin_id(session_id)
        response = self.transport.request(
            "POST",
            f"{self.sessions_path}/{normalized}/messages",
            body={"message": str(message)},
        )
        returned = normalize_devin_id(response.get("session_id"))
        if returned != normalized:
            raise DevinApiError("DEVIN_EXACT_SESSION_ID_MISMATCH")
        return response

    def terminate_session(
        self,
        session_id: str,
        *,
        archive: bool = False,
    ) -> dict[str, Any]:
        normalized = normalize_devin_id(session_id)
        response = self.transport.request(
            "DELETE",
            f"{self.sessions_path}/{normalized}",
            query={"archive": str(bool(archive)).lower()},
        )
        returned = normalize_devin_id(response.get("session_id"))
        if returned != normalized:
            raise DevinApiError("DEVIN_EXACT_SESSION_ID_MISMATCH")
        return response
