"""Streaming helpers with reconnection, cursor resume, and duplicate suppression."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator
from typing import Any

from .errors import CliError, NetworkError, RequestTimeoutError
from .output import Output


class EventStream:
    """Poll/stream loop with bounded backoff and cursor resume."""

    def __init__(
        self,
        fetch: Callable[[str | None], tuple[list[dict[str, Any]], str | None]],
        *,
        cursor: str | None = None,
        id_keys: tuple[str, ...] = ("id", "event_id", "seq", "cursor"),
        min_backoff: float = 0.5,
        max_backoff: float = 30.0,
        idle_sleep: float = 1.0,
        heartbeat_seconds: float = 60.0,
        output: Output | None = None,
    ) -> None:
        self.fetch = fetch
        self.cursor = cursor
        self.id_keys = id_keys
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self.idle_sleep = idle_sleep
        self.heartbeat_seconds = heartbeat_seconds
        self.output = output or Output(format="table")
        self._seen: set[str] = set()
        self._seen_order: list[str] = []
        self._max_seen = 5000

    def _event_id(self, event: dict[str, Any]) -> str | None:
        for key in self.id_keys:
            if key in event and event[key] is not None:
                return f"{key}:{event[key]}"
        # fallback fingerprint
        parts = [
            str(event.get("type") or event.get("event_type") or ""),
            str(event.get("timestamp") or event.get("created_at") or ""),
            str(event.get("message") or event.get("summary") or "")[:80],
        ]
        fingerprint = "|".join(parts)
        return fingerprint if any(parts) else None

    def _remember(self, event_id: str) -> bool:
        """Return True if this is a new event."""
        if event_id in self._seen:
            return False
        self._seen.add(event_id)
        self._seen_order.append(event_id)
        if len(self._seen_order) > self._max_seen:
            old = self._seen_order.pop(0)
            self._seen.discard(old)
        return True

    def iter_events(self, *, once: bool = False) -> Iterator[dict[str, Any]]:
        backoff = self.min_backoff
        last_data_at = time.monotonic()
        disconnected = False
        while True:
            try:
                events, next_cursor = self.fetch(self.cursor)
                if disconnected:
                    self.output.info("reconnected")
                    disconnected = False
                backoff = self.min_backoff
                last_data_at = time.monotonic()
                if next_cursor is not None:
                    self.cursor = next_cursor
                for event in events:
                    if not isinstance(event, dict):
                        event = {"value": event}
                    event_id = self._event_id(event)
                    if event_id and not self._remember(event_id):
                        continue
                    # advance cursor from event when provider does not return one
                    if next_cursor is None:
                        for key in ("cursor", "seq", "id", "event_id"):
                            if key in event and event[key] is not None:
                                self.cursor = str(event[key])
                                break
                    yield event
                if once:
                    return
                if time.monotonic() - last_data_at > self.heartbeat_seconds:
                    self.output.info("heartbeat: waiting for events…")
                    last_data_at = time.monotonic()
                time.sleep(self.idle_sleep)
            except KeyboardInterrupt:
                self.output.info("stream cancelled")
                raise
            except (NetworkError, RequestTimeoutError, CliError) as exc:
                if once:
                    raise
                disconnected = True
                jitter = random.uniform(0, min(1.0, backoff / 4))
                wait = min(self.max_backoff, backoff) + jitter
                self.output.warn(f"disconnected ({exc.message}); reconnecting in {wait:.1f}s")
                time.sleep(wait)
                backoff = min(self.max_backoff, backoff * 2)


def poll_session_events(
    client: Any,
    session_id: str,
    *,
    after_seq: int = 0,
    limit: int = 100,
) -> Callable[[str | None], tuple[list[dict[str, Any]], str | None]]:
    from . import api as api_mod

    state = {"seq": after_seq}

    def fetch(cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
        seq = int(cursor) if cursor is not None and str(cursor).isdigit() else state["seq"]
        payload = api_mod.session_events(client, session_id, after_seq=seq, limit=limit)
        events = payload.get("events") if isinstance(payload, dict) else payload
        events = events if isinstance(events, list) else []
        next_seq = seq
        normalized: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            event_seq = int(event.get("seq") or 0)
            if event_seq > next_seq:
                next_seq = event_seq
            normalized.append(event)
        state["seq"] = next_seq
        return normalized, str(next_seq)

    return fetch


def poll_list(
    fetch_list: Callable[[], list[dict[str, Any]]],
    *,
    id_key: str = "id",
) -> Callable[[str | None], tuple[list[dict[str, Any]], str | None]]:
    """Convert a list endpoint into an incremental stream of new items."""

    def fetch(cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
        items = fetch_list()
        if cursor is None:
            # first poll: emit nothing, set cursor to latest
            if not items:
                return [], None
            last = items[0].get(id_key) if isinstance(items[0], dict) else None
            return [], str(last) if last is not None else None
        # emit items until we hit the cursor (lists assumed newest-first)
        fresh: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get(id_key)) == str(cursor):
                break
            fresh.append(item)
        fresh.reverse()  # chronological
        new_cursor = cursor
        if items and isinstance(items[0], dict) and items[0].get(id_key) is not None:
            new_cursor = str(items[0].get(id_key))
        return fresh, new_cursor

    return fetch


__all__ = ["EventStream", "poll_list", "poll_session_events"]
