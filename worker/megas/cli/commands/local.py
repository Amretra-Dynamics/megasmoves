"""Local command group — inspect and pair the authoritative on-machine runtime."""

from __future__ import annotations

import argparse
import time
from typing import Any

from megas.cli.api_local import LocalRuntimeInspector
from megas.cli.context import RuntimeContext, add_global_flags, build_context


def cmd_local_status(ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    payload = LocalRuntimeInspector().status()
    ctx.output.result(
        payload,
        human_message=(
            f"Megas Local is {payload['status']} on 127.0.0.1:{payload['gateway_port']}. "
            f"Cloud route is {'configured' if payload['cloud_route_configured'] else 'not configured'}."
        ),
    )
    return payload


def cmd_local_pair(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    payload = LocalRuntimeInspector().create_pairing_payload(
        ttl_seconds=args.ttl,
        include_tailscale=not args.no_tailscale,
    )
    pairing = payload["pairing"]
    routes = ", ".join(endpoint["route"] for endpoint in pairing["endpoints"])
    ctx.output.result(
        payload,
        human_message=(
            "Open Megas Moves → Systems → Pair Worker and paste this one-time link:\n\n"
            f"{payload['pair_uri']}\n\n"
            f"Worker: {pairing['worker_name']} ({pairing['worker_id']})\n"
            f"Routes: {routes}\n"
            f"Expires in {args.ttl} seconds. Treat this link like a temporary password."
        ),
    )
    return payload


def cmd_local_workspaces(
    ctx: RuntimeContext,
    _args: argparse.Namespace,
) -> list[dict[str, Any]]:
    rows = LocalRuntimeInspector().list_workspaces()
    ctx.output.result(
        rows,
        columns=[
            ("id", "ID"),
            ("approved", "APPROVED"),
            ("path", "PATH"),
        ],
    )
    return rows


def cmd_local_threads(
    ctx: RuntimeContext,
    _args: argparse.Namespace,
) -> list[dict[str, Any]]:
    rows = LocalRuntimeInspector().list_threads()
    display = [
        {
            "thread": row.get("megas_thread_id"),
            "native": row.get("native_thread_id") or "pending",
            "provider": row.get("provider_id"),
            "worker": row.get("worker_id"),
            "workspace": row.get("workspace_path"),
            "cloud": row.get("cloud_thread_id") or "-",
        }
        for row in rows
    ]
    ctx.output.result(
        display,
        columns=[
            ("thread", "THREAD"),
            ("native", "NATIVE"),
            ("provider", "PROVIDER"),
            ("worker", "WORKER"),
            ("workspace", "WORKSPACE"),
            ("cloud", "CLOUD LINK"),
        ],
    )
    return rows


def cmd_local_stream(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    inspector = LocalRuntimeInspector()
    cursor = int(args.after_seq)
    latest: dict[str, Any] = {}
    try:
        while True:
            latest = inspector.replay_events(
                args.session_id,
                after_seq=cursor,
                limit=args.limit or 100,
            )
            events = latest.get("events") or []
            for event in events:
                ctx.output.stream_event(event)
            cursor = int(latest.get("next_cursor", cursor))

            if args.once:
                if ctx.output.machine and not events:
                    ctx.output.result({**latest, "events": []})
                return latest
            if not latest.get("has_more") and not events:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        return latest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="megas local",
        description=(
            "Inspect and pair the Local Gateway using this machine's authoritative journal."
        ),
    )
    add_global_flags(parser)
    sub = parser.add_subparsers(dest="local_command", required=True)

    status = sub.add_parser("status", help="Show Local runtime, identity, and route health")
    status.set_defaults(local_func=cmd_local_status)

    pair = sub.add_parser(
        "pair",
        help="Create a single-use browser/device pairing link from the Worker owner",
    )
    pair.add_argument(
        "--ttl",
        type=int,
        default=300,
        help="Pairing lifetime in seconds (30-600; default: 300)",
    )
    pair.add_argument(
        "--no-tailscale",
        action="store_true",
        help="Do not include a currently verified private Tailscale route",
    )
    pair.set_defaults(local_func=cmd_local_pair)

    workspaces = sub.add_parser("workspaces", help="List durable workspace approvals")
    workspaces.set_defaults(local_func=cmd_local_workspaces)

    threads = sub.add_parser("threads", help="List Local/native/cloud thread bindings")
    threads.set_defaults(local_func=cmd_local_threads)

    stream = sub.add_parser("stream", help="Replay and follow one Local session event stream")
    stream.add_argument("session_id", help="Durable Local live_session_id")
    stream.add_argument("--after-seq", type=int, default=-1)
    stream.add_argument("--interval", type=float, default=1.0)
    stream.add_argument("--once", action="store_true", help="Fetch available events and exit")
    stream.set_defaults(local_func=cmd_local_stream)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "interval", 1.0) <= 0:
        parser.error("--interval must be greater than zero")
    if getattr(args, "after_seq", -1) < -1:
        parser.error("--after-seq must be at least -1")
    if not 30 <= getattr(args, "ttl", 300) <= 600:
        parser.error("--ttl must be between 30 and 600 seconds")
    ctx = build_context(args)
    try:
        args.local_func(ctx, args)
    except Exception as exc:
        payload = {
            "error": {
                "code": type(exc).__name__,
                "message": str(exc),
            }
        }
        if ctx.output.machine:
            ctx.output.result(payload)
        else:
            ctx.output.error(f"error: {exc}")
        raise SystemExit(1) from exc


__all__ = [
    "cmd_local_pair",
    "cmd_local_status",
    "cmd_local_stream",
    "cmd_local_threads",
    "cmd_local_workspaces",
    "main",
]

if __name__ == "__main__":
    main()
