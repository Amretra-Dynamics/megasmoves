"""Monitor command group — product-wide production observability."""

from __future__ import annotations

import argparse
from typing import Any

from megas.cli import api as api_mod
from megas.cli.context import RuntimeContext, build_context
from megas.cli.errors import CapabilityUnavailableError, InvalidArgumentsError
from megas.cli.output import format_timestamp, iter_as_rows
from megas.cli.safety import require_confirmation
from megas.cli.stream import EventStream, poll_list


def _normalize_incidents(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for key in ("incidents", "items", "results"):
            if isinstance(payload.get(key), list):
                return [p for p in payload[key] if isinstance(p, dict)]
        if payload.get("id") or payload.get("title"):
            return [payload]
    return []


def _incident_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id") or item.get("incident_id"),
        "severity": item.get("severity") or "",
        "status": item.get("status") or item.get("state") or "",
        "title": (item.get("title") or item.get("summary") or "")[:70],
        "service": item.get("service_name")
        or item.get("target")
        or item.get("service")
        or "",
        "first_seen": format_timestamp(
            item.get("first_seen_at")
            or item.get("detected_at")
            or item.get("created_at")
        ),
        "last_seen": format_timestamp(
            item.get("last_seen_at") or item.get("updated_at")
        ),
        "count": item.get("occurrence_count") or "",
        "owner": item.get("assignee")
        or item.get("assigned_to")
        or item.get("owner")
        or "",
    }


def cmd_monitor_status(
    ctx: RuntimeContext, _args: argparse.Namespace
) -> dict[str, Any]:
    health: dict[str, Any] = {}
    try:
        health = ctx.client().healthz()
        health["reachable"] = True
    except Exception as exc:
        health = {"reachable": False, "error": str(exc)}
    ready = ctx.client().readyz()
    monitor = {}
    try:
        monitor = api_mod.monitor_health(ctx.client())
    except CapabilityUnavailableError as exc:
        monitor = {"available": False, "message": exc.message}
    data = {
        "backend": health,
        "ready": ready,
        "monitor": monitor,
        "surfaces": [
            "application crashes",
            "API failures",
            "latency",
            "availability",
            "database / queue / storage",
            "deployment & migration",
            "host / container health",
            "CPU memory disk pressure",
            "network DNS TLS",
            "third-party providers",
            "synthetics",
            "workers agents browser CUA",
        ],
    }
    ctx.output.result(data)
    return data


def cmd_monitor_events(ctx: RuntimeContext, args: argparse.Namespace) -> Any:
    params: dict[str, Any] = {}
    if ctx.limit:
        params["limit"] = ctx.limit
    if ctx.since:
        params["since"] = ctx.since
    if getattr(args, "severity", None):
        params["severity"] = args.severity
    payload = api_mod.monitor_telemetry_events(ctx.client(), **params)
    rows = iter_as_rows(payload, ("events", "items", "results"))
    display = [
        {
            "id": r.get("id") or r.get("event_id") or "",
            "type": r.get("event_type") or r.get("type") or r.get("signal_type") or "",
            "severity": r.get("severity") or "",
            "message": (r.get("message") or r.get("title") or r.get("summary") or "")[
                :80
            ],
            "time": format_timestamp(
                r.get("timestamp") or r.get("created_at") or r.get("detected_at")
            ),
        }
        for r in rows
    ]
    if getattr(args, "limit", None) or ctx.limit:
        limit = int(getattr(args, "limit", None) or ctx.limit or 50)
        display = display[:limit]
    ctx.output.result(
        display,
        columns=[
            ("id", "ID"),
            ("type", "TYPE"),
            ("severity", "SEV"),
            ("message", "MESSAGE"),
            ("time", "TIME"),
        ],
    )
    return payload


def cmd_monitor_tail(ctx: RuntimeContext, args: argparse.Namespace) -> None:
    def fetch_list() -> list[dict[str, Any]]:
        payload = api_mod.monitor_telemetry_events(ctx.client(), limit=50)
        return iter_as_rows(payload, ("events", "items"))

    stream = EventStream(
        poll_list(fetch_list, id_key="id"),
        id_keys=("id", "event_id", "seq"),
        output=ctx.output,
        idle_sleep=float(getattr(args, "interval", 2.0) or 2.0),
    )
    try:
        # Seed cursor without emitting historical backlog as a flood.
        list(stream.iter_events(once=True))
        for event in stream.iter_events(once=False):
            ctx.output.stream_event(
                {
                    "type": event.get("event_type") or event.get("type") or "monitor",
                    "message": event.get("message")
                    or event.get("title")
                    or event.get("summary")
                    or "",
                    "timestamp": event.get("timestamp")
                    or event.get("created_at")
                    or event.get("detected_at"),
                    "id": event.get("id"),
                    "severity": event.get("severity"),
                }
            )
    except KeyboardInterrupt:
        return


def cmd_monitor_incidents(
    ctx: RuntimeContext, args: argparse.Namespace
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    if getattr(args, "state", None):
        params["state"] = args.state
    if getattr(args, "severity", None):
        params["severity"] = args.severity
    if ctx.limit:
        params["limit"] = ctx.limit
    payload = api_mod.list_monitor_incidents(ctx.client(), **params)
    rows = _normalize_incidents(payload)
    display = [_incident_row(r) for r in rows]
    if ctx.limit:
        display = display[: int(ctx.limit)]
    ctx.output.result(
        display,
        columns=[
            ("id", "ID"),
            ("severity", "SEV"),
            ("status", "STATUS"),
            ("title", "TITLE"),
            ("service", "SYSTEM"),
            ("first_seen", "FIRST"),
            ("last_seen", "LAST"),
            ("owner", "OWNER"),
        ],
    )
    return rows


def cmd_monitor_incident_show(
    ctx: RuntimeContext, args: argparse.Namespace
) -> dict[str, Any]:
    payload = api_mod.get_monitor_incident(ctx.client(), args.incident_id)
    incident = (
        payload.get("incident")
        if isinstance(payload, dict) and "incident" in payload
        else payload
    )
    ctx.output.result(incident)
    return incident if isinstance(incident, dict) else {"raw": incident}


def _action(
    ctx: RuntimeContext,
    incident_id: str,
    action: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if action in {"resolve", "mute", "assign"}:
        require_confirmation(ctx, f"{action.capitalize()} incident {incident_id}?")
    result = api_mod.incident_action(ctx.client(), incident_id, action, body)
    ctx.output.result(
        result, human_message=f"Incident {incident_id}: {action} confirmed by backend."
    )
    return result if isinstance(result, dict) else {"result": result}


def cmd_monitor_incident_ack(
    ctx: RuntimeContext, args: argparse.Namespace
) -> dict[str, Any]:
    return _action(
        ctx,
        args.incident_id,
        "acknowledge",
        {"reason": getattr(args, "reason", None) or "acknowledged"},
    )


def cmd_monitor_incident_assign(
    ctx: RuntimeContext, args: argparse.Namespace
) -> dict[str, Any]:
    if not args.assignee:
        raise InvalidArgumentsError("assignee is required")
    return _action(ctx, args.incident_id, "assign", {"assignee": args.assignee})


def cmd_monitor_incident_mute(
    ctx: RuntimeContext, args: argparse.Namespace
) -> dict[str, Any]:
    return _action(
        ctx,
        args.incident_id,
        "mute",
        {
            "reason": getattr(args, "reason", None) or "muted",
            "expires_at": getattr(args, "expires_at", None),
        },
    )


def cmd_monitor_incident_resolve(
    ctx: RuntimeContext, args: argparse.Namespace
) -> dict[str, Any]:
    return _action(
        ctx,
        args.incident_id,
        "resolve",
        {
            "reason": getattr(args, "reason", None) or "resolved",
            "recovery_evidence": bool(getattr(args, "recovery_evidence", False)),
            "force": bool(getattr(args, "force", False)),
        },
    )


def cmd_monitor_alerts(ctx: RuntimeContext, _args: argparse.Namespace) -> Any:
    try:
        payload = ctx.client().try_paths(
            "GET",
            ["/api/v1/monitor/alerts", "/api/v1/alerts", "/api/v1/console/alerts"],
            capability="monitor.alerts",
        )
    except CapabilityUnavailableError as exc:
        raise CapabilityUnavailableError(
            "monitor.alerts",
            message="Alert history API is not available yet.",
            hint="Use `megas monitor incidents` until Agent 6 alert delivery APIs are deployed.",
        ) from exc
    rows = iter_as_rows(payload, ("alerts", "items"))
    ctx.output.result(rows)
    return payload


def cmd_monitor_rules(ctx: RuntimeContext, _args: argparse.Namespace) -> Any:
    try:
        payload = ctx.client().try_paths(
            "GET",
            ["/api/v1/monitor/rules", "/api/v1/alerts/rules"],
            capability="monitor.rules",
        )
    except CapabilityUnavailableError as exc:
        raise CapabilityUnavailableError(
            "monitor.rules",
            message="Monitor rules API is not available yet.",
            hint="Rules management will land with the alerts agent.",
        ) from exc
    ctx.output.result(payload)
    return payload


def cmd_monitor_health(ctx: RuntimeContext, _args: argparse.Namespace) -> Any:
    try:
        payload = api_mod.monitor_health(ctx.client())
    except CapabilityUnavailableError:
        payload = {"backend": ctx.client().healthz(), "ready": ctx.client().readyz()}
    ctx.output.result(payload)
    return payload


def register(sub: argparse._SubParsersAction) -> None:
    monitor = sub.add_parser(
        "monitor", help="Production monitoring and incident operations"
    )
    msub = monitor.add_subparsers(dest="monitor_command", required=True)

    msub.add_parser("status", help="Overall monitor / backend status").set_defaults(
        func=_wrap(cmd_monitor_status)
    )

    events = msub.add_parser("events", help="List recent telemetry / monitor events")
    events.add_argument("--severity", default=None)
    events.set_defaults(func=_wrap(cmd_monitor_events))

    tail = msub.add_parser("tail", help="Tail monitor events (poll with reconnect)")
    tail.add_argument("--interval", type=float, default=2.0)
    tail.set_defaults(func=_wrap(cmd_monitor_tail))

    incidents = msub.add_parser("incidents", help="List incidents")
    incidents.add_argument("--state", default=None)
    incidents.add_argument("--severity", default=None)
    incidents.set_defaults(func=_wrap(cmd_monitor_incidents))

    incident = msub.add_parser("incident", help="Incident actions")
    isub = incident.add_subparsers(dest="incident_command", required=True)

    show = isub.add_parser("show", help="Show incident detail")
    show.add_argument("incident_id")
    show.set_defaults(func=_wrap(cmd_monitor_incident_show))

    ack = isub.add_parser("acknowledge", help="Acknowledge an incident")
    ack.add_argument("incident_id")
    ack.add_argument("--reason", default="acknowledged")
    ack.set_defaults(func=_wrap(cmd_monitor_incident_ack))

    assign = isub.add_parser("assign", help="Assign an incident")
    assign.add_argument("incident_id")
    assign.add_argument("assignee")
    assign.set_defaults(func=_wrap(cmd_monitor_incident_assign))

    mute = isub.add_parser("mute", help="Mute / suppress an incident")
    mute.add_argument("incident_id")
    mute.add_argument("--reason", default="muted")
    mute.add_argument("--expires-at", default=None)
    mute.set_defaults(func=_wrap(cmd_monitor_incident_mute))

    resolve = isub.add_parser("resolve", help="Resolve an incident")
    resolve.add_argument("incident_id")
    resolve.add_argument("--reason", default="resolved")
    resolve.add_argument("--recovery-evidence", action="store_true")
    resolve.add_argument("--force", action="store_true")
    resolve.set_defaults(func=_wrap(cmd_monitor_incident_resolve))

    msub.add_parser("alerts", help="List alert deliveries").set_defaults(
        func=_wrap(cmd_monitor_alerts)
    )
    msub.add_parser("rules", help="List monitor / alert rules").set_defaults(
        func=_wrap(cmd_monitor_rules)
    )
    msub.add_parser("health", help="Monitor subsystem health").set_defaults(
        func=_wrap(cmd_monitor_health)
    )


def _wrap(fn):
    def runner(args: argparse.Namespace) -> None:
        ctx = build_context(args)
        fn(ctx, args)

    runner.__name__ = getattr(fn, "__name__", "monitor_cmd")
    runner.__wrapped__ = fn  # type: ignore[attr-defined]
    return runner


__all__ = ["register"]
