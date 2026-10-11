from __future__ import annotations

import os

from .base import resolve_cli_argv
from .headless_agent import ReadOnlyHeadlessAgentAdapter


class AiderAdapter(ReadOnlyHeadlessAgentAdapter):
    name = "aider"
    adapter_id = "aider"
    supported_range = ">=0.1.0"
    binary_env = "MEGAS_AIDER_BINARY"
    default_binary = "aider"
    timeout_env = "MEGAS_AIDER_TIMEOUT_SEC"

    def is_authenticated(self) -> bool:
        if any(os.getenv(k) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY")):
            return True
        home = os.path.expanduser("~")
        config_paths = [
            os.path.join(home, ".aider.conf.yml"),
            os.path.join(home, ".aider.model.settings.yml"),
        ]
        return any(os.path.isfile(p) for p in config_paths)

    def process_args(self, prompt: str, context_pack: dict) -> list[str]:
        binary = os.getenv(self.binary_env, self.default_binary)
        args = [
            "--message",
            prompt,
            "--dry-run",
            "--no-auto-commits",
            "--no-dirty-commits",
            "--yes",
        ]
        model = str(context_pack.get("model") or "").strip()
        if model:
            args.extend(["--model", model[:100]])
        return resolve_cli_argv(binary, *args)
