"""OpenCode HTTP control plane.

The server is reused per resolved executable. Every session request is scoped to
an approved workspace with ``x-opencode-directory``. CLI remains a fallback only
when the HTTP path fails before a native session is selected.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from .base import windows_subprocess_kwargs


def _home_state_dir() -> Path:
    root = Path(os.environ.get("MEGAS_HOME") or (Path.home() / ".megasmoves"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _bindings_path() -> Path:
    return _home_state_dir() / "opencode_session_bindings.json"


def load_bindings() -> dict[str, Any]:
    path = _bindings_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_binding(
    *,
    megas_session_id: str,
    provider_session_id: str,
    message_id: str = "",
    worker_id: str = "",
) -> None:
    """Persist Megas session ↔ OpenCode session mapping across worker restarts."""
    path = _bindings_path()
    data = load_bindings()
    key = str(megas_session_id or "").strip()
    session_id = str(provider_session_id or "").strip()
    if not key or not session_id:
        return
    updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data[key] = {
        "provider_session_id": session_id,
        "message_id": str(message_id or "").strip(),
        "worker_id": str(worker_id or "").strip(),
        "updated_at": updated_at,
    }
    data[f"native:{session_id}"] = {
        "megas_session_id": key,
        "provider_session_id": session_id,
        "message_id": str(message_id or "").strip(),
        "worker_id": str(worker_id or "").strip(),
        "updated_at": updated_at,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def resolve_bound_session(megas_session_id: str = "", provider_session_id: str = "") -> str:
    data = load_bindings()
    if provider_session_id.strip().startswith("ses_"):
        return provider_session_id.strip()
    key = str(megas_session_id or "").strip()
    if key and isinstance(data.get(key), dict):
        session_id = str(data[key].get("provider_session_id") or "").strip()
        if session_id.startswith("ses_"):
            return session_id
    return ""


def parse_model_ref(raw: Any) -> Optional[dict[str, str]]:
    if isinstance(raw, dict):
        provider = str(raw.get("providerID") or raw.get("provider_id") or "").strip()
        model = str(raw.get("modelID") or raw.get("model_id") or "").strip()
        if provider and model:
            return {"providerID": provider, "modelID": model}
        return None
    text = str(raw or "").strip()
    if not text:
        return None
    if "/" not in text:
        return {"providerID": "opencode", "modelID": text}
    provider, model = text.split("/", 1)
    provider = provider.strip()
    model = model.strip()
    if not provider or not model:
        return None
    return {"providerID": provider, "modelID": model}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class OpenCodeServerHandle:
    base_url: str
    version: str
    username: str
    password: str
    process: Optional[subprocess.Popen]


class OpenCodeControlPlane:
    """Reuse one supervised server per resolved OpenCode executable."""

    _lock = threading.Lock()
    _handles: dict[str, OpenCodeServerHandle] = {}

    def __init__(self, binary: str = "opencode", *, directory: str | Path | None = None) -> None:
        self.binary = binary or "opencode"
        self.binary_key = str(Path(self.binary).expanduser().resolve(strict=False))
        self.directory = (
            str(Path(directory).expanduser().resolve(strict=False)) if directory is not None else ""
        )

    def ensure(self) -> OpenCodeServerHandle:
        with self._lock:
            handle = self._handles.get(self.binary_key)
            if handle is not None:
                if self._health_ok(handle):
                    return handle
                self._stop_unlocked()
            return self._start_unlocked()

    @staticmethod
    def _auth_header(handle: OpenCodeServerHandle) -> dict[str, str]:
        token = base64.b64encode(
            f"{handle.username}:{handle.password}".encode("utf-8")
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def _request(
        self,
        handle: OpenCodeServerHandle,
        method: str,
        path: str,
        body: Any = None,
        *,
        timeout: float = 30.0,
    ) -> tuple[int, Any]:
        url = handle.base_url.rstrip("/") + path
        payload = None
        headers = dict(self._auth_header(handle))
        if self.directory:
            headers["x-opencode-directory"] = self.directory
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                if not raw:
                    return int(response.status), None
                try:
                    return int(response.status), json.loads(raw)
                except json.JSONDecodeError:
                    return int(response.status), raw
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = raw
            return int(exc.code), parsed

    def _health_ok(self, handle: OpenCodeServerHandle) -> bool:
        try:
            status, data = self._request(handle, "GET", "/global/health", timeout=5.0)
            if status != 200 or not isinstance(data, dict) or data.get("healthy") is not True:
                return False
            version = str(data.get("version") or "").strip()
            if version:
                handle.version = version
            return True
        except Exception:
            return False

    def _stop_unlocked(self) -> None:
        handle = self._handles.pop(self.binary_key, None)
        if handle is None or handle.process is None:
            return
        try:
            handle.process.terminate()
            handle.process.wait(timeout=5)
        except Exception:
            try:
                handle.process.kill()
            except Exception:
                pass

    def _start_unlocked(self) -> OpenCodeServerHandle:
        port = _free_port()
        username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode").strip() or "opencode"
        password = os.environ.get("OPENCODE_SERVER_PASSWORD", "").strip()
        if not password:
            password = secrets.token_urlsafe(24)
        try:
            health_timeout = float(os.environ.get("MEGAS_OPENCODE_SERVE_HEALTH_SEC", "45"))
        except ValueError:
            health_timeout = 45.0
        env = dict(os.environ)
        env["OPENCODE_SERVER_USERNAME"] = username
        env["OPENCODE_SERVER_PASSWORD"] = password
        env["NO_COLOR"] = "1"
        log_path = _home_state_dir() / "opencode-serve.log"
        log_file = open(log_path, "ab", buffering=0)
        process = subprocess.Popen(
            [
                self.binary,
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=str(_home_state_dir()),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            **windows_subprocess_kwargs(new_group=True),
        )
        handle = OpenCodeServerHandle(
            base_url=f"http://127.0.0.1:{port}",
            version="",
            username=username,
            password=password,
            process=process,
        )
        deadline = time.time() + max(10.0, health_timeout)
        while time.time() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"opencode serve exited early (code={process.returncode})")
            if self._health_ok(handle):
                self._handles[self.binary_key] = handle
                return handle
            time.sleep(0.5)
        self._stop_unlocked()
        raise RuntimeError("opencode serve failed health gate (GET /global/health)")

    def create_session(self, *, title: str = "") -> dict[str, Any]:
        handle = self.ensure()
        body: dict[str, Any] = {}
        if title.strip():
            body["title"] = title.strip()[:200]
        status, data = self._request(handle, "POST", "/session", body)
        if status not in {200, 201} or not isinstance(data, dict):
            raise RuntimeError(f"opencode create session failed HTTP {status}: {data!r}")
        session_id = str(data.get("id") or "").strip()
        if not session_id.startswith("ses_"):
            raise RuntimeError(f"opencode create session missing ses_ id: {data!r}")
        return data

    def get_session(self, session_id: str) -> dict[str, Any]:
        handle = self.ensure()
        encoded = quote(str(session_id or "").strip(), safe="")
        status, data = self._request(handle, "GET", f"/session/{encoded}")
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(f"opencode get session failed HTTP {status}: {data!r}")
        returned_id = str(data.get("id") or "").strip()
        if returned_id and returned_id != session_id:
            raise RuntimeError(
                f"opencode returned wrong session id {returned_id!r}; expected {session_id!r}"
            )
        return data

    def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        handle = self.ensure()
        encoded = quote(str(session_id or "").strip(), safe="")
        status, data = self._request(handle, "GET", f"/session/{encoded}/message")
        if status != 200:
            raise RuntimeError(f"opencode list messages failed HTTP {status}: {data!r}")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("messages", "data", "items"):
                items = data.get(key)
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
        return []

    def session_status(self, session_id: str) -> str:
        """Return OpenCode's authoritative lifecycle state for one session.

        OpenCode omits idle sessions from ``GET /session/status``. Treating a
        missing exact ID as idle is therefore intentional; unknown response
        shapes fail closed so a new prompt is never submitted into an active
        turn by guesswork.
        """

        handle = self.ensure()
        exact_session_id = str(session_id or "").strip()
        status, data = self._request(
            handle,
            "GET",
            "/session/status",
            timeout=10.0,
        )
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(
                f"opencode session status failed HTTP {status}: {data!r}"
            )
        raw = data.get(exact_session_id)
        if raw is None:
            return "idle"
        if isinstance(raw, str):
            state = raw
        elif isinstance(raw, dict):
            state = str(raw.get("type") or raw.get("status") or "")
        else:
            state = ""
        normalized = state.strip().lower()
        if normalized not in {"idle", "busy", "retry"}:
            raise RuntimeError(
                "opencode session status returned an unsupported state for "
                f"{exact_session_id}: {raw!r}"
            )
        return normalized

    def wait_until_idle(
        self,
        session_id: str,
        *,
        timeout_sec: float = 30.0,
        poll_interval_sec: float = 0.6,
    ) -> None:
        """Wait until the exact session is idle before accepting another turn."""

        deadline = time.monotonic() + max(1.0, float(timeout_sec))
        last_state = "unknown"
        while time.monotonic() < deadline:
            last_state = self.session_status(session_id)
            if last_state == "idle":
                return
            time.sleep(max(0.01, float(poll_interval_sec)))
        raise TimeoutError(
            "OPENCODE_SESSION_BUSY_TIMEOUT: exact session did not become idle "
            f"within {float(timeout_sec):.1f}s (session={session_id}, state={last_state})"
        )

    def prompt_async(
        self,
        session_id: str,
        *,
        text: str,
        model: Any = None,
        agent: str = "",
        variant: str = "",
        persistence_timeout_sec: float = 15.0,
    ) -> str:
        """Submit one turn and return OpenCode's native user-message ID.

        OpenCode message IDs are ordered provider identities. Supplying a
        Megas-derived ID can leave the user row ordered after every generated
        assistant row, causing OpenCode to process the same turn repeatedly.
        Let OpenCode allocate the ID, then bind to the one new native user row
        created by this request. Ambiguous concurrent writes fail closed.
        """

        handle = self.ensure()
        encoded = quote(str(session_id or "").strip(), safe="")
        exact_text = str(text or "")
        before_messages = self.list_messages(session_id)
        before_ids = {
            _message_id(item)
            for item in before_messages
            if _message_id(item)
        }
        body: dict[str, Any] = {
            "parts": [{"type": "text", "text": exact_text}],
        }
        model_object = parse_model_ref(model)
        if model_object:
            body["model"] = model_object
        if str(agent or "").strip():
            body["agent"] = str(agent).strip()[:100]
        if str(variant or "").strip():
            body["variant"] = str(variant).strip()[:80]
        status, data = self._request(
            handle,
            "POST",
            f"/session/{encoded}/prompt_async",
            body,
            timeout=180.0,
        )
        if status not in {200, 204}:
            raise RuntimeError(f"opencode prompt_async failed HTTP {status}: {data!r}")

        deadline = time.monotonic() + max(1.0, float(persistence_timeout_sec))
        while time.monotonic() < deadline:
            messages = self.list_messages(session_id)
            candidates = [
                item
                for item in messages
                if _message_role(item) == "user"
                and _message_id(item)
                and _message_id(item) not in before_ids
                and _message_text(item) == exact_text
            ]
            if len(candidates) == 1:
                return _message_id(candidates[0])
            if len(candidates) > 1:
                raise RuntimeError(
                    "OPENCODE_NATIVE_USER_ID_AMBIGUOUS: multiple new native user "
                    f"messages matched one submission (session={session_id})"
                )
            time.sleep(0.05)
        raise TimeoutError(
            "OPENCODE_NATIVE_USER_PERSISTENCE_TIMEOUT: OpenCode did not persist "
            f"one native user message within {float(persistence_timeout_sec):.1f}s "
            f"(session={session_id})"
        )

    def wait_for_assistant_after(
        self,
        session_id: str,
        *,
        user_message_id: str,
        timeout_sec: float = 300.0,
        poll_interval_sec: float = 0.6,
    ) -> tuple[list[dict[str, Any]], str]:
        """Wait for a completed turn correlated to the exact user message ID."""
        deadline = time.monotonic() + max(5.0, float(timeout_sec))
        last: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            last = self.list_messages(session_id)
            text = _assistant_text_after(last, user_message_id)
            user_persisted = any(
                _message_role(item) == "user"
                and _message_id(item) == user_message_id
                for item in last
            )
            state = self.session_status(session_id)
            if user_persisted and state == "idle" and text is not None:
                return last, text
            time.sleep(max(0.01, float(poll_interval_sec)))
        return last, ""


def _message_id(item: dict[str, Any]) -> str:
    info = item.get("info") if isinstance(item.get("info"), dict) else item
    return str((info or {}).get("id") or item.get("id") or "").strip()


def _message_role(item: dict[str, Any]) -> str:
    info = item.get("info") if isinstance(item.get("info"), dict) else item
    return str((info or {}).get("role") or item.get("role") or "").strip().lower()


def _message_parent_id(item: dict[str, Any]) -> str:
    info = item.get("info") if isinstance(item.get("info"), dict) else item
    return str((info or {}).get("parentID") or (info or {}).get("parent_id") or "").strip()


def _message_error(item: dict[str, Any]) -> str:
    info = item.get("info") if isinstance(item.get("info"), dict) else item
    error = (info or {}).get("error") if isinstance(info, dict) else None
    if error is None:
        error = item.get("error")
    if isinstance(error, dict):
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        for key in ("message", "name", "type"):
            value = data.get(key) if isinstance(data, dict) else None
            if not value:
                value = error.get(key)
            if value:
                return str(value).strip()
        return json.dumps(error)[:2_000]
    if isinstance(error, str) and error.strip():
        return error.strip()
    return ""


def _message_text(item: dict[str, Any]) -> str:
    parts = item.get("parts")
    if not isinstance(parts, list):
        message = item.get("message") if isinstance(item.get("message"), dict) else {}
        parts = message.get("parts") if isinstance(message.get("parts"), list) else []
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("ignored") is True:
            continue
        part_type = str(part.get("type") or "").lower()
        if part_type in {"text", "output_text", "content"}:
            text = str(part.get("text") or part.get("content") or "").strip()
            if text:
                chunks.append(text)
    if chunks:
        return "\n".join(chunks).strip()
    for key in ("text", "content", "output"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _assistant_completed(item: dict[str, Any]) -> bool:
    info = item.get("info") if isinstance(item.get("info"), dict) else item
    if not isinstance(info, dict):
        return False
    time_obj = info.get("time") if isinstance(info.get("time"), dict) else {}
    if time_obj.get("completed") is not None:
        return True
    return info.get("error") is not None or item.get("error") is not None


def _assistant_text_after(messages: list[dict[str, Any]], user_message_id: str) -> Optional[str]:
    """Return the last direct assistant response for the exact user message."""
    seen_user = False
    candidate: Optional[str] = None
    for item in messages:
        role = _message_role(item)
        message_id = _message_id(item)
        if not seen_user:
            if role == "user" and message_id == user_message_id:
                seen_user = True
            continue
        if role not in {"assistant", "ai", "model"}:
            continue
        parent_id = _message_parent_id(item)
        if parent_id != user_message_id:
            continue
        text = _message_text(item)
        if text:
            candidate = text
            continue
        error = _message_error(item)
        if error:
            candidate = f"[opencode error] {error}"
            continue
        if _assistant_completed(item) and candidate is None:
            candidate = ""
    return candidate


def messages_to_thread_payload(
    *,
    session_id: str,
    messages: list[dict[str, Any]],
    title: str = "",
) -> dict[str, Any]:
    out_messages: list[dict[str, Any]] = []
    sequence = 0
    for item in messages:
        role = _message_role(item)
        text = _message_text(item)
        if not text or role not in {"user", "human", "assistant", "ai", "model", "system"}:
            continue
        sequence += 1
        mapped_role = (
            "user"
            if role in {"user", "human"}
            else "system" if role == "system" else "assistant"
        )
        out_messages.append(
            {
                "seq": sequence,
                "role": mapped_role,
                "text": text[:50_000],
                "native_item_id": _message_id(item),
            }
        )
    return {
        "agent_type": "opencode",
        "native_id": session_id,
        "title": title or f"OpenCode {session_id}",
        "messages": out_messages,
        "source": "opencode_http",
        "provider_session_id": session_id,
    }
