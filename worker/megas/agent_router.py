"""Multi-agent selection and limit-aware failover for the Local Worker.

When the preferred agent fails with quota/rate/usage limits, Megas tries the next
available certified agent on the same machine. Experience-based ranking can replace
the static order later without changing call sites.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# Static product default. Later: rank by past success / cost / latency memory.
# Gemini CLI is abandoned product-side — Antigravity (agy) is the Google path.
DEFAULT_AGENT_ORDER: tuple[str, ...] = (
    "codex_cli",
    "antigravity",
    "claude_code",
    "aider",
    "kilo_code",
    "kiro_code",
    "grok_build",
    "opencode",
    "cline",
    "devin_cli",
)

# Collapse legacy / alias agent ids so failover never double-runs the same binary
# (e.g. gemini_cli + antigravity both pointed at agy.exe).
AGENT_CANONICAL_IDS: dict[str, str] = {
    "gemini": "antigravity",
    "gemini_cli": "antigravity",
    "gemini-cli": "antigravity",
    "google_gemini": "antigravity",
    "google-gemini": "antigravity",
    "agy": "antigravity",
    "gravity": "antigravity",
    "codex": "codex_cli",
    "claude": "claude_code",
    "grok": "grok_build",
    "kilo": "kilo_code",
    "kilocode": "kilo_code",
    "kilo-code": "kilo_code",
}

# Preferred + at most one limit failover. Longer chains (Codex→AGY→Grok) felt 100x slow.
MAX_AGENT_CHAIN_LEN = 2

# Keep failover within execution modes advertised by runtime_manifest.py.
# Today only Codex has a certified controlled-write contract; every other
# executable adapter in the ranking is inspection-only.
WRITE_CERTIFIED_AGENTS = frozenset({"codex_cli"})

LIMIT_SIGNAL = re.compile(
    r"("
    r"quota|"
    r"rate[_\s-]?limit|"
    r"usage[_\s-]?limit|"
    r"ran\s+out\s+of|"
    r"out\s+of\s+limit|"
    r"exceeded.{0,40}(limit|quota)|"
    r"insufficient[_\s-]?(quota|credit)|"
    r"billing|"
    r"too\s+many\s+requests|"
    r"\b429\b|"
    r"resource[_\s-]?exhausted|"
    r"limit\s+over|"
    r"spend\s+limit|"
    r"monthly\s+limit|"
    r"tokens?\s+limit"
    r")",
    re.IGNORECASE,
)


def looks_like_limit_failure(*parts: Any) -> bool:
    """True when stderr/stdout/error text indicates provider quota/rate limits."""
    blob = "\n".join(str(part or "") for part in parts)
    return bool(LIMIT_SIGNAL.search(blob))


@dataclass(frozen=True)
class AgentLimitInfo:
    """User-safe, provider-aware limit state sent to cloud/mobile clients."""

    agent_type: str
    code: str
    kind: str
    message: str
    detected_at: str
    retry_after_seconds: int | None = None
    expected_available_at: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


_AGENT_LABELS = {
    "codex_cli": "Codex",
    "claude_code": "Claude Code",
    "gemini_cli": "Antigravity",  # abandoned id — display as product agent
    "antigravity": "Antigravity",
    "aider": "Aider",
    "kilo_code": "Kilo Code",
    "kiro_code": "Kiro Code",
    "grok_build": "Grok Build",
    "opencode": "OpenCode",
    "cline": "Cline",
    "devin_cli": "Devin CLI",
}


def canonicalize_agent_id(agent_type: str) -> str:
    """Map legacy aliases (gemini_cli, agy, …) onto the product agent id."""
    raw = (agent_type or "").strip()
    if not raw:
        return ""
    key = raw.lower().replace(" ", "_")
    if key in AGENT_CANONICAL_IDS:
        return AGENT_CANONICAL_IDS[key]
    key_us = key.replace("-", "_")
    return AGENT_CANONICAL_IDS.get(key_us, key_us)


def agent_display_name(agent_type: str) -> str:
    canonical = canonicalize_agent_id(agent_type) or agent_type
    return _AGENT_LABELS.get(
        canonical,
        canonical.replace("_", " ").title() or "Coding agent",
    )


def _retry_after_seconds(blob: str) -> int | None:
    # Structured Retry-After / retryDelay values emitted by common CLIs.
    direct = re.search(
        r"(?:retry[_\s-]?after|retrydelay)[\s\"':=]+(?P<seconds>\d{1,7})(?:\.\d+)?\s*(?:s|sec|seconds?)?",
        blob,
        re.IGNORECASE,
    )
    if direct:
        return min(int(direct.group("seconds")), 31 * 24 * 60 * 60)

    # Human output such as "try again in 1h 20m" or "resets in 45 minutes".
    human = re.search(
        r"(?:try\s+again|available|reset(?:s)?)[^\n]{0,40}?\bin\s+"
        r"(?:(?P<hours>\d{1,4})\s*(?:h|hours?))?\s*"
        r"(?:(?P<minutes>\d{1,5})\s*(?:m|minutes?))?\s*"
        r"(?:(?P<seconds>\d{1,7})\s*(?:s|seconds?))?",
        blob,
        re.IGNORECASE,
    )
    if human and any(human.group(name) for name in ("hours", "minutes", "seconds")):
        total = (
            int(human.group("hours") or 0) * 3600
            + int(human.group("minutes") or 0) * 60
            + int(human.group("seconds") or 0)
        )
        return min(max(total, 1), 31 * 24 * 60 * 60)
    return None


def parse_limit_failure(agent_type: str, *parts: Any) -> AgentLimitInfo | None:
    """Normalize major-agent quota errors without forwarding raw provider output.

    Provider output can contain prompts, paths, or account details, so only a
    classified code/message and an explicitly parsed retry window cross the wire.
    """

    blob = "\n".join(str(part or "") for part in parts)
    if not LIMIT_SIGNAL.search(blob):
        return None
    lowered = blob.lower()
    if "sessionbudgetexceeded" in lowered or "session budget" in lowered:
        code, kind = "SessionBudgetExceeded", "session_budget"
    elif "usagelimitexceeded" in lowered or "usage limit" in lowered:
        code, kind = "UsageLimitExceeded", "usage_limit"
    elif "resource_exhausted" in lowered or "resource exhausted" in lowered:
        code, kind = "RESOURCE_EXHAUSTED", "usage_limit"
    elif "insufficient_quota" in lowered or "insufficient quota" in lowered:
        code, kind = "insufficient_quota", "usage_limit"
    elif "credit" in lowered or "billing" in lowered or "spend limit" in lowered:
        code, kind = "credits_exhausted", "credits"
    elif re.search(r"\b429\b|too\s+many\s+requests|rate[_\s-]?limit", blob, re.IGNORECASE):
        code, kind = "rate_limit_exceeded", "rate_limit"
    else:
        code, kind = "provider_limit_exceeded", "usage_limit"

    label = agent_display_name(agent_type)
    messages = {
        "session_budget": f"{label} reached this session's budget.",
        "rate_limit": f"{label} is temporarily rate limited.",
        "credits": f"{label} has no available credits or billing capacity.",
        "usage_limit": f"{label} usage limit reached.",
    }
    now = datetime.now(timezone.utc)
    retry_after = _retry_after_seconds(blob)
    expected = (now + timedelta(seconds=retry_after)).isoformat() if retry_after else None
    return AgentLimitInfo(
        agent_type=agent_type,
        code=code,
        kind=kind,
        message=messages[kind],
        detected_at=now.isoformat(),
        retry_after_seconds=retry_after,
        expected_available_at=expected,
    )


def available_coding_agents(
    adapters: Iterable[Any],
    *,
    capabilities: Iterable[str] | None = None,
    write_mode: bool = False,
) -> list[str]:
    """Return healthy executable agents allowed by the certified manifest.

    ``capabilities`` is optional for legacy callers. When supplied, it is the
    authoritative lease capability set. Write-mode fallback is additionally
    restricted to adapters whose runtime contract advertises controlled writes.
    """
    capability_set = (
        {str(item).strip().lower() for item in capabilities if str(item).strip()}
        if capabilities is not None
        else None
    )
    names: list[str] = []
    for adapter in adapters:
        name = str(getattr(adapter, "name", "") or "")
        if name not in DEFAULT_AGENT_ORDER:
            continue
        if write_mode and name not in WRITE_CERTIFIED_AGENTS:
            continue
        if capability_set is not None and name not in capability_set:
            continue
        try:
            status = adapter.health_status()
        except Exception:
            continue
        if status == "available":
            names.append(name)
    return names


def build_agent_chain(
    preferred: str,
    *,
    available: Iterable[str],
    fallbacks: Iterable[str] | None = None,
    max_len: int = MAX_AGENT_CHAIN_LEN,
) -> list[str]:
    """Preferred agent first, then configured fallbacks, then default order.

    Canonicalizes aliases (gemini_cli→antigravity) and caps length so one
    timeout cannot cascade into multi-minute multi-agent delays.
    """
    preferred = canonicalize_agent_id(preferred or "")
    for suffix in ("_w-", "_w_", "_worker_"):
        if suffix in preferred:
            preferred = preferred.split(suffix, 1)[0]
            preferred = canonicalize_agent_id(preferred)
            break

    available_set = {
        canonicalize_agent_id(str(a))
        for a in available
        if str(a).strip()
    }
    available_set.discard("")
    chain: list[str] = []
    limit = max(1, int(max_len or MAX_AGENT_CHAIN_LEN))

    def _add(agent_id: str, *, require_available: bool) -> None:
        agent_id = canonicalize_agent_id(agent_id)
        if not agent_id or agent_id in chain:
            return
        if require_available and agent_id not in available_set:
            return
        chain.append(agent_id)

    # Always attempt the preferred agent first (even if health probe is flaky).
    if preferred:
        _add(preferred, require_available=False)
    for agent_id in fallbacks or []:
        if len(chain) >= limit:
            break
        _add(str(agent_id), require_available=True)
    for agent_id in DEFAULT_AGENT_ORDER:
        if len(chain) >= limit:
            break
        _add(agent_id, require_available=True)

    if not chain and preferred:
        chain = [preferred]
    return chain[:limit]


def with_agent_type(command: dict[str, Any], agent_type: str) -> dict[str, Any]:
    """Return a shallow-copied command bound to a specific agent type."""
    agent_type = canonicalize_agent_id(agent_type) or agent_type
    cloned = dict(command)
    context = dict(cloned.get("context_pack") or {}) if isinstance(cloned.get("context_pack"), dict) else {}
    context["agent_type"] = agent_type
    context["resolved_agent_type"] = agent_type
    cloned["context_pack"] = context
    cloned["agent_type"] = agent_type
    # Keep command_type aligned for adapters that key off it.
    cmd_type = str(cloned.get("command_type") or "")
    ranked = set(DEFAULT_AGENT_ORDER) | set(AGENT_CANONICAL_IDS.keys()) | set(AGENT_CANONICAL_IDS.values())
    if cmd_type in {"", "custom_shell"} or cmd_type in ranked or canonicalize_agent_id(cmd_type) in ranked:
        cloned["command_type"] = agent_type
    return cloned
