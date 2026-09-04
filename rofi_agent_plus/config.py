"""Strict configuration loading for the Rofi Agent Plus.

The DMS plugin historically stored these values as loosely typed plugin
settings.  The standalone picker uses TOML so configuration errors can be
reported to the user instead of silently changing which hosts are queried.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .engine import (
    DEFAULT_LIMIT,
    DEFAULT_SSH_CONNECT_TIMEOUT,
    DEFAULT_SSH_CONNECTION_ATTEMPTS,
    HostTarget,
    PickerError,
    SshPolicy,
    parse_host_aliases,
    parse_host_routes,
)

DEFAULT_REFRESH_SECONDS = 30
MIN_MAX_SESSIONS = 1
MAX_MAX_SESSIONS = 100
MIN_REFRESH_SECONDS = 5
MAX_REFRESH_SECONDS = 300
MIN_SSH_CONNECT_TIMEOUT = 1
MAX_SSH_CONNECT_TIMEOUT = 30
MIN_SSH_CONNECTION_ATTEMPTS = 1
MAX_SSH_CONNECTION_ATTEMPTS = 5
CONFIG_RELATIVE_PATH = Path("rofi-agent-plus") / "config.toml"
CONFIG_KEYS = frozenset(
    {
        "host_routes",
        "hosts",
        "aliases",
        "terminal",
        "max_sessions",
        "refresh_seconds",
        "ssh_connect_timeout",
        "ssh_connection_attempts",
    }
)


class ConfigError(PickerError):
    """A user-facing configuration error."""


def _config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / CONFIG_RELATIVE_PATH


def _list_of_strings(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"config {key} must be an array of strings")
    values = tuple(item.strip() for item in value if item.strip())
    return values


def _override_strings(value: Any, key: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ConfigError(f"config {key} must be an array of strings")
    if any(not isinstance(item, str) for item in value):
        raise ConfigError(f"config {key} must be an array of strings")
    return tuple(item.strip() for item in value if item.strip())


def _integer(value: Any, key: str, minimum: int, maximum: int) -> int:
    # bool is a subclass of int, but is never a meaningful setting here.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"config {key} must be an integer")
    if value < minimum or value > maximum:
        raise ConfigError(f"config {key} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class PickerConfig:
    """Resolved picker settings.

    ``routes`` and ``aliases`` are parsed once so discovery and opening use
    exactly the same validation rules.  ``hosts`` is retained as a tuple for
    the legacy host-list mode; routes take precedence over it.
    """

    hosts: tuple[str, ...] = ()
    host_route_values: tuple[str, ...] = ()
    alias_values: tuple[str, ...] = ()
    terminal: str = "ghostty"
    max_sessions: int = DEFAULT_LIMIT
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS
    ssh_connect_timeout: int = DEFAULT_SSH_CONNECT_TIMEOUT
    ssh_connection_attempts: int = DEFAULT_SSH_CONNECTION_ATTEMPTS

    @property
    def routes(self) -> tuple[HostTarget, ...]:
        try:
            return tuple(parse_host_routes(self.host_route_values))
        except PickerError as exc:
            raise ConfigError(str(exc)) from exc

    @property
    def host_routes(self) -> tuple[str, ...]:
        """Raw route specifications, exposed under the TOML field name."""

        return self.host_route_values

    @property
    def aliases(self) -> dict[str, str]:
        try:
            return parse_host_aliases(self.alias_values)
        except PickerError as exc:
            raise ConfigError(str(exc)) from exc

    def validate(self) -> None:
        """Validate route and alias syntax eagerly."""

        try:
            parse_host_routes(self.host_route_values)
            parse_host_aliases(self.alias_values)
        except PickerError as exc:
            raise ConfigError(str(exc)) from exc

    @property
    def ssh_policy(self) -> SshPolicy:
        return SshPolicy(self.ssh_connect_timeout, self.ssh_connection_attempts)

    @property
    def fingerprint(self) -> str:
        """Fingerprint settings that can change discovered sessions.

        The terminal and cache TTL do not affect discovery and therefore do
        not invalidate a snapshot.  The configured session limit does affect
        which rows are present, so it is included.
        """

        payload = {
            "hosts": self.hosts,
            "host_routes": self.host_route_values,
            "aliases": self.alias_values,
            "max_sessions": self.max_sessions,
            "ssh_connect_timeout": self.ssh_connect_timeout,
            "ssh_connection_attempts": self.ssh_connection_attempts,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def with_overrides(self, **values: Any) -> PickerConfig:
        """Return a validated config with non-``None`` values replaced."""

        candidate = self
        if values.get("hosts") is not None:
            hosts = values["hosts"]
            candidate = replace(
                candidate,
                hosts=_override_strings(hosts, "hosts"),
            )
        if values.get("host_route_values") is not None:
            routes = values["host_route_values"]
            candidate = replace(
                candidate,
                host_route_values=_override_strings(routes, "host_routes"),
            )
        if values.get("alias_values") is not None:
            aliases = values["alias_values"]
            candidate = replace(
                candidate,
                alias_values=_override_strings(aliases, "aliases"),
            )
        if values.get("terminal") is not None:
            if not isinstance(values["terminal"], str):
                raise ConfigError("config terminal must be a non-empty string")
            terminal = values["terminal"].strip()
            if not terminal:
                raise ConfigError("terminal command is empty")
            candidate = replace(candidate, terminal=terminal)
        for field, minimum, maximum in (
            ("max_sessions", MIN_MAX_SESSIONS, MAX_MAX_SESSIONS),
            ("refresh_seconds", MIN_REFRESH_SECONDS, MAX_REFRESH_SECONDS),
            ("ssh_connect_timeout", MIN_SSH_CONNECT_TIMEOUT, MAX_SSH_CONNECT_TIMEOUT),
            (
                "ssh_connection_attempts",
                MIN_SSH_CONNECTION_ATTEMPTS,
                MAX_SSH_CONNECTION_ATTEMPTS,
            ),
        ):
            value = values.get(field)
            if value is not None:
                value = _integer(value, field, minimum, maximum)
                candidate = replace(candidate, **{field: value})
        # Force parsing of user-provided route and alias values before the
        # config leaves this module.  This makes all callers fail visibly.
        candidate.validate()
        return candidate


def load_config(path: Path | None = None) -> PickerConfig:
    """Load the optional strict TOML config, or return local-only defaults."""

    path = path or _config_path()
    if not path.exists():
        return PickerConfig(terminal=os.environ.get("TERMINAL", "ghostty"))
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a TOML table")
    unknown = sorted(set(raw) - CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)}")

    hosts = _list_of_strings(raw["hosts"], "hosts") if "hosts" in raw else ()
    routes = _list_of_strings(raw["host_routes"], "host_routes") if "host_routes" in raw else ()
    aliases = _list_of_strings(raw["aliases"], "aliases") if "aliases" in raw else ()
    terminal = raw.get("terminal", os.environ.get("TERMINAL", "ghostty"))
    if not isinstance(terminal, str) or not terminal.strip():
        raise ConfigError("config terminal must be a non-empty string")

    config = PickerConfig(
        hosts=hosts,
        host_route_values=routes,
        alias_values=aliases,
        terminal=terminal.strip(),
        max_sessions=(
            _integer(raw["max_sessions"], "max_sessions", MIN_MAX_SESSIONS, MAX_MAX_SESSIONS)
            if "max_sessions" in raw
            else DEFAULT_LIMIT
        ),
        refresh_seconds=(
            _integer(
                raw["refresh_seconds"],
                "refresh_seconds",
                MIN_REFRESH_SECONDS,
                MAX_REFRESH_SECONDS,
            )
            if "refresh_seconds" in raw
            else DEFAULT_REFRESH_SECONDS
        ),
        ssh_connect_timeout=(
            _integer(
                raw["ssh_connect_timeout"],
                "ssh_connect_timeout",
                MIN_SSH_CONNECT_TIMEOUT,
                MAX_SSH_CONNECT_TIMEOUT,
            )
            if "ssh_connect_timeout" in raw
            else DEFAULT_SSH_CONNECT_TIMEOUT
        ),
        ssh_connection_attempts=(
            _integer(
                raw["ssh_connection_attempts"],
                "ssh_connection_attempts",
                MIN_SSH_CONNECTION_ATTEMPTS,
                MAX_SSH_CONNECTION_ATTEMPTS,
            )
            if "ssh_connection_attempts" in raw
            else DEFAULT_SSH_CONNECTION_ATTEMPTS
        ),
    )
    config.validate()
    return config


def config_from_mapping(values: Mapping[str, Any]) -> PickerConfig:
    """Build config from a mapping, primarily for deterministic tests."""

    unknown = sorted(set(values) - CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)}")
    temporary = PickerConfig()
    return temporary.with_overrides(
        hosts=_list_of_strings(values["hosts"], "hosts") if "hosts" in values else None,
        host_route_values=(
            _list_of_strings(values["host_routes"], "host_routes")
            if "host_routes" in values
            else None
        ),
        alias_values=(
            _list_of_strings(values["aliases"], "aliases") if "aliases" in values else None
        ),
        terminal=values.get("terminal"),
        max_sessions=values.get("max_sessions"),
        refresh_seconds=values.get("refresh_seconds"),
        ssh_connect_timeout=values.get("ssh_connect_timeout"),
        ssh_connection_attempts=values.get("ssh_connection_attempts"),
    )
