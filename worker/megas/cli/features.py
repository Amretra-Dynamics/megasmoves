"""Feature flags and capability checks for unfinished/optional CLI surfaces."""

from __future__ import annotations

import os
from typing import Any

# Capabilities that may be partially implemented on the backend.
FEATURE_FLAGS: dict[str, str] = {
    "MEGAS_CLI_OPERATE_BROWSER": "operate.browser",
    "MEGAS_CLI_OPERATE_WINDOWS": "operate.windows",
    "MEGAS_CLI_SECURITY_AUDIT": "security.audit",
    "MEGAS_CLI_SECURITY_REMEDIATION": "security.remediation",
    "MEGAS_CLI_MONITOR_STREAM": "monitor.stream",
    "MEGAS_CLI_NOTIFICATIONS": "notifications",
    "MEGAS_CLI_MOCK_READS": "cli.mock_reads",
}


def flag_enabled(env_name: str, default: bool = True) -> bool:
    raw = os.getenv(env_name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def capability_enabled(capability: str) -> bool:
    for env_name, cap in FEATURE_FLAGS.items():
        if cap == capability:
            return flag_enabled(env_name, default=True)
    return True


def feature_status() -> list[dict[str, Any]]:
    rows = []
    for env_name, capability in FEATURE_FLAGS.items():
        rows.append(
            {
                "env": env_name,
                "capability": capability,
                "enabled": flag_enabled(env_name, default=True),
            }
        )
    return rows


__all__ = ["FEATURE_FLAGS", "capability_enabled", "feature_status", "flag_enabled"]
