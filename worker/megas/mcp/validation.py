from __future__ import annotations

from typing import Any

from jsonschema import exceptions as jsonschema_exceptions
from jsonschema.validators import validator_for


MAX_SCHEMA_NODES = 20_000
MAX_SCHEMA_DEPTH = 64

# The runtime RPC contract already exposes ValueError as its stable validation
# category. Keep the public alias descriptive without introducing a transport-
# specific subclass name that would make managed and ephemeral calls diverge.
ToolInputValidationError = ValueError


def _validate_schema_safety(value: Any, *, depth: int = 0, counter: list[int] | None = None) -> None:
    """Reject schemas that could escape local deterministic validation."""

    if depth > MAX_SCHEMA_DEPTH:
        raise ToolInputValidationError("tool input schema exceeds maximum nesting depth")
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_SCHEMA_NODES:
        raise ToolInputValidationError("tool input schema exceeds maximum complexity")

    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in {"$ref", "$dynamicRef"} and isinstance(item, str):
                # Only document-local fragment references are allowed. Absolute
                # URLs and relative paths can trigger external resource retrieval
                # depending on validator/ref-resolution behavior, so both are
                # rejected before the schema reaches jsonschema.
                if not item.startswith("#"):
                    raise ToolInputValidationError(
                        f"external schema reference is not allowed: {item}"
                    )
            _validate_schema_safety(item, depth=depth + 1, counter=counter)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_schema_safety(item, depth=depth + 1, counter=counter)


def _format_path(path: Any) -> str:
    segments = list(path)
    if not segments:
        return "$"
    out = "$"
    for segment in segments:
        if isinstance(segment, int):
            out += f"[{segment}]"
        else:
            out += f".{segment}"
    return out


def validate_tool_arguments(
    tool_name: str,
    schema: dict[str, Any],
    arguments: dict[str, Any],
) -> None:
    """Validate one MCP tool invocation locally before dispatch."""

    if not isinstance(arguments, dict):
        raise ToolInputValidationError("tool arguments must be a JSON object")
    if not isinstance(schema, dict):
        raise ToolInputValidationError(f"tool {tool_name} reported a non-object input schema")

    _validate_schema_safety(schema)
    try:
        validator_type = validator_for(schema)
        validator_type.check_schema(schema)
        validator = validator_type(schema)
    except jsonschema_exceptions.SchemaError as exc:
        raise ToolInputValidationError(
            f"tool {tool_name} reported an invalid input schema: {exc.message}"
        ) from exc

    errors = sorted(
        validator.iter_errors(arguments),
        key=lambda error: (
            tuple(f"{type(segment).__name__}:{segment}" for segment in error.absolute_path),
            error.message,
        ),
    )
    if not errors:
        return

    first = errors[0]
    path = _format_path(first.absolute_path)
    raise ToolInputValidationError(
        f"tool arguments failed schema validation for {tool_name} at {path}: {first.message}"
    )


def tool_schema_from_probe(probe: dict[str, Any], tool_name: str) -> dict[str, Any]:
    tools = probe.get("tools") or {}
    items = tools.get("items") if isinstance(tools, dict) else None
    if not isinstance(items, list):
        raise ToolInputValidationError("capability probe did not return a tool list")
    for item in items:
        if isinstance(item, dict) and str(item.get("name") or "") == tool_name:
            schema = item.get("input_schema")
            if not isinstance(schema, dict):
                raise ToolInputValidationError(
                    f"tool {tool_name} did not report a valid input schema"
                )
            return schema
    raise ToolInputValidationError(f"MCP server does not report tool: {tool_name}")


__all__ = [
    "ToolInputValidationError",
    "tool_schema_from_probe",
    "validate_tool_arguments",
]
