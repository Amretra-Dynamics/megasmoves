"""Persistent machine-local opt-in gate for Megas Operate.

Operate can control browsers, the desktop, and remote systems through hands such
as SSH. Merely installing or pairing Megas must never enable those capabilities.
The owner must acknowledge the current safety notice on each machine.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from megas.config import default_config_path

OPERATE_ACK_VERSION = "megas-operate-risk-v1"


@dataclass(frozen=True)
class OperateGateState:
    enabled: bool = False
    acknowledgement_version: str = ""
    enabled_at: str | None = None
    safe_environment_acknowledged: bool = False

    @property
    def active(self) -> bool:
        return bool(
            self.enabled
            and self.safe_environment_acknowledged
            and self.acknowledgement_version == OPERATE_ACK_VERSION
        )


def operate_gate_path() -> Path:
    return default_config_path().parent / "operate.json"


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_operate_gate(path: Path | None = None) -> OperateGateState:
    payload = _read_payload(path or operate_gate_path())
    return OperateGateState(
        enabled=payload.get("enabled") is True,
        acknowledgement_version=str(payload.get("acknowledgement_version") or ""),
        enabled_at=(
            str(payload.get("enabled_at"))
            if isinstance(payload.get("enabled_at"), str)
            else None
        ),
        safe_environment_acknowledged=(
            payload.get("safe_environment_acknowledged") is True
        ),
    )


def is_operate_enabled(path: Path | None = None) -> bool:
    return load_operate_gate(path).active


def _write_state(state: OperateGateState, path: Path) -> OperateGateState:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(asdict(state), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return state


def enable_operate(
    *,
    accepted_risk: bool,
    safe_environment_acknowledged: bool,
    path: Path | None = None,
) -> OperateGateState:
    if not accepted_risk or not safe_environment_acknowledged:
        raise ValueError(
            "Operate requires both the risk acknowledgement and safe-environment acknowledgement"
        )
    state = OperateGateState(
        enabled=True,
        acknowledgement_version=OPERATE_ACK_VERSION,
        enabled_at=datetime.now(timezone.utc).isoformat(),
        safe_environment_acknowledged=True,
    )
    return _write_state(state, path or operate_gate_path())


def disable_operate(path: Path | None = None) -> OperateGateState:
    state = OperateGateState()
    return _write_state(state, path or operate_gate_path())


def operate_status(path: Path | None = None) -> dict[str, Any]:
    state = load_operate_gate(path)
    return {
        "enabled": state.active,
        "configured_enabled": state.enabled,
        "acknowledgement_current": (
            state.acknowledgement_version == OPERATE_ACK_VERSION
        ),
        "acknowledgement_version": state.acknowledgement_version or None,
        "required_acknowledgement_version": OPERATE_ACK_VERSION,
        "enabled_at": state.enabled_at,
        "safe_environment_acknowledged": state.safe_environment_acknowledged,
    }


__all__ = [
    "OPERATE_ACK_VERSION",
    "OperateGateState",
    "disable_operate",
    "enable_operate",
    "is_operate_enabled",
    "load_operate_gate",
    "operate_gate_path",
    "operate_status",
]
