"""Megas Moves orchestration CLI subcommands (submit, list, status, cancel, retry, resume)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from megas.cli.context import RuntimeContext, build_context
from megas.cli.errors import CliError, InvalidArgumentsError
from megas.config import load_config


def _api_request(
    context: RuntimeContext,
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    backend_url = context.config.backend_url if hasattr(context, "config") and context.config else getattr(context.settings, "api_url", "http://localhost:8000")
    url = f"{backend_url.rstrip('/')}/api/v1/orchestrations{endpoint}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "MegasCLI/1.0",
    }
    if context.token:
        headers["Authorization"] = f"Bearer {context.token}"

    payload_bytes = json.dumps(data).encode("utf-8") if data is not None else None
    req = Request(url, data=payload_bytes, headers=headers, method=method)

    try:
        with urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}
    except HTTPError as err:
        error_body = err.read().decode("utf-8") if err.fp else ""
        try:
            detail = json.loads(error_body).get("detail", error_body)
        except Exception:
            detail = error_body or err.reason
        raise CliError(f"API request failed ({err.code}): {detail}") from err
    except (URLError, TimeoutError, OSError) as err:
        raise CliError(f"Failed to connect to Megas API: {err}") from err


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "moves",
        aliases=["move", "orchestration"],
        help="Manage Megas Moves multi-agent orchestrations",
    )
    msub = parser.add_subparsers(dest="move_command", required=True)

    # submit
    submit_p = msub.add_parser("submit", help="Submit a new Moves orchestration plan")
    submit_p.add_argument("plan_file", help="Path to plan JSON file (or '-' for stdin)")
    submit_p.add_argument("--start", action="store_true", help="Start execution immediately after submission")
    submit_p.set_defaults(func=_handle_submit)

    # list
    list_p = msub.add_parser("list", help="List recent orchestrations")
    list_p.add_argument("--limit", type=int, default=10, help="Number of records to show")
    list_p.set_defaults(func=_handle_list)

    # status
    status_p = msub.add_parser("status", help="Get orchestration status and DAG node graph")
    status_p.add_argument("id", help="Orchestration ID")
    status_p.set_defaults(func=_handle_status)

    # cancel
    cancel_p = msub.add_parser("cancel", help="Cancel an active orchestration")
    cancel_p.add_argument("id", help="Orchestration ID")
    cancel_p.add_argument("--reason", default="Cancelled from Megas CLI", help="Cancellation reason")
    cancel_p.add_argument("--idempotency-key", default=None, help="Idempotency key for request replay/debugging")
    cancel_p.set_defaults(func=_handle_cancel)

    # retry
    retry_p = msub.add_parser("retry", help="Retry a failed child step in an orchestration")
    retry_p.add_argument("id", help="Orchestration ID")
    retry_p.add_argument("child_name", help="Name of the child step to retry")
    retry_p.add_argument("--reason", default="", help="Reason for retrying child step")
    retry_p.add_argument("--idempotency-key", default=None, help="Idempotency key for request replay/debugging")
    retry_p.set_defaults(func=_handle_retry)

    # resume
    resume_p = msub.add_parser("resume", help="Resume a paused/failed orchestration")
    resume_p.add_argument("id", help="Orchestration ID")
    resume_p.add_argument("--reason", default="", help="Reason for resuming orchestration")
    resume_p.add_argument("--idempotency-key", default=None, help="Idempotency key for request replay/debugging")
    resume_p.set_defaults(func=_handle_resume)


register_parser = register


def _handle_submit(args: argparse.Namespace) -> int:
    context = build_context(load_config())
    plan_path = args.plan_file
    if plan_path == "-":
        import sys
        raw = sys.stdin.read()
    else:
        p = Path(plan_path)
        if not p.exists():
            raise InvalidArgumentsError(f"Plan file '{plan_path}' does not exist.")
        raw = p.read_text(encoding="utf-8")

    try:
        plan_dict = json.loads(raw)
    except json.JSONDecodeError as err:
        raise InvalidArgumentsError(f"Invalid JSON in plan file: {err}") from err

    res = _api_request(context, "", method="POST", data=plan_dict)
    orch_id = res.get("orchestration_id")
    print(f"✓ Submitted orchestration plan: {orch_id}")

    if getattr(args, "start", False) and orch_id:
        _api_request(context, f"/{orch_id}/start", method="POST")
        print(f"✓ Started orchestration {orch_id}")
    return 0


def _handle_list(args: argparse.Namespace) -> int:
    context = build_context(load_config())
    limit = getattr(args, "limit", 10)
    res = _api_request(context, f"?limit={limit}")
    orchestrations = res.get("orchestrations") or []

    if not orchestrations:
        print("No orchestrations found.")
        return 0

    print("=" * 72)
    print(f"  {'ID':<36} {'STRATEGY':<18} {'STATUS':<12}")
    print("=" * 72)
    for o in orchestrations:
        print(f"  {o.get('id'):<36} {o.get('strategy'):<18} {str(o.get('status')).upper():<12}")
    print("=" * 72)
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    context = build_context(load_config())
    resp = _api_request(context, f"/{args.id}")
    orch = resp.get("orchestration") or {}
    children = resp.get("children") or []

    print("=" * 72)
    print(f"                   MOVES DETAILS: {orch.get('id', args.id)}")
    print("=" * 72)
    print(f"  Strategy:     {orch.get('strategy')}")
    print(f"  Status:       {str(orch.get('status')).upper()}")
    print(f"  Task ID:      {orch.get('task_id')}")
    print(f"  Session ID:   {orch.get('session_id')}")
    print(f"  Created At:   {orch.get('created_at')}")
    if orch.get("terminal_reason"):
        print(f"  Reason:       {orch.get('terminal_reason')}")
    print("-" * 72)
    print("  CHILD STEPS (DAG):")
    for child in children:
        c_name = child.get("name")
        c_status = (child.get("status") or "").upper()
        c_deps = ", ".join(child.get("depends_on", [])) or "none"
        c_worker = child.get("worker_id") or "unassigned"
        print(f"  • {c_name:<18} [{c_status:<10}] deps: {c_deps:<16} worker: {c_worker}")
    print("=" * 72)
    return 0


def _handle_cancel(args: argparse.Namespace) -> int:
    import uuid
    context = build_context(load_config())
    key = getattr(args, "idempotency_key", None) or str(uuid.uuid4())
    reason = getattr(args, "reason", "Cancelled from Megas CLI") or "Cancelled from Megas CLI"
    _api_request(context, f"/{args.id}/cancel", method="POST", data={"idempotency_key": key, "reason": reason})
    print(f"✓ Cancelled orchestration {args.id}")
    return 0


def _handle_retry(args: argparse.Namespace) -> int:
    import uuid
    context = build_context(load_config())
    key = getattr(args, "idempotency_key", None) or str(uuid.uuid4())
    reason = getattr(args, "reason", "") or ""
    _api_request(context, f"/{args.id}/retry/{args.child_name}", method="POST", data={"idempotency_key": key, "reason": reason})
    print(f"✓ Scheduled retry for step '{args.child_name}' in {args.id}")
    return 0


def _handle_resume(args: argparse.Namespace) -> int:
    import uuid
    context = build_context(load_config())
    key = getattr(args, "idempotency_key", None) or str(uuid.uuid4())
    reason = getattr(args, "reason", "") or ""
    _api_request(context, f"/{args.id}/resume", method="POST", data={"idempotency_key": key, "reason": reason})
    print(f"✓ Resumed orchestration {args.id}")
    return 0
