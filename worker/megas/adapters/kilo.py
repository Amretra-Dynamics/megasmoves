"""Kilo Code CLI adapter."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time

from .base import (
    prefer_console_free_tool_path,
    resolve_cli_argv,
    subprocess_hide_window_kwargs,
)
from .headless_agent import ReadOnlyHeadlessAgentAdapter
from .opencode import OpenCodeAdapter
from ..thread_model import ProviderCapabilities


class KiloCodeAdapter(ReadOnlyHeadlessAgentAdapter):
    name = "kilo_code"
    adapter_id = "kilo_code"
    supported_range = ">=1.0.0"
    binary_env = "MEGAS_KILO_BINARY"
    default_binary = "kilo"
    timeout_env = "MEGAS_KILO_TIMEOUT_SEC"
    version_args = ["--version"]
    version_regex = r"(\d+\.\d+\.\d+)"
    _auth_cache: tuple[float, bool] | None = None
    _AUTH_CACHE_TTL_SEC = 60.0

    def discover_executable(self) -> Path | None:
        for candidate in (
            os.getenv(self.binary_env, "").strip(),
            "kilo",
            "kilocode",
        ):
            if not candidate:
                continue
            which = shutil.which(candidate)
            if which:
                return Path(which)
            path = Path(candidate)
            if path.is_file():
                return path
        return None

    def is_authenticated(self) -> bool:
        """Use provider commands as evidence instead of assuming login state."""
        now = time.monotonic()
        cached = type(self)._auth_cache
        if cached is not None and now - cached[0] < self._AUTH_CACHE_TTL_SEC:
            return cached[1]

        executable = self.discover_executable()
        if executable is None:
            type(self)._auth_cache = (now, False)
            return False

        authenticated = False
        probe_timed_out = False
        for command in (("profile", "--json"), ("auth", "list")):
            try:
                result = subprocess.run(
                    resolve_cli_argv(str(executable), *command),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10.0,
                    check=False,
                    env=prefer_console_free_tool_path(dict(os.environ)),
                    **subprocess_hide_window_kwargs(),
                )
            except subprocess.TimeoutExpired:
                probe_timed_out = True
                continue
            except (OSError, subprocess.SubprocessError):
                continue
            output = (result.stdout + "\n" + result.stderr).strip().lower()
            if result.returncode == 0 and output and not any(
                marker in output
                for marker in (
                    "not authenticated",
                    "not logged in",
                    "no credentials",
                    "no providers configured",
                    "sign in required",
                )
            ):
                authenticated = True
                break

        if not authenticated and probe_timed_out:
            # If the Node wrapper timed out or returned no usable text, use
            # credential-store presence as local metadata-only evidence.  The
            # file is not opened, parsed, logged, or transmitted.
            for raw in (
                "~/.local/share/kilo/auth.json",
                "~/.config/kilo/auth.json",
                "~/.kilocode/auth.json",
                "~/AppData/Local/kilo/auth.json",
                "~/AppData/Roaming/kilo/auth.json",
            ):
                path = Path.home() / raw[2:]
                try:
                    if path.is_file() and path.stat().st_size > 0:
                        authenticated = True
                        break
                except OSError:
                    continue

        type(self)._auth_cache = (now, authenticated)
        return authenticated

    def process_args(self, prompt: str, context_pack: dict) -> list[str]:
        binary = str(
            self.discover_executable()
            or os.getenv(self.binary_env, self.default_binary)
        )
        # Kilo 1.x automation uses `kilo run --auto`; without --auto an
        # unattended task may block on an approval prompt.
        args: list[str] = ["run", "--format", "json", "--auto"]
        model = str(context_pack.get("model") or "").strip()
        if model:
            args.extend(["--model", model[:160]])
        native_agent = str(context_pack.get("native_agent") or "").strip()
        if native_agent:
            args.extend(["--agent", native_agent[:100]])
        session_id = str(
            context_pack.get("provider_session_id") or ""
        ).strip()
        if session_id:
            args.extend(["--session", session_id[:200]])
        args.append(prompt)
        return resolve_cli_argv(binary, *args)

    def stream_event(self, stream: str, line: str):
        # Kilo 1.x is built on the OpenCode event contract, but certification
        # still has to prove the observed schema for the exact installation.
        return OpenCodeAdapter.stream_event(self, stream, line)

    def capabilities(self) -> list[str]:
        return [
            "read_only",
            "code_inspection",
            "session_resume",
            "realtime_stream",
            "kilo_code",
        ]

    def provider_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            history_discovery=True,
            message_read=True,
            project_metadata=True,
            lineage_metadata=True,
            create_conversation=True,
            exact_resume=True,
            import_conversation=True,
            interactive_runtime=True,
            headless_runtime=True,
            streaming=True,
            cancellation=False,
            restart_recovery=True,
        )
