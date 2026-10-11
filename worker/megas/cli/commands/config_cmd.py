"""Configuration commands."""

from __future__ import annotations

import argparse
from typing import Any

from megas.cli.config_store import (
    flatten_for_list,
    list_config_paths,
    load_settings,
    save_user_settings,
    user_config_path,
)
from megas.cli.context import RuntimeContext, build_context
from megas.cli.errors import InvalidArgumentsError, NotFoundError


def cmd_config_path(ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    paths = list_config_paths()
    ctx.output.result(paths)
    return paths


def cmd_config_list(ctx: RuntimeContext, _args: argparse.Namespace) -> dict[str, Any]:
    settings = load_settings(profile=ctx.settings.profile, flag_overrides={})
    # Merge active runtime overrides for display
    rows = flatten_for_list(ctx.settings)
    ctx.output.result(
        rows,
        columns=[("key", "KEY"), ("value", "VALUE")],
        human_message=f"Active profile: {ctx.settings.profile}",
    )
    return {"profile": ctx.settings.profile, "values": settings.to_public_dict()}


def cmd_config_get(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    key = args.key
    value = ctx.settings.get(key)
    if value is None and key not in ctx.settings.to_public_dict():
        raise NotFoundError(f"Unknown config key: {key}")
    result = {"key": key, "value": value}
    ctx.output.result(result, human_message=f"{key}={value}")
    return result


def cmd_config_set(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    key = args.key
    value = args.value
    settings = load_settings(profile=None, flag_overrides={})
    settings.set(key, value)
    path = save_user_settings(settings)
    result = {"key": key, "value": settings.get(key), "path": str(path)}
    ctx.output.result(result, human_message=f"Set {key}={settings.get(key)} ({path})")
    return result


def cmd_config_unset(ctx: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    key = args.key
    settings = load_settings(profile=None, flag_overrides={})
    if not settings.unset(key):
        # still save defaults if known key
        if key not in settings.to_public_dict() and key not in {"api_url", "timeout", "output", "profile", "color", "non_interactive"}:
            raise NotFoundError(f"Unknown config key: {key}")
    path = save_user_settings(settings)
    result = {"key": key, "unset": True, "path": str(path)}
    ctx.output.result(result, human_message=f"Unset {key}")
    return result


def register(sub: argparse._SubParsersAction) -> None:
    config = sub.add_parser("config", help="Manage CLI configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)

    path_p = config_sub.add_parser("path", help="Show configuration file paths")
    path_p.set_defaults(func=_wrap(cmd_config_path))

    list_p = config_sub.add_parser("list", help="List active configuration values")
    list_p.set_defaults(func=_wrap(cmd_config_list))

    get_p = config_sub.add_parser("get", help="Get a configuration value")
    get_p.add_argument("key")
    get_p.set_defaults(func=_wrap(cmd_config_get))

    set_p = config_sub.add_parser("set", help="Set a configuration value in the user config file")
    set_p.add_argument("key")
    set_p.add_argument("value")
    set_p.set_defaults(func=_wrap(cmd_config_set))

    unset_p = config_sub.add_parser("unset", help="Unset a configuration value")
    unset_p.add_argument("key")
    unset_p.set_defaults(func=_wrap(cmd_config_unset))


def _wrap(fn):
    def runner(args: argparse.Namespace) -> None:
        ctx = build_context(args)
        fn(ctx, args)

    runner.__name__ = getattr(fn, "__name__", "config_cmd")
    runner.__wrapped__ = fn  # type: ignore[attr-defined]
    return runner


__all__ = ["register"]
