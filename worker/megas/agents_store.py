"""Persistent coding-agent knowledge store (local DB) + live web refresh.

Strategy (fast + correct):
1. Always resolve against the local store first (instant).
2. If the store is stale or a name is unknown, refresh from live web sources
   and *merge* (never wipe) into the local store so knowledge keeps growing.
3. Successful installs and on-machine detections are written back so the next
   run is faster and richer.

Live sources (best-effort, any success is stored):
- Megas catalog: {backend}/api/v1/cli/agents/catalog and /agents/catalog.json
- npm registry search (coding agent packages)
- PyPI search (e.g. aider-chat)

Seed: small bootstrap set so first offline run still works (codex, claude,
gemini, aider, agy, kimi, glm, opencode). Live data overwrites/extends.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STORE_SCHEMA = "megas.agents-store.v1"
DEFAULT_TTL_SEC = 6 * 3600  # prefer local for 6h unless force/miss
USER_AGENT = "MegasMoves-AgentStore/1.0 (+https://megasmoves.io)"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_store_path() -> Path:
    root = os.getenv("MEGAS_HOME")
    if root:
        return Path(root).expanduser() / "agents_store.json"
    return Path.home() / ".megasmoves" / "agents_store.json"


def _backend_bases() -> list[str]:
    bases: list[str] = []
    for key in ("MEGAS_AGENTS_CATALOG_URL", "MEGAS_MOVES_API_URL", "MEGAS_BACKEND_URL"):
        raw = (os.getenv(key) or "").strip().rstrip("/")
        if raw:
            bases.append(raw)
    bases.append("https://megasmoves.io")
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for b in bases:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def bootstrap_seed_agents() -> dict[str, dict[str, Any]]:
    """Minimal offline seed — live refresh extends this forever."""
    return {
        "codex_cli": {
            "agent_id": "codex_cli",
            "display_name": "OpenAI Codex CLI",
            "aliases": ["codex", "codex_cli", "openai-codex", "openai", "gpt"],
            "binary_names": ["codex"],
            "binary_env": "MEGAS_CODEX_BINARY",
            "certified": True,
            "auth_hint": "Sign in with ChatGPT or set OPENAI_API_KEY.",
            "package_hints": ["@openai/codex"],
            "installers": [
                {
                    "id": "npm",
                    "kind": "npm",
                    "argv": ["npm", "install", "-g", "@openai/codex"],
                    "platforms": ["*"],
                    "requires": ["npm"],
                    "summary": "npm install -g @openai/codex",
                }
            ],
            "source": "seed",
        },
        "claude_code": {
            "agent_id": "claude_code",
            "display_name": "Anthropic Claude Code",
            "aliases": ["claude", "claude_code", "claude-code", "anthropic"],
            "binary_names": ["claude"],
            "binary_env": "MEGAS_CLAUDE_BINARY",
            "certified": True,
            "auth_hint": "Claude Pro/Max or ANTHROPIC_API_KEY.",
            "package_hints": ["@anthropic-ai/claude-code"],
            "installers": [
                {
                    "id": "npm",
                    "kind": "npm",
                    "argv": ["npm", "install", "-g", "@anthropic-ai/claude-code"],
                    "platforms": ["*"],
                    "requires": ["npm"],
                    "summary": "npm install -g @anthropic-ai/claude-code",
                }
            ],
            "source": "seed",
        },
        # Abandoned product agent. Google coding path is Antigravity (agy).
        # Kept as a seed row so old configs/disable flags still resolve, but
        # not certified and not advertised for install/failover.
        "gemini_cli": {
            "agent_id": "gemini_cli",
            "display_name": "Google Gemini CLI (abandoned — use Antigravity)",
            "aliases": ["gemini_cli"],
            "binary_names": ["gemini"],
            "binary_env": "MEGAS_GEMINI_BINARY",
            "certified": False,
            "auth_hint": "Abandoned. Use: megas install antigravity (agy).",
            "package_hints": [],
            "installers": [],
            "source": "seed",
            "abandoned": True,
            "successor": "antigravity",
        },
        "aider": {
            "agent_id": "aider",
            "display_name": "Aider",
            "aliases": ["aider", "aider-chat"],
            "binary_names": ["aider"],
            "binary_env": "MEGAS_AIDER_BINARY",
            "certified": True,
            "auth_hint": "Set provider API keys for the models you use.",
            "package_hints": ["aider-chat"],
            "installers": [
                {
                    "id": "pipx",
                    "kind": "pipx",
                    "argv": ["pipx", "install", "aider-chat"],
                    "platforms": ["*"],
                    "requires": ["pipx"],
                    "summary": "pipx install aider-chat",
                },
                {
                    "id": "pip",
                    "kind": "pip",
                    "argv": [os.path.basename(__import__("sys").executable) or "python", "-m", "pip", "install", "--user", "aider-chat"],
                    "platforms": ["*"],
                    "requires": [],
                    "summary": "pip install --user aider-chat",
                },
            ],
            "source": "seed",
        },
        "antigravity": {
            "agent_id": "antigravity",
            "display_name": "Antigravity CLI",
            "aliases": ["agy", "antigravity", "gravity", "gemini", "gemini_cli", "gemini-cli", "google-gemini"],
            "binary_names": ["agy"],
            "binary_env": "MEGAS_ANTIGRAVITY_BINARY",
            "certified": True,
            "auth_hint": "Run agy once for Google sign-in.",
            "package_hints": [],
            "installers": [
                {
                    "id": "native_windows",
                    "kind": "script",
                    "platforms": ["windows"],
                    "summary": "Official Antigravity PowerShell installer",
                    "script_windows": "irm https://antigravity.google/cli/install.ps1 | iex",
                },
                {
                    "id": "native_unix",
                    "kind": "script",
                    "platforms": ["darwin", "linux"],
                    "summary": "Official Antigravity install script",
                    "script_unix": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
                },
            ],
            "source": "seed",
        },
        "kiro_code": {
            "agent_id": "kiro_code",
            "display_name": "Kiro Code (AWS Spec-Driven IDE/CLI)",
            "aliases": ["kiro", "kiro_code", "kiro-code", "kiro-cli"],
            "binary_names": ["kiro-cli", "kiro"],
            "binary_env": "MEGAS_KIRO_BINARY",
            "certified": True,
            "auth_hint": "Authenticate with AWS / Kiro credentials when prompted.",
            "package_hints": ["kiro-cli"],
            "installers": [
                {
                    "id": "manual",
                    "kind": "manual",
                    "summary": "Official Kiro IDE / CLI (AWS Agentic Engineering)",
                    "manual_url": "https://kiro.dev",
                }
            ],
            "source": "seed",
        },
        "kimi_code": {
            "agent_id": "kimi_code",
            "display_name": "Kimi Code",
            "aliases": ["kimi", "kimi_code", "kimi-code", "moonshot"],
            "binary_names": ["kimi"],
            "binary_env": "MEGAS_KIMI_BINARY",
            "certified": False,
            "auth_hint": "Authenticate with Moonshot / Kimi when prompted.",
            "package_hints": ["@moonshotcom/kimi-cli", "kimi-cli"],
            "installers": [
                {
                    "id": "npm",
                    "kind": "npm",
                    "argv": ["npm", "install", "-g", "@moonshotcom/kimi-cli"],
                    "platforms": ["*"],
                    "requires": ["npm"],
                    "summary": "npm install -g @moonshotcom/kimi-cli (if published)",
                },
                {
                    "id": "manual",
                    "kind": "manual",
                    "summary": "Install Kimi Code from Moonshot docs when package name differs",
                    "manual_url": "https://www.moonshot.cn/",
                },
            ],
            "source": "seed",
        },
        "glm_code": {
            "agent_id": "glm_code",
            "display_name": "GLM Code",
            "aliases": ["glm", "glm_code", "glm-code", "chatglm", "zhipu"],
            "binary_names": ["glm"],
            "binary_env": "MEGAS_GLM_BINARY",
            "certified": False,
            "auth_hint": "Authenticate with Zhipu/GLM when prompted.",
            "package_hints": ["@z_ai/coding-helper", "glm-cli"],
            "installers": [
                {
                    "id": "npm",
                    "kind": "npm",
                    "argv": ["npm", "install", "-g", "@z_ai/coding-helper"],
                    "platforms": ["*"],
                    "requires": ["npm"],
                    "summary": "npm global coding helper (live catalog may replace package)",
                },
                {
                    "id": "manual",
                    "kind": "manual",
                    "summary": "Install GLM CLI from Zhipu docs",
                    "manual_url": "https://open.bigmodel.cn/",
                },
            ],
            "source": "seed",
        },
        "opencode": {
            "agent_id": "opencode",
            "display_name": "OpenCode",
            "aliases": ["opencode", "open-code", "open_code"],
            "binary_names": ["opencode"],
            "binary_env": "MEGAS_OPENCODE_BINARY",
            "certified": False,
            "auth_hint": "Follow OpenCode auth after install.",
            "package_hints": ["opencode-ai", "@opencode-ai/cli"],
            "installers": [
                {
                    "id": "npm",
                    "kind": "npm",
                    "argv": ["npm", "install", "-g", "opencode-ai"],
                    "platforms": ["*"],
                    "requires": ["npm"],
                    "summary": "npm install -g opencode-ai (live catalog may refine)",
                },
                {
                    "id": "manual",
                    "kind": "manual",
                    "summary": "Install OpenCode from project docs if package differs",
                    "manual_url": "",
                },
            ],
            "source": "seed",
        },
        "cline": {
            "agent_id": "cline",
            "display_name": "Cline",
            "aliases": ["cline", "cline-cli", "cline_cli"],
            "binary_names": ["cline"],
            "binary_env": "MEGAS_CLINE_BINARY",
            "certified": True,
            "auth_hint": "Run cline auth to configure a provider and model.",
            "package_hints": [],
            "installers": [
                {
                    "id": "manual",
                    "kind": "manual",
                    "summary": "Install Cline CLI from the official Cline guide",
                    "manual_url": "https://docs.cline.bot/getting-started/installing-cline",
                }
            ],
            "source": "seed",
        },
        "devin_cli": {
            "agent_id": "devin_cli",
            "display_name": "Devin CLI",
            "aliases": ["devin", "devin_cli", "devin-cli", "cognition"],
            "binary_names": ["devin"],
            "binary_env": "MEGAS_DEVIN_BINARY",
            "certified": True,
            "auth_hint": "Run devin auth login.",
            "package_hints": [],
            "installers": [
                {
                    "id": "official_windows",
                    "kind": "script",
                    "platforms": ["windows"],
                    "summary": "Official Devin CLI PowerShell installer",
                    "script_windows": "irm https://static.devin.ai/cli/setup.ps1 | iex",
                },
                {
                    "id": "official_unix",
                    "kind": "script",
                    "platforms": ["darwin", "linux"],
                    "summary": "Official Devin CLI installer",
                    "script_unix": "curl -fsSL https://cli.devin.ai/install.sh | bash",
                },
            ],
            "source": "seed",
        },
        "kilo_code": {
            "agent_id": "kilo_code",
            "display_name": "Kilo Code CLI",
            "aliases": [
                "kilo",
                "kilocode",
                "kilo_code",
                "kilo-code",
                "kilo code",
                "@kilocode/cli",
            ],
            "binary_names": ["kilocode", "kilo"],
            "binary_env": "MEGAS_KILO_BINARY",
            "certified": False,
            "auth_hint": "Run kilocode once to sign in / configure models (kilo.ai).",
            "package_hints": ["@kilocode/cli"],
            "npm_package": "@kilocode/cli",
            "installers": [
                {
                    "id": "npm",
                    "kind": "npm",
                    "argv": ["npm", "install", "-g", "@kilocode/cli"],
                    "platforms": ["*"],
                    "requires": ["npm"],
                    "summary": "npm install -g @kilocode/cli",
                },
                {
                    "id": "manual",
                    "kind": "manual",
                    "summary": "See Kilo Code CLI docs",
                    "manual_url": "https://kilo.ai/docs/code-with-ai/platforms/cli",
                },
            ],
            "source": "seed",
        },
        "grok_build": {
            "agent_id": "grok_build",
            "display_name": "Grok Build",
            "aliases": ["grok", "grok_build", "grok-build", "xai"],
            "binary_names": ["grok-build", "grok"],
            "binary_env": "MEGAS_GROK_BINARY",
            "certified": False,
            "auth_hint": "xAI auth as required by the binary.",
            "package_hints": [],
            "installers": [
                {
                    "id": "manual",
                    "kind": "manual",
                    "summary": "Install from xAI / Grok Build distribution",
                    "manual_url": "https://x.ai/",
                }
            ],
            "source": "seed",
        },
    }


def empty_store() -> dict[str, Any]:
    return {
        "schema": STORE_SCHEMA,
        "updated_at": None,
        "last_live_refresh_at": None,
        "last_live_sources": [],
        "agents": {},
        "history": [],  # recent merge notes (capped)
    }


def load_store(path: Path | None = None) -> dict[str, Any]:
    store_path = path or default_store_path()
    if not store_path.exists():
        store = empty_store()
        store["agents"] = bootstrap_seed_agents()
        store["updated_at"] = _now_iso()
        save_store(store, store_path)
        return store
    try:
        data = json.loads(store_path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("invalid store")
        agents = data.get("agents")
        if not isinstance(agents, dict):
            agents = {}
        # Seed always present; merge discoveries carefully.
        # Drop historical npm-search spam (long random package slugs) while
        # preserving seed agents and short intentional ids.
        merged = bootstrap_seed_agents()
        for agent_id, rec in agents.items():
            if not isinstance(rec, dict):
                continue
            source = str(rec.get("source") or "")
            # Never re-import open registry-search noise into a clean store, but
            # retain short intentional discoveries and explicitly merged local
            # records. Previous code discarded its own `npm`/`npm_query` live
            # refresh results on the next load.
            registry_search = source in {"npm", "npm_query", "npm_search"}
            if registry_search and agent_id not in merged:
                if len(agent_id) > 32 or agent_id.count("_") >= 3:
                    continue
            trusted_source = source in {
                "seed",
                "npm_registry",
                "megas_catalog",
                "pypi",
                "install",
                "setup",
            }
            if agent_id in merged or trusted_source or registry_search:
                merged[agent_id] = _merge_agent_record(merged.get(agent_id), rec)
            elif rec.get("last_binary_path") and len(agent_id) <= 24 and agent_id.count("_") <= 2:
                merged[agent_id] = _merge_agent_record(merged.get(agent_id), rec)
        data["agents"] = apply_product_agent_policy(merged)
        data.setdefault("schema", STORE_SCHEMA)
        data.setdefault("history", [])
        return data
    except Exception:
        store = empty_store()
        store["agents"] = bootstrap_seed_agents()
        store["updated_at"] = _now_iso()
        return store


def save_store(store: dict[str, Any], path: Path | None = None) -> Path:
    store_path = path or default_store_path()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store = dict(store)
    store["schema"] = STORE_SCHEMA
    store["updated_at"] = _now_iso()
    # Cap history
    history = store.get("history") or []
    if isinstance(history, list) and len(history) > 100:
        store["history"] = history[-100:]
    tmp = store_path.with_suffix(store_path.suffix + ".tmp")
    tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(store_path)
    return store_path


def apply_product_agent_policy(agents: dict[str, Any] | None) -> dict[str, Any]:
    """Force product abandonments (e.g. gemini_cli) over live npm re-certification.

    Live registry refresh must never resurrect abandoned peers of Antigravity.
    """
    if not isinstance(agents, dict):
        return {}
    for agent_id, seed_rec in bootstrap_seed_agents().items():
        if not isinstance(seed_rec, dict) or not seed_rec.get("abandoned"):
            continue
        prev = agents.get(agent_id) if isinstance(agents.get(agent_id), dict) else {}
        forced = deepcopy(seed_rec)
        # Keep local diagnostics only — never re-certify or re-list as installable.
        for keep in ("last_binary_path", "last_version", "times_seen_on_machine", "first_seen_at"):
            if prev.get(keep) not in (None, "", 0):
                forced[keep] = prev[keep]
        forced["abandoned"] = True
        forced["certified"] = False
        forced["installers"] = []
        forced["package_hints"] = []
        if seed_rec.get("successor"):
            forced["successor"] = seed_rec["successor"]
        agents[agent_id] = forced
    return agents


def _merge_agent_record(base: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge two agent records; prefer richer installers/aliases; keep local detections."""
    if not base:
        out = deepcopy(incoming)
        out.setdefault("first_seen_at", _now_iso())
        out["last_seen_at"] = _now_iso()
        return out
    # Abandoned product seed always wins over live/npm reintroduction.
    if base.get("abandoned") or incoming.get("abandoned"):
        seed = bootstrap_seed_agents().get(str(base.get("agent_id") or incoming.get("agent_id") or ""))
        if isinstance(seed, dict) and seed.get("abandoned"):
            out = deepcopy(seed)
            for keep in ("last_binary_path", "last_version", "times_seen_on_machine", "first_seen_at"):
                if base.get(keep) not in (None, "", 0):
                    out[keep] = base[keep]
                elif incoming.get(keep) not in (None, "", 0):
                    out[keep] = incoming[keep]
            out["last_seen_at"] = _now_iso()
            return out
    out = deepcopy(base)
    for key in (
        "display_name",
        "binary_env",
        "auth_hint",
        "certified",
        "source",
        "docs_url",
        "npm_package",
        "pypi_package",
        "abandoned",
        "successor",
    ):
        if incoming.get(key) not in (None, "", [], {}):
            # Don't let uncertified live overwrite certified True unless explicit
            if key == "certified" and out.get("certified") is True and incoming.get("certified") is False:
                continue
            # Never let live registry re-certify an abandoned product agent.
            if key == "certified" and out.get("abandoned") and incoming.get("certified") is True:
                continue
            out[key] = incoming[key]
    # union lists
    for key in ("aliases", "binary_names", "package_hints", "search_terms"):
        left = [str(x) for x in (out.get(key) or []) if str(x).strip()]
        right = [str(x) for x in (incoming.get(key) or []) if str(x).strip()]
        seen: set[str] = set()
        merged: list[str] = []
        for item in left + right:
            low = item.lower()
            if low in seen:
                continue
            seen.add(low)
            merged.append(item)
        out[key] = merged
    # installers: merge by id/kind, prefer non-empty argv/script
    inst_map: dict[str, dict[str, Any]] = {}
    for inst in list(out.get("installers") or []) + list(incoming.get("installers") or []):
        if not isinstance(inst, dict):
            continue
        iid = str(inst.get("id") or inst.get("kind") or "manual")
        prev = inst_map.get(iid)
        if not prev:
            inst_map[iid] = dict(inst)
            continue
        # prefer one with argv or script
        score = lambda d: int(bool(d.get("argv"))) + int(bool(d.get("script_windows") or d.get("script_unix")))
        if score(inst) >= score(prev):
            merged_i = dict(prev)
            merged_i.update({k: v for k, v in inst.items() if v not in (None, "", [])})
            inst_map[iid] = merged_i
    out["installers"] = list(inst_map.values())
    # preserve machine state fields from base
    for key in ("last_binary_path", "last_version", "last_installed_at", "times_seen_on_machine"):
        if base.get(key) not in (None, "", 0):
            out[key] = base[key]
        elif incoming.get(key) not in (None, "", 0):
            out[key] = incoming[key]
    out["first_seen_at"] = base.get("first_seen_at") or incoming.get("first_seen_at") or _now_iso()
    out["last_seen_at"] = _now_iso()
    out["agent_id"] = str(incoming.get("agent_id") or base.get("agent_id") or "")
    return out


def merge_agents_into_store(
    store: dict[str, Any],
    agents: dict[str, dict[str, Any]],
    *,
    source: str,
    note: str = "",
) -> dict[str, Any]:
    existing = store.setdefault("agents", {})
    added = 0
    updated = 0
    for agent_id, rec in agents.items():
        if not agent_id or not isinstance(rec, dict):
            continue
        rec = dict(rec)
        rec["agent_id"] = agent_id
        rec.setdefault("source", source)
        if agent_id in existing:
            existing[agent_id] = _merge_agent_record(existing[agent_id], rec)
            updated += 1
        else:
            existing[agent_id] = _merge_agent_record(None, rec)
            added += 1
    apply_product_agent_policy(existing)
    history = store.setdefault("history", [])
    if isinstance(history, list):
        history.append(
            {
                "at": _now_iso(),
                "source": source,
                "added": added,
                "updated": updated,
                "note": note[:200],
            }
        )
    return store


def record_machine_detection(
    store: dict[str, Any],
    agent_id: str,
    *,
    binary_path: str | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    agents = store.setdefault("agents", {})
    rec = dict(agents.get(agent_id) or {"agent_id": agent_id, "aliases": [agent_id]})
    rec["agent_id"] = agent_id
    if binary_path:
        rec["last_binary_path"] = binary_path
    if version:
        rec["last_version"] = version
    rec["times_seen_on_machine"] = int(rec.get("times_seen_on_machine") or 0) + 1
    rec["last_detected_at"] = _now_iso()
    agents[agent_id] = _merge_agent_record(agents.get(agent_id), rec)
    return store


def record_install_success(
    store: dict[str, Any],
    agent_id: str,
    *,
    binary_path: str,
    version: str | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    agents = store.setdefault("agents", {})
    rec = dict(agents.get(agent_id) or {"agent_id": agent_id})
    rec["agent_id"] = agent_id
    rec["last_binary_path"] = binary_path
    if version:
        rec["last_version"] = version
    rec["last_installed_at"] = _now_iso()
    rec["last_install_method"] = method or ""
    agents[agent_id] = _merge_agent_record(agents.get(agent_id), rec)
    return store


def store_is_fresh(store: dict[str, Any], *, ttl_sec: int = DEFAULT_TTL_SEC) -> bool:
    ts = store.get("last_live_refresh_at") or store.get("updated_at")
    if not ts:
        return False
    try:
        # accept Z
        text = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return age < ttl_sec
    except Exception:
        return False


def _http_get_json(url: str, *, timeout: float = 8.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def _normalize_incoming_agent(raw: dict[str, Any], fallback_id: str = "") -> dict[str, Any] | None:
    agent_id = str(raw.get("agent_id") or raw.get("id") or fallback_id or "").strip()
    if not agent_id:
        return None
    agent_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", agent_id).lower().replace("-", "_")
    aliases = raw.get("aliases") or raw.get("search_terms") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    binaries = raw.get("binary_names") or raw.get("binaries") or raw.get("binary") or []
    if isinstance(binaries, str):
        binaries = [binaries]
    installers = raw.get("installers") or raw.get("install") or []
    if isinstance(installers, dict):
        installers = [installers]
    # Normalize npm/pip shortcut fields into installers
    if raw.get("npm_package") and not any(
        isinstance(i, dict) and i.get("kind") == "npm" for i in installers
    ):
        pkg = str(raw["npm_package"])
        installers = list(installers) + [
            {
                "id": "npm",
                "kind": "npm",
                "argv": ["npm", "install", "-g", pkg],
                "platforms": ["*"],
                "requires": ["npm"],
                "summary": f"npm install -g {pkg}",
            }
        ]
    if raw.get("pypi_package") and not any(
        isinstance(i, dict) and i.get("kind") in {"pip", "pipx"} for i in installers
    ):
        pkg = str(raw["pypi_package"])
        installers = list(installers) + [
            {
                "id": "pipx",
                "kind": "pipx",
                "argv": ["pipx", "install", pkg],
                "platforms": ["*"],
                "requires": ["pipx"],
                "summary": f"pipx install {pkg}",
            }
        ]
    return {
        "agent_id": agent_id,
        "display_name": str(raw.get("display_name") or raw.get("name") or agent_id),
        "aliases": [str(a) for a in aliases] + [agent_id],
        "binary_names": [str(b) for b in binaries] or [agent_id.split("_")[0]],
        "binary_env": str(raw.get("binary_env") or f"MEGAS_{agent_id.upper()}_BINARY"),
        "certified": bool(raw.get("certified", False)),
        "auth_hint": str(raw.get("auth_hint") or raw.get("auth") or ""),
        "package_hints": [str(p) for p in (raw.get("package_hints") or [])],
        "installers": [i for i in installers if isinstance(i, dict)],
        "docs_url": str(raw.get("docs_url") or raw.get("url") or raw.get("homepage") or ""),
        "source": str(raw.get("source") or "live"),
    }


def fetch_megas_catalog() -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Fetch Megas-hosted catalog from API and static JSON."""
    found: dict[str, dict[str, Any]] = {}
    sources: list[str] = []
    paths = [
        "/api/v1/cli/agents/catalog",
        "/agents/catalog.json",
        "/api/v1/agents/catalog",
    ]
    for base in _backend_bases():
        for path in paths:
            url = base if path in base else f"{base}{path}"
            try:
                data = _http_get_json(url, timeout=6.0)
            except Exception:
                continue
            agents_raw = None
            if isinstance(data, dict):
                agents_raw = data.get("agents") or data.get("items") or data
            elif isinstance(data, list):
                agents_raw = data
            if agents_raw is None:
                continue
            batch: dict[str, dict[str, Any]] = {}
            if isinstance(agents_raw, dict) and all(isinstance(v, dict) for v in agents_raw.values()):
                for aid, rec in agents_raw.items():
                    norm = _normalize_incoming_agent(rec, fallback_id=str(aid))
                    if norm:
                        batch[norm["agent_id"]] = norm
            elif isinstance(agents_raw, list):
                for rec in agents_raw:
                    if isinstance(rec, dict):
                        norm = _normalize_incoming_agent(rec)
                        if norm:
                            batch[norm["agent_id"]] = norm
            if batch:
                found.update(batch)
                sources.append(url)
                break  # one good Megas source per base is enough
        if found:
            break
    return found, sources


def fetch_npm_coding_agents(query: str = "coding agent cli", *, size: int = 25) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Discover npm packages that look like coding agents; store install recipes."""
    found: dict[str, dict[str, Any]] = {}
    sources: list[str] = []
    # Known high-signal package → agent mapping + open search
    curated_queries = [
        query,
        "codex cli",
        "claude code",
        "gemini cli",
        "opencode",
        "kimi cli",
        "aider",
    ]
    # High confidence package names
    known_pkgs = {
        "@openai/codex": ("codex_cli", "codex", True),
        "@anthropic-ai/claude-code": ("claude_code", "claude", True),
        "@google/gemini-cli": ("gemini_cli", "gemini", True),
        "opencode-ai": ("opencode", "opencode", False),
        "@opencode-ai/cli": ("opencode", "opencode", False),
    }
    for pkg, (agent_id, binary, certified) in known_pkgs.items():
        url = f"https://registry.npmjs.org/{urllib.parse.quote(pkg, safe='@/')}"
        try:
            data = _http_get_json(url, timeout=5.0)
            if not isinstance(data, dict) or data.get("error"):
                continue
            name = str(data.get("name") or pkg)
            desc = str(data.get("description") or "")
            latest = ""
            try:
                latest = str((data.get("dist-tags") or {}).get("latest") or "")
            except Exception:
                pass
            found[agent_id] = {
                "agent_id": agent_id,
                "display_name": name,
                "aliases": [agent_id, binary, name, pkg.split("/")[-1]],
                "binary_names": [binary],
                "binary_env": f"MEGAS_{agent_id.upper()}_BINARY",
                "certified": certified,
                "package_hints": [pkg],
                "npm_package": pkg,
                "installers": [
                    {
                        "id": "npm",
                        "kind": "npm",
                        "argv": ["npm", "install", "-g", pkg],
                        "platforms": ["*"],
                        "requires": ["npm"],
                        "summary": f"npm install -g {pkg}" + (f" (latest {latest})" if latest else ""),
                    }
                ],
                "auth_hint": desc[:200],
                "source": "npm_registry",
            }
            sources.append(url)
        except Exception:
            continue

    # Targeted open search — only keep high-signal CLI packages (avoid catalog spam).
    allow_name_tokens = (
        "codex",
        "claude-code",
        "claude_code",
        "gemini-cli",
        "gemini_cli",
        "opencode",
        "kilocode",
        "kilo-code",
        "aider",
        "kimi",
        "antigravity",
    )
    for q in curated_queries + ["kilocode cli", "@kilocode/cli"]:
        url = (
            "https://registry.npmjs.org/-/v1/search?"
            + urllib.parse.urlencode({"text": q, "size": str(min(size, 12))})
        )
        try:
            data = _http_get_json(url, timeout=6.0)
        except Exception:
            continue
        objects = data.get("objects") if isinstance(data, dict) else None
        if not isinstance(objects, list):
            continue
        sources.append(url)
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            pkg = (obj.get("package") or {})
            if not isinstance(pkg, dict):
                continue
            name = str(pkg.get("name") or "").strip()
            if not name or name in known_pkgs:
                continue
            low = name.lower()
            # Reject scoped platform binaries / SDKs / random plugins.
            if any(
                bad in low
                for bad in (
                    "darwin",
                    "linux",
                    "win32",
                    "musl",
                    "sdk",
                    "plugin",
                    "provider",
                    "adapter",
                    "eslint",
                    "trace",
                    "proxy",
                    "template",
                )
            ):
                continue
            if not any(tok in low for tok in allow_name_tokens):
                continue
            # Prefer known global CLIs only
            if name not in {
                "@openai/codex",
                "@anthropic-ai/claude-code",
                "@google/gemini-cli",
                "opencode-ai",
                "@opencode-ai/cli",
                "@kilocode/cli",
                "aider-chat",
            } and not low.endswith("/cli") and "cli" not in low.split("/")[-1]:
                # still allow exact product names
                if low not in {"opencode-ai", "kilocode", "aider"}:
                    continue
            if name == "@kilocode/cli":
                agent_id = "kilo_code"
                binary = "kilocode"
                certified = False
            elif "opencode" in low and "auth" not in low and "sdk" not in low:
                agent_id = "opencode"
                binary = "opencode"
                certified = False
            else:
                slug = re.sub(r"^@", "", name)
                slug = re.sub(r"[^a-zA-Z0-9]+", "_", slug).strip("_").lower()[:48]
                agent_id = slug
                binary = re.sub(r"[^a-zA-Z0-9._-]+", "", name.split("/")[-1])[:40] or agent_id
                certified = False
            # Don't overwrite curated registry hits
            if agent_id in found and found[agent_id].get("source") == "npm_registry":
                continue
            found[agent_id] = {
                "agent_id": agent_id,
                "display_name": str(pkg.get("description") or name)[:80],
                "aliases": [agent_id, binary, name, name.split("/")[-1]],
                "binary_names": [binary],
                "binary_env": f"MEGAS_{agent_id.upper()[:40]}_BINARY",
                "certified": certified,
                "package_hints": [name],
                "npm_package": name,
                "installers": [
                    {
                        "id": "npm",
                        "kind": "npm",
                        "argv": ["npm", "install", "-g", name],
                        "platforms": ["*"],
                        "requires": ["npm"],
                        "summary": f"npm install -g {name}",
                    }
                ],
                "docs_url": str((pkg.get("links") or {}).get("npm") or ""),
                "source": "npm_search",
            }
    # Always ensure Kilo CLI mapping even if search missed it
    if "kilo_code" not in found:
        found["kilo_code"] = {
            "agent_id": "kilo_code",
            "display_name": "Kilo Code CLI",
            "aliases": ["kilo", "kilocode", "kilo_code", "kilo-code"],
            "binary_names": ["kilocode", "kilo"],
            "binary_env": "MEGAS_KILO_BINARY",
            "certified": False,
            "package_hints": ["@kilocode/cli"],
            "npm_package": "@kilocode/cli",
            "installers": [
                {
                    "id": "npm",
                    "kind": "npm",
                    "argv": ["npm", "install", "-g", "@kilocode/cli"],
                    "platforms": ["*"],
                    "requires": ["npm"],
                    "summary": "npm install -g @kilocode/cli",
                }
            ],
            "source": "npm_registry",
        }
    return found, sources


def fetch_pypi_agents() -> tuple[dict[str, dict[str, Any]], list[str]]:
    found: dict[str, dict[str, Any]] = {}
    sources: list[str] = []
    # Direct known packages
    known = {"aider-chat": ("aider", "aider", True)}
    for pkg, (agent_id, binary, certified) in known.items():
        url = f"https://pypi.org/pypi/{urllib.parse.quote(pkg)}/json"
        try:
            data = _http_get_json(url, timeout=5.0)
        except Exception:
            continue
        info = data.get("info") if isinstance(data, dict) else None
        if not isinstance(info, dict):
            continue
        found[agent_id] = {
            "agent_id": agent_id,
            "display_name": str(info.get("name") or pkg),
            "aliases": [agent_id, binary, pkg],
            "binary_names": [binary],
            "binary_env": f"MEGAS_{agent_id.upper()}_BINARY",
            "certified": certified,
            "package_hints": [pkg],
            "pypi_package": pkg,
            "installers": [
                {
                    "id": "pipx",
                    "kind": "pipx",
                    "argv": ["pipx", "install", pkg],
                    "platforms": ["*"],
                    "requires": ["pipx"],
                    "summary": f"pipx install {pkg}",
                },
                {
                    "id": "pip",
                    "kind": "pip",
                    "argv": ["python", "-m", "pip", "install", "--user", pkg],
                    "platforms": ["*"],
                    "requires": [],
                    "summary": f"pip install --user {pkg}",
                },
            ],
            "docs_url": str(info.get("home_page") or info.get("project_url") or ""),
            "source": "pypi",
        }
        sources.append(url)
    return found, sources


def refresh_live(*, force: bool = False, path: Path | None = None, query: str | None = None) -> dict[str, Any]:
    """Fetch live sources and merge into local store. Returns updated store + meta."""
    store_path = path or default_store_path()
    store = load_store(store_path)
    if not force and store_is_fresh(store) and not query:
        store["_refresh"] = {"skipped": True, "reason": "fresh_local", "sources": store.get("last_live_sources") or []}
        return store

    all_sources: list[str] = []
    added_total = 0

    for fetcher_name, fetcher in (
        ("megas_catalog", lambda: fetch_megas_catalog()),
        ("npm", lambda: fetch_npm_coding_agents(query or "coding agent cli")),
        ("pypi", lambda: fetch_pypi_agents()),
    ):
        try:
            agents, sources = fetcher()
        except Exception as exc:
            store.setdefault("history", []).append(
                {"at": _now_iso(), "source": fetcher_name, "error": type(exc).__name__}
            )
            continue
        if not agents:
            continue
        before = len(store.get("agents") or {})
        store = merge_agents_into_store(store, agents, source=fetcher_name, note=f"live {fetcher_name}")
        after = len(store.get("agents") or {})
        added_total += max(0, after - before)
        all_sources.extend(sources)

    # Targeted live search when user typed a half-baked name
    if query:
        try:
            agents, sources = fetch_npm_coding_agents(query, size=15)
            store = merge_agents_into_store(store, agents, source="npm_query", note=f"query:{query[:40]}")
            all_sources.extend(sources)
        except Exception:
            pass

    store["last_live_refresh_at"] = _now_iso()
    store["last_live_sources"] = all_sources[:20]
    save_store(store, store_path)
    store["_refresh"] = {
        "skipped": False,
        "sources": all_sources,
        "agent_count": len(store.get("agents") or {}),
        "force": force,
    }
    return store


def ensure_store(*, force_refresh: bool = False, query: str | None = None, path: Path | None = None) -> dict[str, Any]:
    """Fast path: local store. Refresh live when stale, forced, or resolving unknown name."""
    store_path = path or default_store_path()
    store = load_store(store_path)
    never_refreshed = not store.get("last_live_refresh_at")
    need_live = force_refresh or never_refreshed or not store_is_fresh(store) or bool(query)
    if need_live:
        try:
            store = refresh_live(
                force=force_refresh or never_refreshed or bool(query),
                path=store_path,
                query=query,
            )
        except Exception:
            # Offline: keep local; still stamp so we do not hammer network every call
            if never_refreshed:
                store["last_live_refresh_at"] = _now_iso()
                store.setdefault("history", []).append(
                    {"at": _now_iso(), "source": "live", "note": "offline_first_refresh"}
                )
                try:
                    save_store(store, store_path)
                except Exception:
                    pass
            store["_refresh"] = {"skipped": True, "reason": "live_failed_offline"}
    return store


def agents_dict(store: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    if store is None:
        store = load_store()
    agents = store.get("agents") or {}
    return {k: v for k, v in agents.items() if isinstance(v, dict)}
