"""Command-line and Rofi entry points."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import replace

from . import engine
from .cache import CacheStore
from .config import ConfigError, PickerConfig, load_config
from .rofi import run_rofi


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover and open agent sessions")
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--ssh-connect-timeout", type=int, default=None)
    parser.add_argument("--ssh-connection-attempts", type=int, default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list agent sessions as JSON")
    list_parser.add_argument("--host", action="append", default=None)
    list_parser.add_argument("--route", action="append", default=None)
    list_parser.add_argument("--alias", action="append", default=None)
    list_parser.add_argument("--limit", type=int, default=None)
    list_parser.add_argument("--no-local", action="store_true")
    list_parser.add_argument("--stream", action="store_true")

    subparsers.add_parser("active", help="show active local agent sessions as JSON")

    for command, help_text, id_type in (
        ("open", "open or resume a Codex session", engine._valid_thread_id),
        ("open-claude", "open or resume a Claude session", engine._valid_thread_id),
        ("open-opencode", "open or resume an OpenCode session", engine._valid_opencode_id),
    ):
        open_parser = subparsers.add_parser(command, help=help_text)
        open_parser.add_argument("--host", default=None)
        open_parser.add_argument("--window-host", default=None)
        open_parser.add_argument("--id", required=True, type=id_type)
        open_parser.add_argument("--name", default=None)
        open_parser.add_argument("--cwd", default=None)
        open_parser.add_argument("--terminal", default=None)
        open_parser.add_argument("--detach", action="store_true")

    refresh_parser = subparsers.add_parser("refresh", help="refresh the session cache")
    refresh_parser.add_argument("--background", action="store_true")
    return parser


def _apply_cli_config(config: PickerConfig, args: argparse.Namespace) -> PickerConfig:
    values: dict[str, object] = {}
    cli_limit: int | None = None
    if args.command == "list":
        if args.host is not None:
            values["hosts"] = args.host
            if args.route is None:
                # An explicit legacy host list is also the escape hatch from
                # file-configured routes for side-by-side diagnostics.
                values["host_route_values"] = []
        if args.route is not None:
            values["host_route_values"] = args.route
        if args.alias is not None:
            values["alias_values"] = args.alias
        if args.limit is not None:
            cli_limit = args.limit
    if args.ssh_connect_timeout is not None:
        values["ssh_connect_timeout"] = args.ssh_connect_timeout
    if args.ssh_connection_attempts is not None:
        values["ssh_connection_attempts"] = args.ssh_connection_attempts
    # A diagnostic --timeout remains a probe setting rather than a config
    # field; all other supported CLI values are handled here.
    config = config.with_overrides(**values)
    if cli_limit is not None:
        # The historical diagnostic CLI accepts up to 200 rows, while the
        # persisted DMS-compatible setting remains bounded at 100.
        if isinstance(cli_limit, bool) or not 1 <= cli_limit <= 200:
            raise engine.PickerError("limit must be between 1 and 200")
        config = replace(config, max_sessions=cli_limit)
    return config


def diagnostic_main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    refresh_store = CacheStore() if args.command == "refresh" else None
    try:
        config = _apply_cli_config(load_config(), args)
        timeout = args.timeout if args.timeout is not None else engine.DEFAULT_TIMEOUT
        if timeout <= 0:
            raise engine.PickerError("timeout must be positive")

        if args.command == "refresh":
            store = refresh_store or CacheStore()
            try:
                snapshot = store.refresh(config, force=True)
            finally:
                if args.background:
                    store.clear_background_marker()
            if not args.background:
                print(json.dumps(snapshot, separators=(",", ":")))
            return 0

        if args.command == "active":
            print(json.dumps(engine.active_snapshot(), separators=(",", ":")))
            return 0

        ssh_policy = config.ssh_policy
        if args.command == "list":
            if not 1 <= config.max_sessions <= 200:
                raise engine.PickerError("limit must be between 1 and 200")
            if args.stream:
                for event in engine.stream_session_events(
                    config.hosts,
                    config.max_sessions,
                    timeout,
                    include_local=not args.no_local,
                    aliases=config.aliases,
                    routes=config.routes,
                    ssh_policy=ssh_policy,
                ):
                    print(json.dumps(event, separators=(",", ":")), flush=True)
            else:
                payload = engine.aggregate_sessions(
                    config.hosts,
                    config.max_sessions,
                    timeout,
                    include_local=not args.no_local,
                    aliases=config.aliases,
                    routes=config.routes,
                    ssh_policy=ssh_policy,
                )
                print(json.dumps(payload, separators=(",", ":")))
            return 0

        host = args.host or "local"
        target = engine.resolve_host_target(engine.parse_host_target(host), ssh_policy)
        if args.command == "open-opencode":
            session = engine.resolve_opencode_open_target(
                target, args.id, args.name, args.cwd, timeout, ssh_policy
            )
        elif args.command == "open-claude":
            session = engine.resolve_claude_open_target(
                target, args.id, args.name, args.cwd, timeout, ssh_policy
            )
        else:
            session = engine.resolve_open_target(
                target, args.id, args.name, args.cwd, timeout, ssh_policy
            )
        window_host = args.window_host or target.connect_host or engine.socket.gethostname()
        if engine.focus_existing_window(session, window_host, timeout):
            return 0
        terminal = args.terminal or config.terminal
        engine.launch_attach(target, session, terminal, ssh_policy, detach=args.detach)
        return 0
    except (ConfigError, engine.PickerError) as exc:
        if refresh_store is not None and args.background:
            refresh_store.clear_background_marker()
        print(f"rofi-agent-plus: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # Rofi passes the visible row text as argv[1:] on a selection and marks
    # every callback with ROFI_RETV.  The selection identity is deliberately
    # taken from ROFI_INFO, never from that display text.  Diagnostic callers
    # should unset ROFI_RETV (as normal shells do).
    if "ROFI_RETV" in os.environ:
        return run_rofi()
    return diagnostic_main(argv)
