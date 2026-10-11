from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

from megas.policy import redact

from .capability import browser_runtime_root, configure_playwright_runtime
from .control import (
    BrowserCheckpoint,
    BrowserCheckpointStore,
    BrowserFailure,
    BrowserFileVault,
    IRREVERSIBLE_ACTIONS,
    SAFE_RETRY_ACTIONS,
    download_quarantine,
    evidence_root,
    page_signature,
)


READ_ACTIONS = {
    "list_tabs",
    "get_page_state",
    "inspect_page",
    "observe",
    "screenshot",
    "wait",
}
LOW_RISK_ACTIONS = {
    "navigate",
    "click",
    "double_click",
    "hover",
    "focus",
    "type_text",
    "fill_field",
    "select_option",
    "check",
    "uncheck",
    "scroll",
    "switch_tab",
    "takeover_request",
    "takeover_release",
}
SENSITIVE_ACTIONS = {"upload_file", "download", "submit_form", "enter_credentials"}
DESTRUCTIVE_ACTIONS = {"delete_data", "submit_purchase", "transfer_money", "message_send", "legal_attestation"}
SUPPORTED_ACTIONS = READ_ACTIONS | LOW_RISK_ACTIONS | SENSITIVE_ACTIONS | DESTRUCTIVE_ACTIONS
MAX_OBSERVED_ELEMENTS = 200
MAX_OBSERVATION_TEXT = 50_000
MAX_TIMELINE = 500
MAX_DIAGNOSTICS = 200
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


async def _start_playwright() -> Any:
    configure_playwright_runtime()
    from playwright.async_api import async_playwright

    return await async_playwright().start()


def canonical_action_digest(action: dict[str, Any]) -> str:
    payload = {
        "action_type": str(action.get("action_type") or "").strip().lower(),
        "params": action.get("params") if isinstance(action.get("params"), dict) else {},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def action_risk(action_type: str) -> str:
    normalized = action_type.strip().lower()
    if normalized in READ_ACTIONS:
        return "read_only"
    if normalized in LOW_RISK_ACTIONS:
        return "low_risk_write"
    if normalized in SENSITIVE_ACTIONS:
        return "sensitive"
    if normalized in DESTRUCTIVE_ACTIONS:
        return "destructive"
    raise ValueError("unsupported_browser_action")


def _product_root() -> Path:
    if os.name == "nt":
        local = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        root = local / "Amretra" / "MegasMoves"
    else:
        configured = os.getenv("MEGAS_HOME", "").strip()
        root = Path(configured).expanduser() if configured else Path.home() / ".megas"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _profile_root() -> Path:
    path = _product_root() / "profiles"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _safe_component(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return f"profile-{digest}"


def _safe_url_for_evidence(value: str) -> str:
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https", "about"}:
            return ""
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))[:4000]
    except Exception:
        return ""


class _ProfileLease:
    def __init__(self, path: Path, browser_session_id: str) -> None:
        self.path = path
        self.browser_session_id = browser_session_id
        self._handle: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="ascii")
        try:
            handle.seek(0)
            if handle.read(1) == "":
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            json.dump(
                {
                    "browser_session_id": self.browser_session_id,
                    "pid": os.getpid(),
                    "acquired_at": datetime.now(timezone.utc).isoformat(),
                },
                handle,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
            self._handle = handle
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise RuntimeError("browser_profile_in_use") from exc

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _browser_address_allowed(address: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_private: bool) -> bool:
    # Loopback is a first-class Megas local-testing target. It is intentionally
    # distinct from broader private-network access so localhost development does
    # not require opening the browser to RFC1918/ULA or cloud metadata ranges.
    if address.is_loopback:
        return True
    if address.is_global:
        return True
    if not allow_private:
        return False
    # Owner-enabled private-network access still refuses link-local metadata,
    # multicast, unspecified and reserved destinations.
    return bool(
        address.is_private
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
    )


async def _validate_navigation_url(url: str) -> str:
    normalized = url.strip()
    if normalized == "about:blank":
        return normalized
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise PermissionError("Browser navigation requires an http(s) URL without embedded credentials.")
    allow_private = os.getenv("MEGAS_BROWSER_ALLOW_PRIVATE_NETWORK", "").lower() in {"1", "true", "yes"}
    hostname = parsed.hostname.lower()
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise PermissionError("Browser navigation target could not be resolved.") from exc
    resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for result in addresses:
        try:
            address = ipaddress.ip_address(result[4][0])
        except ValueError as exc:
            raise PermissionError("Browser navigation target resolved to an invalid address.") from exc
        resolved.append(address)
        if not _browser_address_allowed(address, allow_private=allow_private):
            raise PermissionError("Private-network browser navigation is disabled for this target.")
    if not resolved:
        raise PermissionError("Browser navigation target could not be resolved.")
    return normalized


async def _guard_browser_request(route: Any, request: Any) -> None:
    try:
        parsed = urlparse(str(request.url))
        if parsed.scheme in {"http", "https"}:
            await _validate_navigation_url(str(request.url))
    except PermissionError:
        await route.abort("blockedbyclient")
        return
    await route.continue_()


def _engine_executable(engine: Any) -> Path:
    try:
        value = str(engine.executable_path)
    except Exception as exc:
        raise RuntimeError("dedicated_browser_runtime_unavailable") from exc
    candidate = Path(value).expanduser().resolve()
    root = browser_runtime_root().resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("dedicated_browser_runtime_outside_megas_root") from exc
    if not candidate.is_file():
        raise RuntimeError("dedicated_browser_runtime_unavailable")
    return candidate


def _bounded_text(value: Any, limit: int = 2000) -> str:
    return redact(str(value or ""))[:limit]


@dataclass
class BrowserRuntimeSession:
    browser_session_id: str
    session_id: str
    task_id: str
    agent_id: str | None
    browser_type: str
    profile_id: str | None
    profile_mode: str
    allowed_folders: list[str]
    file_grants: list[dict[str, Any]] = field(default_factory=list)
    objective: str | None = None
    context: Any = None
    browser: Any = None
    playwright: Any = None
    profile_path: Path | None = None
    profile_lease: _ProfileLease | None = None
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    takeover_active: bool = False
    _results: OrderedDict[str, dict[str, Any]] = field(default_factory=OrderedDict)
    _page_ids: dict[int, str] = field(default_factory=dict)
    _element_refs: dict[str, dict[str, Any]] = field(default_factory=dict)
    _active_tab_id: str | None = None
    _timeline: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_TIMELINE))
    _console: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_DIAGNOSTICS))
    _network: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_DIAGNOSTICS))
    _trace_active: bool = False
    _resetting: bool = False
    _retry_count: int = 0
    _step_index: int = 0

    def __post_init__(self) -> None:
        self.file_vault = BrowserFileVault(self.allowed_folders, self.file_grants)
        self.checkpoints = BrowserCheckpointStore()

    async def start(self, initial_url: str = "about:blank") -> dict[str, Any]:
        configure_playwright_runtime()
        self.playwright = await _start_playwright()
        engine = self.playwright.chromium
        executable = _engine_executable(engine)
        headless_env = os.getenv("MEGAS_BROWSER_HEADLESS", "").lower()
        headless = headless_env in {"1", "true", "yes"} or (
            not headless_env and sys.platform.startswith("linux") and not os.getenv("DISPLAY")
        )
        launch_options = {
            "headless": headless,
            "handle_sigint": False,
            "handle_sigterm": False,
            "handle_sighup": False,
        }
        if self.profile_mode == "persistent" and self.profile_id:
            self.profile_path = _profile_root() / _safe_component(self.profile_id)
            self.profile_path.mkdir(parents=True, exist_ok=True)
            self.profile_lease = _ProfileLease(
                self.profile_path / ".megas-session.lock", self.browser_session_id
            )
            self.profile_lease.acquire()
            self.context = await engine.launch_persistent_context(
                str(self.profile_path),
                accept_downloads=True,
                **launch_options,
            )
        else:
            self.profile_path = _profile_root() / "sessions" / _safe_component(self.browser_session_id)
            self.profile_path.mkdir(parents=True, exist_ok=True)
            self.browser = await engine.launch(**launch_options)
            self.context = await self.browser.new_context(accept_downloads=True)
            if hasattr(self.browser, "on"):
                self.browser.on("disconnected", self._on_browser_disconnected)
        await self.context.route("**/*", _guard_browser_request)
        self.context.on("close", self._on_context_closed)
        if hasattr(self.context, "on"):
            self.context.on("page", self._attach_page)
        await self._start_trace()
        pages = list(self.context.pages)
        page = pages[0] if pages else await self.context.new_page()
        for item in list(self.context.pages):
            self._attach_page(item)
        target = await _validate_navigation_url(initial_url or "about:blank")
        if target != "about:blank":
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                raise self._classify_exception(exc, "navigate") from exc
        state = await self._page_state(page)
        self._active_tab_id = state["tab_id"]
        self._timeline_event("browser.started", state="verified", url=state.get("url"), runtime=str(executable))
        await self._save_checkpoint(page, action_id="startup", action_type="start")
        return {
            **state,
            "runtime": {"dedicated": True, "engine": "chromium", "executable": executable.name},
            "profile_mode": self.profile_mode,
            "takeover": self.takeover_active,
        }

    def _on_context_closed(self, *_args: Any) -> None:
        if not self._resetting:
            self.closed.set()

    def _on_browser_disconnected(self, *_args: Any) -> None:
        if not self._resetting:
            self._timeline_event("browser.disconnected", state="failed")

    def _attach_page(self, page: Any) -> None:
        if getattr(page, "_megas_observed", False):
            return
        try:
            setattr(page, "_megas_observed", True)
        except Exception:
            pass
        if not hasattr(page, "on"):
            return
        try:
            page.on(
                "console",
                lambda message: self._console.append(
                    {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "type": _bounded_text(getattr(message, "type", "console"), 40),
                        "text": _bounded_text(getattr(message, "text", ""), 1000),
                    }
                ),
            )
            page.on(
                "response",
                lambda response: self._network.append(
                    {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "status": int(getattr(response, "status", 0) or 0),
                        "url": _safe_url_for_evidence(str(getattr(response, "url", ""))),
                    }
                ),
            )
            page.on(
                "crash",
                lambda *_: self._timeline_event("page.crashed", state="failed", url=_safe_url_for_evidence(str(getattr(page, "url", "")))),
            )
        except Exception:
            return

    async def _start_trace(self) -> None:
        tracing = getattr(self.context, "tracing", None)
        if tracing is None or self._trace_active:
            return
        try:
            await tracing.start(screenshots=True, snapshots=True, sources=False)
            self._trace_active = True
        except Exception:
            self._trace_active = False

    async def _rotate_trace(self, action_id: str) -> str | None:
        tracing = getattr(self.context, "tracing", None)
        if tracing is None or not self._trace_active:
            return None
        path = evidence_root(self.browser_session_id) / f"failure-{hashlib.sha256(action_id.encode()).hexdigest()[:16]}.zip"
        try:
            await tracing.stop(path=str(path))
            self._trace_active = False
            await self._start_trace()
            return str(path)
        except Exception:
            self._trace_active = False
            return None

    async def execute(
        self,
        action_id: str,
        action: dict[str, Any],
        approval: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if action_id in self._results:
            return {**self._results[action_id], "already_executed": True}
        if not self.context or self.closed.is_set():
            raise BrowserFailure(
                "BROWSER_DISCONNECTED",
                "Dedicated browser session is not active.",
                recoverable=True,
            )
        action_type = str(action.get("action_type") or "").strip().lower()
        if action_type not in SUPPORTED_ACTIONS:
            raise ValueError("unsupported_browser_action")
        if self.takeover_active and action_type not in {
            "get_page_state",
            "inspect_page",
            "screenshot",
            "list_tabs",
            "takeover_release",
        }:
            raise BrowserFailure(
                "HUMAN_REQUIRED",
                "Agent browser actions are paused while the operator has control.",
                recoverable=True,
                human_required=True,
            )
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        risk = action_risk(action_type)
        digest = canonical_action_digest({"action_type": action_type, "params": params})
        self._require_approval(risk, digest, approval)

        started = time.monotonic()
        self._timeline_event("browser.action.started", action_id=action_id, action_type=action_type, state="active")
        attempts = 0
        recovery_steps: list[str] = []
        while True:
            attempts += 1
            try:
                result, verification = await self._execute_once(action_type, params)
                duration_ms = max(0, int((time.monotonic() - started) * 1000))
                evidence = {
                    "kind": "browser_action",
                    "browser_session_id": self.browser_session_id,
                    "task_id": self.task_id,
                    "agent_id": self.agent_id,
                    "action_id": action_id,
                    "action_type": action_type,
                    "action_digest": digest,
                    "risk": risk,
                    "url": _safe_url_for_evidence(str(result.get("url") or "")),
                    "title": _bounded_text(result.get("title"), 500),
                    "screenshot_sha256": result.get("sha256"),
                    "duration_ms": duration_ms,
                    "attempts": attempts,
                    "verification": verification,
                    "recovery_steps": recovery_steps,
                }
                accepted = {"result": result, "evidence": evidence, "already_executed": False}
                self._results[action_id] = accepted
                while len(self._results) > 256:
                    self._results.popitem(last=False)
                page = await self._page(params)
                self._step_index += 1
                await self._save_checkpoint(page, action_id=action_id, action_type=action_type, result=result)
                self._timeline_event(
                    "browser.action.completed",
                    action_id=action_id,
                    action_type=action_type,
                    state="verified",
                    duration_ms=duration_ms,
                )
                return accepted
            except BrowserFailure as failure:
                evidence = await self._capture_failure(action_id, action_type, failure)
                failure.evidence.update(evidence)
                if attempts == 1 and failure.recoverable and action_type in SAFE_RETRY_ACTIONS:
                    recovery_steps.append("re-observe")
                    try:
                        page = await self._page(params)
                        await self._observe(page)
                    except Exception:
                        pass
                    recovery_steps.append("bounded-retry")
                    self._retry_count += 1
                    continue
                if (
                    attempts == 1
                    and failure.code in {"PAGE_CRASHED", "BROWSER_DISCONNECTED"}
                    and action_type in SAFE_RETRY_ACTIONS
                ):
                    recovery_steps.append("restart-dedicated-browser")
                    await self._restart_from_checkpoint()
                    self._retry_count += 1
                    continue
                self._timeline_event(
                    "browser.action.failed",
                    action_id=action_id,
                    action_type=action_type,
                    state="failed",
                    error_code=failure.code,
                )
                raise
            except Exception as exc:
                failure = self._classify_exception(exc, action_type)
                evidence = await self._capture_failure(action_id, action_type, failure)
                failure.evidence.update(evidence)
                if attempts == 1 and failure.recoverable and action_type in SAFE_RETRY_ACTIONS:
                    recovery_steps.extend(["re-observe", "bounded-retry"])
                    self._retry_count += 1
                    continue
                self._timeline_event(
                    "browser.action.failed",
                    action_id=action_id,
                    action_type=action_type,
                    state="failed",
                    error_code=failure.code,
                )
                raise failure from exc

    def _require_approval(self, risk: str, digest: str, approval: dict[str, Any] | None) -> None:
        if risk not in {"sensitive", "destructive"}:
            return
        approval = approval if isinstance(approval, dict) else {}
        expires_at = str(approval.get("expires_at") or "")
        try:
            approval_current = (
                datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                > datetime.now(timezone.utc)
            )
        except (TypeError, ValueError):
            approval_current = False
        if (
            approval.get("approved") is not True
            or approval.get("action_digest") != digest
            or not approval_current
        ):
            raise BrowserFailure(
                "ACTION_NOT_ALLOWED",
                "Browser action requires a matching one-time approval.",
                recoverable=True,
                human_required=True,
            )
        if risk == "destructive" and os.getenv("MEGAS_BROWSER_DESTRUCTIVE_ENABLED", "").lower() not in {"1", "true", "yes"}:
            raise BrowserFailure(
                "ACTION_NOT_ALLOWED",
                "Destructive browser actions are disabled on this workstation.",
                recoverable=False,
            )

    async def _execute_once(self, action_type: str, params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        page = await self._page(params)
        before = await self._state_fingerprint(page)
        result: dict[str, Any]
        verification: dict[str, Any]

        if action_type == "list_tabs":
            result = {"tabs": [await self._page_state(item, include_text=False) for item in self.context.pages]}
            verification = {"verified": True, "postcondition": "tab-list-observed"}
        elif action_type == "get_page_state":
            result = await self._page_state(page)
            verification = {"verified": True, "postcondition": "page-observed"}
        elif action_type in {"inspect_page", "observe"}:
            result = await self._observe(page)
            verification = {"verified": True, "postcondition": "semantic-observation-captured"}
        elif action_type == "screenshot":
            image = await page.screenshot(type="jpeg", quality=65, full_page=bool(params.get("full_page", False)))
            if len(image) > 1_500_000:
                raise BrowserFailure("ACTION_TIMEOUT", "Browser screenshot exceeded the evidence size limit.", recoverable=False)
            result = {
                **(await self._page_state(page, include_text=False)),
                "image_base64": base64.b64encode(image).decode("ascii"),
                "media_type": "image/jpeg",
                "sha256": hashlib.sha256(image).hexdigest(),
                "size_bytes": len(image),
            }
            verification = {"verified": True, "postcondition": "screenshot-hash-captured"}
        elif action_type == "navigate":
            target = await _validate_navigation_url(str(params.get("url") or ""))
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=self._timeout(params))
            except Exception as exc:
                failure = self._classify_exception(exc, action_type)
                if failure.code == "ACTION_TIMEOUT":
                    failure.code = "NAVIGATION_TIMEOUT"
                raise failure from exc
            result = await self._page_state(page)
            current = str(result.get("url") or "")
            if target != "about:blank" and not self._same_navigation_target(target, current):
                raise BrowserFailure(
                    "OUTCOME_AMBIGUOUS",
                    "Navigation completed without reaching the expected destination.",
                    recoverable=True,
                )
            verification = {"verified": True, "postcondition": "expected-navigation-destination"}
        elif action_type == "switch_tab":
            tab_id = str(params.get("tab_id") or "").strip()
            selected = await self._page({"tab_id": tab_id})
            self._active_tab_id = tab_id
            if hasattr(selected, "bring_to_front"):
                await selected.bring_to_front()
            result = await self._page_state(selected)
            verification = {"verified": result["tab_id"] == tab_id, "postcondition": "requested-tab-active"}
        elif action_type == "wait":
            verification = await self._wait_condition(page, params.get("condition"), self._timeout(params))
            result = await self._page_state(page, include_text=False)
        elif action_type in {"takeover_request", "takeover_release"}:
            self.takeover_active = action_type == "takeover_request"
            result = {
                **(await self._page_state(page, include_text=False)),
                "takeover": self.takeover_active,
                "agent_paused": self.takeover_active,
            }
            verification = {"verified": True, "postcondition": "operator-control-state-updated"}
        elif action_type in {"click", "double_click", "hover", "focus"}:
            locator, resolution = await self._locator(page, params)
            await self._precondition(locator, params)
            method = {
                "click": locator.click,
                "double_click": locator.dblclick,
                "hover": locator.hover,
                "focus": locator.focus,
            }[action_type]
            await method(timeout=self._timeout(params))
            result = await self._page_state(page, include_text=False)
            verification = await self._verify_after_action(page, params, before, action_type, locator)
            verification["target_resolution"] = resolution
        elif action_type in {"type_text", "fill_field"}:
            locator, resolution = await self._locator(page, params)
            await self._precondition(locator, params)
            input_type = (await locator.get_attribute("type") or "").lower()
            if input_type == "password":
                raise BrowserFailure(
                    "HUMAN_REQUIRED",
                    "Password fields require the dedicated credential or human flow.",
                    recoverable=True,
                    human_required=True,
                )
            value = str(params.get("text") or params.get("value") or "")
            if action_type == "fill_field":
                await locator.fill(value, timeout=self._timeout(params))
            else:
                await locator.press_sequentially(value, timeout=self._timeout(params))
            actual = await self._input_value(locator)
            if actual is not None and actual != value:
                raise BrowserFailure("OUTCOME_AMBIGUOUS", "Input value did not match the intended value.", recoverable=True)
            result = await self._page_state(page, include_text=False)
            verification = {"verified": True, "postcondition": "input-value-matched", "target_resolution": resolution}
        elif action_type == "select_option":
            locator, resolution = await self._locator(page, params)
            await self._precondition(locator, params)
            expected = params.get("value")
            await locator.select_option(expected, timeout=self._timeout(params))
            actual = await self._input_value(locator)
            if actual is not None and str(actual) != str(expected):
                raise BrowserFailure("OUTCOME_AMBIGUOUS", "Selected option did not remain selected.", recoverable=True)
            result = await self._page_state(page, include_text=False)
            verification = {"verified": True, "postcondition": "selected-value-matched", "target_resolution": resolution}
        elif action_type in {"check", "uncheck"}:
            locator, resolution = await self._locator(page, params)
            await self._precondition(locator, params)
            method = locator.check if action_type == "check" else locator.uncheck
            await method(timeout=self._timeout(params))
            if hasattr(locator, "is_checked"):
                checked = bool(await locator.is_checked())
                if checked != (action_type == "check"):
                    raise BrowserFailure("OUTCOME_AMBIGUOUS", "Checkbox state did not match the requested state.", recoverable=True)
            result = await self._page_state(page, include_text=False)
            verification = {"verified": True, "postcondition": "checkbox-state-matched", "target_resolution": resolution}
        elif action_type == "scroll":
            delta_x = max(-10_000, min(10_000, int(params.get("delta_x") or 0)))
            delta_y = max(-10_000, min(10_000, int(params.get("delta_y") or 0)))
            await page.mouse.wheel(delta_x, delta_y)
            result = await self._page_state(page, include_text=False)
            verification = {"verified": True, "postcondition": "scroll-command-completed"}
        elif action_type == "upload_file":
            file_ref = str(params.get("file_ref") or "").strip()
            if not file_ref:
                raise BrowserFailure(
                    "ACTION_NOT_ALLOWED",
                    "Upload requires a task-approved file reference, not a local path.",
                    recoverable=False,
                )
            upload = self.file_vault.resolve(file_ref)
            locator, resolution = await self._locator(page, params)
            await locator.set_input_files(str(upload), timeout=self._timeout(params))
            uploaded_name = await self._uploaded_filename(locator)
            if uploaded_name and uploaded_name != upload.name:
                raise BrowserFailure("UPLOAD_FAILED", "Website file input did not contain the approved file.", recoverable=True)
            result = {**(await self._page_state(page, include_text=False)), "file_ref": file_ref, "filename": upload.name}
            verification = {"verified": True, "postcondition": "approved-filename-attached", "target_resolution": resolution}
        elif action_type == "download":
            locator, resolution = await self._locator(page, params)
            await self._precondition(locator, params)
            if not hasattr(page, "expect_download"):
                raise BrowserFailure("RECOVERY_EXHAUSTED", "Browser runtime cannot observe a download event.", recoverable=False)
            async with page.expect_download(timeout=self._timeout(params)) as info:
                await locator.click(timeout=self._timeout(params))
            download = await info.value
            filename = Path(str(download.suggested_filename or "download.bin")).name[:240] or "download.bin"
            destination = download_quarantine(self.browser_session_id) / filename
            await download.save_as(str(destination))
            size = destination.stat().st_size
            if size <= 0 or size > MAX_DOWNLOAD_BYTES:
                destination.unlink(missing_ok=True)
                raise BrowserFailure("OUTCOME_AMBIGUOUS", "Downloaded file failed quarantine size validation.", recoverable=False)
            digest = hashlib.sha256(destination.read_bytes()).hexdigest() if size <= 16 * 1024 * 1024 else await asyncio.to_thread(self._hash_file, destination)
            result = {
                **(await self._page_state(page, include_text=False)),
                "download_ref": f"download_{digest[:24]}",
                "filename": filename,
                "size_bytes": size,
                "sha256": digest,
                "quarantined": True,
            }
            verification = {"verified": True, "postcondition": "download-event-and-quarantined-file", "target_resolution": resolution}
        elif action_type == "submit_form":
            locator, resolution = await self._locator(page, params)
            await self._precondition(locator, params)
            method = str(params.get("submit_method") or "press_enter")
            if method == "click":
                await locator.click(timeout=self._timeout(params))
            else:
                await locator.press("Enter", timeout=self._timeout(params))
            result = await self._page_state(page, include_text=False)
            verification = await self._verify_after_action(page, params, before, action_type, locator, require_explicit=True)
            verification["target_resolution"] = resolution
        elif action_type == "enter_credentials":
            raise BrowserFailure(
                "HUMAN_REQUIRED",
                "Credential entry requires the dedicated credential or human takeover flow.",
                recoverable=True,
                human_required=True,
            )
        else:
            raise BrowserFailure(
                "ACTION_NOT_ALLOWED",
                "This irreversible browser operation is not enabled by the local policy.",
                recoverable=False,
            )
        return result, verification

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    async def stop(self, reason: str = "requested") -> dict[str, Any]:
        already_stopped = self.closed.is_set()
        errors: list[str] = []
        self._resetting = True
        try:
            tracing = getattr(self.context, "tracing", None)
            if tracing is not None and self._trace_active:
                try:
                    await tracing.stop()
                except Exception:
                    errors.append("trace_stop")
                self._trace_active = False
            if self.context:
                try:
                    await self.context.close()
                except Exception as exc:
                    errors.append(type(exc).__name__)
            if self.browser:
                try:
                    await self.browser.close()
                except Exception as exc:
                    errors.append(type(exc).__name__)
            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception as exc:
                    errors.append(type(exc).__name__)
        finally:
            if self.profile_lease:
                self.profile_lease.release()
                self.profile_lease = None
            if self.profile_mode != "persistent" and self.profile_path:
                shutil.rmtree(self.profile_path, ignore_errors=True)
            self.context = None
            self.browser = None
            self.playwright = None
            self._resetting = False
            self.closed.set()
        self._timeline_event("browser.stopped", state="verified", reason=reason)
        return {
            "browser_session_id": self.browser_session_id,
            "reason": reason,
            "already_stopped": already_stopped,
            "cleanup_ok": not errors,
            "cleanup_errors": errors,
        }

    async def _restart_from_checkpoint(self) -> None:
        checkpoint = self.checkpoints.latest(self.task_id)
        target = str((checkpoint or {}).get("current_url") or "about:blank")
        self._resetting = True
        try:
            if self.context:
                try:
                    await self.context.close()
                except Exception:
                    pass
            if self.browser:
                try:
                    await self.browser.close()
                except Exception:
                    pass
            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass
            if self.profile_lease:
                self.profile_lease.release()
                self.profile_lease = None
            self.context = None
            self.browser = None
            self.playwright = None
            self._trace_active = False
        finally:
            self._resetting = False
        await self.start(target)
        self._timeline_event("browser.recovered", state="verified", checkpoint=bool(checkpoint))

    async def _page(self, params: dict[str, Any]) -> Any:
        pages = list(self.context.pages)
        tab_id = str(params.get("tab_id") or self._active_tab_id or "")
        if tab_id:
            for index, page in enumerate(pages):
                if self._tab_id(page, index) == tab_id:
                    return page
            if params.get("tab_id"):
                raise BrowserFailure("TARGET_NOT_FOUND", "Requested browser tab no longer exists.", recoverable=True)
        if pages:
            page = pages[-1]
            self._active_tab_id = self._tab_id(page, len(pages) - 1)
            return page
        page = await self.context.new_page()
        self._attach_page(page)
        self._active_tab_id = self._tab_id(page, 0)
        return page

    async def _page_state(self, page: Any, include_text: bool = False) -> dict[str, Any]:
        pages = list(self.context.pages)
        try:
            index = pages.index(page)
        except ValueError:
            index = 0
        state: dict[str, Any] = {
            "tab_id": self._tab_id(page, index),
            "url": str(page.url),
            "title": (await page.title())[:500],
            "takeover": self.takeover_active,
        }
        if include_text:
            text = await page.locator("body").inner_text(timeout=10_000)
            state["text"] = text[:MAX_OBSERVATION_TEXT]
            state["text_truncated"] = len(text) > MAX_OBSERVATION_TEXT
        return state

    async def _observe(self, page: Any) -> dict[str, Any]:
        state = await self._page_state(page, include_text=True)
        elements: list[dict[str, Any]] = []
        script = """
        (limit) => Array.from(document.querySelectorAll(
          'a[href],button,input,select,textarea,[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="tab"]'
        )).slice(0, limit).map((el, index) => {
          const label = el.labels && el.labels.length ? (el.labels[0].innerText || '').trim() : '';
          const text = (el.innerText || '').trim();
          const aria = (el.getAttribute('aria-label') || '').trim();
          const placeholder = (el.getAttribute('placeholder') || '').trim();
          const role = (el.getAttribute('role') || '').trim();
          const tag = el.tagName.toLowerCase();
          const type = (el.getAttribute('type') || '').toLowerCase();
          return {index, tag, type, role, name: aria || label || text || placeholder, label, placeholder, text: text.slice(0,300), disabled: !!el.disabled};
        })
        """
        rows: list[Any] = []
        if hasattr(page, "evaluate"):
            try:
                value = await page.evaluate(script, MAX_OBSERVED_ELEMENTS)
                if isinstance(value, list):
                    rows = value
            except Exception:
                rows = []
        self._element_refs.clear()
        for row in rows[:MAX_OBSERVED_ELEMENTS]:
            if not isinstance(row, dict):
                continue
            material = json.dumps(
                {
                    "tab": state["tab_id"],
                    "index": row.get("index"),
                    "role": row.get("role"),
                    "name": row.get("name"),
                    "tag": row.get("tag"),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            ref = f"el_{hashlib.sha256(material.encode()).hexdigest()[:20]}"
            entry = {
                "ref": ref,
                "role": str(row.get("role") or "")[:40],
                "name": str(row.get("name") or "")[:300],
                "label": str(row.get("label") or "")[:300],
                "placeholder": str(row.get("placeholder") or "")[:300],
                "text": str(row.get("text") or "")[:300],
                "tag": str(row.get("tag") or "")[:40],
                "type": str(row.get("type") or "")[:40],
                "disabled": bool(row.get("disabled", False)),
                "index": int(row.get("index") or 0),
                "tab_id": state["tab_id"],
            }
            self._element_refs[ref] = entry
            elements.append({key: entry[key] for key in ("ref", "role", "name", "label", "placeholder", "tag", "type", "disabled")})
        return {**state, "elements": elements, "element_count": len(elements)}

    def _tab_id(self, page: Any, index: int) -> str:
        page_key = id(page)
        if page_key not in self._page_ids:
            material = f"{self.browser_session_id}:{len(self._page_ids)}:{index}"
            self._page_ids[page_key] = f"tab_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"
        return self._page_ids[page_key]

    async def _locator(self, page: Any, params: dict[str, Any]) -> tuple[Any, str]:
        target = params.get("target") if isinstance(params.get("target"), dict) else {}
        ref = str(params.get("element_ref") or target.get("ref") or "").strip()
        if ref:
            observed = self._element_refs.get(ref)
            if observed is None:
                await self._observe(page)
                observed = self._element_refs.get(ref)
            if observed is None:
                raise BrowserFailure("TARGET_NOT_FOUND", "Observed element reference is stale or unavailable.", recoverable=True)
            locator = self._semantic_locator(page, observed)
            return locator.first, "element_ref"

        role = str(target.get("role") or params.get("role") or "").strip()
        name = str(target.get("name") or params.get("name") or "").strip()
        label = str(target.get("label") or params.get("label") or "").strip()
        placeholder = str(target.get("placeholder") or params.get("placeholder") or "").strip()
        text = str(target.get("text") or params.get("text_target") or "").strip()
        test_id = str(target.get("test_id") or params.get("test_id") or "").strip()
        if role and name and hasattr(page, "get_by_role"):
            return page.get_by_role(role, name=name, exact=True).first, "role+name"
        if label and hasattr(page, "get_by_label"):
            return page.get_by_label(label, exact=True).first, "label"
        if placeholder and hasattr(page, "get_by_placeholder"):
            return page.get_by_placeholder(placeholder, exact=True).first, "placeholder"
        if test_id and hasattr(page, "get_by_test_id"):
            return page.get_by_test_id(test_id).first, "test-id"
        if text and hasattr(page, "get_by_text"):
            return page.get_by_text(text, exact=True).first, "text"

        # Compatibility escape hatch only. New Megas-facing contracts should
        # use semantic targets or opaque observed element refs.
        selector = str(params.get("selector") or "").strip()
        if selector and len(selector) <= 1000:
            return page.locator(selector).first, "selector-fallback"
        raise BrowserFailure("TARGET_NOT_FOUND", "No semantic browser target could be resolved.", recoverable=True)

    def _semantic_locator(self, page: Any, observed: dict[str, Any]) -> Any:
        role = str(observed.get("role") or "")
        name = str(observed.get("name") or "")
        label = str(observed.get("label") or "")
        placeholder = str(observed.get("placeholder") or "")
        text = str(observed.get("text") or "")
        if role and name and hasattr(page, "get_by_role"):
            return page.get_by_role(role, name=name, exact=True)
        if label and hasattr(page, "get_by_label"):
            return page.get_by_label(label, exact=True)
        if placeholder and hasattr(page, "get_by_placeholder"):
            return page.get_by_placeholder(placeholder, exact=True)
        if text and hasattr(page, "get_by_text"):
            return page.get_by_text(text, exact=True)
        query = 'a[href],button,input,select,textarea,[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="tab"]'
        return page.locator(query).nth(int(observed.get("index") or 0))

    async def _precondition(self, locator: Any, params: dict[str, Any]) -> None:
        timeout = self._timeout(params)
        try:
            if hasattr(locator, "wait_for"):
                await locator.wait_for(state="visible", timeout=timeout)
            if hasattr(locator, "is_enabled") and not await locator.is_enabled():
                raise BrowserFailure("TARGET_NOT_FOUND", "Browser target is visible but disabled.", recoverable=True)
        except BrowserFailure:
            raise
        except Exception as exc:
            raise BrowserFailure("TARGET_NOT_FOUND", "Browser target did not become ready before its deadline.", recoverable=True) from exc

    async def _input_value(self, locator: Any) -> str | None:
        if not hasattr(locator, "input_value"):
            return None
        try:
            return str(await locator.input_value())
        except Exception:
            return None

    async def _uploaded_filename(self, locator: Any) -> str | None:
        if not hasattr(locator, "evaluate"):
            return None
        try:
            value = await locator.evaluate("el => el.files && el.files.length ? el.files[0].name : ''")
            return str(value or "") or None
        except Exception:
            return None

    async def _state_fingerprint(self, page: Any) -> str:
        try:
            title = await page.title()
            body = await page.locator("body").inner_text(timeout=5000)
        except Exception:
            title = ""
            body = ""
        material = f"{page.url}\n{title}\n{body[:8000]}".encode("utf-8", errors="replace")
        return hashlib.sha256(material).hexdigest()

    async def _verify_after_action(
        self,
        page: Any,
        params: dict[str, Any],
        before: str,
        action_type: str,
        locator: Any,
        *,
        require_explicit: bool = False,
    ) -> dict[str, Any]:
        postcondition = params.get("postcondition")
        if isinstance(postcondition, dict) and postcondition:
            return await self._verify_postcondition(page, locator, postcondition, self._timeout(params))
        if require_explicit or action_type in {"click", "double_click"}:
            after = await self._state_fingerprint(page)
            if after == before:
                raise BrowserFailure(
                    "OUTCOME_AMBIGUOUS",
                    "Browser action completed but no intended state change could be proven; provide a postcondition.",
                    recoverable=False,
                )
            return {"verified": True, "postcondition": "observable-page-state-changed"}
        return {"verified": True, "postcondition": "target-action-completed"}

    async def _verify_postcondition(
        self,
        page: Any,
        locator: Any,
        condition: dict[str, Any],
        timeout_ms: float,
    ) -> dict[str, Any]:
        if "url_contains" in condition:
            expected = str(condition["url_contains"])
            try:
                await page.wait_for_url(re.compile(f".*{re.escape(expected)}.*"), timeout=timeout_ms)
            except Exception as exc:
                raise BrowserFailure("ACTION_TIMEOUT", "Expected URL postcondition was not observed.", recoverable=True) from exc
            return {"verified": True, "postcondition": "url_contains", "expected": expected[:500]}
        if "text_contains" in condition:
            expected = str(condition["text_contains"])
            try:
                await page.get_by_text(expected, exact=False).first.wait_for(state="visible", timeout=timeout_ms)
            except Exception as exc:
                raise BrowserFailure("ACTION_TIMEOUT", "Expected page text postcondition was not observed.", recoverable=True) from exc
            return {"verified": True, "postcondition": "text_contains", "expected": expected[:500]}
        if "target_hidden" in condition and bool(condition["target_hidden"]):
            try:
                await locator.wait_for(state="hidden", timeout=timeout_ms)
            except Exception as exc:
                raise BrowserFailure("ACTION_TIMEOUT", "Target did not become hidden as expected.", recoverable=True) from exc
            return {"verified": True, "postcondition": "target_hidden"}
        if "target_visible" in condition and bool(condition["target_visible"]):
            try:
                await locator.wait_for(state="visible", timeout=timeout_ms)
            except Exception as exc:
                raise BrowserFailure("ACTION_TIMEOUT", "Target did not become visible as expected.", recoverable=True) from exc
            return {"verified": True, "postcondition": "target_visible"}
        if "value_equals" in condition:
            expected = str(condition["value_equals"])
            actual = await self._input_value(locator)
            if actual != expected:
                raise BrowserFailure("OUTCOME_AMBIGUOUS", "Input value postcondition did not match.", recoverable=True)
            return {"verified": True, "postcondition": "value_equals"}
        raise BrowserFailure("ACTION_NOT_ALLOWED", "Unsupported browser postcondition.", recoverable=False)

    async def _wait_condition(self, page: Any, raw: Any, timeout_ms: float) -> dict[str, Any]:
        condition = raw if isinstance(raw, dict) else {}
        if not condition:
            raise BrowserFailure("ACTION_NOT_ALLOWED", "browser.wait requires an explicit evidence condition.", recoverable=False)
        return await self._verify_postcondition(page, page.locator("body"), condition, timeout_ms)

    @staticmethod
    def _same_navigation_target(expected: str, actual: str) -> bool:
        try:
            left = urlparse(expected)
            right = urlparse(actual)
            return left.scheme == right.scheme and left.netloc.lower() == right.netloc.lower() and right.path.startswith(left.path)
        except Exception:
            return False

    def _timeout(self, params: dict[str, Any]) -> float:
        try:
            value = float(params.get("timeout_ms") or 15_000)
        except (TypeError, ValueError):
            value = 15_000
        return max(250.0, min(60_000.0, value))

    def _classify_exception(self, exc: BaseException, action_type: str) -> BrowserFailure:
        if isinstance(exc, BrowserFailure):
            return exc
        name = type(exc).__name__.lower()
        message = str(exc).lower()
        if isinstance(exc, PermissionError):
            return BrowserFailure("ACTION_NOT_ALLOWED", "Megas policy denied the browser action.", recoverable=False)
        if "timeout" in name or "timeout" in message:
            code = "NAVIGATION_TIMEOUT" if action_type == "navigate" else "ACTION_TIMEOUT"
            return BrowserFailure(code, "Browser action exceeded its bounded deadline.", recoverable=True)
        if "target closed" in message or "browser has been closed" in message or "disconnected" in message:
            return BrowserFailure("BROWSER_DISCONNECTED", "Dedicated browser or automation channel disappeared.", recoverable=True)
        if "crash" in message or "page closed" in message:
            return BrowserFailure("PAGE_CRASHED", "Browser page became unusable.", recoverable=True)
        if "strict mode" in message or "locator" in message or "selector" in message or "not found" in message:
            return BrowserFailure("TARGET_NOT_FOUND", "Browser target could not be resolved from current page state.", recoverable=True)
        return BrowserFailure("RECOVERY_EXHAUSTED", "Browser operation failed after bounded recovery.", recoverable=False)

    async def _capture_failure(self, action_id: str, action_type: str, failure: BrowserFailure) -> dict[str, Any]:
        root = evidence_root(self.browser_session_id)
        screenshot_path: str | None = None
        try:
            page = await self._page({})
            path = root / f"failure-{hashlib.sha256(action_id.encode()).hexdigest()[:16]}.jpg"
            await page.screenshot(path=str(path), type="jpeg", quality=65, full_page=False)
            screenshot_path = str(path)
        except Exception:
            screenshot_path = None
        trace_path = await self._rotate_trace(action_id)
        return {
            "error_code": failure.code,
            "action_type": action_type,
            "screenshot": screenshot_path,
            "trace": trace_path,
            "console": list(self._console)[-40:],
            "network": list(self._network)[-40:],
            "retry_count": self._retry_count,
        }

    async def _save_checkpoint(
        self,
        page: Any,
        *,
        action_id: str,
        action_type: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        try:
            state = await self._page_state(page, include_text=False)
            previous = self.checkpoints.latest(self.task_id) or {}
            completed = [str(item) for item in (previous.get("completed_actions") or [])][-127:]
            if action_id not in completed:
                completed.append(action_id)
            uploaded = [str(item) for item in (previous.get("uploaded_files") or [])][-63:]
            if action_type == "upload_file" and result and result.get("file_ref"):
                uploaded.append(str(result["file_ref"]))
            irreversible = [str(item) for item in (previous.get("irreversible_actions") or [])][-63:]
            if action_type in IRREVERSIBLE_ACTIONS:
                irreversible.append(action_id)
            checkpoint = BrowserCheckpoint(
                task_id=self.task_id,
                session_id=self.browser_session_id,
                step_index=self._step_index,
                objective=self.objective,
                current_url=str(state.get("url") or "about:blank"),
                page_signature=page_signature(str(state.get("url") or ""), str(state.get("title") or "")),
                completed_actions=completed,
                uploaded_files=uploaded,
                irreversible_actions=irreversible,
                evidence_refs=[action_id],
                auth_profile_id=self.profile_id,
            )
            self.checkpoints.save(checkpoint)
        except Exception:
            # A checkpoint failure must never turn a verified browser action into
            # fake failure; it remains observable through the action evidence.
            self._timeline_event("browser.checkpoint.failed", state="warning", action_id=action_id)

    def _timeline_event(self, event: str, **payload: Any) -> None:
        row: dict[str, Any] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "browser_session_id": self.browser_session_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
        }
        for key, value in payload.items():
            if value is None or isinstance(value, (bool, int, float)):
                row[key] = value
            else:
                row[key] = _bounded_text(value, 2000)
        self._timeline.append(row)

    def operator_snapshot(self) -> dict[str, Any]:
        return {
            "browser_session_id": self.browser_session_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "profile_id": self.profile_id,
            "takeover": self.takeover_active,
            "retry_count": self._retry_count,
            "timeline": list(self._timeline)[-100:],
            "checkpoint": self.checkpoints.latest(self.task_id),
            "approved_file_refs": self.file_vault.refs(),
            "dedicated_runtime": True,
        }


class BrowserSessionManager:
    def __init__(self, allowed_folders: list[str]) -> None:
        self.allowed_folders = list(allowed_folders)
        self.sessions: dict[str, BrowserRuntimeSession] = {}
        self._lock = asyncio.Lock()

    async def start(self, command: dict[str, Any]) -> dict[str, Any]:
        descriptor = command.get("browser_session") if isinstance(command.get("browser_session"), dict) else {}
        browser_session_id = str(descriptor.get("id") or "").strip()
        session_id = str(command.get("session_id") or "").strip()
        if not browser_session_id or not session_id or str(descriptor.get("session_id") or "") != session_id:
            raise ValueError("browser_session_identity_mismatch")
        task_id = str(descriptor.get("task_id") or command.get("task_id") or session_id).strip()
        if not task_id:
            raise ValueError("browser_task_identity_missing")
        file_grants = descriptor.get("file_capabilities") if isinstance(descriptor.get("file_capabilities"), list) else []
        async with self._lock:
            existing = self.sessions.get(browser_session_id)
            if existing and not existing.closed.is_set():
                pages = list(existing.context.pages)
                return await existing._page_state(pages[-1] if pages else await existing.context.new_page())
            profile = descriptor.get("profile") if isinstance(descriptor.get("profile"), dict) else {}
            runtime = BrowserRuntimeSession(
                browser_session_id=browser_session_id,
                session_id=session_id,
                task_id=task_id,
                agent_id=str(descriptor.get("agent_id") or command.get("agent_id") or "").strip() or None,
                browser_type="chromium",
                profile_id=str(profile.get("id") or descriptor.get("profile_id") or "") or None,
                profile_mode=str(profile.get("mode") or "ephemeral").lower(),
                allowed_folders=self.allowed_folders,
                file_grants=[dict(item) for item in file_grants if isinstance(item, dict)],
                objective=str(descriptor.get("objective") or command.get("instruction") or "").strip()[:4000] or None,
            )
            self.sessions[browser_session_id] = runtime
        context = command.get("context_pack") if isinstance(command.get("context_pack"), dict) else {}
        policy = context.get("cognitive_policy") if isinstance(context.get("cognitive_policy"), dict) else {}
        browser_policy = policy.get("browser") if isinstance(policy.get("browser"), dict) else {}
        try:
            return await runtime.start(str(browser_policy.get("initial_url") or "about:blank"))
        except Exception:
            await runtime.stop("start_failed")
            self.sessions.pop(browser_session_id, None)
            raise

    async def execute(
        self,
        browser_session_id: str,
        action_id: str,
        action: dict[str, Any],
        approval: dict[str, Any] | None,
    ) -> dict[str, Any]:
        runtime = self.sessions.get(browser_session_id)
        if not runtime:
            raise BrowserFailure("BROWSER_DISCONNECTED", "Browser session is not active.", recoverable=True)
        return await runtime.execute(action_id, action, approval)

    async def stop(self, browser_session_id: str, reason: str = "requested") -> dict[str, Any]:
        runtime = self.sessions.get(browser_session_id)
        if not runtime:
            return {"browser_session_id": browser_session_id, "already_stopped": True, "cleanup_ok": True}
        result = await runtime.stop(reason)
        self.sessions.pop(browser_session_id, None)
        return result

    async def wait_closed(self, browser_session_id: str) -> None:
        runtime = self.sessions.get(browser_session_id)
        if runtime:
            await runtime.closed.wait()

    async def delete_profile(self, profile_id: str) -> bool:
        if any(runtime.profile_id == profile_id and not runtime.closed.is_set() for runtime in self.sessions.values()):
            raise RuntimeError("browser_profile_in_use")
        path = _profile_root() / _safe_component(profile_id)
        shutil.rmtree(path, ignore_errors=True)
        return not path.exists()

    def operator_snapshot(self, browser_session_id: str) -> dict[str, Any]:
        runtime = self.sessions.get(browser_session_id)
        if not runtime:
            raise BrowserFailure("BROWSER_DISCONNECTED", "Browser session is not active.", recoverable=True)
        return runtime.operator_snapshot()

    async def shutdown(self) -> None:
        for browser_session_id in list(self.sessions):
            await self.stop(browser_session_id, "worker_shutdown")


__all__ = [
    "BrowserRuntimeSession",
    "BrowserSessionManager",
    "DESTRUCTIVE_ACTIONS",
    "LOW_RISK_ACTIONS",
    "READ_ACTIONS",
    "SENSITIVE_ACTIONS",
    "SUPPORTED_ACTIONS",
    "action_risk",
    "canonical_action_digest",
]
