"""Kiro Code CLI adapter (AWS spec-driven AI agent)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
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


class KiroCodeAdapter(ReadOnlyHeadlessAgentAdapter):
    """Certified read/inspect adapter for Kiro Code CLI (`kiro` / `kiro-cli`)."""

    name = "kiro_code"
    adapter_id = "kiro_code"
    supported_range = ">=0.1.0"
    binary_env = "MEGAS_KIRO_BINARY"
    # Kiro's official executable is ``kiro-cli``.  Older local installs used
    # ``kiro``; retain it as a fallback but never make it the primary probe.
    default_binary = "kiro-cli"
    timeout_env = "MEGAS_KIRO_TIMEOUT_SEC"
    version_args = ["--version"]
    version_regex = r"(\d+\.\d+\.\d+)"
    _AUTH_CACHE_TTL_SEC = 60.0
    _auth_cache: tuple[float, str] | None = None

    def discover_executable(self) -> Path | None:
        for candidate in (
            os.getenv(self.binary_env, "").strip(),
            "kiro-cli",
            "kiro",
            "kiro_code",
        ):
            if not candidate:
                continue
            which = shutil.which(candidate)
            if which:
                return Path(which)
            path = Path(candidate)
            if path.is_file():
                return path
            # On Windows, try appending .exe when the bare name doesn't resolve.
            if os.name == "nt" and not path.suffix:
                exe_path = path.with_suffix(".exe")
                if exe_path.is_file():
                    return exe_path
        return None

    @classmethod
    def _whoami_identity(cls, executable: Path | None) -> str:
        """Read Kiro's local auth state without exposing credentials.

        ``kiro-cli whoami`` is Kiro's documented authoritative login check.
        Its stable account fields become input to Megas' one-way local
        certificate fingerprint; neither the command output nor the identity
        leaves the machine.
        """

        now = time.monotonic()
        cached = cls._auth_cache
        if cached is not None and now - cached[0] < cls._AUTH_CACHE_TTL_SEC:
            return cached[1]
        if executable is None:
            cls._auth_cache = (now, "")
            return ""

        identity = ""
        for command in (("whoami", "--format", "json"), ("whoami",)):
            try:
                result = subprocess.run(
                    resolve_cli_argv(str(executable), *command),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=4.0,
                    check=False,
                    env=prefer_console_free_tool_path(dict(os.environ)),
                    **subprocess_hide_window_kwargs(),
                )
            except (OSError, subprocess.SubprocessError):
                continue
            output = (result.stdout or "").strip()
            if result.returncode != 0 or not output:
                continue
            try:
                payload = json.loads(output)
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                for key in ("email", "userId", "user_id", "username", "id"):
                    value = str(payload.get(key) or "").strip()
                    if value:
                        identity = f"{key}:{value[:500]}"
                        break
            if not identity:
                email = re.search(
                    r"^\s*email\s*:\s*(\S+)", output, re.IGNORECASE | re.MULTILINE
                )
                if email:
                    identity = f"email:{email.group(1)[:500]}"
            if identity:
                break
        cls._auth_cache = (now, identity)
        return identity

    def is_authenticated(self) -> bool:
        return bool(self._whoami_identity(self.discover_executable()))

    def certification_identity(self) -> str:
        return self._whoami_identity(self.discover_executable())

    def process_args(self, prompt: str, context_pack: dict) -> list[str]:
        binary = str(self.discover_executable() or os.getenv(self.binary_env, self.default_binary))
        # Kiro's current documented headless contract is ``kiro-cli chat
        # --no-interactive``.  The former invented ``--non-interactive`` root
        # flag made a successfully authenticated Windows installation look
        # unusable during Megas certification.
        args: list[str] = ["chat", "--no-interactive"]
        model = str(context_pack.get("model") or "").strip()
        if model:
            args.extend(["--model", model[:160]])
        native_agent = str(context_pack.get("native_agent") or "").strip()
        if native_agent:
            args.extend(["--agent", native_agent[:100]])
        args.append(prompt)
        return resolve_cli_argv(binary, *args)

    def stream_event(self, stream: str, line: str):
        return OpenCodeAdapter.stream_event(self, stream, line)

    def capabilities(self) -> list[str]:
        return ["read_only", "code_inspection", "kiro_code"]
