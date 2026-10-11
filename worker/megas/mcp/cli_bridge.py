from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


CLI_BRIDGE_FORMAT = "megas-cli-mcp"
CLI_BRIDGE_VERSION = 1
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
SUPPORTED_TYPES = {"string", "integer", "number", "boolean"}
SUPPORTED_KINDS = {"option", "flag", "positional"}
SUPPORTED_COLLECTION_STYLES = {"repeat", "values"}
REVIEW_STATES = {"reviewed", "unreviewed"}
REDACTED = "[REDACTED]"


def _clean_name(value: str, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must be non-empty")
    return text


def _json_type(value: str) -> str:
    normalized = str(value or "string").strip().lower()
    if normalized not in SUPPORTED_TYPES:
        raise ValueError(f"unsupported CLI argument type: {value}")
    return normalized


def _scalar_schema(argument_type: str, enum: list[Any]) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": argument_type}
    if enum:
        schema["enum"] = list(enum)
    return schema


def _redact_values(value: Any, secrets: list[str]) -> Any:
    active = [secret for secret in secrets if secret]
    if not active:
        return value
    if isinstance(value, str):
        text = value
        for secret in active:
            text = text.replace(secret, REDACTED)
        return text
    if isinstance(value, dict):
        return {str(key): _redact_values(item, active) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_values(item, active) for item in value]
    if isinstance(value, tuple):
        return [_redact_values(item, active) for item in value]
    return value


@dataclass(slots=True)
class CLIArgument:
    name: str
    kind: str = "option"
    flag: str | None = None
    argument_type: str = "string"
    description: str = ""
    required: bool = False
    multiple: bool = False
    collection_style: str = "repeat"
    enum: list[Any] = field(default_factory=list)
    flag_when: bool = True

    def validate(self) -> None:
        self.name = _clean_name(self.name, field_name="argument name")
        self.kind = str(self.kind or "option").strip().lower()
        if self.kind not in SUPPORTED_KINDS:
            raise ValueError(f"unsupported CLI argument kind for {self.name}: {self.kind}")
        self.argument_type = _json_type(self.argument_type)
        if self.kind == "flag":
            self.argument_type = "boolean"
            if not self.flag:
                raise ValueError(f"flag argument {self.name} requires flag")
            if self.multiple:
                raise ValueError(f"flag argument {self.name} cannot be multiple")
        elif self.kind == "option":
            if not self.flag:
                raise ValueError(f"option argument {self.name} requires flag")
        elif self.flag:
            raise ValueError(f"positional argument {self.name} must not define flag")
        if self.flag is not None:
            self.flag = _clean_name(self.flag, field_name=f"flag for {self.name}")
            if not self.flag.startswith("-"):
                raise ValueError(f"CLI flag for {self.name} must start with '-'")
        if self.collection_style not in SUPPORTED_COLLECTION_STYLES:
            raise ValueError(
                f"unsupported collection_style for {self.name}: {self.collection_style}"
            )
        if self.enum and self.multiple:
            for value in self.enum:
                if isinstance(value, (dict, list)):
                    raise ValueError(f"enum for {self.name} must contain scalar values")

    def input_schema(self) -> dict[str, Any]:
        self.validate()
        scalar = _scalar_schema(self.argument_type, self.enum)
        if not self.multiple:
            return scalar
        return {
            "type": "array",
            "items": scalar,
            "minItems": 1 if self.required else 0,
        }

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "name": self.name,
            "kind": self.kind,
            "flag": self.flag,
            "type": self.argument_type,
            "description": self.description,
            "required": self.required,
            "multiple": self.multiple,
            "collection_style": self.collection_style,
            "enum": list(self.enum),
            "flag_when": self.flag_when,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CLIArgument":
        argument = cls(
            name=str(data.get("name") or ""),
            kind=str(data.get("kind") or "option"),
            flag=str(data["flag"]) if data.get("flag") is not None else None,
            argument_type=str(data.get("type") or "string"),
            description=str(data.get("description") or ""),
            required=bool(data.get("required", False)),
            multiple=bool(data.get("multiple", False)),
            collection_style=str(data.get("collection_style") or "repeat"),
            enum=list(data.get("enum") or []),
            flag_when=bool(data.get("flag_when", True)),
        )
        argument.validate()
        return argument


@dataclass(slots=True)
class CLITool:
    name: str
    argv: list[str]
    arguments: list[CLIArgument] = field(default_factory=list)
    title: str = ""
    description: str = ""
    output: str = "auto"
    timeout_seconds: float | None = None
    read_only: bool = False
    destructive: bool = False
    open_world: bool = True

    def validate(self) -> None:
        self.name = _clean_name(self.name, field_name="tool name")
        if any(char.isspace() for char in self.name):
            raise ValueError(f"tool name may not contain whitespace: {self.name}")
        if not self.argv or any(not str(value) for value in self.argv):
            raise ValueError(f"tool {self.name} requires non-empty fixed argv")
        if self.output not in {"auto", "json", "text"}:
            raise ValueError(f"unsupported output mode for {self.name}: {self.output}")
        if self.timeout_seconds is not None and not (0 < self.timeout_seconds <= 300):
            raise ValueError(f"timeout for {self.name} must be between 0 and 300 seconds")
        seen: set[str] = set()
        for argument in self.arguments:
            argument.validate()
            if argument.name in seen:
                raise ValueError(f"tool {self.name} has duplicate argument: {argument.name}")
            seen.add(argument.name)

    def input_schema(self) -> dict[str, Any]:
        self.validate()
        properties = {
            argument.name: {
                **argument.input_schema(),
                **({"description": argument.description} if argument.description else {}),
            }
            for argument in self.arguments
        }
        required = [argument.name for argument in self.arguments if argument.required]
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required
        return schema

    def mcp_tool(self) -> dict[str, Any]:
        self.validate()
        description = self.description.strip() or f"Run the declared CLI capability {self.name}."
        return {
            "name": self.name,
            "title": self.title.strip() or self.name.replace("_", " ").strip().title(),
            "description": description,
            "inputSchema": self.input_schema(),
            "outputSchema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "exit_code": {"type": "integer"},
                    "result": {},
                    "stderr": {"type": "string"},
                },
                "required": ["ok", "exit_code", "result", "stderr"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": bool(self.read_only),
                "destructiveHint": bool(self.destructive),
                "openWorldHint": bool(self.open_world),
            },
            "securitySchemes": [{"type": "noauth"}],
        }

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "argv": list(self.argv),
            "arguments": [argument.to_dict() for argument in self.arguments],
            "output": self.output,
            "timeout_seconds": self.timeout_seconds,
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "openWorldHint": self.open_world,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CLITool":
        annotations = data.get("annotations") if isinstance(data.get("annotations"), dict) else {}
        tool = cls(
            name=str(data.get("name") or ""),
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            argv=[str(value) for value in (data.get("argv") or [])],
            arguments=[
                CLIArgument.from_dict(row)
                for row in (data.get("arguments") or [])
                if isinstance(row, dict)
            ],
            output=str(data.get("output") or "auto"),
            timeout_seconds=(
                float(data["timeout_seconds"])
                if data.get("timeout_seconds") is not None
                else None
            ),
            read_only=bool(annotations.get("readOnlyHint", False)),
            destructive=bool(annotations.get("destructiveHint", False)),
            open_world=bool(annotations.get("openWorldHint", True)),
        )
        tool.validate()
        return tool


@dataclass(slots=True)
class CLIBridgeManifest:
    name: str
    command: list[str]
    tools: list[CLITool]
    description: str = ""
    fixed_args: list[str] = field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    env_refs: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    review_state: str = "reviewed"
    generated_by: str = "manual"
    format: str = CLI_BRIDGE_FORMAT
    version: int = CLI_BRIDGE_VERSION

    def validate(self) -> None:
        if self.format != CLI_BRIDGE_FORMAT:
            raise ValueError(f"unsupported CLI bridge format: {self.format}")
        if self.version != CLI_BRIDGE_VERSION:
            raise ValueError(f"unsupported CLI bridge version: {self.version}")
        self.name = _clean_name(self.name, field_name="bridge name")
        if not self.command or any(not str(value) for value in self.command):
            raise ValueError("CLI bridge command must contain at least one executable token")
        if not (0 < float(self.timeout_seconds) <= 300):
            raise ValueError("CLI bridge timeout_seconds must be between 0 and 300")
        self.review_state = str(self.review_state or "reviewed").strip().lower()
        if self.review_state not in REVIEW_STATES:
            raise ValueError(f"unsupported manifest review_state: {self.review_state}")
        self.generated_by = _clean_name(self.generated_by or "manual", field_name="generated_by")
        if self.cwd is not None and not str(self.cwd).strip():
            raise ValueError("CLI bridge cwd must be null or a non-empty path")
        for target, source in self.env_refs.items():
            _clean_name(target, field_name="env_refs target")
            _clean_name(source, field_name=f"env_refs source for {target}")
        names: set[str] = set()
        if not self.tools:
            raise ValueError("CLI bridge manifest must expose at least one tool")
        for tool in self.tools:
            tool.validate()
            if tool.name in names:
                raise ValueError(f"duplicate CLI bridge tool name: {tool.name}")
            names.add(tool.name)

    def tool(self, name: str) -> CLITool:
        for tool in self.tools:
            if tool.name == name:
                return tool
        raise KeyError(f"unknown CLI bridge tool: {name}")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "format": self.format,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "command": list(self.command),
            "fixed_args": list(self.fixed_args),
            "cwd": self.cwd,
            "env": dict(sorted(self.env.items())),
            "env_refs": dict(sorted(self.env_refs.items())),
            "timeout_seconds": self.timeout_seconds,
            "review_state": self.review_state,
            "generated_by": self.generated_by,
            "tools": [tool.to_dict() for tool in sorted(self.tools, key=lambda item: item.name)],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CLIBridgeManifest":
        manifest = cls(
            format=str(data.get("format") or ""),
            version=int(data.get("version") or 0),
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            command=[str(value) for value in (data.get("command") or [])],
            fixed_args=[str(value) for value in (data.get("fixed_args") or [])],
            cwd=str(data["cwd"]) if data.get("cwd") is not None else None,
            env={str(key): str(value) for key, value in (data.get("env") or {}).items()},
            env_refs={
                str(key): str(value) for key, value in (data.get("env_refs") or {}).items()
            },
            timeout_seconds=float(data.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
            review_state=str(data.get("review_state") or "reviewed"),
            generated_by=str(data.get("generated_by") or "manual"),
            tools=[
                CLITool.from_dict(row)
                for row in (data.get("tools") or [])
                if isinstance(row, dict)
            ],
        )
        manifest.validate()
        return manifest


def load_manifest(path: Path | str) -> CLIBridgeManifest:
    file_path = Path(path).expanduser()
    raw = file_path.read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("CLI bridge manifest exceeds 2 MiB")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("CLI bridge manifest must be a JSON object")
    return CLIBridgeManifest.from_dict(data)


def save_manifest(path: Path | str, manifest: CLIBridgeManifest) -> Path:
    manifest.validate()
    file_path = Path(path).expanduser()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    file_path.write_text(payload, encoding="utf-8")
    try:
        os.chmod(file_path, 0o600)
    except OSError:
        pass
    return file_path


def _encode_scalar(value: Any, argument_type: str) -> str:
    if argument_type == "boolean":
        if type(value) is not bool:
            raise ValueError("boolean CLI argument requires true/false")
        return "true" if value else "false"
    if argument_type == "integer":
        if type(value) is not int:
            raise ValueError("integer CLI argument requires an integer")
    elif argument_type == "number":
        if type(value) not in {int, float}:
            raise ValueError("number CLI argument requires a number")
    elif argument_type == "string" and not isinstance(value, str):
        raise ValueError("string CLI argument requires a string")
    return str(value)


def build_argv(manifest: CLIBridgeManifest, tool: CLITool, arguments: dict[str, Any]) -> list[str]:
    schema = tool.input_schema()
    errors = sorted(Draft202012Validator(schema).iter_errors(arguments), key=lambda exc: list(exc.path))
    if errors:
        first = errors[0]
        location = ".".join(str(value) for value in first.path) or "arguments"
        raise ValueError(f"invalid arguments at {location}: {first.message}")

    argv = [*manifest.command, *manifest.fixed_args, *tool.argv]
    for spec in tool.arguments:
        if spec.name not in arguments:
            continue
        value = arguments[spec.name]
        if spec.kind == "flag":
            if bool(value) is bool(spec.flag_when):
                assert spec.flag is not None
                argv.append(spec.flag)
            continue

        values = value if spec.multiple else [value]
        encoded = [_encode_scalar(item, spec.argument_type) for item in values]
        if spec.kind == "positional":
            argv.extend(encoded)
            continue

        assert spec.flag is not None
        if spec.multiple and spec.collection_style == "repeat":
            for item in encoded:
                argv.extend([spec.flag, item])
        else:
            argv.append(spec.flag)
            argv.extend(encoded)
    return argv


def _read_capture(handle: Any, *, label: str) -> str:
    handle.flush()
    size = handle.tell()
    if size > MAX_CAPTURE_BYTES:
        raise RuntimeError(f"CLI {label} exceeded {MAX_CAPTURE_BYTES} bytes")
    handle.seek(0)
    raw = handle.read(MAX_CAPTURE_BYTES + 1)
    if len(raw) > MAX_CAPTURE_BYTES:
        raise RuntimeError(f"CLI {label} exceeded {MAX_CAPTURE_BYTES} bytes")
    return raw.decode("utf-8", errors="replace")


@dataclass(slots=True)
class CLIExecutionResult:
    tool: str
    ok: bool
    exit_code: int
    result: Any
    stderr: str

    def structured(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "result": self.result,
            "stderr": self.stderr,
        }


def execute_tool(
    manifest: CLIBridgeManifest,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout_seconds: float | None = None,
) -> CLIExecutionResult:
    manifest.validate()
    try:
        tool = manifest.tool(tool_name)
    except KeyError as exc:
        raise ValueError(str(exc)) from exc
    argv = build_argv(manifest, tool, arguments)
    timeout = float(timeout_seconds or tool.timeout_seconds or manifest.timeout_seconds)
    if not (0 < timeout <= 300):
        raise ValueError("CLI execution timeout must be between 0 and 300 seconds")

    environment = os.environ.copy()
    environment.update(manifest.env)
    secret_values: list[str] = []
    for target, source in manifest.env_refs.items():
        if source not in os.environ:
            raise RuntimeError(f"required environment variable is not set: {source}")
        secret_value = os.environ[source]
        environment[target] = secret_value
        if secret_value:
            secret_values.append(secret_value)

    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            cwd=manifest.cwd or None,
            env=environment,
            shell=False,
        )
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait(timeout=5)
            raise TimeoutError(
                f"CLI tool {tool.name} exceeded {timeout:g} seconds"
            ) from exc
        stdout = _read_capture(stdout_file, label="stdout")
        stderr = _read_capture(stderr_file, label="stderr")

    stripped = stdout.strip()
    parsed: Any
    if tool.output in {"auto", "json"} and stripped:
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            if tool.output == "json" and process.returncode == 0:
                raise RuntimeError(
                    f"CLI tool {tool.name} promised JSON output but returned invalid JSON"
                ) from exc
            parsed = stdout
    elif tool.output == "json":
        parsed = None
    else:
        parsed = stdout

    clean_result = _redact_values(parsed, secret_values)
    clean_stderr = _redact_values(stderr, secret_values)
    assert isinstance(clean_stderr, str)
    return CLIExecutionResult(
        tool=tool.name,
        ok=process.returncode == 0,
        exit_code=int(process.returncode if process.returncode is not None else -1),
        result=clean_result,
        stderr=clean_stderr,
    )


__all__ = [
    "CLIArgument",
    "CLIBridgeManifest",
    "CLIExecutionResult",
    "CLITool",
    "CLI_BRIDGE_FORMAT",
    "CLI_BRIDGE_VERSION",
    "REVIEW_STATES",
    "build_argv",
    "execute_tool",
    "load_manifest",
    "save_manifest",
]
