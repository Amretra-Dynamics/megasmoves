"""Megas Auto authorization profiles.

Profiles tune initiative. They never establish identity, entitlements,
source ownership, connector grants, or authority to execute external effects.
The caller must provide current trusted grants for every decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Profile = Literal['autonomous', 'supervised', 'custom']
Decision = Literal['allow', 'approval_required', 'out_of_scope']

PROFILES = frozenset({'autonomous', 'supervised', 'custom'})
ROUTINE_EFFECTS = frozenset({'read', 'workspace_write', 'external_write'})
SENSITIVE_EFFECTS = frozenset({'spend', 'credential_release', 'irreversible', 'third_party_publish'})
ALL_EFFECTS = ROUTINE_EFFECTS | SENSITIVE_EFFECTS


def validate_preferences(profile: str, custom_effects: tuple[str, ...] = ()) -> tuple[str, tuple[str, ...]]:
    """Normalize preferences without converting them into authorization grants."""
    if profile not in PROFILES:
        raise ValueError('Unknown authorization profile')
    if not isinstance(custom_effects, (tuple, list)) or len(custom_effects) > len(ROUTINE_EFFECTS):
        raise ValueError('Invalid custom effect preferences')
    if any(effect not in ROUTINE_EFFECTS for effect in custom_effects):
        raise ValueError('Custom profiles can only select routine effects')
    if len(set(custom_effects)) != len(custom_effects):
        raise ValueError('Duplicate custom effects')
    return profile, tuple(sorted(custom_effects)) if profile == 'custom' else ()


@dataclass(frozen=True)
class TrustedScope:
    """Fresh, server-verified permissions. Never populate from form values."""
    context_sources: frozenset[str]
    capabilities: frozenset[str]
    effects: frozenset[str]
    active: bool = True


@dataclass(frozen=True)
class ActionRequest:
    source: str
    capability: str
    effect: str


def decide(
    action: ActionRequest,
    scope: TrustedScope,
    *,
    profile: str = 'autonomous',
    custom_effects: tuple[str, ...] = (),
    specific_action_approved: bool = False,
) -> Decision:
    """Evaluate a single action. Re-evaluate before execution and after grants change.

    `specific_action_approved` must come from a trusted, unexpired, action-bound
    approval record. It does not authorize an ungranted source/capability/effect.
    """
    profile, allowed_by_custom = validate_preferences(profile, custom_effects)
    if (not scope.active or action.effect not in ALL_EFFECTS
            or action.source not in scope.context_sources
            or action.capability not in scope.capabilities):
        return 'out_of_scope'
    if action.effect not in scope.effects:
        return 'approval_required'
    if action.effect in SENSITIVE_EFFECTS:
        return 'allow' if specific_action_approved else 'approval_required'
    if profile == 'supervised' and action.effect != 'read':
        return 'allow' if specific_action_approved else 'approval_required'
    if profile == 'custom' and action.effect not in allowed_by_custom:
        return 'approval_required'
    return 'allow'
