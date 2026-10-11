"""Terminal stream parsing for coding-agent CLIs.

Patterns drawn from open-source agent tooling (Aider stream/reply cleanup,
Claude Code / AtomCode `--print` style single-shot output, jetski/agy task
envelope lines). Goals:

1. Line-buffer byte chunks so completion markers never split mid-match.
2. Detect completion / fatal errors across multiple CLI dialects.
3. Extract assistant answer body by stripping envelopes (Task completed,
   Stdout:/Stderr: headers, empty scaffolding).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


# Jetski / Antigravity: "Task uuid/task-N has completed with exit code 0."
TASK_COMPLETED_RE = re.compile(
    r"has completed with exit code\s+(-?\d+)",
    re.IGNORECASE,
)
# Explicit process-style exits some CLIs emit
PROCESS_EXIT_RE = re.compile(
    r"(?:process\s+exited\s+with\s+code|exit(?:ed)?\s*(?:code)?\s*[:=]?\s*)(-?\d+)",
    re.IGNORECASE,
)
# Hard-fail banners (agy model wait, auth) — keep narrow to avoid false reaps.
FATAL_ERROR_RE = re.compile(
    r"(?:Error:\s*timeout waiting for response|"
    r"Authentication required|"
    r"API key (?:missing|invalid)|"
    r"rate limit exceeded|"
    r"quota exceeded)",
    re.IGNORECASE,
)
# Envelope labels from wrapped task runners
STDOUT_HEADER_RE = re.compile(r"^\s*Stdout:\s*$", re.IGNORECASE | re.MULTILINE)
STDERR_HEADER_RE = re.compile(r"^\s*Stderr:\s*$", re.IGNORECASE | re.MULTILINE)
TASK_LINE_RE = re.compile(r"^\s*Task\s+\S+\s+has completed with exit code\s+-?\d+\.?\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass
class StreamLineBuffer:
    """Accumulate incomplete lines from arbitrary read(4096) chunks."""

    _buf: str = ""
    lines: list[str] = field(default_factory=list)

    def feed(self, chunk: str) -> list[str]:
        if not chunk:
            return []
        self._buf += chunk
        out: list[str] = []
        while True:
            idx = self._buf.find("\n")
            if idx < 0:
                break
            line = self._buf[: idx + 1]
            self._buf = self._buf[idx + 1 :]
            out.append(line)
            self.lines.append(line)
        return out

    def flush(self) -> list[str]:
        if not self._buf:
            return []
        line = self._buf
        self._buf = ""
        if not line.endswith("\n"):
            line = line + "\n"
        self.lines.append(line)
        return [line]

    @property
    def full_text(self) -> str:
        return "".join(self.lines) + self._buf


@dataclass
class CompletionHit:
    exit_code: int | None
    kind: str  # completed | fatal | process_exit
    line: str


def detect_completion_in_text(text: str) -> CompletionHit | None:
    if not text:
        return None
    m = TASK_COMPLETED_RE.search(text)
    if m:
        try:
            code = int(m.group(1))
        except ValueError:
            code = 0
        return CompletionHit(exit_code=code, kind="completed", line=m.group(0))
    m = PROCESS_EXIT_RE.search(text)
    if m:
        try:
            code = int(m.group(1))
        except ValueError:
            code = 1
        return CompletionHit(exit_code=code, kind="process_exit", line=m.group(0))
    m = FATAL_ERROR_RE.search(text)
    if m:
        # timeout waiting for response = failure (exit 1)
        return CompletionHit(exit_code=1, kind="fatal", line=m.group(0)[:200])
    return None


def extract_assistant_answer(raw: str) -> str:
    """Strip CLI envelopes; return human-facing answer for chat + live card.

    Inspired by Aider's post-stream content cleanup (reasoning tags / fences)
    and Claude Code ``-p`` / agy ``--print`` which ideally emit the answer alone.
    When jetski wraps output as Task… / Stdout: / body / Stderr:, keep body only.
    """
    if not raw or not raw.strip():
        return ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")

    # Prefer content under Stdout: until Stderr: or EOF
    stdout_m = STDOUT_HEADER_RE.search(text)
    if stdout_m:
        body = text[stdout_m.end() :]
        stderr_m = STDERR_HEADER_RE.search(body)
        if stderr_m:
            body = body[: stderr_m.start()]
        text = body

    # Codex CLI chat: final answer is after a bare "codex" speaker line.
    codex_parts = re.split(r"(?m)^codex\s*$", text)
    if len(codex_parts) > 1:
        last = codex_parts[-1]
        last = re.split(r"(?m)^tokens used\s*$", last, maxsplit=1)[0]
        last = re.split(r"(?m)^\[finished\]", last, maxsplit=1)[0]
        last = last.strip()
        if last:
            return re.sub(r"\n{3,}", "\n\n", last)

    # Drop task completion lines
    lines = []
    for line in text.split("\n"):
        if TASK_LINE_RE.match(line.strip() + "\n") or TASK_COMPLETED_RE.search(line):
            continue
        if STDERR_HEADER_RE.match(line) or STDOUT_HEADER_RE.match(line):
            continue
        # Skip pure PowerShell directory listings noise that is not the answer
        if re.match(r"^\s*Directory:\s+", line):
            continue
        if re.match(r"^\s*Mode\s+LastWriteTime\s+", line):
            continue
        if re.match(r"^\s*-{3,}\s+-{3,}", line):
            continue
        if re.match(r"^\s*\[codex_cli\]\s*Chat:", line, re.I):
            continue
        if re.match(r"^\s*OpenAI Codex v", line, re.I):
            continue
        if re.match(r"^\s*Reading additional input", line, re.I):
            continue
        lines.append(line)

    cleaned = "\n".join(lines).strip()
    # Collapse excessive blank lines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def merge_stream_texts(chunks: Iterable[str]) -> str:
    return "".join(str(c or "") for c in chunks)


def prefer_answer_for_chat(chunks: Iterable[str]) -> str:
    raw = merge_stream_texts(chunks)
    answer = extract_assistant_answer(raw)
    return answer if answer.strip() else raw.strip()
