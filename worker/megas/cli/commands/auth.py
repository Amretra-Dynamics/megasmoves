"""Authentication commands: login, logout, status."""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Any

from megas.cli.context import RuntimeContext
from megas.cli.errors import AuthenticationError, InvalidArgumentsError
from megas.cli.user_credentials import (
    delete_user_token,
    load_user_meta,
    load_user_token,
    mask_token,
    store_user_token,
)
from megas.config import load_config


def cmd_auth_login(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    token = (getattr(args, "token", None) or ctx.token or "").strip()
    if not token and getattr(args, "stdin", False):
        token = sys.stdin.read().strip()
    if not token and not ctx.non_interactive and sys.stdin.isatty():
        ctx.output.print(
            "Paste a Firebase ID token (from a signed-in browser session) or set MEGAS_TOKEN."
        )
        ctx.output.print(
            "Device pairing for this machine uses `megas connect` / `megas workers pair`."
        )
        try:
            token = getpass.getpass("Token (input hidden): ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise AuthenticationError("Login cancelled.") from exc
    if not token:
        raise InvalidArgumentsError(
            "No token provided.",
            hint="Use `megas auth login --token <token>`, pipe via --stdin, or set MEGAS_TOKEN.",
        )
    email = (getattr(args, "email", None) or "").strip()
    store_user_token(token, email=email, source="cli_login")
    # Validate against backend when possible
    client = ctx.client()
    client.token = token
    identity: dict[str, Any] = {"authenticated": True, "token": mask_token(token)}
    try:
        me = client.get("/v1/me")
        if isinstance(me, dict):
            identity["email"] = me.get("email") or email
            identity["name"] = me.get("name")
            identity["tier"] = me.get("tier")
            if me.get("email"):
                store_user_token(token, email=str(me.get("email")), source="cli_login")
    except Exception as exc:  # validation is best-effort
        ctx.output.warn(f"Token stored but profile check failed: {exc}")
        identity["profile_check"] = "failed"
        if email:
            identity["email"] = email
    ctx.output.result(identity, human_message="Signed in. Token stored in the platform secure store.")
    return identity


def cmd_auth_logout(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    delete_user_token()
    result = {"authenticated": False, "message": "User API token removed."}
    if getattr(args, "revoke_worker", False):
        result["hint"] = "Worker pairing is separate; use `megas worker revoke` to disconnect the machine."
    ctx.output.result(result, human_message="Logged out (user API token cleared).")
    return result


def cmd_auth_status(ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    meta = load_user_meta()
    token = load_user_token()
    worker = load_config()
    status = {
        "user_authenticated": bool(token),
        "user_token_preview": mask_token(token) if token else "",
        "user_email": meta.get("email") or "",
        "credential_backend": meta.get("backend") or ("env" if token and not meta else ""),
        "api_url": ctx.api_url,
        "worker_paired": worker.is_paired,
        "worker_id": worker.worker_id or "",
        "machine_name": worker.machine_name or "",
    }
    if token:
        try:
            me = ctx.client().get("/v1/me")
            if isinstance(me, dict):
                status["profile"] = {
                    "email": me.get("email"),
                    "name": me.get("name"),
                    "tier": me.get("tier"),
                }
        except Exception as exc:
            status["profile_error"] = str(exc)
    ctx.output.result(status)
    return status


def register(sub: argparse._SubParsersAction) -> None:
    auth = sub.add_parser("auth", help="Authenticate the CLI for user API access")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    login = auth_sub.add_parser("login", help="Store a user API bearer token securely")
    login.add_argument("--token", default=None, help="Firebase ID token (prefer env MEGAS_TOKEN)")
    login.add_argument("--email", default="", help="Optional email label for the credential")
    login.add_argument("--stdin", action="store_true", help="Read token from stdin")
    login.set_defaults(func=_wrap(cmd_auth_login))

    logout = auth_sub.add_parser("logout", help="Remove stored user API token")
    logout.add_argument("--revoke-worker", action="store_true", help="Print hint about worker revoke")
    logout.set_defaults(func=_wrap(cmd_auth_logout))

    status = auth_sub.add_parser("status", help="Show authentication status")
    status.set_defaults(func=_wrap(cmd_auth_status))


def _wrap(fn):
    def runner(args: argparse.Namespace) -> None:
        from megas.cli.context import build_context

        ctx = build_context(args)
        fn(ctx, args)

    runner.__name__ = getattr(fn, "__name__", "auth_cmd")
    runner.__wrapped__ = fn  # type: ignore[attr-defined]
    return runner


__all__ = ["register", "cmd_auth_login", "cmd_auth_logout", "cmd_auth_status"]
