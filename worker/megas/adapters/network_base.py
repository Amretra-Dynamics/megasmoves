from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("megas.network.base")


@dataclass
class NetworkRestoreState:
    original_env: dict[str, str] = field(default_factory=dict)
    active_interfaces: list[str] = field(default_factory=list)
    dns_servers: list[str] = field(default_factory=list)
    routing_summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class NetworkProfile:
    provider_id: str
    profile_name: str
    credential_reference: str  # secure credential alias, not raw secret
    policy: dict[str, Any] = field(default_factory=dict)


@dataclass
class NetworkCapability:
    provider_type: str
    is_supported: bool
    installed: bool


@dataclass
class NetworkSession:
    session_id: str
    provider_id: str
    connected_at: str
    original_state: NetworkRestoreState = field(default_factory=NetworkRestoreState)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class NetworkVerification:
    request_id: str
    success: bool
    public_ip: str
    dns_ok: bool
    connectivity_ok: bool
    timestamp: str
    failed_checks: list[str] = field(default_factory=list)


@dataclass
class NetworkEvidence:
    profile_id: str
    adapter_id: str
    worker_id: str
    session_id: str
    connected_at: str
    verified_public_ip: str
    route_summary: dict[str, Any]
    verification_source: str
    timestamp: str
    cleanup_result: dict[str, Any]


@dataclass
class NetworkFailure:
    reason: str
    error_log: str
    timestamp: str


class NetworkAdapter(ABC):
    """
    Abstract interface defining the six-phase lifecycle for any network/VPN provider.
    """

    @abstractmethod
    def prepare(self, profile: NetworkProfile) -> None:
        """
        Phase 1: Prepare the local system, write non-sensitive configs, and validate pre-requisites.
        """
        pass

    @abstractmethod
    def connect(self, profile: NetworkProfile) -> NetworkSession:
        """
        Phase 2: Establish the VPN/proxy connection. Captures previous state into NetworkRestoreState.
        """
        pass

    @abstractmethod
    def verify(self, session: NetworkSession, request_id: str) -> NetworkVerification:
        """
        Phase 3: Verify DNS, public egress IP, and backend connectivity.
        """
        pass

    @abstractmethod
    def run(self, session: NetworkSession) -> None:
        """
        Phase 4: Run-phase monitor. Can block or watch connection heartbeat.
        """
        pass

    @abstractmethod
    def disconnect(self, session: NetworkSession) -> None:
        """
        Phase 5: Safely terminate the network session.
        """
        pass

    @abstractmethod
    def restore(self, restore_state: NetworkRestoreState) -> None:
        """
        Phase 6: Revert environmental, DNS, routing, and interface changes to pre-connect state.
        """
        pass
