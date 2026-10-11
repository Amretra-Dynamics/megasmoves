from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .activity import _tail_jsonl
from .store import MCPStore


DEFAULT_ACTIVE_WINDOW_SECONDS = 120
MAX_ACTIVE_WINDOW_SECONDS = 3600
MAX_AGENT_PRESENCE_ROWS = 2000
MAX_AGENT_PRESENCE_AGENTS = 64

_RELEVANT_OPERATION_SUFFIXES = (
    ".operation.started",
    ".operation.completed",
    ".operation.failed",
)


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _is_presence_event(event: str) -> bool:
    return event in {"agent.client.observed", "agent.client.disconnected"} or event.endswith(
        _RELEVANT_OPERATION_SUFFIXES
    )


def summarize_agent_presence(
    store: MCPStore,
    *,
    now: datetime | None = None,
    active_window_seconds: int = DEFAULT_ACTIVE_WINDOW_SECONDS,
) -> dict[str, Any]:
    """Build a bounded, read-only view of MCP client activity.

    ``active`` deliberately means recent protocol/tool activity, not a network
    connection claim. Streamable HTTP is stateless. For stdio, an unmatched
    successful handshake is exposed only as ``open_evidence``; it is not treated
    as proof that the client process is still alive after the activity window.
    Client labels are MCP-supplied attribution metadata and are not authentication.
    """

    window = int(active_window_seconds)
    if window <= 0 or window > MAX_ACTIVE_WINDOW_SECONDS:
        raise ValueError(
            f"active_window_seconds must be between 1 and {MAX_ACTIVE_WINDOW_SECONDS}"
        )
    current = _utc_now(now)
    rows = _tail_jsonl(store.history_path, max_rows=MAX_AGENT_PRESENCE_ROWS)

    by_agent: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        event = str(row.get("event") or "").strip()
        if not _is_presence_event(event):
            continue
        agent = str(row.get("agent") or "").strip()
        if not agent:
            continue
        version = str(row.get("agent_version") or "").strip()
        transport = str(row.get("transport") or "mcp").strip() or "mcp"
        at = _parse_timestamp(row.get("at"))
        if at is None:
            continue

        key = (agent, version, transport)
        item = by_agent.setdefault(
            key,
            {
                "name": agent,
                "version": version or None,
                "transport": transport,
                "first_seen": at,
                "last_seen": at,
                "last_handshake_at": None,
                "last_disconnect_at": None,
                "handshake": None,
                "protocol_version": None,
                "operation_count": 0,
            },
        )
        if at < item["first_seen"]:
            item["first_seen"] = at
        if at > item["last_seen"]:
            item["last_seen"] = at

        if event == "agent.client.observed":
            previous = item["last_handshake_at"]
            if previous is None or at >= previous:
                item["last_handshake_at"] = at
                item["handshake"] = str(row.get("handshake") or "").strip() or None
                item["protocol_version"] = (
                    str(row.get("protocol_version") or "").strip() or None
                )
        elif event == "agent.client.disconnected":
            previous = item["last_disconnect_at"]
            if previous is None or at >= previous:
                item["last_disconnect_at"] = at
        elif event.endswith(_RELEVANT_OPERATION_SUFFIXES) and event.endswith(
            ".operation.started"
        ):
            item["operation_count"] += 1

    all_agents: list[dict[str, Any]] = []
    active_count = 0
    stdio_open_evidence = 0
    for item in by_agent.values():
        last_seen: datetime = item["last_seen"]
        last_handshake: datetime | None = item["last_handshake_at"]
        last_disconnect: datetime | None = item["last_disconnect_at"]
        age_seconds = max(0, int((current - last_seen).total_seconds()))

        if item["transport"] == "streamable-http":
            transport_state = "stateless"
            closed = False
        elif item["transport"] == "stdio":
            closed = bool(
                last_disconnect is not None
                and (last_handshake is None or last_disconnect >= last_handshake)
            )
            transport_state = "closed" if closed else "open_evidence"
            if not closed and last_handshake is not None:
                stdio_open_evidence += 1
        else:
            closed = False
            transport_state = "unknown"

        active = age_seconds <= window and not closed
        if active:
            active_count += 1

        all_agents.append(
            {
                "name": item["name"],
                "version": item["version"],
                "transport": item["transport"],
                "state": "active" if active else "inactive",
                "transport_state": transport_state,
                "last_seen": last_seen.isoformat(),
                "age_seconds": age_seconds,
                "handshake": item["handshake"],
                "protocol_version": item["protocol_version"],
                "operation_count": item["operation_count"],
                "identity_semantics": "client-supplied-attribution",
            }
        )

    all_agents.sort(
        key=lambda row: (
            0 if row["state"] == "active" else 1,
            row["age_seconds"],
            row["name"].lower(),
        )
    )
    truncated = len(all_agents) > MAX_AGENT_PRESENCE_AGENTS
    visible = all_agents[:MAX_AGENT_PRESENCE_AGENTS]

    return {
        "generated_at": current.isoformat(),
        "activity_window_seconds": window,
        "counts": {
            "active": active_count,
            "observed": len(all_agents),
            "stdio_open_evidence": stdio_open_evidence,
        },
        "agents": visible,
        "truncated": truncated,
        "semantics": {
            "active": "protocol or tool activity observed inside the bounded freshness window",
            "streamable_http": "stateless; no durable connected-session claim is made",
            "stdio": "open_evidence means a successful handshake has no later observed EOF; freshness still controls active state",
            "identity": "agent name/version are client-supplied attribution metadata, not authentication",
        },
    }


__all__ = [
    "DEFAULT_ACTIVE_WINDOW_SECONDS",
    "MAX_ACTIVE_WINDOW_SECONDS",
    "MAX_AGENT_PRESENCE_AGENTS",
    "MAX_AGENT_PRESENCE_ROWS",
    "summarize_agent_presence",
]
