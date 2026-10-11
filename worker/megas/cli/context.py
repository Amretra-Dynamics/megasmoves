"""Runtime context assembled from global flags, env, and config files."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any

from megas.config import WorkerConfig, load_config

from .api import OpsApiClient
from .config_store import CliSettings, load_settings
from .output import Output
from .user_credentials import load_user_token


@dataclass
class RuntimeContext:
    settings: CliSettings
    output: Output
    yes: bool = False
    non_interactive: bool = False
    watch: bool = False
    since: str | None = None
    limit: int | None = None
    token: str | None = None
    debug: bool = False
    verbose: bool = False
    worker_config: WorkerConfig = field(default_factory=load_config)
    _client: OpsApiClient | None = field(default=None, repr=False)

    @property
    def api_url(self) -> str:
        return self.settings.api_url

    @property
    def timeout(self) -> float:
        return float(self.settings.timeout)

    def client(self) -> OpsApiClient:
        if self._client is None:
            token = self.token if self.token is not None else load_user_token()
            self._client = OpsApiClient(
                api_url=self.api_url,
                token=token or "",
                timeout=self.timeout,
                worker_config=self.worker_config,
            )
        return self._client


def build_context(args: argparse.Namespace) -> RuntimeContext:
    flag_overrides: dict[str, Any] = {}
    api_url = getattr(args, "api_url", None)
    if api_url:
        flag_overrides["api_url"] = str(api_url).rstrip("/")
    if getattr(args, "timeout", None) is not None:
        flag_overrides["timeout"] = float(args.timeout)

    output_format = getattr(args, "output", None)
    if getattr(args, "json", False):
        output_format = "json"
    if output_format:
        flag_overrides["output"] = output_format

    profile = getattr(args, "profile", None)
    settings = load_settings(profile=profile, flag_overrides=flag_overrides)

    quiet = bool(getattr(args, "quiet", False))
    verbose = bool(getattr(args, "verbose", False))
    debug = bool(getattr(args, "debug", False))
    no_color = bool(getattr(args, "no_color", False)) or not settings.color
    non_interactive = bool(getattr(args, "non_interactive", False)) or settings.non_interactive

    fmt = settings.output if settings.output in {"table", "json", "yaml", "jsonl"} else "table"
    if output_format in {"table", "json", "yaml", "jsonl"}:
        fmt = output_format

    output = Output(
        format=fmt,
        quiet=quiet,
        verbose=verbose,
        debug=debug,
        no_color=no_color,
        non_interactive=non_interactive,
    )

    token = getattr(args, "token", None)
    if token is not None:
        token = str(token).strip() or None

    worker_config = load_config()
    # Allow api-url override to also influence worker backend for local status
    if api_url:
        worker_config.backend_url = str(api_url).rstrip("/")

    return RuntimeContext(
        settings=settings,
        output=output,
        yes=bool(getattr(args, "yes", False)),
        non_interactive=non_interactive,
        watch=bool(getattr(args, "watch", False)),
        since=getattr(args, "since", None),
        limit=getattr(args, "limit", None),
        token=token,
        debug=debug,
        verbose=verbose,
        worker_config=worker_config,
    )


def add_global_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit JSON on stdout (alias for --output json)")
    parser.add_argument(
        "--output",
        choices=["json", "table", "yaml", "jsonl"],
        default=None,
        help="Output format (default: table)",
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress non-essential human output")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose human output")
    parser.add_argument("--debug", action="store_true", help="Debug logging on stderr")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    parser.add_argument("--profile", default=None, help="Named configuration profile")
    parser.add_argument("--api-url", default=None, help="Megas Backend base URL")
    parser.add_argument("--timeout", type=float, default=None, help="HTTP timeout in seconds")
    parser.add_argument("--token", default=None, help="User API bearer token (prefer secure store)")
    parser.add_argument("--yes", "-y", action="store_true", help="Confirm destructive actions without prompting")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never prompt; fail if confirmation or input is required",
    )
    parser.add_argument("--watch", action="store_true", help="Watch / stream updates when supported")
    parser.add_argument("--since", default=None, help="Start time or cursor for list/stream commands")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of items to return")


__all__ = ["RuntimeContext", "add_global_flags", "build_context"]
