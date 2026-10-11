from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


SCHEMA_VERSION = 2
REMOTE_TRANSPORTS = {"http", "sse", "streamable-http"}
SUPPORTED_TRANSPORTS = {"stdio", *REMOTE_TRANSPORTS}
PROFILE_HEALTH_POLICIES = {"required", "best-effort"}
MAX_PROJECT_METADATA_ENTRIES = 32
MAX_PROJECT_METADATA_KEY = 64
MAX_PROJECT_METADATA_VALUE = 1024
MAX_PROFILE_REQUIREMENTS = 256


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(kind: str) -> str:
    return f"{kind}_{uuid.uuid4().hex}"


def legacy_id(kind: str, value: str) -> str:
    """Stable ID for a record created before Megas persisted explicit IDs."""

    return f"{kind}_{uuid.uuid5(uuid.NAMESPACE_URL, f'megas:{kind}:{value}').hex}"


def _metadata(
    data: dict[str, Any],
    *,
    kind: str,
    legacy_key: str,
) -> tuple[str, str, str, int, str]:
    now = utc_now()
    record_id = str(data.get("id") or legacy_id(kind, legacy_key)).strip()
    created_at = str(data.get("created_at") or now)
    updated_at = str(data.get("updated_at") or created_at)
    schema_version = int(data.get("schema_version") or SCHEMA_VERSION)
    source = str(data.get("source") or "local")
    return record_id, created_at, updated_at, schema_version, source


def _requirement_map(value: Any, *, field_name: str) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"profile {field_name} must be an object")
    result: dict[str, list[str]] = {}
    for raw_server, raw_values in value.items():
        if not isinstance(raw_server, str) or not raw_server.strip():
            raise ValueError(f"profile {field_name} server references must be non-empty strings")
        if not isinstance(raw_values, list):
            raise ValueError(f"profile {field_name} values must be arrays")
        values: list[str] = []
        for raw_value in raw_values:
            if not isinstance(raw_value, str) or not raw_value.strip():
                raise ValueError(f"profile {field_name} entries must be non-empty strings")
            values.append(raw_value.strip())
        result[raw_server.strip()] = values
    return result


@dataclass(slots=True)
class MCPServer:
    name: str
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    # Compatibility/hydration field. Canonical persistence lives in SecretBinding.
    env_refs: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    description: str = ""
    id: str = field(default_factory=lambda: new_id("srv"))
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    schema_version: int = SCHEMA_VERSION
    source: str = "local"

    def validate(self) -> None:
        if not self.id or any(ch.isspace() for ch in self.id):
            raise ValueError("server id must be non-empty and contain no whitespace")
        if not self.name or any(ch.isspace() for ch in self.name):
            raise ValueError("server name must be non-empty and contain no whitespace")
        if self.transport not in SUPPORTED_TRANSPORTS:
            raise ValueError(
                "transport must be one of: stdio, http, sse, streamable-http"
            )
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio server requires a command")
            if self.url:
                raise ValueError("stdio server must not define a url")
        else:
            if not self.url:
                raise ValueError(f"{self.transport} server requires a url")
            if not str(self.url).startswith(("http://", "https://")):
                raise ValueError(f"{self.transport} server requires an http:// or https:// url")
            if self.command:
                raise ValueError(f"{self.transport} server must not define a command")
            if self.args:
                raise ValueError(f"{self.transport} server must not define command args")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported server schema version: {self.schema_version}")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "cwd": self.cwd,
            "env": dict(self.env),
            "env_refs": dict(self.env_refs),
            "enabled": self.enabled,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MCPServer":
        name = str(data["name"])
        record_id, created_at, updated_at, schema_version, source = _metadata(
            data,
            kind="srv",
            legacy_key=name,
        )
        server = cls(
            name=name,
            transport=str(data.get("transport") or "stdio"),
            command=data.get("command"),
            args=[str(v) for v in (data.get("args") or [])],
            url=data.get("url"),
            cwd=data.get("cwd"),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            env_refs={str(k): str(v) for k, v in (data.get("env_refs") or {}).items()},
            enabled=bool(data.get("enabled", True)),
            description=str(data.get("description") or ""),
            id=record_id,
            created_at=created_at,
            updated_at=updated_at,
            schema_version=schema_version,
            source=source,
        )
        server.validate()
        return server


@dataclass(slots=True, init=False)
class MCPProfile:
    name: str
    server_ids: list[str]
    description: str
    health_policy: str
    startup_timeout_seconds: float
    project_metadata: dict[str, str]
    tool_permission_requirements: dict[str, list[str]]
    secret_binding_requirements: dict[str, list[str]]
    id: str
    created_at: str
    updated_at: str
    schema_version: int
    source: str

    def __init__(
        self,
        name: str,
        servers: list[str] | None = None,
        description: str = "",
        *,
        server_ids: list[str] | None = None,
        health_policy: str = "required",
        startup_timeout_seconds: float = 8.0,
        project_metadata: dict[str, str] | None = None,
        tool_permission_requirements: dict[str, list[str]] | None = None,
        secret_binding_requirements: dict[str, list[str]] | None = None,
        id: str | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
        schema_version: int = SCHEMA_VERSION,
        source: str = "local",
    ) -> None:
        now = utc_now()
        self.name = name
        self.server_ids = list(server_ids if server_ids is not None else (servers or []))
        self.description = description
        self.health_policy = str(health_policy)
        self.startup_timeout_seconds = float(startup_timeout_seconds)
        self.project_metadata = {
            str(key): str(value)
            for key, value in (project_metadata or {}).items()
        }
        self.tool_permission_requirements = _requirement_map(
            tool_permission_requirements,
            field_name="tool_permission_requirements",
        )
        self.secret_binding_requirements = _requirement_map(
            secret_binding_requirements,
            field_name="secret_binding_requirements",
        )
        self.id = id or new_id("prf")
        self.created_at = created_at or now
        self.updated_at = updated_at or self.created_at
        self.schema_version = schema_version
        self.source = source

    @property
    def servers(self) -> list[str]:
        """Compatibility alias. Values are stable server IDs, not display names."""

        return self.server_ids

    @servers.setter
    def servers(self, values: list[str]) -> None:
        self.server_ids = list(values)

    @property
    def startup_order(self) -> list[str]:
        """Profile membership order is the canonical deterministic startup order."""

        return list(self.server_ids)

    def _validate_requirements(self) -> None:
        server_ids = set(self.server_ids)
        count = 0
        for field_name, requirements in (
            ("tool_permission_requirements", self.tool_permission_requirements),
            ("secret_binding_requirements", self.secret_binding_requirements),
        ):
            unknown = sorted(set(requirements) - server_ids)
            if unknown:
                raise ValueError(
                    f"profile {field_name} references servers outside the profile: {', '.join(unknown)}"
                )
            for server_id, values in requirements.items():
                if len(values) != len(set(values)):
                    raise ValueError(
                        f"profile {field_name} contains duplicate entries for {server_id}"
                    )
                count += len(values)
                for value in values:
                    if not value or any(ch.isspace() for ch in value):
                        raise ValueError(
                            f"profile {field_name} entries must be non-empty and contain no whitespace"
                        )
                    if field_name == "secret_binding_requirements" and "=" in value:
                        raise ValueError(
                            "profile secret_binding_requirements entries must be environment target names"
                        )
        if count > MAX_PROFILE_REQUIREMENTS:
            raise ValueError(
                f"profile supports at most {MAX_PROFILE_REQUIREMENTS} permission/binding requirements"
            )

    def validate(self) -> None:
        if not self.id or any(ch.isspace() for ch in self.id):
            raise ValueError("profile id must be non-empty and contain no whitespace")
        if not self.name or any(ch.isspace() for ch in self.name):
            raise ValueError("profile name must be non-empty and contain no whitespace")
        if not self.server_ids:
            raise ValueError("profile must contain at least one server")
        if any(not value or any(ch.isspace() for ch in value) for value in self.server_ids):
            raise ValueError("profile server IDs must be non-empty and contain no whitespace")
        if len(self.server_ids) != len(set(self.server_ids)):
            raise ValueError("profile server IDs must be unique")
        if self.health_policy not in PROFILE_HEALTH_POLICIES:
            raise ValueError(
                "profile health_policy must be one of: required, best-effort"
            )
        if not (0 < self.startup_timeout_seconds <= 300):
            raise ValueError("profile startup_timeout_seconds must be between 0 and 300")
        if len(self.project_metadata) > MAX_PROJECT_METADATA_ENTRIES:
            raise ValueError(
                f"profile project_metadata supports at most {MAX_PROJECT_METADATA_ENTRIES} entries"
            )
        for key, value in self.project_metadata.items():
            if not key.strip():
                raise ValueError("profile project_metadata keys must be non-empty")
            if len(key) > MAX_PROJECT_METADATA_KEY:
                raise ValueError(
                    f"profile project_metadata key exceeds {MAX_PROJECT_METADATA_KEY} characters"
                )
            if len(value) > MAX_PROJECT_METADATA_VALUE:
                raise ValueError(
                    f"profile project_metadata value exceeds {MAX_PROJECT_METADATA_VALUE} characters"
                )
        self._validate_requirements()
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported profile schema version: {self.schema_version}")

    @staticmethod
    def _sorted_requirements(value: dict[str, list[str]]) -> dict[str, list[str]]:
        return {
            server_id: sorted(values)
            for server_id, values in sorted(value.items())
        }

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "name": self.name,
            "server_ids": list(self.server_ids),
            "startup_order": list(self.server_ids),
            "description": self.description,
            "health_policy": self.health_policy,
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "project_metadata": dict(sorted(self.project_metadata.items())),
            "tool_permission_requirements": self._sorted_requirements(
                self.tool_permission_requirements
            ),
            "secret_binding_requirements": self._sorted_requirements(
                self.secret_binding_requirements
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MCPProfile":
        name = str(data["name"])
        record_id, created_at, updated_at, schema_version, source = _metadata(
            data,
            kind="prf",
            legacy_key=name,
        )
        refs = data.get("server_ids")
        if refs is None:
            refs = data.get("servers") or []
        server_ids = [str(v) for v in refs]
        advertised_order = data.get("startup_order")
        if advertised_order is not None and not isinstance(advertised_order, list):
            raise ValueError("profile startup_order must be an array")
        # startup_order is a persisted explanatory mirror, not independent authority.
        # Imports may remap source-machine server IDs by stable name before hydration;
        # the next write always canonicalizes the mirror from server_ids.
        raw_project_metadata = data.get("project_metadata", {})
        if raw_project_metadata is None:
            raw_project_metadata = {}
        if not isinstance(raw_project_metadata, dict):
            raise ValueError("profile project_metadata must be an object")
        profile = cls(
            name=name,
            server_ids=server_ids,
            description=str(data.get("description") or ""),
            health_policy=str(data.get("health_policy", "required")),
            startup_timeout_seconds=float(data.get("startup_timeout_seconds", 8.0)),
            project_metadata={
                str(key): str(value)
                for key, value in raw_project_metadata.items()
            },
            tool_permission_requirements=_requirement_map(
                data.get("tool_permission_requirements"),
                field_name="tool_permission_requirements",
            ),
            secret_binding_requirements=_requirement_map(
                data.get("secret_binding_requirements"),
                field_name="secret_binding_requirements",
            ),
            id=record_id,
            created_at=created_at,
            updated_at=updated_at,
            schema_version=schema_version,
            source=source,
        )
        profile.validate()
        return profile


@dataclass(slots=True)
class SecretBinding:
    """Machine-local reference to secret material; never the secret value itself."""

    server_id: str
    target: str
    reference: str
    provider: str = "env"
    id: str = field(default_factory=lambda: new_id("sec"))
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    schema_version: int = SCHEMA_VERSION
    source: str = "local"

    def validate(self) -> None:
        if not self.id or any(ch.isspace() for ch in self.id):
            raise ValueError("secret binding id must be non-empty and contain no whitespace")
        if not self.server_id or any(ch.isspace() for ch in self.server_id):
            raise ValueError("secret binding server_id must be non-empty and contain no whitespace")
        if not self.target or any(ch.isspace() for ch in self.target) or "=" in self.target:
            raise ValueError("secret binding target must be an environment variable name")
        if self.provider != "env":
            raise ValueError("only env secret bindings are supported in the current local core")
        if not self.reference or any(ch.isspace() for ch in self.reference) or "=" in self.reference:
            raise ValueError("env secret binding reference must be an environment variable name")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported secret binding schema version: {self.schema_version}")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "server_id": self.server_id,
            "target": self.target,
            "provider": self.provider,
            "reference": self.reference,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SecretBinding":
        server_id = str(data.get("server_id") or "")
        target = str(data.get("target") or "")
        provider = str(data.get("provider") or "env")
        reference = str(data.get("reference") or "")
        record_id, created_at, updated_at, schema_version, source = _metadata(
            data,
            kind="sec",
            legacy_key=f"{server_id}:{target}:{provider}:{reference}",
        )
        binding = cls(
            server_id=server_id,
            target=target,
            reference=reference,
            provider=provider,
            id=record_id,
            created_at=created_at,
            updated_at=updated_at,
            schema_version=schema_version,
            source=source,
        )
        binding.validate()
        return binding
