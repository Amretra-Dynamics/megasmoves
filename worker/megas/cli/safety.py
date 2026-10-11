"""Confirmation prompts and destructive-action guards."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from .errors import ApprovalRequiredError, InvalidArgumentsError
from .output import is_tty

if TYPE_CHECKING:
    from .context import RuntimeContext


def require_confirmation(
    ctx: RuntimeContext,
    message: str,
    *,
    destructive: bool = True,
) -> None:
    """Require explicit confirmation unless --yes or non-interactive with approval.

    Rules:
    - --yes always proceeds
    - non-interactive without --yes raises (never prompts)
    - interactive TTY prompts y/N
    """
    if getattr(ctx, "yes", False):
        return
    if ctx.non_interactive or not is_tty(sys.stdin):
        raise ApprovalRequiredError(
            f"Confirmation required: {message}",
            hint="Re-run with --yes to confirm, or run interactively in a TTY.",
            details={"destructive": destructive},
        )
    prompt = f"{message} [y/N] "
    try:
        answer = input(prompt).strip().lower()
    except EOFError as exc:
        raise ApprovalRequiredError(
            "Confirmation prompt closed without an answer.",
            hint="Re-run with --yes for non-interactive approval.",
        ) from exc
    if answer not in {"y", "yes"}:
        raise InvalidArgumentsError("Aborted by user.")


__all__ = ["require_confirmation"]
