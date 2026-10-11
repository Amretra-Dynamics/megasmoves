"""CLI exception types mapped to stable exit codes."""

from __future__ import annotations

from typing import Any

from .exit_codes import (
    EXIT_APPROVAL_REQUIRED,
    EXIT_AUTH,
    EXIT_CONFLICT,
    EXIT_GENERAL,
    EXIT_INVALID_ARGS,
    EXIT_NETWORK,
    EXIT_NOT_FOUND,
    EXIT_PARTIAL,
    EXIT_PERMISSION,
    EXIT_TIMEOUT,
)


class CliError(Exception):
    """Base CLI error with a stable exit code."""

    exit_code: int = EXIT_GENERAL

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        details: dict[str, Any] | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code
        self.message = message
        self.details = details or {}
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": True,
            "message": self.message,
            "exit_code": self.exit_code,
            "code": type(self).__name__,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.details:
            payload["details"] = self.details
        return payload


class InvalidArgumentsError(CliError):
    exit_code = EXIT_INVALID_ARGS


class AuthenticationError(CliError):
    exit_code = EXIT_AUTH


class PermissionDeniedError(CliError):
    exit_code = EXIT_PERMISSION


class NotFoundError(CliError):
    exit_code = EXIT_NOT_FOUND


class ConflictError(CliError):
    exit_code = EXIT_CONFLICT


class NetworkError(CliError):
    exit_code = EXIT_NETWORK


class RequestTimeoutError(CliError):
    exit_code = EXIT_TIMEOUT


class PartialFailureError(CliError):
    exit_code = EXIT_PARTIAL


class ApprovalRequiredError(CliError):
    exit_code = EXIT_APPROVAL_REQUIRED


class CapabilityUnavailableError(CliError):
    """Backend capability or feature flag is not available."""

    exit_code = EXIT_GENERAL

    def __init__(self, capability: str, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(
            message
            or (
                f"Capability '{capability}' is not available on this backend. "
                "The API may not be deployed yet, or your account lacks access."
            ),
            details={"capability": capability, **(kwargs.get("details") or {})},
            hint=kwargs.get("hint")
            or "Check docs/cli for supported endpoints and feature flags.",
            exit_code=kwargs.get("exit_code", EXIT_GENERAL),
        )
        self.capability = capability


def map_http_status(status: int, message: str, *, details: dict[str, Any] | None = None) -> CliError:
    """Map an HTTP status code to a typed CLI error."""
    if status in {401, 428}:
        return AuthenticationError(message, details=details, hint="Run `megas auth login` or pass --token.")
    if status == 403:
        return PermissionDeniedError(message, details=details)
    if status == 404:
        return NotFoundError(message, details=details)
    if status in {409, 412}:
        return ConflictError(message, details=details)
    if status == 408 or status == 504:
        return RequestTimeoutError(message, details=details)
    if status == 402 or status == 425:
        return ApprovalRequiredError(message, details=details)
    if status == 422:
        return InvalidArgumentsError(message, details=details)
    if status >= 500:
        return NetworkError(message, details=details, hint="Backend error; retry or check monitor status.")
    return CliError(message, details=details)


__all__ = [
    "CliError",
    "InvalidArgumentsError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "NetworkError",
    "RequestTimeoutError",
    "PartialFailureError",
    "ApprovalRequiredError",
    "CapabilityUnavailableError",
    "map_http_status",
]
