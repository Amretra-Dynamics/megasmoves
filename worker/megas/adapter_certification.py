"""Installation-scoped certification for native coding-agent adapters.

A binary being present is discovery evidence, not proof that Megas can safely
list, read, continue, stream, cancel, or report limits for that provider.
Certificates are generated only by explicit provider verification runs and are
bound to the exact executable, provider version, OS/architecture, adapter
contract, authentication identity, auth mode, and execution mode that were
tested.

A certificate may prove a safe capability subset. For example, a provider may
be allowed to expose authoritative native history while execution and exact
resume remain disabled. ``CertificationDecision.certified`` therefore means
full execution certification; ``verified_capabilities`` is the granular source
of truth for every narrower operation.

The runtime manifest consumes only a compact, path-free summary. Full evidence
stays local to the Worker under ``~/.megas/certifications`` (or the explicit
``MEGAS_CERTIFICATION_HOME`` override).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile
from typing import Any, Iterable, Mapping


CERTIFICATION_SCHEMA = "megas.adapter-certification.v1"
CHECK_PASSED = "passed"
CHECK_FAILED = "failed"
CHECK_NOT_RUN = "not_run"
CHECK_NOT_APPLICABLE = "not_applicable"
VALID_CHECK_STATES = {
    CHECK_PASSED,
    CHECK_FAILED,
    CHECK_NOT_RUN,
    CHECK_NOT_APPLICABLE,
}

# These checks together prove the full native execution route. A partial
# certificate need not pass every one; each capability below has its own proof
# requirements and remains available only when those exact checks passed.
CORE_REQUIRED_CHECKS: tuple[str, ...] = (
    "binary_identity",
    "provider_version",
    "authentication",
    "thread_list",
    "thread_read",
    "new_thread",
    "exact_resume_same_native_id",
    "native_persistence",
    "realtime_stream",
    "post_turn_reconciliation",
    "bogus_native_id_rejected",
    "no_sibling_thread_created",
    "no_duplicate_turn",
    "negative_nonconversation_sources_rejected",
    "secret_redaction",
)

KNOWN_CAPABILITIES: frozenset[str] = frozenset(
    {
        "new_thread",
        "thread_list",
        "thread_read",
        "exact_resume",
        "realtime_stream",
        "native_persistence",
        "reconciliation",
        "cancellation",
        "offline_replica",
        "limit_state",
    }
)

# A capability string is never evidence. The certificate keeps a capability
# only when every load-bearing check for that capability actually passed.
CAPABILITY_REQUIRED_CHECKS: Mapping[str, frozenset[str]] = {
    "thread_list": frozenset(
        {
            "binary_identity",
            "provider_version",
            "authentication",
            "thread_list",
            "negative_nonconversation_sources_rejected",
        }
    ),
    "thread_read": frozenset(
        {
            "binary_identity",
            "provider_version",
            "authentication",
            "thread_list",
            "thread_read",
            "negative_nonconversation_sources_rejected",
            "secret_redaction",
        }
    ),
    "native_persistence": frozenset(
        {
            "thread_read",
            "native_persistence",
        }
    ),
    "reconciliation": frozenset(
        {
            "thread_read",
            "post_turn_reconciliation",
        }
    ),
    "offline_replica": frozenset(
        {
            "thread_read",
            "native_persistence",
        }
    ),
    "new_thread": frozenset(
        {
            "binary_identity",
            "provider_version",
            "authentication",
            "new_thread",
            "no_sibling_thread_created",
            "no_duplicate_turn",
        }
    ),
    "exact_resume": frozenset(
        {
            "thread_read",
            "native_persistence",
            "new_thread",
            "exact_resume_same_native_id",
            "post_turn_reconciliation",
            "bogus_native_id_rejected",
            "no_sibling_thread_created",
            "no_duplicate_turn",
        }
    ),
    "realtime_stream": frozenset(
        {
            "new_thread",
            "realtime_stream",
        }
    ),
    # These optional checks must be produced by real provider operations. The
    # current runner intentionally cannot grant them from adapter declarations.
    "cancellation": frozenset({"provider_cancellation"}),
    "limit_state": frozenset({"limit_state_observed"}),
}

FULL_EXECUTION_CAPABILITIES: frozenset[str] = frozenset(
    {
        "new_thread",
        "thread_list",
        "thread_read",
        "exact_resume",
        "realtime_stream",
        "native_persistence",
        "reconciliation",
    }
)

_AUTH_SECRET_ENVIRONMENTS: Mapping[str, tuple[str, ...]] = {
    "codex_cli": ("OPENAI_API_KEY", "CODEX_API_KEY"),
    "claude_code": ("ANTHROPIC_API_KEY",),
    "cursor_cli": ("CURSOR_API_KEY",),
    "jules_cli": ("JULES_API_KEY",),
    "devin_cli": ("DEVIN_API_TOKEN", "DEVIN_API_KEY"),
    "grok_build": ("XAI_API_KEY", "GROK_API_KEY"),
    "opencode": ("OPENCODE_API_KEY",),
    "kilo_code": ("KILO_API_KEY",),
    "kiro_code": ("KIRO_API_KEY",),
    "cline": ("CLINE_API_KEY",),
    "aider": (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
    ),
}
_AUTH_IDENTITY_ENVIRONMENTS: Mapping[str, tuple[str, ...]] = {
    "devin_cli": ("DEVIN_ORG_ID",),
}
_AUTH_FILES: Mapping[str, tuple[str, ...]] = {
    "codex_cli": (
        "~/.codex/auth.json",
        "~/.config/codex/auth.json",
        "~/.codex.json",
    ),
    "claude_code": (
        "~/.claude.json",
        "~/.claude/config.json",
        "~/.config/claude/config.json",
    ),
    "aider": (
        "~/.aider.conf.yml",
        "~/.aider.model.settings.yml",
    ),
    "cursor_cli": (
        "~/.cursor/auth.json",
        "~/.config/cursor/auth.json",
    ),
    "opencode": (
        "~/.local/share/opencode/auth.json",
        "~/.config/opencode/auth.json",
        "~/AppData/Roaming/opencode/auth.json",
        "~/AppData/Local/opencode/auth.json",
        # OpenCode also supports authenticated local/OpenAI-compatible
        # providers whose identity lives in the provider configuration rather
        # than its optional cloud auth store.  The file is hashed below; its
        # API key or other credential material is never persisted or emitted.
        "~/.config/opencode/opencode.json",
    ),
    "kilo_code": (
        "~/.local/share/kilo/auth.json",
        "~/.config/kilo/auth.json",
        "~/.kilocode/auth.json",
        "~/.kilocode/credentials.json",
        "~/.config/kilocode/auth.json",
        "~/AppData/Roaming/kilo/auth.json",
        "~/AppData/Local/kilo/auth.json",
    ),
    "cline": (
        "~/.cline/data/auth.json",
        "~/.cline/data/settings/providers.json",
        "~/.config/cline/auth.json",
    ),
    "antigravity": (
        "~/.gemini/antigravity-cli/settings.json",
    ),
    "grok_build": (
        "~/.grok/auth.json",
    ),
}
_IDENTITY_KEYS: frozenset[str] = frozenset(
    {
        "sub",
        "subject",
        "email",
        "login",
        "accountid",
        "userid",
        "organizationid",
        "orgid",
        "profileid",
        "workspaceid",
        "tenantid",
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _safe_token(value: str) -> str:
    token = "".join(
        character
        for character in value.lower()
        if character.isalnum() or character in "-_."
    )
    return token[:80] or "unknown"


def _jwt_identity(value: str) -> list[str]:
    parts = value.split(".")
    if len(parts) != 3:
        return []
    try:
        payload_bytes = base64.urlsafe_b64decode(
            parts[1] + "=" * (-len(parts[1]) % 4)
        )
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return []
    return _identity_values(payload)


def _identity_values(value: object) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = "".join(
                character
                for character in str(raw_key).lower()
                if character.isalnum()
            )
            if (
                key in _IDENTITY_KEYS
                and isinstance(item, (str, int))
                and str(item).strip()
            ):
                values.append(f"{key}:{str(item).strip()[:500]}")
            elif isinstance(item, (dict, list)):
                values.extend(_identity_values(item))
            elif isinstance(item, str) and (
                "token" in key or "jwt" in key
            ):
                values.extend(_jwt_identity(item))
    elif isinstance(value, list):
        for item in value:
            values.extend(_identity_values(item))
    return values


def _resolve_auth_file_path(raw_path: str) -> Path:
    """Resolve account files through ``Path.home`` for deterministic identity.

    ``Path.expanduser`` delegates to process-level user databases and environment
    state, which can disagree with an explicitly selected Megas/test home. Using
    ``Path.home`` for the common ``~/`` contract keeps certification bound to the
    same account store used by the Worker and makes replacement tests reliable.
    """

    text = str(raw_path or "").strip()
    if text == "~":
        return Path.home()
    if text.startswith("~/") or text.startswith("~\\"):
        return Path.home() / text[2:]
    return Path(text).expanduser()


def derive_authentication_identity(
    provider_id: str,
    auth_mode: str,
    explicit: str = "",
) -> str:
    """Return a non-secret fingerprint for the active provider identity.

    API keys are hashed, never stored. Account files contribute stable account
    fields when available; otherwise their bytes are hashed so token/account
    replacement still invalidates prior certificates. Providers whose OAuth
    identity exists only in an inaccessible OS keyring return an empty value and
    must refuse certification until a runtime probe can supply explicit account
    evidence.
    """

    provider = str(provider_id or "").strip().lower()
    material: list[str] = []
    if explicit:
        material.append(f"explicit:{explicit}")

    for name in _AUTH_IDENTITY_ENVIRONMENTS.get(provider, ()):
        value = os.getenv(name, "").strip()
        if value:
            material.append(f"env-id:{name}:{value}")

    for name in _AUTH_SECRET_ENVIRONMENTS.get(provider, ()):
        value = os.getenv(name, "").strip()
        if value:
            secret_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
            material.append(f"env-secret:{name}:{secret_hash}")

    for raw_path in _AUTH_FILES.get(provider, ()):
        path = _resolve_auth_file_path(raw_path)
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if not data or len(data) > 4 * 1024 * 1024:
            continue
        identity_values: list[str] = []
        try:
            identity_values = _identity_values(
                json.loads(data.decode("utf-8"))
            )
        except (UnicodeError, json.JSONDecodeError):
            pass
        if identity_values:
            material.extend(
                f"file-id:{path.name}:{value}"
                for value in sorted(set(identity_values))
            )
        else:
            material.append(
                f"file-hash:{path.name}:{hashlib.sha256(data).hexdigest()}"
            )

    if not material:
        return ""
    payload = {
        "provider_id": provider,
        "auth_mode": str(auth_mode or "unknown"),
        "material": sorted(set(material)),
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def adapter_authentication_identity(adapter: object) -> str:
    """Read a provider's optional non-secret account identity safely.

    Some CLIs retain browser-login credentials only in an OS keyring.  Those
    adapters can expose a documented local identity probe (for example Kiro's
    ``whoami``).  Its value is immediately folded into the existing one-way
    local fingerprint and is never emitted in a certificate or manifest.
    """

    method = getattr(adapter, "certification_identity", None)
    if not callable(method):
        return ""
    try:
        return str(method() or "").strip()[:500]
    except Exception:
        return ""


def certification_home() -> Path:
    explicit = os.getenv("MEGAS_CERTIFICATION_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".megas" / "certifications"


def installation_identity(
    *,
    provider_id: str,
    executable: Path | str,
    provider_version: str,
    adapter_contract_version: str,
    auth_mode: str,
    execution_mode: str,
    system: str | None = None,
    architecture: str | None = None,
    authentication_identity: str = "",
) -> dict[str, Any]:
    """Return the canonical local identity used to bind a certificate.

    The resolved path and authentication fingerprint are part of the local
    identity so a replacement install or account/key change cannot inherit an
    earlier proof. Neither is emitted by :func:`certificate_summary` or the
    runtime manifest.
    """

    path = Path(executable).expanduser().resolve(strict=True)
    stat = path.stat()
    return {
        "provider_id": str(provider_id),
        "provider_version": str(provider_version or ""),
        "adapter_contract_version": str(adapter_contract_version or ""),
        "auth_mode": str(auth_mode or "unknown"),
        "authentication_identity": derive_authentication_identity(
            provider_id,
            auth_mode,
            authentication_identity,
        ),
        "execution_mode": str(execution_mode or "unknown"),
        "system": str(system or platform.system() or "unknown").lower(),
        "architecture": str(
            architecture or platform.machine() or "unknown"
        ).lower(),
        "executable_path": str(path),
        "executable_size": int(stat.st_size),
        "executable_mtime_ns": int(stat.st_mtime_ns),
    }


def installation_fingerprint(identity: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def certificate_path(provider_id: str, fingerprint: str) -> Path:
    return (
        certification_home()
        / _safe_token(provider_id)
        / f"{_safe_token(fingerprint)}.json"
    )


def normalize_checks(checks: Mapping[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_name, raw_state in checks.items():
        name = _safe_token(str(raw_name))
        state = str(raw_state or "").strip().lower()
        if state not in VALID_CHECK_STATES:
            raise ValueError(
                f"Invalid certification check state for {name}: {state}"
            )
        result[name] = state
    return result


def normalize_capabilities(capabilities: Iterable[object]) -> list[str]:
    return sorted(
        {
            str(item).strip()
            for item in capabilities
            if str(item).strip() in KNOWN_CAPABILITIES
        }
    )


def verified_capabilities_from_checks(
    *,
    checks: Mapping[str, object],
    requested_capabilities: Iterable[object],
) -> list[str]:
    """Return only capability claims backed by their exact passed checks."""

    normalized_checks = normalize_checks(checks)
    verified: list[str] = []
    for capability in normalize_capabilities(requested_capabilities):
        required = CAPABILITY_REQUIRED_CHECKS.get(capability, frozenset())
        if required and all(
            normalized_checks.get(check_name) == CHECK_PASSED
            for check_name in required
        ):
            verified.append(capability)
    return verified


def build_certificate(
    *,
    identity: Mapping[str, Any],
    checks: Mapping[str, object],
    verified_capabilities: Iterable[object],
    evidence: Mapping[str, object] | None = None,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    normalized_identity = dict(identity)
    fingerprint = installation_fingerprint(normalized_identity)
    normalized_checks = normalize_checks(checks)
    authentication_identity = str(
        normalized_identity.get("authentication_identity") or ""
    )
    capabilities = (
        verified_capabilities_from_checks(
            checks=normalized_checks,
            requested_capabilities=verified_capabilities,
        )
        if authentication_identity
        else []
    )
    core_ready = all(
        normalized_checks.get(name) == CHECK_PASSED
        for name in CORE_REQUIRED_CHECKS
    )
    execution_ready = core_ready and FULL_EXECUTION_CAPABILITIES.issubset(
        capabilities
    )
    status = (
        "verified"
        if execution_ready
        else "partial"
        if capabilities
        else "incomplete"
    )
    return {
        "schema_version": CERTIFICATION_SCHEMA,
        "provider_id": str(normalized_identity.get("provider_id") or ""),
        "installation_fingerprint": fingerprint,
        "identity": normalized_identity,
        "status": status,
        "checks": normalized_checks,
        "verified_capabilities": capabilities,
        "evidence": dict(evidence or {}),
        "issued_at": _iso(issued_at or _utc_now()),
        "expires_at": _iso(expires_at) if expires_at else None,
    }


def write_certificate(certificate: Mapping[str, Any]) -> Path:
    provider_id = str(certificate.get("provider_id") or "")
    fingerprint = str(certificate.get("installation_fingerprint") or "")
    if not provider_id or not fingerprint:
        raise ValueError(
            "Certificate provider_id and installation_fingerprint are required"
        )
    target = certificate_path(provider_id, fingerprint)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(certificate, indent=2, sort_keys=True, ensure_ascii=True)
        + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return target


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


@dataclass(frozen=True)
class CertificationDecision:
    certified: bool
    status: str
    reason: str
    installation_fingerprint: str
    verified_capabilities: tuple[str, ...]
    issued_at: str | None = None
    expires_at: str | None = None


def evaluate_certificate(
    *,
    identity: Mapping[str, Any],
    certificate: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> CertificationDecision:
    fingerprint = installation_fingerprint(identity)
    if not certificate:
        return CertificationDecision(
            False,
            "not_verified",
            "runtime_certification_required",
            fingerprint,
            (),
        )
    if certificate.get("schema_version") != CERTIFICATION_SCHEMA:
        return CertificationDecision(
            False,
            "stale",
            "certificate_schema_mismatch",
            fingerprint,
            (),
        )
    if str(certificate.get("provider_id") or "") != str(
        identity.get("provider_id") or ""
    ):
        return CertificationDecision(
            False,
            "stale",
            "certificate_provider_mismatch",
            fingerprint,
            (),
        )
    if str(certificate.get("installation_fingerprint") or "") != fingerprint:
        return CertificationDecision(
            False,
            "stale",
            "certificate_installation_mismatch",
            fingerprint,
            (),
        )
    stored_identity = certificate.get("identity")
    if not isinstance(stored_identity, dict) or stored_identity != dict(identity):
        return CertificationDecision(
            False,
            "stale",
            "certificate_identity_mismatch",
            fingerprint,
            (),
        )

    expires_at = _parse_iso(certificate.get("expires_at"))
    if expires_at is not None and (now or _utc_now()) >= expires_at:
        return CertificationDecision(
            False,
            "expired",
            "certificate_expired",
            fingerprint,
            (),
        )

    if (
        str(identity.get("auth_mode") or "") != "unauthenticated"
        and not str(identity.get("authentication_identity") or "")
    ):
        return CertificationDecision(
            False,
            "incomplete",
            "authentication_identity_unverified",
            fingerprint,
            (),
        )

    try:
        checks = normalize_checks(
            certificate.get("checks")
            if isinstance(certificate.get("checks"), dict)
            else {}
        )
    except ValueError:
        return CertificationDecision(
            False,
            "invalid",
            "certificate_check_state_invalid",
            fingerprint,
            (),
        )

    # Re-filter on every read. Manual or buggy insertion of a capability string
    # into the local JSON cannot bypass its required checks.
    capabilities = tuple(
        verified_capabilities_from_checks(
            checks=checks,
            requested_capabilities=(
                certificate.get("verified_capabilities") or []
            ),
        )
    )
    core_ready = all(
        checks.get(name) == CHECK_PASSED for name in CORE_REQUIRED_CHECKS
    )
    execution_ready = core_ready and FULL_EXECUTION_CAPABILITIES.issubset(
        capabilities
    )
    issued_text = str(certificate.get("issued_at") or "") or None
    expires_text = str(certificate.get("expires_at") or "") or None

    if execution_ready:
        return CertificationDecision(
            True,
            "verified",
            "verified_from_runtime",
            fingerprint,
            capabilities,
            issued_at=issued_text,
            expires_at=expires_text,
        )
    if capabilities:
        return CertificationDecision(
            False,
            "partial",
            "verified_capability_subset",
            fingerprint,
            capabilities,
            issued_at=issued_text,
            expires_at=expires_text,
        )

    failed = [
        name for name, state in checks.items() if state == CHECK_FAILED
    ]
    if failed:
        return CertificationDecision(
            False,
            "failed",
            f"certification_check_failed:{failed[0]}",
            fingerprint,
            (),
        )
    missing = [
        name
        for name in CORE_REQUIRED_CHECKS
        if checks.get(name) != CHECK_PASSED
    ]
    if missing:
        return CertificationDecision(
            False,
            "incomplete",
            f"certification_check_missing:{missing[0]}",
            fingerprint,
            (),
        )
    return CertificationDecision(
        False,
        "incomplete",
        "verified_capability_missing:new_thread",
        fingerprint,
        (),
    )


def load_installation_certificate(
    identity: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, CertificationDecision]:
    fingerprint = installation_fingerprint(identity)
    certificate = _read_json(
        certificate_path(
            str(identity.get("provider_id") or ""),
            fingerprint,
        )
    )
    return certificate, evaluate_certificate(
        identity=identity,
        certificate=certificate,
    )


def certificate_summary(decision: CertificationDecision) -> dict[str, Any]:
    """Return a privacy-safe manifest projection (no path, auth ID, or evidence)."""

    return {
        "status": decision.status,
        "certified": decision.certified,
        "reason": decision.reason,
        "installation_fingerprint": decision.installation_fingerprint,
        "verified_capabilities": list(decision.verified_capabilities),
        "issued_at": decision.issued_at,
        "expires_at": decision.expires_at,
    }
