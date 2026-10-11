"""Runs (sessions) command group."""

from __future__ import annotations

import argparse
import uuid
from typing import Any

from megas.cli import api as api_mod
from megas.cli.context import RuntimeContext, build_context
from megas.cli.errors import CapabilityUnavailableError, NotFoundError
from megas.cli.output import format_timestamp
from megas.cli.safety import require_confirmation
from megas.cli.stream import EventStream, poll_session_events


def cmd_runs_create(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    instruction = (getattr(args, "instruction", None) or "").strip()
    if not instruction:
        raise CapabilityUnavailableError(
            "runs.create",
            message="Instruction is required.",
            hint='Example: megas runs create "Fix the login crash and open a PR"',
        )
    agent_type = (getattr(args, "agent", None) or "").strip()
    workdir = (getattr(args, "workdir", None) or "").strip()
    preferred_worker_id = (getattr(args, "worker", None) or "").strip()
    result = api_mod.create_session(
        ctx.client(),
        instruction=instruction,
        agent_type=agent_type,
        workdir=workdir,
        preferred_worker_id=preferred_worker_id,
        source="cli",
        idempotency_key=str(uuid.uuid4()),
    )
    session_id = (
        result.get("session_id")
        or result.get("id")
        if isinstance(result, dict)
        else None
    )
    plan = ""
    if isinstance(result, dict):
        conductor = result.get("conductor") if isinstance(result.get("conductor"), dict) else {}
        plan = str(conductor.get("human_plan") or "")
    ctx.output.result(
        result,
        human_message=(
            f"Created run {session_id}."
            + (f" Plan: {plan}" if plan else "")
        ),
    )
    return result if isinstance(result, dict) else {"raw": result}


def cmd_runs_list(ctx: RuntimeContext, args: argparse.Namespace) -> list[dict[str, Any]]:
    limit = getattr(args, "limit", None) or ctx.limit or 50
    rows = api_mod.list_sessions(ctx.client(), limit=int(limit))
    display = [
        {
            "id": r.get("id"),
            "status": r.get("status"),
            "worker_id": r.get("worker_id") or "",
            "instruction": (r.get("instruction") or "")[:60],
            "updated": format_timestamp(r.get("updated_at")),
        }
        for r in rows
    ]
    ctx.output.result(
        display,
        columns=[
            ("id", "ID"),
            ("status", "STATUS"),
            ("worker_id", "WORKER"),
            ("instruction", "INSTRUCTION"),
            ("updated", "UPDATED"),
        ],
    )
    return rows


def cmd_runs_inspect(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    session = api_mod.get_session(ctx.client(), args.run_id)
    ctx.output.result(session)
    return session


def cmd_runs_watch(ctx: RuntimeContext, args: argparse.Namespace) -> None:
    after = 0
    if ctx.since and str(ctx.since).isdigit():
        after = int(ctx.since)
    stream = EventStream(
        poll_session_events(ctx.client(), args.run_id, after_seq=after),
        cursor=str(after),
        id_keys=("seq", "id", "event_id"),
        output=ctx.output,
    )
    try:
        for event in stream.iter_events(once=bool(getattr(args, "once", False))):
            ctx.output.stream_event(event)
    except KeyboardInterrupt:
        return


def cmd_runs_cancel(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    require_confirmation(ctx, f"Cancel run {args.run_id}?")
    reason = (getattr(args, "reason", None) or "user_cancelled").strip() or "user_cancelled"
    result = api_mod.cancel_session(ctx.client(), args.run_id, reason=reason)
    ctx.output.result(result, human_message=f"Run {args.run_id} cancelled.")
    return result


def cmd_runs_retry(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    """Retry by creating a new session with the prior instruction."""
    session = api_mod.get_session(ctx.client(), args.run_id)
    instruction = session.get("instruction") if isinstance(session, dict) else None
    if not instruction:
        raise CapabilityUnavailableError(
            "runs.retry",
            message="Cannot retry: prior session has no instruction.",
            details={"run_id": args.run_id},
        )
    agent_type = ""
    if isinstance(session, dict):
        agent_type = str(session.get("agent_id") or "")
        if agent_type and not (
            agent_type.startswith("agent_")
            or agent_type
            in {
                "codex_cli",
                "claude_code",
                "gemini_cli",
                "aider",
                "antigravity",
                "grok_build",
                "kimi_code",
                "glm_code",
                "browser",
                "custom_shell",
            }
        ):
            agent_type = ""
    result = api_mod.create_session(
        ctx.client(),
        instruction=str(instruction),
        agent_type=agent_type,
        source="cli",
        idempotency_key=str(uuid.uuid4()),
    )
    new_id = result.get("session_id") if isinstance(result, dict) else None
    ctx.output.result(
        result,
        human_message=f"Retried as new run {new_id} (prior {args.run_id}).",
    )
    return result if isinstance(result, dict) else {"raw": result}


def cmd_runs_evidence(ctx: RuntimeContext, args: argparse.Namespace) -> list[dict[str, Any]]:
    limit = getattr(args, "limit", None) or ctx.limit or 50
    rows = api_mod.list_evidence(ctx.client(), session_id=args.run_id, limit=int(limit))
    display = [
        {
            "id": r.get("id"),
            "type": r.get("type") or r.get("evidence_type") or r.get("kind") or "",
            "created": format_timestamp(r.get("created_at")),
            "summary": (r.get("summary") or r.get("title") or "")[:80],
        }
        for r in rows
    ]
    ctx.output.result(
        display,
        columns=[("id", "ID"), ("type", "TYPE"), ("created", "CREATED"), ("summary", "SUMMARY")],
    )
    return rows


def register(sub: argparse._SubParsersAction) -> None:
    runs = sub.add_parser("runs", help="Inspect and control agent runs (sessions)")
    rsub = runs.add_subparsers(dest="runs_command", required=True)

    create_p = rsub.add_parser("create", help="Create a run from an instruction")
    create_p.add_argument("instruction", help="User instruction for the worker session")
    create_p.add_argument(
        "--agent",
        default="",
        help="Optional agent/adapter (default: backend codex_cli routing)",
    )
    create_p.add_argument(
        "--workdir",
        default="",
        help="Optional workspace path already allowed by Megas Worker",
    )
    create_p.add_argument(
        "--worker",
        default="",
        help="Preferred Worker id (fleet pin), e.g. wrk_… for a specific PC",
    )
    create_p.set_defaults(func=_wrap(cmd_runs_create))

    rsub.add_parser("list", help="List recent runs").set_defaults(func=_wrap(cmd_runs_list))

    inspect_p = rsub.add_parser("inspect", help="Inspect a run")
    inspect_p.add_argument("run_id")
    inspect_p.set_defaults(func=_wrap(cmd_runs_inspect))

    watch_p = rsub.add_parser("watch", help="Stream run events (poll with resume)")
    watch_p.add_argument("run_id")
    watch_p.add_argument("--once", action="store_true", help="Fetch one page and exit")
    watch_p.set_defaults(func=_wrap(cmd_runs_watch))

    cancel_p = rsub.add_parser("cancel", help="Cancel a run (user abort)")
    cancel_p.add_argument("run_id")
    cancel_p.add_argument("--reason", default="user_cancelled")
    cancel_p.set_defaults(func=_wrap(cmd_runs_cancel))

    retry_p = rsub.add_parser("retry", help="Retry a run as a new session")
    retry_p.add_argument("run_id")
    retry_p.set_defaults(func=_wrap(cmd_runs_retry))

    evidence_p = rsub.add_parser("evidence", help="List evidence for a run")
    evidence_p.add_argument("run_id")
    evidence_p.set_defaults(func=_wrap(cmd_runs_evidence))


def _wrap(fn):
    def runner(args: argparse.Namespace) -> None:
        ctx = build_context(args)
        fn(ctx, args)

    runner.__name__ = getattr(fn, "__name__", "runs_cmd")
    runner.__wrapped__ = fn  # type: ignore[attr-defined]
    return runner


__all__ = ["register"]
