"""Stable exit codes for the Megas CLI.

These codes are part of the automation contract. Do not renumber without a
documented major-version change.
"""

from __future__ import annotations

EXIT_SUCCESS = 0
EXIT_GENERAL = 1
EXIT_INVALID_ARGS = 2
EXIT_AUTH = 3
EXIT_PERMISSION = 4
EXIT_NOT_FOUND = 5
EXIT_CONFLICT = 6
EXIT_NETWORK = 7
EXIT_TIMEOUT = 8
EXIT_PARTIAL = 9
EXIT_APPROVAL_REQUIRED = 10

EXIT_CODE_NAMES: dict[int, str] = {
    EXIT_SUCCESS: "success",
    EXIT_GENERAL: "general_failure",
    EXIT_INVALID_ARGS: "invalid_arguments",
    EXIT_AUTH: "authentication_failure",
    EXIT_PERMISSION: "permission_denied",
    EXIT_NOT_FOUND: "not_found",
    EXIT_CONFLICT: "conflict",
    EXIT_NETWORK: "network_failure",
    EXIT_TIMEOUT: "timeout",
    EXIT_PARTIAL: "partial_failure",
    EXIT_APPROVAL_REQUIRED: "approval_required",
}

__all__ = [
    "EXIT_SUCCESS",
    "EXIT_GENERAL",
    "EXIT_INVALID_ARGS",
    "EXIT_AUTH",
    "EXIT_PERMISSION",
    "EXIT_NOT_FOUND",
    "EXIT_CONFLICT",
    "EXIT_NETWORK",
    "EXIT_TIMEOUT",
    "EXIT_PARTIAL",
    "EXIT_APPROVAL_REQUIRED",
    "EXIT_CODE_NAMES",
]
