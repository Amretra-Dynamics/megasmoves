"""Read-only, OAuth-funded Megas Agent Leader planning for the local OSS client.

Model output is a proposal, never tool authority or proof of execution.
"""
from __future__ import annotations
import json
import re
from typing import Any

from .siwc_oss import ChatGPTConnectionError, OSSChatGPTClient

TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_STEPS = 8


def connected_agents() -> list[dict[str, str]]:
    """Only agents reported as both detected and connected to Megas."""
    from .agent_management import status
    rows = status()
    if not isinstance(rows, list):
        raise ChatGPTConnectionError("Connected-agent inventory is unavailable")
    agents: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        target = row.get("client")
        if (row.get("detected") is not True
                or row.get("megas_configured") is not True
                or not isinstance(target, str)
                or not TARGET_RE.fullmatch(target)):
            continue
        catalog = row.get("catalog")
        display = (catalog.get("display_name", target)
                   if isinstance(catalog, dict) else target)
        agents.append({"kind": "agent", "target": target,
                       "name": str(display)[:80]})
    return agents[:32]


def preview(*, account_email: str, model: str, goal: str,
            client: OSSChatGPTClient | None = None,
            inventory: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Verify targets and return bounded plan without creating or assigning tasks."""
    if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 4000:
        raise ChatGPTConnectionError("Enter a coordination goal under 4,000 characters")
    if not isinstance(account_email, str) or not account_email.strip():
        raise ChatGPTConnectionError("Select the authorized ChatGPT account")
    candidates = connected_agents() if inventory is None else inventory
    if not isinstance(candidates, list) or not candidates:
        raise ChatGPTConnectionError("No connected Megas agents; connect one before planning")
    eligible: dict[str, str] = {}
    safe = []
    for row in candidates[:32]:
        if not isinstance(row, dict) or row.get("kind") != "agent":
            raise ChatGPTConnectionError("Invalid agent inventory")
        target = row.get("target")
        if not isinstance(target, str) or not TARGET_RE.fullmatch(target):
            raise ChatGPTConnectionError("Invalid connected agent target")
        if target in eligible:
            raise ChatGPTConnectionError("Duplicate agent target")
        label = str(row.get("name") or target)[:80]
        eligible[target] = label
        safe.append({"kind": "agent", "target": target, "name": label})

    instructions = (
        'You are the MegasMoves Agent Leader. Return ONLY valid JSON with schema '
        '{"summary":string,"steps":[{"kind":"agent","target":string,'
        '"task":string}]}. Propose 1 to 8 small verifiable tasks. '
        'Use only the exact target IDs supplied. Do not use tools, execute work, '
        'reveal secrets, or claim completion. This open-source preview has '
        'no trusted WebMCP discovery, so do not invent WebMCP tools. '
        'Every proposed step requires a human review.'
    )
    payload = json.dumps({"goal": goal.strip(), "agents": safe},
                         ensure_ascii=False, separators=(",", ":"))
    source = client or OSSChatGPTClient()
    response = source.infer(email=account_email, model=model,
                            prompt=instructions + "\n\n" + payload)
    raw = str(response.get("text") or "").strip()
    fence = chr(96) * 3
    if raw.startswith(fence):
        raw = re.sub(r"^" + re.escape(fence) + r"(?:json)?\s*|\s*"
                     + re.escape(fence) + r"$", "", raw).strip()
    if len(raw) > 24000:
        raise ChatGPTConnectionError("Agent Leader response exceeded size limit")
    try:
        candidate = json.loads(raw)
    except (TypeError, ValueError):
        raise ChatGPTConnectionError("Agent Leader returned invalid JSON") from None
    if not isinstance(candidate, dict) or not isinstance(candidate.get("steps"), list):
        raise ChatGPTConnectionError("Agent Leader plan schema is invalid")
    steps = candidate["steps"]
    if not 1 <= len(steps) <= MAX_STEPS:
        raise ChatGPTConnectionError("Agent Leader must propose 1 to 8 tasks")
    reviewed = []
    for step in steps:
        if not isinstance(step, dict):
            raise ChatGPTConnectionError("Agent Leader proposed an invalid task")
        kind, target, task = (step.get("kind"), step.get("target"),
                              step.get("task"))
        if (kind != "agent" or not isinstance(target, str)
                or target not in eligible or not isinstance(task, str)
                or not 1 <= len(task.strip()) <= 1000):
            raise ChatGPTConnectionError("Agent Leader invented or misused a target")
        reviewed.append({"kind": "agent", "target": target,
                         "task": task.strip(), "state": "awaiting_review"})
    return {"provider": "chatgpt_plan", "model": model,
            "summary": str(candidate.get("summary") or "")[:600],
            "steps": reviewed, "review_required": True,
            "dispatched": False, "webmcp_available": False}
