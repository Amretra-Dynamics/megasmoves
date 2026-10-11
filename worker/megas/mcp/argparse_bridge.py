from __future__ import annotations

import argparse
import importlib
import re
from dataclasses import dataclass, field

from .cli_bridge import CLIArgument, CLIBridgeManifest, CLITool


TECHNICAL_DESTS = {
    "help",
    "json",
    "output",
    "quiet",
    "verbose",
    "debug",
    "no_color",
    "non_interactive",
    "yes",
    "token",
}
SECRET_WORDS = {"token", "password", "passwd", "secret", "api_key", "apikey", "private_key"}
READ_ONLY_VERBS = {
    "list",
    "show",
    "status",
    "get",
    "inspect",
    "doctor",
    "logs",
    "history",
    "version",
    "capabilities",
    "sessions",
    "monitor",
}
DESTRUCTIVE_VERBS = {
    "delete",
    "remove",
    "revoke",
    "disable",
    "stop",
    "uninstall",
    "cancel",
    "reset",
    "reinstall",
    "update",
    "emergency-stop",
}


@dataclass(slots=True)
class ArgparseConversion:
    manifest: CLIBridgeManifest
    warnings: list[str] = field(default_factory=list)


def _tool_name(path: list[str]) -> str:
    raw = "_".join(path).replace("-", "_")
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_").lower()
    if not cleaned:
        raise ValueError("cannot derive MCP tool name from empty argparse path")
    return cleaned


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _choice_help(action: argparse._SubParsersAction, name: str) -> str:
    for row in getattr(action, "_choices_actions", []):
        if getattr(row, "dest", None) == name:
            return str(getattr(row, "help", "") or "")
    return ""


def _is_secret_dest(dest: str) -> bool:
    normalized = dest.lower().replace("-", "_")
    return normalized in SECRET_WORDS or any(
        normalized.endswith("_" + word) for word in SECRET_WORDS
    )


def _argument_type(action: argparse.Action) -> str:
    converter = getattr(action, "type", None)
    if converter is int:
        return "integer"
    if converter is float:
        return "number"
    if converter is bool:
        return "boolean"
    return "string"


def _multiple(action: argparse.Action) -> bool:
    nargs = getattr(action, "nargs", None)
    return isinstance(action, argparse._AppendAction) or nargs in {"+", "*"} or (
        isinstance(nargs, int) and nargs > 1
    )


def _required(action: argparse.Action) -> bool:
    if action.option_strings:
        return bool(getattr(action, "required", False))
    nargs = getattr(action, "nargs", None)
    return nargs not in {"?", "*"}


def _convert_action(action: argparse.Action, warnings: list[str], path: list[str]) -> CLIArgument | None:
    if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
        return None
    dest = str(getattr(action, "dest", "") or "")
    if not dest or dest in TECHNICAL_DESTS:
        return None
    if getattr(action, "help", None) is argparse.SUPPRESS:
        return None
    if _is_secret_dest(dest):
        warnings.append(
            f"{' '.join(path)}: skipped secret-like argument {dest}; bind secrets through environment references instead of MCP inputs"
        )
        return None

    description = str(getattr(action, "help", "") or "")
    choices = list(getattr(action, "choices", None) or [])
    if isinstance(action, argparse._StoreTrueAction):
        return CLIArgument(
            name=dest,
            kind="flag",
            flag=_preferred_flag(action),
            description=description,
            required=False,
            flag_when=True,
        )
    if isinstance(action, argparse._StoreFalseAction):
        return CLIArgument(
            name=dest,
            kind="flag",
            flag=_preferred_flag(action),
            description=description,
            required=False,
            flag_when=False,
        )
    if isinstance(action, argparse._AppendAction):
        return CLIArgument(
            name=dest,
            kind="option",
            flag=_preferred_flag(action),
            argument_type=_argument_type(action),
            description=description,
            required=bool(getattr(action, "required", False)),
            multiple=True,
            collection_style="repeat",
            enum=choices,
        )
    if isinstance(action, argparse._StoreAction):
        nargs = getattr(action, "nargs", None)
        if nargs in {argparse.REMAINDER, argparse.PARSER}:
            warnings.append(
                f"{' '.join(path)}: skipped unsupported remainder/parser argument {dest}"
            )
            return None
        kind = "option" if action.option_strings else "positional"
        return CLIArgument(
            name=dest,
            kind=kind,
            flag=_preferred_flag(action) if kind == "option" else None,
            argument_type=_argument_type(action),
            description=description,
            required=_required(action),
            multiple=_multiple(action),
            collection_style="values",
            enum=choices,
        )

    warnings.append(
        f"{' '.join(path)}: skipped unsupported argparse action {type(action).__name__} for {dest}"
    )
    return None


def _preferred_flag(action: argparse.Action) -> str:
    options = [str(value) for value in action.option_strings]
    if not options:
        raise ValueError(f"argparse option {action.dest} has no option string")
    long_flags = [value for value in options if value.startswith("--")]
    return long_flags[0] if long_flags else options[0]


def _annotations(path: list[str]) -> tuple[bool, bool, bool]:
    lowered = [part.lower() for part in path]
    final = lowered[-1] if lowered else ""
    read_only = final in READ_ONLY_VERBS or any(part in READ_ONLY_VERBS for part in lowered[-2:])
    destructive = final in DESTRUCTIVE_VERBS or any(part in DESTRUCTIVE_VERBS for part in lowered[-2:])
    return read_only, destructive, True


def _root_fixed_args(parser: argparse.ArgumentParser) -> list[str]:
    fixed: list[str] = []
    for action in parser._actions:
        dest = str(getattr(action, "dest", "") or "")
        if dest == "json" and isinstance(action, argparse._StoreTrueAction):
            fixed.append(_preferred_flag(action))
        elif dest == "non_interactive" and isinstance(action, argparse._StoreTrueAction):
            fixed.append(_preferred_flag(action))
        elif dest == "no_color" and isinstance(action, argparse._StoreTrueAction):
            fixed.append(_preferred_flag(action))
    return fixed


def _root_has_json(parser: argparse.ArgumentParser) -> bool:
    return any(
        str(getattr(action, "dest", "") or "") == "json"
        and isinstance(action, argparse._StoreTrueAction)
        for action in parser._actions
    )


def _prefix_parts(value: str) -> tuple[str, ...]:
    text = str(value or "").strip().lower()
    if not text:
        raise ValueError("command prefix must be non-empty")
    parts = tuple(part for part in re.split(r"[\s./:]+", text) if part)
    if not parts:
        raise ValueError("command prefix must contain at least one path segment")
    return parts


def _matches_prefix(path: list[str], prefix: tuple[str, ...]) -> bool:
    lowered = tuple(part.lower() for part in path)
    return len(lowered) >= len(prefix) and lowered[: len(prefix)] == prefix


def convert_argparse_parser(
    parser: argparse.ArgumentParser,
    *,
    command: list[str],
    name: str | None = None,
    description: str | None = None,
    include_prefixes: list[str] | None = None,
    exclude_prefixes: list[str] | None = None,
) -> ArgparseConversion:
    """Convert selected leaf argparse commands into a deterministic CLI→MCP manifest.

    Global machine-formatting flags such as --json and --non-interactive are fixed
    transport behavior, not MCP tool inputs. Secret-like flags are deliberately
    omitted and must be supplied through manifest env_refs instead. Generated
    manifests are always unreviewed until the owner explicitly approves them.
    """

    if not command:
        raise ValueError("argparse conversion requires the executable command tokens")
    includes = [_prefix_parts(value) for value in (include_prefixes or [])]
    excludes = [_prefix_parts(value) for value in (exclude_prefixes or [])]
    warnings: list[str] = []
    tools: list[CLITool] = []

    def walk(current: argparse.ArgumentParser, path: list[str], inherited_help: str = "") -> None:
        nested = _subparsers(current)
        local_actions = [
            action
            for action in current._actions
            if not isinstance(action, (argparse._HelpAction, argparse._SubParsersAction))
            and str(getattr(action, "dest", "") or "") not in TECHNICAL_DESTS
            and getattr(action, "help", None) is not argparse.SUPPRESS
        ]
        if nested is not None:
            if path and local_actions:
                warnings.append(
                    f"{' '.join(path)}: intermediate parser arguments are not exposed automatically; declare them manually if semantically required"
                )
            for choice, child in sorted(nested.choices.items()):
                walk(child, [*path, choice], _choice_help(nested, choice))
            return
        if not path:
            return
        if includes and not any(_matches_prefix(path, prefix) for prefix in includes):
            return
        if any(_matches_prefix(path, prefix) for prefix in excludes):
            return

        arguments: list[CLIArgument] = []
        for action in current._actions:
            converted = _convert_action(action, warnings, path)
            if converted is not None:
                arguments.append(converted)
        read_only, destructive, open_world = _annotations(path)
        text = (
            str(current.description or "").strip()
            or inherited_help.strip()
            or str(current.prog or "").strip()
        )
        tools.append(
            CLITool(
                name=_tool_name(path),
                title=" ".join(part.replace("-", " ").title() for part in path),
                description=text or f"Run {' '.join(path)} through the wrapped CLI.",
                argv=list(path),
                arguments=arguments,
                output="json" if _root_has_json(parser) else "auto",
                read_only=read_only,
                destructive=destructive,
                open_world=open_world,
            )
        )

    walk(parser, [])
    if not tools:
        scope = " after applying include/exclude prefixes" if includes or excludes else ""
        raise ValueError(f"argparse parser contains no leaf commands to expose{scope}")
    manifest = CLIBridgeManifest(
        name=name or str(parser.prog or command[0]),
        description=description or str(parser.description or ""),
        command=list(command),
        fixed_args=_root_fixed_args(parser),
        tools=tools,
        review_state="unreviewed",
        generated_by="argparse",
    )
    manifest.validate()
    return ArgparseConversion(manifest=manifest, warnings=warnings)


def load_parser_target(target: str) -> argparse.ArgumentParser:
    """Load ``module:function`` returning an ArgumentParser from installed code."""

    module_name, separator, function_name = target.partition(":")
    if not separator or not module_name or not function_name:
        raise ValueError("parser target must be MODULE:FUNCTION")
    module = importlib.import_module(module_name)
    factory = getattr(module, function_name, None)
    if not callable(factory):
        raise ValueError(f"argparse parser factory is not callable: {target}")
    parser = factory()
    if not isinstance(parser, argparse.ArgumentParser):
        raise ValueError(f"argparse parser factory did not return ArgumentParser: {target}")
    return parser


__all__ = [
    "ArgparseConversion",
    "convert_argparse_parser",
    "load_parser_target",
]
