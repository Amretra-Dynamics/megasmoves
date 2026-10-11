"""Workers command group — inventory, inspect, pair, control, logs, cancel."""

from __future__ import annotations

import argparse
import asyncio
import socket
from typing import Any

from megas.cli import api as api_mod
from megas.cli.context import RuntimeContext, build_context
from megas.cli.errors import CapabilityUnavailableError, InvalidArgumentsError, NotFoundError
from megas.cli.output import format_timestamp
from megas.cli.safety import require_confirmation
from megas.client import pair_worker, runtime_capabilities
from megas.config import load_config, save_config
from megas.service import (
    daemon_status,
    service_installed,
    start_background,
    start_installed_service,
    stop_background,
    stop_installed_service,
    tail_logs,
    log_path,
)


def cmd_workers_list(ctx: RuntimeContext, args: argparse.Namespace) -> list[dict[str, Any]]:
    limit = args.limit if getattr(args, "limit", None) is not None else ctx.limit
    rows = api_mod.list_workers(ctx.client(), limit=limit)
    display = [
        {
            "id": r.get("id") or r.get("worker_id"),
            "name": (
                r.get("display_name")
                or r.get("worker_name")
                or r.get("name")
                or r.get("machine_name")
                or ""
            ),
            "status": r.get("status"),
            "last_seen": format_timestamp(
                r.get("last_seen_at") or r.get("last_heartbeat_at")
            ),
            "created_by": r.get("created_by") or r.get("user_email") or "",
        }
        for r in rows
    ]
    ctx.output.result(
        display if ctx.output.machine else display,
        columns=[
            ("id", "ID"),
            ("name", "NAME"),
            ("status", "STATUS"),
            ("last_seen", "LAST SEEN"),
            ("created_by", "OWNER"),
        ],
    )
    return rows


def cmd_workers_inspect(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    worker = api_mod.get_worker(ctx.client(), args.worker_id)
    ctx.output.result(worker)
    return worker


def cmd_workers_pair(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    """Pair this machine (pairing code) or start device authorization via connect flow."""
    config = load_config()
    if getattr(args, "backend", None):
        config.backend_url = str(args.backend).rstrip("/")
    if getattr(args, "name", None):
        config.worker_name = args.name
    from megas.config import resolve_machine_label

    config.machine_name = resolve_machine_label(
        getattr(args, "machine", None) or config.machine_name
    )
    code = (getattr(args, "code", None) or "").strip()
    if not code:
        raise InvalidArgumentsError(
            "Pairing code required.",
            hint="Pass --code from the Megas console, or use `megas connect` for browser device auth.",
        )
    asyncio.run(pair_worker(config, code))
    result = {
        "paired": True,
        "worker_id": config.worker_id,
        "machine_name": config.machine_name,
        "capabilities": runtime_capabilities(config),
    }
    ctx.output.result(result, human_message=f"Paired worker {config.worker_id}")
    return result


def cmd_workers_start(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    from megas.cli.main import _ensure_paired, _start_managed
    from megas.client import WorkerRuntime

    config = _ensure_paired(load_config())
    if getattr(args, "foreground", False):
        ctx.output.print("Megas is running in the foreground. Press Ctrl+C to stop.")
        try:
            asyncio.run(WorkerRuntime(config).start_forever())
        except KeyboardInterrupt:
            ctx.output.print("Megas stopped.")
        return {"running": False, "mode": "foreground"}
    status = _start_managed()
    result = {"running": True, "pid": status.get("pid"), "mode": "background"}
    ctx.output.result(result, human_message=f"Megas started (PID {status.get('pid')}).")
    return result


def cmd_workers_stop(ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    from megas.service import stop_watchdog, watchdog_status

    was_running = daemon_status()["running"] or watchdog_status()["running"]
    # Stop supervisor first so it cannot resurrect the Worker mid-stop
    # (root cause of stop/restart races and "why is it offline" flaps).
    stop_watchdog()
    if service_installed():
        stop_installed_service()
    stop_background()
    result = {"running": False, "was_running": was_running}
    ctx.output.result(
        result,
        human_message="Megas stopped." if was_running else "Megas is already stopped.",
    )
    return result


def cmd_workers_restart(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    cmd_workers_stop(ctx, args)
    return cmd_workers_start(ctx, args)


def cmd_workers_capabilities(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    worker_id = getattr(args, "worker_id", None)
    if worker_id:
        worker = api_mod.get_worker(ctx.client(), worker_id)
        caps = worker.get("capabilities_json") or worker.get("capabilities") or []
        result = {"worker_id": worker_id, "capabilities": caps}
    else:
        config = load_config()
        result = {
            "worker_id": config.worker_id or "",
            "capabilities": runtime_capabilities(config),
            "local": True,
        }
    ctx.output.result(result)
    return result


def cmd_workers_logs(ctx: RuntimeContext, args: argparse.Namespace) -> None:
    lines = getattr(args, "lines", 100) or 100
    follow = bool(getattr(args, "follow", False) or ctx.watch)
    if ctx.output.machine and not follow:
        content = list(tail_logs(lines))
        ctx.output.result({"lines": [line.rstrip("\n") for line in content], "path": str(log_path())})
        return
    for line in tail_logs(lines):
        print(line, end="")
    if not follow:
        if not log_path().exists():
            ctx.output.print(f"No Megas logs yet ({log_path()}).")
        return
    import time

    try:
        position = log_path().stat().st_size if log_path().exists() else 0
        while True:
            time.sleep(0.4)
            if not log_path().exists():
                position = 0
                continue
            size = log_path().stat().st_size
            if size < position:
                position = 0
            if size == position:
                continue
            with log_path().open("r", encoding="utf-8", errors="replace") as stream:
                stream.seek(position)
                chunk = stream.read()
                if ctx.output.format in {"json", "jsonl"}:
                    for log_line in chunk.splitlines():
                        ctx.output.stream_event({"type": "log", "message": log_line})
                else:
                    print(chunk, end="", flush=True)
                position = stream.tell()
    except KeyboardInterrupt:
        return


def cmd_workers_cancel(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    """Cancel a job/session associated with worker dispatch."""
    job_id = args.job_id
    require_confirmation(ctx, f"Cancel job/session {job_id}?")
    # Prefer session close; mark-lost is heavier and separate
    try:
        result = api_mod.close_session(ctx.client(), job_id)
        ctx.output.result(result, human_message=f"Cancelled session {job_id}")
        return result
    except Exception:
        pass
    try:
        result = ctx.client().post(f"/api/v1/workers/{job_id}/mark-lost")
        ctx.output.result(result if isinstance(result, dict) else {"ok": True}, human_message=f"Marked lost: {job_id}")
        return result if isinstance(result, dict) else {"ok": True}
    except Exception as exc:
        raise CapabilityUnavailableError(
            "workers.cancel",
            message=f"Could not cancel job {job_id}: {exc}",
            hint="Pass a session id for `POST /api/v1/sessions/{id}/close`.",
        ) from exc


def register(sub: argparse._SubParsersAction) -> None:
    workers = sub.add_parser("workers", help="Manage and inspect Megas workers")
    wsub = workers.add_subparsers(dest="workers_command", required=True)

    list_p = wsub.add_parser("list", help="List workers for the authenticated user")
    list_p.set_defaults(func=_wrap(cmd_workers_list))

    inspect_p = wsub.add_parser("inspect", help="Inspect a worker")
    inspect_p.add_argument("worker_id")
    inspect_p.set_defaults(func=_wrap(cmd_workers_inspect))

    pair_p = wsub.add_parser("pair", help="Pair this machine with a short-lived code")
    pair_p.add_argument("--code", required=True)
    pair_p.add_argument("--backend", default=None)
    pair_p.add_argument("--name", default="Megas Local Worker")
    pair_p.add_argument("--machine", default="")
    pair_p.set_defaults(func=_wrap(cmd_workers_pair))

    start_p = wsub.add_parser("start", help="Start the local worker")
    start_p.add_argument("--foreground", action="store_true")
    start_p.set_defaults(func=_wrap(cmd_workers_start))

    wsub.add_parser("stop", help="Stop the local worker").set_defaults(func=_wrap(cmd_workers_stop))

    restart_p = wsub.add_parser("restart", help="Restart the local worker")
    restart_p.add_argument("--foreground", action="store_true")
    restart_p.set_defaults(func=_wrap(cmd_workers_restart))

    caps = wsub.add_parser("capabilities", help="Show worker capabilities")
    caps.add_argument("worker_id", nargs="?", default=None)
    caps.set_defaults(func=_wrap(cmd_workers_capabilities))

    logs = wsub.add_parser("logs", help="Show local worker logs")
    logs.add_argument("--lines", type=int, default=100)
    logs.add_argument("--follow", "-f", action="store_true")
    logs.set_defaults(func=_wrap(cmd_workers_logs))

    cancel = wsub.add_parser("cancel", help="Cancel a job or session")
    cancel.add_argument("job_id", help="Session or job id")
    cancel.set_defaults(func=_wrap(cmd_workers_cancel))


def _wrap(fn):
    def runner(args: argparse.Namespace) -> None:
        ctx = build_context(args)
        fn(ctx, args)

    runner.__name__ = getattr(fn, "__name__", "workers_cmd")
    runner.__wrapped__ = fn  # type: ignore[attr-defined]
    return runner


__all__ = ["register"]
