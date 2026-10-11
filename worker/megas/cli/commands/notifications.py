"""Notifications list, watch, and acknowledge."""

from __future__ import annotations

import argparse
from typing import Any

from megas.cli import api as api_mod
from megas.cli.context import RuntimeContext, build_context
from megas.cli.errors import CapabilityUnavailableError
from megas.cli.features import capability_enabled
from megas.cli.output import format_timestamp
from megas.cli.stream import EventStream, poll_list


def cmd_notifications_list(ctx: RuntimeContext, _args: argparse.Namespace) -> list[dict[str, Any]]:
    if not capability_enabled("notifications"):
        raise CapabilityUnavailableError("notifications")
    rows = api_mod.list_notifications(ctx.client())
    if ctx.limit:
        rows = rows[: int(ctx.limit)]
    display = [
        {
            "id": r.get("id") or r.get("notification_id") or r.get("event_id") or "",
            "type": r.get("type") or r.get("event_type") or r.get("kind") or "",
            "status": r.get("status") or r.get("state") or "",
            "message": (r.get("message") or r.get("title") or r.get("summary") or "")[:80],
            "time": format_timestamp(r.get("created_at") or r.get("timestamp")),
        }
        for r in rows
    ]
    ctx.output.result(
        display,
        columns=[
            ("id", "ID"),
            ("type", "TYPE"),
            ("status", "STATUS"),
            ("message", "MESSAGE"),
            ("time", "TIME"),
        ],
    )
    return rows


def cmd_notifications_watch(ctx: RuntimeContext, args: argparse.Namespace) -> None:
    if not capability_enabled("notifications"):
        raise CapabilityUnavailableError("notifications")

    def fetch_list() -> list[dict[str, Any]]:
        return api_mod.list_notifications(ctx.client())

    stream = EventStream(
        poll_list(fetch_list, id_key="id"),
        id_keys=("id", "notification_id", "event_id"),
        output=ctx.output,
        idle_sleep=float(getattr(args, "interval", 2.0) or 2.0),
    )
    try:
        for event in stream.iter_events(once=False):
            ctx.output.stream_event(
                {
                    "type": event.get("type") or event.get("event_type") or "notification",
                    "message": event.get("message") or event.get("title") or event.get("summary") or "",
                    "timestamp": event.get("created_at") or event.get("timestamp"),
                    "id": event.get("id") or event.get("notification_id"),
                }
            )
    except KeyboardInterrupt:
        return


def cmd_notifications_ack(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    if not capability_enabled("notifications"):
        raise CapabilityUnavailableError("notifications")
    result = api_mod.acknowledge_notification(ctx.client(), args.notification_id)
    ctx.output.result(result, human_message=f"Acknowledged notification {args.notification_id}.")
    return result if isinstance(result, dict) else {"ok": True}


def register(sub: argparse._SubParsersAction) -> None:
    notifications = sub.add_parser("notifications", help="Notification inbox and watch")
    nsub = notifications.add_subparsers(dest="notifications_command", required=True)

    nsub.add_parser("list", help="List notifications").set_defaults(func=_wrap(cmd_notifications_list))

    watch = nsub.add_parser("watch", help="Watch notifications (poll with reconnect)")
    watch.add_argument("--interval", type=float, default=2.0)
    watch.set_defaults(func=_wrap(cmd_notifications_watch))

    ack = nsub.add_parser("acknowledge", help="Acknowledge a notification")
    ack.add_argument("notification_id")
    ack.set_defaults(func=_wrap(cmd_notifications_ack))


def _wrap(fn):
    def runner(args: argparse.Namespace) -> None:
        ctx = build_context(args)
        fn(ctx, args)

    runner.__name__ = getattr(fn, "__name__", "notifications_cmd")
    runner.__wrapped__ = fn  # type: ignore[attr-defined]
    return runner


__all__ = ["register"]
