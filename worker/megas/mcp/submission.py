from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .model import MCPServer


SUBMISSION_PACK_VERSION = 1
SENSITIVE_KEY_FRAGMENTS = (
    "access_token",
    "refresh_token",
    "bearer",
    "password",
    "passwd",
    "secret",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "client_secret",
    "credential",
)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS)


def _public_value(value: Any) -> Any:
    """Drop credential-shaped fields while preserving public submission metadata."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if _sensitive_key(text_key):
                result[text_key] = "[REDACTED]"
            else:
                result[text_key] = _public_value(item)
        return result
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    if isinstance(value, tuple):
        return [_public_value(item) for item in value]
    return value


def _tool_public_view(tool: dict[str, Any]) -> dict[str, Any]:
    schemes: list[dict[str, Any]] = []
    for row in tool.get("security_schemes") or []:
        if not isinstance(row, dict):
            continue
        schemes.append(
            {
                "type": str(row.get("type") or ""),
                "scopes": sorted(
                    str(scope)
                    for scope in (row.get("scopes") or [])
                    if isinstance(scope, str) and scope.strip()
                ),
            }
        )
    return {
        "name": str(tool.get("name") or ""),
        "title": str(tool.get("title") or ""),
        "description": str(tool.get("description") or ""),
        "input_schema": _public_value(tool.get("input_schema") or {}),
        "output_schema": _public_value(tool.get("output_schema") or {}),
        "annotations": _public_value(tool.get("annotations") or {}),
        "security_schemes": schemes,
    }


def _oauth_summary(report: dict[str, Any]) -> dict[str, Any] | None:
    oauth = report.get("oauth")
    if not isinstance(oauth, dict):
        return None
    authorization_servers: list[dict[str, Any]] = []
    for item in oauth.get("authorization_servers") or []:
        if not isinstance(item, dict):
            continue
        authorization_servers.append(
            {
                "issuer": str(item.get("issuer") or ""),
                "verified": bool(item.get("verified")),
                "metadata_url": str(item.get("metadata_url") or ""),
            }
        )
    return {
        "verified": bool(oauth.get("verified")),
        "protected_resource_metadata_url": str(
            oauth.get("protected_resource_metadata_url") or ""
        ),
        "authorization_servers": authorization_servers,
        "required_scopes": sorted(
            str(scope)
            for scope in (report.get("oauth_required_scopes") or [])
            if isinstance(scope, str)
        ),
    }


def readiness_evidence(server: MCPServer, report: dict[str, Any]) -> dict[str, Any]:
    probe = report.get("live_probe")
    tools: list[dict[str, Any]] = []
    ui_resources: list[dict[str, Any]] = []
    protocol_version: str | None = None
    server_info: dict[str, Any] = {}
    instructions = ""
    if isinstance(probe, dict):
        protocol_version = (
            str(probe.get("protocol_version"))
            if probe.get("protocol_version") is not None
            else None
        )
        server_info = _public_value(probe.get("server_info") or {})
        instructions = str(probe.get("instructions") or "")
        tools = [
            _tool_public_view(tool)
            for tool in (probe.get("tools") or [])
            if isinstance(tool, dict)
        ]
        for item in probe.get("ui_resources") or []:
            if not isinstance(item, dict):
                continue
            ui_resources.append(
                {
                    "uri": str(item.get("uri") or ""),
                    "readable": bool(item.get("readable")),
                    "mime_type": str(item.get("mime_type") or ""),
                    "content_bytes": int(item.get("content_bytes") or 0),
                    "content_sha256": str(item.get("content_sha256") or ""),
                }
            )

    return {
        "schema_version": SUBMISSION_PACK_VERSION,
        "target": str(report.get("target") or ""),
        "technical_ready": bool(report.get("technical_ready")),
        "live_scan_complete": bool(report.get("live_scan_complete", probe is not None)),
        "server": {
            "id": server.id,
            "name": server.name,
            "transport": server.transport,
            "url": server.url,
        },
        "protocol_version": protocol_version,
        "server_info": server_info,
        "instructions": instructions,
        "tools": tools,
        "ui_resources": ui_resources,
        "oauth": _oauth_summary(report),
        "blockers": _public_value(report.get("blockers") or []),
        "warnings": _public_value(report.get("warnings") or []),
        "passed": _public_value(report.get("passed") or []),
        "manual_remaining": _public_value(report.get("manual_remaining") or []),
        "scan_evidence_sha256": str(report.get("evidence_sha256") or ""),
    }


def _listing_template(server: MCPServer, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SUBMISSION_PACK_VERSION,
        "target": evidence["target"],
        "plugin_name": "FILL_ME",
        "short_description": "FILL_ME",
        "long_description": "FILL_ME",
        "logo_asset": "FILL_ME_PORTAL_UPLOAD",
        "category": "FILL_ME",
        "website_url": "FILL_ME",
        "support_url": "FILL_ME",
        "privacy_policy_url": "FILL_ME",
        "terms_of_service_url": "FILL_ME",
        "developer_identity": "FILL_ME_VERIFIED_OPENAI_PLATFORM_IDENTITY",
        "mcp_server_url": server.url or "FILL_ME",
        "domain_verification": {
            "challenge_base_url": "FILL_ME_IF_PORTAL_REQUESTS_OVERRIDE",
            "challenge_path": "/.well-known/openai-apps-challenge",
            "status": "COMPLETE_IN_OPENAI_SUBMISSION_PORTAL",
            "token": "DO_NOT_STORE_CHALLENGE_TOKEN_IN_THIS_PACK",
        },
        "authentication": {
            "required": bool(evidence.get("oauth")),
            "reviewer_demo_account": "PROVIDE_IN_PORTAL_ONLY_DO_NOT_STORE_CREDENTIALS_HERE",
            "reviewer_access_requirements": (
                "Verify the review account works without MFA, SMS, email confirmation, "
                "or private-network access."
            ),
        },
        "content_security_policy": "VERIFY_EXACT_UI_DOMAINS_IF_UI_IS_USED",
        "country_or_region_availability": ["FILL_ME"],
        "release_notes": "FILL_ME",
        "policy_attestations": "COMPLETE_IN_OPENAI_SUBMISSION_PORTAL",
    }


def _test_case_templates(tools: list[dict[str, Any]]) -> dict[str, Any]:
    names = [str(tool.get("name") or "") for tool in tools if str(tool.get("name") or "")]
    positives: list[dict[str, str]] = []
    for index in range(5):
        tool = names[index % len(names)] if names else "FILL_ME_TOOL"
        positives.append(
            {
                "id": f"POS-{index + 1:02d}",
                "user_prompt": f"FILL_ME: realistic user request that should use {tool}",
                "expected_tool_or_workflow_behavior": (
                    f"FILL_ME: exact behavior expected from {tool} or the containing workflow"
                ),
                "expected_result_shape": "FILL_ME: reviewer-visible result shape and key fields",
                "test_account_or_fixture_data": (
                    "FILL_ME_REFERENCE_ONLY: describe reproducible fixture state; "
                    "provide credentials separately in the submission portal"
                ),
            }
        )

    negatives = [
        {
            "id": "NEG-01",
            "user_prompt_or_scenario": "FILL_ME: out-of-scope request",
            "expected_refusal_clarification_or_safe_fallback": "FILL_ME",
            "why_plugin_should_not_complete_action": (
                "FILL_ME: explain why no plugin/tool action is appropriate"
            ),
        },
        {
            "id": "NEG-02",
            "user_prompt_or_scenario": "FILL_ME: invalid or missing required input",
            "expected_refusal_clarification_or_safe_fallback": "FILL_ME",
            "why_plugin_should_not_complete_action": (
                "FILL_ME: identify the missing/invalid requirement and required clarification"
            ),
        },
        {
            "id": "NEG-03",
            "user_prompt_or_scenario": "FILL_ME: unauthorized or insufficiently scoped action",
            "expected_refusal_clarification_or_safe_fallback": "FILL_ME",
            "why_plugin_should_not_complete_action": (
                "FILL_ME: identify the authorization/scope boundary"
            ),
        },
    ]
    return {
        "schema_version": SUBMISSION_PACK_VERSION,
        "positive": positives,
        "negative": negatives,
    }


def _starter_prompt_templates(tools: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    for index, tool in enumerate(tools[:6], start=1):
        name = str(tool.get("name") or "FILL_ME_TOOL")
        title = str(tool.get("title") or name)
        rows.append(
            {
                "id": f"STARTER-{index:02d}",
                "tool": name,
                "label": title,
                "prompt": f"FILL_ME: realistic starter prompt demonstrating {title}",
            }
        )
    if not rows:
        rows.append(
            {
                "id": "STARTER-01",
                "tool": "FILL_ME_TOOL",
                "label": "FILL_ME",
                "prompt": "FILL_ME",
            }
        )
    return {"schema_version": SUBMISSION_PACK_VERSION, "starter_prompts": rows}


def _manual_checklist(evidence: dict[str, Any]) -> str:
    readiness = "PASS" if evidence["technical_ready"] else "BLOCKED"
    lines = [
        "# OpenAI Plugin submission manual checklist",
        "",
        f"Technical preflight: **{readiness}**",
        "",
        "This pack contains no reviewer password, bearer token, refresh token, API key, or client secret by design.",
        "Do not add secrets or the domain-verification challenge token to this directory before uploading or sharing it.",
        "",
        "## Human work still required",
        "",
    ]
    manual = evidence.get("manual_remaining") or []
    if manual:
        for item in manual:
            if isinstance(item, dict):
                lines.append(f"- [ ] {item.get('id', 'MANUAL')}: {item.get('message', '')}")
    else:
        lines.append("- [ ] Review the current OpenAI submission portal and complete every manual field.")
    lines.extend(
        [
            "",
            "## Before submission",
            "",
            "- [ ] Resolve every technical blocker in `technical-readiness.json`.",
            "- [ ] Review every warning and document intentional exceptions.",
            "- [ ] Replace every `FILL_ME` value in the templates.",
            "- [ ] Run all positive and negative reviewer test cases against production.",
            "- [ ] Positive cases specify user prompt, expected tool/workflow behavior, expected result shape, and reproducible fixture data.",
            "- [ ] Negative cases specify request/scenario, expected refusal/clarification/fallback, and why the action must not complete.",
            "- [ ] If authentication is required, reviewer demo access works without MFA, SMS, email confirmation, or private-network access.",
            "- [ ] Verify OAuth and reviewer access without placing credentials in this pack.",
            "- [ ] If the portal requests domain verification, serve only the exact current token at `/.well-known/openai-apps-challenge` on the allowed challenge host.",
            "- [ ] Verify privacy policy, terms, support, domain, logo, and listing copy are live and match the verified publisher identity.",
            "- [ ] If UI is used, verify CSP allows exactly the domains the UI actually fetches from.",
            "- [ ] Re-run `megas mcp publish-check` against the production MCP endpoint immediately before submission.",
            "",
        ]
    )
    return "\n".join(lines)


def build_submission_files(server: MCPServer, report: dict[str, Any]) -> dict[str, bytes]:
    """Build deterministic pack bytes. No current timestamp or machine path is embedded."""

    evidence = readiness_evidence(server, report)
    files: dict[str, bytes] = {
        "technical-readiness.json": _json_bytes(evidence),
        "listing-template.json": _json_bytes(_listing_template(server, evidence)),
        "test-cases-template.json": _json_bytes(_test_case_templates(evidence["tools"])),
        "starter-prompts-template.json": _json_bytes(
            _starter_prompt_templates(evidence["tools"])
        ),
        "MANUAL_CHECKLIST.md": _manual_checklist(evidence).encode("utf-8"),
    }
    hashes = {name: _sha256(data) for name, data in sorted(files.items())}
    pack_hash = _sha256(_json_bytes(hashes))
    manifest = {
        "schema_version": SUBMISSION_PACK_VERSION,
        "target": evidence["target"],
        "server_id": server.id,
        "server": server.name,
        "technical_ready": evidence["technical_ready"],
        "live_scan_complete": evidence["live_scan_complete"],
        "files": hashes,
        "pack_sha256": pack_hash,
        "contains_credentials": False,
    }
    files["manifest.json"] = _json_bytes(manifest)
    return dict(sorted(files.items()))


def write_submission_pack(
    path: Path | str,
    server: MCPServer,
    report: dict[str, Any],
    *,
    replace: bool = False,
) -> dict[str, Any]:
    destination = Path(path).expanduser()
    if destination.exists() and not replace:
        raise FileExistsError(
            f"submission pack destination already exists: {destination}; use --replace"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = build_submission_files(server, report)

    temp_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.tmp-",
            dir=str(destination.parent),
        )
    )
    try:
        for name, data in files.items():
            target = temp_dir / name
            target.write_bytes(data)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        os.replace(temp_dir, destination)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    manifest = json.loads(files["manifest.json"].decode("utf-8"))
    return {
        "written": str(destination),
        "target": manifest["target"],
        "server_id": manifest["server_id"],
        "server": manifest["server"],
        "technical_ready": manifest["technical_ready"],
        "live_scan_complete": manifest["live_scan_complete"],
        "pack_sha256": manifest["pack_sha256"],
        "files": sorted(files),
        "contains_credentials": False,
    }


__all__ = [
    "SUBMISSION_PACK_VERSION",
    "build_submission_files",
    "readiness_evidence",
    "write_submission_pack",
]
