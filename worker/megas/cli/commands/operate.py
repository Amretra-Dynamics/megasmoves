"""Operate command group — explicit opt-in plus safe inspection and stop controls."""

from __future__ import annotations

import argparse
import platform
from typing import Any

from megas.cli import api as api_mod
from megas.cli.context import RuntimeContext, build_context
from megas.cli.errors import CapabilityUnavailableError, InvalidArgumentsError
from megas.cli.features import capability_enabled
from megas.cli.output import format_timestamp
from megas.cli.safety import require_confirmation
from megas.operate.gate import (
    OPERATE_ACK_VERSION,
    disable_operate,
    enable_operate,
    is_operate_enabled,
    operate_status,
)

_OPERATE_CONFIRMATION = "ENABLE OPERATE"
_OPERATE_WARNING = """Megas Operate can control browsers, type into applications, execute
commands, change files and settings, submit forms, and act inside signed-in accounts.
It can misunderstand instructions or cause unintended changes, data loss, charges, or
account actions. Use a test or otherwise safe environment, keep backups, grant minimum
permissions, supervise active work, and keep Emergency Stop available. Enabling Operate
records this acknowledgement on this machine; it is not a guarantee of error-free action.
"""


def _require_operate_start_capability(name: str) -> None:
    """Gate creation of new Operate work; never gate inspection or emergency stop."""
    if not is_operate_enabled():
        raise CapabilityUnavailableError(
            name,
            message="Megas Operate is disabled on this machine.",
            hint="Review the warning and run `megas operate enable` on the Worker machine.",
        )
    if not capability_enabled(name):
        raise CapabilityUnavailableError(
            name,
            message=f"Feature flag disabled for {name}.",
            hint="This Operate capability is unavailable in the current Worker build.",
        )


def cmd_operate_status(ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    payload = operate_status()
    payload["risk_notice_version"] = OPERATE_ACK_VERSION
    payload["warning"] = _OPERATE_WARNING.strip()
    payload["inspection_and_emergency_stop_available_when_disabled"] = True
    ctx.output.result(
        payload,
        human_message=(
            "Megas Operate is enabled on this machine."
            if payload["enabled"]
            else "Megas Operate is disabled on this machine."
        ),
    )
    return payload


def cmd_operate_enable(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    accepted_risk = bool(getattr(args, "accept_risk", False))
    safe_environment = bool(getattr(args, "safe_environment", False))

    if not (accepted_risk and safe_environment):
        if ctx.non_interactive:
            raise InvalidArgumentsError(
                "Non-interactive Operate enablement requires both "
                "--accept-risk and --safe-environment."
            )
        print(_OPERATE_WARNING)
        typed = input(
            f"Type {_OPERATE_CONFIRMATION!r} to enable Operate on this machine: "
        ).strip()
        if typed != _OPERATE_CONFIRMATION:
            raise InvalidArgumentsError(
                "Operate was not enabled; acknowledgement did not match."
            )
        accepted_risk = True
        safe_environment = True

    state = enable_operate(
        accepted_risk=accepted_risk,
        safe_environment_acknowledged=safe_environment,
    )
    payload = operate_status()
    payload["enabled_at"] = state.enabled_at
    ctx.output.result(
        payload,
        human_message=(
            "Megas Operate enabled on this machine. Restart Megas so the Worker "
            "advertises Operate capabilities."
        ),
    )
    return payload


def cmd_operate_disable(ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    disable_operate()
    payload = operate_status()
    ctx.output.result(
        payload,
        human_message=(
            "Megas Operate disabled on this machine. New Operate work is blocked. "
            "Inspection and Emergency Stop remain available; restart Megas to remove "
            "advertised Operate capabilities."
        ),
    )
    return payload


def cmd_operate_browser_start(
    ctx: RuntimeContext,
    args: argparse.Namespace,
) -> dict[str, Any]:
    _require_operate_start_capability("operate.browser")
    session = api_mod.create_browser_session(
        ctx.client(),
        url=getattr(args, "url", None),
        browser=getattr(args, "browser", "chromium"),
        profile_id=getattr(args, "profile", None),
    )
    ctx.output.result(
        session,
        human_message=(
            f"Browser session {session.get('id')} queued for a capable Megas Worker."
        ),
    )
    return session


def _session_display(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.get("id") or row.get("session_id"),
            "status": row.get("status") or "",
            "type": row.get("type") or row.get("kind") or "session",
            "updated": format_timestamp(
                row.get("updated_at") or row.get("created_at")
            ),
        }
        for row in rows
    ]


def _emit_sessions(ctx: RuntimeContext, rows: list[dict[str, Any]]) -> None:
    ctx.output.result(
        _session_display(rows),
        columns=[
            ("id", "ID"),
            ("status", "STATUS"),
            ("type", "TYPE"),
            ("updated", "UPDATED"),
        ],
    )


def cmd_operate_browser_sessions(
    ctx: RuntimeContext,
    _args: argparse.Namespace,
) -> list[dict[str, Any]]:
    rows = api_mod.list_operate_sessions(ctx.client())
    selected = [
        row
        for row in rows
        if "browser"
        in str(
            row.get("type")
            or row.get("kind")
            or row.get("mode")
            or row.get("capabilities")
            or ""
        ).lower()
    ]
    _emit_sessions(ctx, selected)
    return selected


def cmd_operate_browser_inspect(
    ctx: RuntimeContext,
    args: argparse.Namespace,
) -> dict[str, Any]:
    try:
        session = api_mod.get_browser_session(ctx.client(), args.session_id)
    except Exception as exc:
        raise CapabilityUnavailableError(
            "operate.browser.inspect",
            message=f"Could not inspect session {args.session_id}: {exc}",
        ) from exc
    ctx.output.result(session)
    return session


def cmd_operate_browser_stop(
    ctx: RuntimeContext,
    args: argparse.Namespace,
) -> dict[str, Any]:
    require_confirmation(ctx, f"Stop browser session {args.session_id}?")
    result = api_mod.stop_browser_session(ctx.client(), args.session_id)
    ctx.output.result(
        result,
        human_message=f"Stop requested for session {args.session_id}.",
    )
    return result if isinstance(result, dict) else {"ok": True}


def cmd_operate_windows_start(
    ctx: RuntimeContext,
    args: argparse.Namespace,
) -> dict[str, Any]:
    _require_operate_start_capability("operate.windows")
    if platform.system() != "Windows" and not getattr(args, "force", False):
        raise CapabilityUnavailableError(
            "operate.windows.start",
            message="Windows CUA start is only supported on Windows hosts.",
            hint="Run this command on a paired Windows Worker with CUA capability.",
        )
    raise CapabilityUnavailableError(
        "operate.windows.start",
        message=(
            "Windows CUA session start is controlled by the Worker runtime, "
            "not a public REST start API yet."
        ),
        hint=(
            "Ensure the Windows Worker is paired and healthy; dispatch comes "
            "from the backend session spine."
        ),
    )


def cmd_operate_windows_sessions(
    ctx: RuntimeContext,
    _args: argparse.Namespace,
) -> list[dict[str, Any]]:
    rows = api_mod.list_operate_sessions(ctx.client())
    selected = [
        row
        for row in rows
        if "windows"
        in str(
            row.get("type")
            or row.get("kind")
            or row.get("platform")
            or ""
        ).lower()
        or "cua" in str(row.get("type") or row.get("kind") or "").lower()
    ]
    _emit_sessions(ctx, selected)
    return selected


def cmd_operate_stop(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    # Emergency Stop is deliberately available even when the local owner gate is
    # disabled or its acknowledgement version has expired.
    require_confirmation(ctx, f"Emergency-stop operate session {args.session_id}?")
    result = api_mod.emergency_stop_session(ctx.client(), args.session_id)
    ctx.output.result(
        result,
        human_message=f"Emergency stop requested for {args.session_id}.",
    )
    return result if isinstance(result, dict) else {"ok": True}


def register(sub: argparse._SubParsersAction) -> None:
    operate = sub.add_parser(
        "operate",
        help="Explicitly enable and control browser/system automation",
    )
    osub = operate.add_subparsers(dest="operate_command", required=True)

    osub.add_parser(
        "status",
        help="Show whether Operate is enabled on this machine",
    ).set_defaults(func=_wrap(cmd_operate_status))

    enable = osub.add_parser(
        "enable",
        help="Acknowledge risk and enable Operate on this machine",
    )
    enable.add_argument(
        "--accept-risk",
        action="store_true",
        help="Acknowledge that Operate may perform unintended or damaging actions",
    )
    enable.add_argument(
        "--safe-environment",
        action="store_true",
        help="Acknowledge that this machine/accounts are prepared for automation",
    )
    enable.set_defaults(func=_wrap(cmd_operate_enable))

    osub.add_parser(
        "disable",
        help="Block new Operate work on this machine",
    ).set_defaults(func=_wrap(cmd_operate_disable))

    browser = osub.add_parser("browser", help="Browser automation sessions")
    bsub = browser.add_subparsers(dest="browser_command", required=True)
    bstart = bsub.add_parser("start", help="Start a dedicated Megas Chromium session")
    bstart.add_argument("--url", default=None)
    bstart.add_argument(
        "--browser",
        choices=("chromium", "chrome", "edge"),
        default="chromium",
        help=(
            "Dedicated Megas Chromium runtime. 'chrome' and 'edge' are retained "
            "only as compatibility aliases and do not select installed browsers."
        ),
    )
    bstart.add_argument("--profile", default=None, help="Saved browser profile ID")
    bstart.set_defaults(func=_wrap(cmd_operate_browser_start))
    bsub.add_parser("sessions", help="List browser sessions").set_defaults(
        func=_wrap(cmd_operate_browser_sessions)
    )
    binspect = bsub.add_parser("inspect", help="Inspect a browser session")
    binspect.add_argument("session_id")
    binspect.set_defaults(func=_wrap(cmd_operate_browser_inspect))
    bstop = bsub.add_parser("stop", help="Stop a browser session")
    bstop.add_argument("session_id")
    bstop.set_defaults(func=_wrap(cmd_operate_browser_stop))

    windows = osub.add_parser("windows", help="Windows CUA sessions")
    wsub = windows.add_subparsers(dest="windows_command", required=True)
    wstart = wsub.add_parser("start", help="Start a Windows CUA session")
    wstart.add_argument("--force", action="store_true", help="Skip local OS check")
    wstart.set_defaults(func=_wrap(cmd_operate_windows_start))
    wsub.add_parser("sessions", help="List Windows CUA sessions").set_defaults(
        func=_wrap(cmd_operate_windows_sessions)
    )

    stop = osub.add_parser("stop", help="Emergency-stop any Operate session")
    stop.add_argument("session_id")
    stop.set_defaults(func=_wrap(cmd_operate_stop))


def _wrap(fn):
    def runner(args: argparse.Namespace) -> None:
        ctx = build_context(args)
        fn(ctx, args)

    runner.__name__ = getattr(fn, "__name__", "operate_cmd")
    runner.__wrapped__ = fn  # type: ignore[attr-defined]
    return runner


__all__ = ["register"]
