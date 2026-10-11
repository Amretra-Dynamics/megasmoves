"""Human and machine output formatting for the Megas CLI."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence, TextIO

SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)([^\s,;]+)"),
    re.compile(r"(?i)(x-megas-worker-token\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|worker_token|id_token)\s*[:=]\s*)([^\s,;\"']+)"),
    re.compile(r"(?i)(Bearer\s+)([A-Za-z0-9._\-]{12,})"),
)

SECRET_KEYS = {
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "worker_token",
    "password",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "private_key",
    "client_secret",
    "ciphertext",
}


def is_tty(stream: TextIO | None = None) -> bool:
    stream = stream or sys.stdout
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def use_color(*, no_color: bool = False, force: bool | None = None, stream: TextIO | None = None) -> bool:
    if no_color or os.getenv("NO_COLOR"):
        return False
    if force is True:
        return True
    if force is False:
        return False
    if os.getenv("MEGAS_FORCE_COLOR", "").lower() in {"1", "true", "yes"}:
        return True
    return is_tty(stream)


class Colors:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def blue(self, text: str) -> str:
        return self._wrap("34", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def status(self, value: str) -> str:
        lowered = (value or "").lower()
        if lowered in {"ok", "healthy", "running", "online", "connected", "resolved", "success", "ready", "active"}:
            return self.green(value)
        if lowered in {"degraded", "warning", "acknowledged", "pending", "muted", "suppressed"}:
            return self.yellow(value)
        if lowered in {"error", "failed", "critical", "offline", "lost", "revoked", "down", "unhealthy"}:
            return self.red(value)
        return value


def redact_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(r"\1***REDACTED***", redacted)
    return redacted


def redact_value(value: Any, *, key: str | None = None) -> Any:
    if key and key.lower() in SECRET_KEYS:
        if value in (None, "", [], {}):
            return value
        return "***REDACTED***"
    if isinstance(value, dict):
        return {k: redact_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def format_timestamp(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except (OverflowError, OSError, ValueError):
            return str(value)
    text = str(value)
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return text


def terminal_width(default: int = 100) -> int:
    try:
        return max(40, shutil_get_terminal_size(default))
    except Exception:
        return default


def shutil_get_terminal_size(default: int) -> int:
    import shutil

    return shutil.get_terminal_size((default, 24)).columns


def render_table(
    rows: Sequence[dict[str, Any]],
    columns: Sequence[tuple[str, str]],
    *,
    colors: Colors | None = None,
) -> str:
    """Render a compact table. columns is a list of (key, header)."""
    if not rows:
        return "(none)"
    colors = colors or Colors(False)
    headers = [header for _, header in columns]
    keys = [key for key, _ in columns]
    string_rows: list[list[str]] = []
    for row in rows:
        cells: list[str] = []
        for key in keys:
            raw = row.get(key, "")
            if key in {"status", "state", "severity", "health"} and isinstance(raw, str):
                cells.append(str(raw))
            else:
                cells.append("" if raw is None else str(raw))
        string_rows.append(cells)

    widths = [len(h) for h in headers]
    for cells in string_rows:
        for index, cell in enumerate(cells):
            widths[index] = max(widths[index], min(len(cell), 48))

    def fmt_row(cells: Sequence[str], *, colorize: bool = False) -> str:
        parts = []
        for index, cell in enumerate(cells):
            display = cell if len(cell) <= widths[index] else cell[: widths[index] - 1] + "…"
            padded = display.ljust(widths[index])
            if colorize and keys[index] in {"status", "state", "severity", "health"}:
                padded = colors.status(padded.rstrip()).ljust(widths[index] + (len(colors.status("x")) - 1 if colors.enabled else 0))
                # Simpler: color the raw status text without fighting width padding
                padded = colors.status(display).ljust(widths[index] + _ansi_overhead(colors.status(display), display))
            parts.append(padded)
        return "  ".join(parts)

    lines = [colors.bold(fmt_row(headers)), colors.dim("  ".join("-" * w for w in widths))]
    for cells in string_rows:
        colored: list[str] = []
        for index, cell in enumerate(cells):
            display = cell if len(cell) <= widths[index] else cell[: widths[index] - 1] + "…"
            if keys[index] in {"status", "state", "severity", "health"}:
                colored_text = colors.status(display)
                pad = widths[index] + max(0, len(colored_text) - len(display))
                colored.append(colored_text.ljust(pad))
            else:
                colored.append(display.ljust(widths[index]))
        lines.append("  ".join(colored))
    return "\n".join(lines)


def _ansi_overhead(colored: str, plain: str) -> int:
    return max(0, len(colored) - len(plain))


def emit_json(data: Any, *, stream: TextIO | None = None, jsonl: bool = False) -> None:
    stream = stream or sys.stdout
    safe = redact_value(data)
    if jsonl:
        stream.write(json.dumps(safe, ensure_ascii=False, default=str) + "\n")
    else:
        stream.write(json.dumps(safe, indent=2, ensure_ascii=False, default=str) + "\n")
    stream.flush()


def emit_yaml(data: Any, *, stream: TextIO | None = None) -> None:
    """Minimal YAML emitter without PyYAML dependency (maps/lists/scalars only)."""
    stream = stream or sys.stdout
    stream.write(_to_yaml(redact_value(data)))
    if not str(_to_yaml(redact_value(data))).endswith("\n"):
        stream.write("\n")
    stream.flush()


def _to_yaml(value: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return pad + "{}\n"
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.append(_to_yaml(item, indent + 1).rstrip("\n"))
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(item)}")
        return "\n".join(lines) + "\n"
    if isinstance(value, list):
        if not value:
            return pad + "[]\n"
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                nested = _to_yaml(item, indent + 1).rstrip("\n")
                lines.append(nested)
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
        return "\n".join(lines) + "\n"
    return pad + _yaml_scalar(value) + "\n"


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or any(ch in text for ch in ":#{}[]&*!|>'\"%@`\n") or text.strip() != text:
        return json.dumps(text, ensure_ascii=False)
    return text


class Output:
    """Unified stdout/stderr emitter respecting global output mode."""

    def __init__(
        self,
        *,
        format: str = "table",  # noqa: A002
        quiet: bool = False,
        verbose: bool = False,
        debug: bool = False,
        no_color: bool = False,
        non_interactive: bool = False,
    ) -> None:
        self.format = format
        self.quiet = quiet
        self.verbose = verbose or debug
        self.debug = debug
        self.no_color = no_color
        self.non_interactive = non_interactive or not is_tty()
        self.colors = Colors(use_color(no_color=no_color))

    @property
    def machine(self) -> bool:
        return self.format in {"json", "yaml", "jsonl"}

    def print(self, message: str = "", *, err: bool = False, force: bool = False) -> None:
        if self.quiet and not force and not err:
            return
        if self.machine and not err and not force:
            return
        stream = sys.stderr if err else sys.stdout
        stream.write(redact_text(message) + ("" if message.endswith("\n") else "\n"))
        stream.flush()

    def info(self, message: str) -> None:
        if self.verbose and not self.machine:
            self.print(self.colors.dim(message))

    def debug_log(self, message: str) -> None:
        if self.debug:
            self.print(self.colors.dim(f"[debug] {message}"), err=True, force=True)

    def warn(self, message: str) -> None:
        self.print(self.colors.yellow(f"warning: {message}"), err=True, force=True)

    def error(self, message: str) -> None:
        self.print(self.colors.red(message), err=True, force=True)

    def success(self, message: str) -> None:
        if not self.machine:
            self.print(self.colors.green(message))

    def result(self, data: Any, *, columns: Sequence[tuple[str, str]] | None = None, human_message: str | None = None) -> None:
        if self.format == "json":
            emit_json(data)
            return
        if self.format == "yaml":
            emit_yaml(data)
            return
        if self.format == "jsonl":
            if isinstance(data, list):
                for item in data:
                    emit_json(item, jsonl=True)
            else:
                emit_json(data, jsonl=True)
            return
        if human_message and not self.quiet:
            self.print(human_message)
        if columns and isinstance(data, list):
            self.print(render_table(data, columns, colors=self.colors))
        elif isinstance(data, dict) and not human_message:
            for key, value in data.items():
                if key.lower() in SECRET_KEYS:
                    value = "***REDACTED***"
                self.print(f"{key}: {value}")
        elif isinstance(data, list) and data and isinstance(data[0], dict) and columns is None:
            # best-effort keys from first row
            keys = list(data[0].keys())[:6]
            self.print(render_table(data, [(k, k) for k in keys], colors=self.colors))
        elif data is not None and human_message is None:
            self.print(str(data))

    def stream_event(self, event: dict[str, Any]) -> None:
        safe = redact_value(event)
        if self.format in {"json", "jsonl"}:
            emit_json(safe, jsonl=True)
            return
        if self.format == "yaml":
            emit_yaml(safe)
            return
        ts = format_timestamp(safe.get("timestamp") or safe.get("created_at") or safe.get("time"))
        etype = safe.get("type") or safe.get("event_type") or safe.get("kind") or "event"
        message = safe.get("message") or safe.get("summary") or safe.get("title") or ""
        self.print(f"{self.colors.dim(ts)}  {self.colors.cyan(str(etype))}  {message}")


def iter_as_rows(data: Any, key_candidates: Iterable[str] = ()) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item if isinstance(item, dict) else {"value": item} for item in data]
    if isinstance(data, dict):
        for key in key_candidates:
            if isinstance(data.get(key), list):
                return iter_as_rows(data[key])
        return [data]
    return [{"value": data}]


__all__ = [
    "Colors",
    "Output",
    "emit_json",
    "emit_yaml",
    "format_timestamp",
    "is_tty",
    "iter_as_rows",
    "redact_text",
    "redact_value",
    "render_table",
    "use_color",
]
