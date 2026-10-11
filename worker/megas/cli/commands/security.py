"""Megas Security CLI surface.

Security remains visible as a product direction, but no audit, finding, or
remediation command is allowed to imply that the product is operational before
the waitlist release is ready.
"""

from __future__ import annotations

import argparse
from typing import Any, NoReturn

from megas.cli.context import RuntimeContext, build_context
from megas.cli.errors import CapabilityUnavailableError

_SECURITY_WAITLIST_MESSAGE = (
    "Megas Security is waitlist-only. Security audits, findings, and automated "
    "remediation are not enabled in this release."
)
_SECURITY_WAITLIST_HINT = (
    "Use the Security waitlist in the Megas Moves pricing/product UI. "
    "No security scan or remediation was started."
)


def _require_security_available(capability: str) -> NoReturn:
    raise CapabilityUnavailableError(
        capability,
        message=_SECURITY_WAITLIST_MESSAGE,
        hint=_SECURITY_WAITLIST_HINT,
    )


def cmd_security_status(ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    payload = {
        "product": "megas_security",
        "availability": "waitlist",
        "operational": False,
        "audits_enabled": False,
        "findings_enabled": False,
        "remediation_enabled": False,
        "message": _SECURITY_WAITLIST_MESSAGE,
    }
    ctx.output.result(payload, human_message=_SECURITY_WAITLIST_MESSAGE)
    return payload


def cmd_security_findings(_ctx: RuntimeContext, _args: argparse.Namespace) -> list[dict[str, Any]]:
    _require_security_available("security.findings")


def cmd_security_finding_show(_ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    _require_security_available("security.findings.detail")


def cmd_security_audit_start(_ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    _require_security_available("security.audit.start")


def cmd_security_audit_status(_ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    _require_security_available("security.audit.status")


def cmd_security_remediation_review(_ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    _require_security_available("security.remediation.review")


def cmd_security_remediation_approve(_ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    _require_security_available("security.remediation.approve")


def cmd_security_remediation_reject(_ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    _require_security_available("security.remediation.reject")


def cmd_security_remediation_rollback(_ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    _require_security_available("security.remediation.rollback")


def register(sub: argparse._SubParsersAction) -> None:
    security = sub.add_parser(
        "security",
        help="Megas Security product status (waitlist-only in this release)",
    )
    ssub = security.add_subparsers(dest="security_command", required=True)

    ssub.add_parser("status", help="Show Security waitlist availability").set_defaults(
        func=_wrap(cmd_security_status)
    )
    ssub.add_parser("findings", help="List security findings (waitlist-only)").set_defaults(
        func=_wrap(cmd_security_findings)
    )

    show = ssub.add_parser("finding", help="Finding detail (waitlist-only)")
    show_sub = show.add_subparsers(dest="finding_command", required=True)
    show_one = show_sub.add_parser("show", help="Show a finding")
    show_one.add_argument("finding_id")
    show_one.set_defaults(func=_wrap(cmd_security_finding_show))

    audit = ssub.add_parser("audit", help="Security audits (waitlist-only)")
    asub = audit.add_subparsers(dest="audit_command", required=True)
    asub.add_parser("start", help="Start a security audit").set_defaults(
        func=_wrap(cmd_security_audit_start)
    )
    astatus = asub.add_parser("status", help="Audit status")
    astatus.add_argument("audit_id", nargs="?", default=None)
    astatus.set_defaults(func=_wrap(cmd_security_audit_status))

    rem = ssub.add_parser("remediation", help="Remediation actions (waitlist-only)")
    rsub = rem.add_subparsers(dest="remediation_command", required=True)

    review = rsub.add_parser("review", help="Review a remediation")
    review.add_argument("remediation_id")
    review.set_defaults(func=_wrap(cmd_security_remediation_review))

    approve = rsub.add_parser("approve", help="Approve a remediation")
    approve.add_argument("remediation_id")
    approve.add_argument("--reason", default="")
    approve.set_defaults(func=_wrap(cmd_security_remediation_approve))

    reject = rsub.add_parser("reject", help="Reject a remediation")
    reject.add_argument("remediation_id")
    reject.add_argument("--reason", default="")
    reject.set_defaults(func=_wrap(cmd_security_remediation_reject))

    rollback = rsub.add_parser("rollback", help="Rollback a remediation")
    rollback.add_argument("remediation_id")
    rollback.add_argument("--reason", default="")
    rollback.set_defaults(func=_wrap(cmd_security_remediation_rollback))


def _wrap(fn):
    def runner(args: argparse.Namespace) -> None:
        ctx = build_context(args)
        fn(ctx, args)

    runner.__name__ = getattr(fn, "__name__", "security_cmd")
    runner.__wrapped__ = fn  # type: ignore[attr-defined]
    return runner


__all__ = ["register"]
