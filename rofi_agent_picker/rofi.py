"""Rofi script-mode adapter for the standalone agent picker."""

from __future__ import annotations

import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from . import engine
from .cache import CacheStore
from .config import ConfigError, PickerConfig, load_config

ROFI_RETV_SELECTED = 1
ROFI_RETV_CUSTOM_1 = 10
ROFI_RETV_CUSTOM_2 = 11
ROFI_RETV_CUSTOM_3 = 12
ROFI_RETV_CUSTOM_4 = 13
ROFI_RETV_CUSTOM_5 = 14
ROFI_RETV_CUSTOM_6 = 15
ROFI_RETV_CUSTOM_19 = 28
MAX_MESSAGE_LENGTH = 360
FORCED_REFRESH_TIMEOUT_SECONDS = 30
AUTO_REFRESH_POLL_SECONDS = 1
AUTO_REFRESH_MAX_SECONDS = 30
AUTO_REFRESH_DATA_PREFIX = "background-refresh:"
AUTO_REFRESH_IDLE_DATA = "idle"
ERROR_NOTICE_SECONDS = 3
ERROR_NOTICE_DATA_PREFIX = "error-notice:"
NAVIGATION_DATA_PREFIX = "navigation:"
NAVIGATION_DATA_VERSION = 1
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_DISPLAY_CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")

PROVIDER_LABELS = {
    "codex": "Codex",
    "claude": "Claude Code",
    "opencode": "OpenCode",
}
PROVIDER_ORDER = ("codex", "claude", "opencode")
PROVIDER_SEARCH_TERMS = {
    "codex": "codex",
    "claude": "claude claude-code claude code",
    "opencode": "opencode open-code open code",
}
PROVIDER_ICON_PATHS = {
    kind: Path(__file__).resolve().parent / "assets" / "providers" / f"{kind}.svg"
    for kind in PROVIDER_LABELS
}
FALLBACK_ICON_PATH = Path(__file__).resolve().parent / "assets" / "providers" / "generic.svg"
HOST_GROUP_ICON = "network-server-symbolic"
ROW_SEPARATOR = "\n"
ROFI_RECORD_SEPARATOR = "\t"
ROFI_DELIMITER_VALUE = r"\t"


def sanitize(value: object) -> str:
    """Remove control characters that could corrupt Rofi's script protocol."""

    text = str(value) if value is not None else ""
    return _CONTROL_CHARS.sub(" ", text).strip()


@dataclass(frozen=True)
class NavigationState:
    """The view and optional group scope currently shown by the picker.

    ``view`` is always one of the three top-level views.  A host scope stores
    the displayed host label, while a provider scope stores its stable provider
    kind.  The constructor normalizes malformed values so untrusted
    continuation data can never create an invalid state.
    """

    view: str = "recent"
    scope_kind: str | None = None
    scope_value: str | None = None

    def __post_init__(self) -> None:
        view = (
            self.view
            if isinstance(self.view, str)
            and self.view
            in {
                "recent",
                "hosts",
                "providers",
            }
            else "recent"
        )
        scope_kind = self.scope_kind
        scope_value = sanitize(self.scope_value) if self.scope_value is not None else None
        if view == "recent":
            scope_kind = None
            scope_value = None
        elif not isinstance(scope_kind, str) or scope_kind not in {"host", "provider"}:
            scope_kind = None
            scope_value = None
        elif (view == "hosts" and scope_kind != "host") or (
            view == "providers" and scope_kind != "provider"
        ):
            scope_kind = None
            scope_value = None
        elif not scope_value:
            scope_kind = None
            scope_value = None
        elif len(scope_value) > 256:
            scope_kind = None
            scope_value = None
        elif scope_kind == "provider" and scope_value not in PROVIDER_LABELS:
            scope_kind = None
            scope_value = None

        object.__setattr__(self, "view", view)
        object.__setattr__(self, "scope_kind", scope_kind)
        object.__setattr__(self, "scope_value", scope_value)

    @property
    def nested(self) -> bool:
        return self.scope_kind is not None and self.scope_value is not None

    @property
    def is_default(self) -> bool:
        return self.view == "recent" and not self.nested

    def root(self) -> NavigationState:
        """Return the current top-level view without its group scope."""

        return NavigationState(self.view)


@dataclass(frozen=True)
class ContinuationState:
    """Rofi continuation data shared by navigation and refresh callbacks.

    Deadlines are kept in their wire-format form until :meth:`active` is
    called.  The timeout callback needs to see an expired deadline in order to
    clear it, while navigation callbacks only need the still-live portions.
    Keeping this small state object in one place also prevents a navigation
    callback from accidentally dropping a background refresh or notice.
    """

    navigation: NavigationState = NavigationState()
    refresh_deadline: float | None = None
    error_deadline: float | None = None
    error_message: str = ""

    @property
    def has_lifecycle(self) -> bool:
        return self.refresh_deadline is not None or self.error_deadline is not None

    def active(self, now: float | None = None) -> ContinuationState:
        """Return only unexpired refresh/notice components."""

        current = time.time() if now is None else now

        def live(deadline: float | None) -> float | None:
            if deadline is None:
                return None
            try:
                return deadline if math.isfinite(deadline) and deadline > current else None
            except (TypeError, ValueError, OverflowError):
                return None

        error_deadline = live(self.error_deadline)
        return ContinuationState(
            navigation=self.navigation,
            refresh_deadline=live(self.refresh_deadline),
            error_deadline=error_deadline,
            error_message=self.error_message if error_deadline is not None else "",
        )


def _protocol(key: str, value: object) -> str:
    return "\0" + key + "\x1f" + sanitize(value)


def _row_options(options: Sequence[tuple[str, object]]) -> str:
    """Encode all options for one row after a single NUL separator."""

    fields: list[str] = []
    for key, value in options:
        encoded_value = (
            _DISPLAY_CONTROL_CHARS.sub(" ", str(value)).strip()
            if key == "display"
            else sanitize(value)
        )
        fields.extend((sanitize(key), encoded_value))
    return "\0" + "\x1f".join(fields) if fields else ""


def _pango_escape(value: object) -> str:
    """Escape dynamic text before embedding it in row Pango markup."""

    # U+2028 is our intentional display line separator.  Do not let an input
    # value create an additional visual line, and keep the protocol itself
    # one physical LF-delimited row.
    text = sanitize(value).replace("\u0085", " ").replace("\u2028", " ").replace("\u2029", " ")
    return escape(text, quote=False)


def _shorten_cwd(value: object, width: int = 42) -> str:
    cwd = sanitize(value)
    if not cwd:
        return "~"
    home = str(Path.home())
    if cwd == home:
        cwd = "~"
    elif cwd.startswith(home + "/"):
        cwd = "~" + cwd[len(home) :]
    if len(cwd) <= width:
        return cwd
    return "…" + cwd[-(width - 1) :]


def _age(timestamp: object, now: float | None = None) -> str:
    try:
        numeric_timestamp = float(timestamp)
        if numeric_timestamp <= 0:
            return "unknown"
        seconds = max(0, int((now if now is not None else time.time()) - numeric_timestamp))
    except (TypeError, ValueError, OverflowError):
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 30:
        return f"{days}d"
    months = days // 30
    if months < 12:
        return f"{months}mo"
    return f"{days // 365}y"


def _session_key(session: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(session.get("windowHost") or session.get("host") or "local"),
        str(session.get("kind") or ""),
        str(session.get("id") or ""),
    )


def _recency_timestamp(value: object) -> float | None:
    """Return a usable positive recency timestamp, if one is present."""

    if isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(timestamp) or timestamp <= 0:
        return None
    return timestamp


def _session_sort_key(session: Mapping[str, Any]) -> tuple[object, ...]:
    """Sort sessions newest-first with deterministic, human-friendly ties."""

    timestamp = _recency_timestamp(session.get("recencyAt"))
    name = sanitize(session.get("name") or session.get("id") or "Agent")
    host = sanitize(session.get("host") or session.get("windowHost") or "local")
    kind = sanitize(session.get("kind") or "")
    identifier = sanitize(session.get("id") or "")
    return (
        0 if timestamp is not None else 1,
        -(timestamp or 0),
        name.casefold(),
        name,
        host.casefold(),
        host,
        kind.casefold(),
        kind,
        identifier.casefold(),
        identifier,
    )


def _valid_sessions(snapshot: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Copy renderable session rows while rejecting malformed identities."""

    rows = snapshot.get("sessions", []) if isinstance(snapshot, Mapping) else []
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("kind")
        identifier = item.get("id")
        if not isinstance(kind, str) or kind not in PROVIDER_LABELS:
            continue
        if not isinstance(identifier, str) or not identifier:
            continue
        result.append(dict(item))
    return result


def _session_host(session: Mapping[str, Any]) -> str:
    return sanitize(session.get("host") or session.get("windowHost") or "local") or "local"


def _newest_session_timestamp(sessions: Sequence[Mapping[str, Any]]) -> float | None:
    timestamps = [
        timestamp
        for timestamp in (_recency_timestamp(item.get("recencyAt")) for item in sessions)
        if timestamp is not None
    ]
    return max(timestamps) if timestamps else None


def _group_secondary(sessions: Sequence[Mapping[str, Any]], now: float | None = None) -> str:
    count = len(sessions)
    noun = "session" if count == 1 else "sessions"
    parts = [f"{count} {noun}"]
    active_count = sum(1 for item in sessions if item.get("active"))
    if active_count:
        parts.append(f"{active_count} active")
    newest = _newest_session_timestamp(sessions)
    parts.append(f"newest {_age(newest, now) if newest is not None else 'unknown'}")
    return "  ·  ".join(parts)


def _host_groups(sessions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for session in sessions:
        grouped.setdefault(_session_host(session), []).append(session)

    def sort_key(item: tuple[str, list[Mapping[str, Any]]]) -> tuple[object, ...]:
        label, members = item
        newest = _newest_session_timestamp(members)
        return (
            0 if newest is not None else 1,
            -(newest or 0),
            label.casefold(),
            label,
        )

    return [
        {"groupType": "host", "value": label, "label": label, "sessions": members}
        for label, members in sorted(grouped.items(), key=sort_key)
        if members
    ]


def _provider_groups(sessions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {kind: [] for kind in PROVIDER_ORDER}
    for session in sessions:
        kind = session.get("kind")
        if kind in grouped:
            grouped[kind].append(session)
    return [
        {
            "groupType": "provider",
            "value": kind,
            "label": PROVIDER_LABELS[kind],
            "sessions": members,
        }
        for kind in PROVIDER_ORDER
        if (members := grouped[kind])
    ]


def _sessions_for_navigation(
    sessions: Sequence[Mapping[str, Any]], navigation: NavigationState
) -> list[dict[str, Any]]:
    if not navigation.nested:
        return sorted((dict(item) for item in sessions), key=_session_sort_key)
    if navigation.scope_kind == "host":
        return sorted(
            (dict(item) for item in sessions if _session_host(item) == navigation.scope_value),
            key=_session_sort_key,
        )
    return sorted(
        (dict(item) for item in sessions if item.get("kind") == navigation.scope_value),
        key=_session_sort_key,
    )


def _breadcrumb(navigation: NavigationState) -> str:
    view_label = navigation.view.title()
    pieces = ["Agents", view_label]
    if navigation.nested:
        if navigation.scope_kind == "provider":
            label = PROVIDER_LABELS.get(navigation.scope_value or "", navigation.scope_value or "")
        else:
            label = navigation.scope_value or ""
        pieces.append(sanitize(label))
    return " › ".join(sanitize(piece) for piece in pieces)


def _navigation_data(navigation: NavigationState) -> str:
    payload: dict[str, object] = {
        "version": NAVIGATION_DATA_VERSION,
        "view": navigation.view,
    }
    if navigation.nested:
        payload["scopeType"] = navigation.scope_kind
        payload["scopeValue"] = navigation.scope_value
    encoded = quote(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), safe="")
    return NAVIGATION_DATA_PREFIX + encoded


def _parse_navigation_state(value: object) -> NavigationState:
    """Decode navigation state while accepting legacy refresh-only data."""

    if not isinstance(value, str):
        return NavigationState()
    for component in value.split(";"):
        if not component.startswith(NAVIGATION_DATA_PREFIX):
            continue
        encoded = component[len(NAVIGATION_DATA_PREFIX) :]
        if not encoded or len(encoded) > 4096:
            return NavigationState()
        try:
            payload = json.loads(unquote(encoded))
        except (UnicodeError, json.JSONDecodeError):
            return NavigationState()
        if not isinstance(payload, Mapping):
            return NavigationState()
        version = payload.get("version", NAVIGATION_DATA_VERSION)
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != NAVIGATION_DATA_VERSION
        ):
            return NavigationState()
        view = payload.get("view")
        if not isinstance(view, str) or view not in {"recent", "hosts", "providers"}:
            return NavigationState()
        scope_kind = payload.get("scopeType")
        scope_value = payload.get("scopeValue")
        if not isinstance(scope_kind, str) or not isinstance(scope_value, str):
            return NavigationState(view)
        if scope_kind == "host" and view == "hosts":
            return NavigationState(view, scope_kind, scope_value)
        if scope_kind == "provider" and view == "providers":
            return NavigationState(view, scope_kind, scope_value)
        return NavigationState(view)
    return NavigationState()


# Keep a descriptive public spelling for focused callers and tests while the
# private spelling makes it clear this parses untrusted Rofi input.
parse_navigation_state = _parse_navigation_state


def selection_payload(session: Mapping[str, Any]) -> str:
    """Encode a row's trusted selection identity for ``ROFI_INFO``."""

    payload = {
        key: session[key]
        for key in (
            "kind",
            "id",
            "name",
            "cwd",
            "host",
            "windowHost",
            "connectHost",
            "route",
        )
        if key in session
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _row_text(session: Mapping[str, Any], now: float | None = None) -> str:
    kind = str(session.get("kind") or "")
    provider = PROVIDER_LABELS.get(kind, kind.title() or "Agent")
    name = sanitize(session.get("name") or session.get("id") or "Agent")
    host = sanitize(session.get("host") or session.get("windowHost") or "local")
    cwd = _shorten_cwd(session.get("cwd"))
    age = _age(session.get("recencyAt"), now)
    activity = sanitize(
        session.get("activityState") or ("active" if session.get("active") else "idle")
    )
    return f"{name}  ·  {provider}  ·  {host}  ·  {cwd}  ·  {age}  ·  {activity}"


def _row_display(session: Mapping[str, Any], now: float | None = None) -> str:
    """Return the two-line Pango presentation for one session row."""

    name = sanitize(session.get("name") or session.get("id") or "Agent")
    host = sanitize(session.get("host") or session.get("windowHost") or "local")
    cwd = _shorten_cwd(session.get("cwd"))
    age = _age(session.get("recencyAt"), now)
    activity = sanitize(
        session.get("activityState") or ("active" if session.get("active") else "idle")
    )
    secondary = "  ·  ".join((host, cwd, age, activity))
    return (
        f"<b>{_pango_escape(name)}</b>"
        f'{ROW_SEPARATOR}<span size="smaller" alpha="75%">'
        f"{_pango_escape(secondary)}</span>"
    )


def _group_display(group: Mapping[str, Any], now: float | None = None) -> str:
    """Return the two-line Pango presentation for a host/provider group."""

    label = sanitize(group.get("label") or "Group")
    members = group.get("sessions", [])
    if not isinstance(members, Sequence):
        members = []
    sessions = [item for item in members if isinstance(item, Mapping)]
    return (
        f'<b>{_pango_escape(label)}</b><span alpha="60%">  ›</span>'
        f'{ROW_SEPARATOR}<span size="smaller" alpha="75%">'
        f"{_pango_escape(_group_secondary(sessions, now))}</span>"
    )


def _provider_icon(kind: str) -> str:
    """Resolve a bundled provider icon, with a bundled generic fallback."""

    candidate = PROVIDER_ICON_PATHS.get(kind)
    if candidate is not None and candidate.is_file():
        return str(candidate)
    if FALLBACK_ICON_PATH.is_file():
        return str(FALLBACK_ICON_PATH)
    return ""


def summarize_errors(errors: Sequence[object]) -> str:
    valid: list[str] = []
    for item in errors:
        if not isinstance(item, Mapping):
            continue
        host = sanitize(item.get("host") or "host")
        stage = sanitize(item.get("stage") or "refresh")
        message = sanitize(item.get("message") or "failed")
        valid.append(f"{host}/{stage}: {message}")
    if not valid:
        return ""
    joined = "Refresh errors: " + "; ".join(valid)
    return joined if len(joined) <= MAX_MESSAGE_LENGTH else joined[: MAX_MESSAGE_LENGTH - 1] + "…"


def _timeout_theme(
    delay: int | float | bool | None = None,
    *,
    enabled: bool | None = None,
) -> str:
    """Build the per-dialog timeout configuration used by script mode."""

    if delay is None:
        delay = bool(enabled)
    if isinstance(delay, bool):
        delay = AUTO_REFRESH_POLL_SECONDS if delay else 0
    normalized_delay = max(0, int(delay))
    return f'configuration {{ timeout {{ delay: {normalized_delay}; action: "kb-custom-19"; }} }}'


def _refresh_data(
    refresh_deadline: float | None = None,
    error_deadline: float | None = None,
    error_message: str = "",
    *,
    deadline: float | None = None,
    navigation: NavigationState | None = None,
) -> str:
    """Encode refresh, notice, and optional navigation state for Rofi."""

    if refresh_deadline is None:
        refresh_deadline = deadline
    values: list[str] = []
    if refresh_deadline is not None:
        values.append(f"{AUTO_REFRESH_DATA_PREFIX}{max(0, int(refresh_deadline))}")
    if error_deadline is not None:
        encoded_message = quote(sanitize(error_message), safe="")
        values.append(
            f"{ERROR_NOTICE_DATA_PREFIX}{max(0, int(error_deadline))}"
            f"{':' + encoded_message if encoded_message else ''}"
        )
    if navigation is not None and not navigation.is_default:
        values.append(_navigation_data(navigation))
    if not values:
        return AUTO_REFRESH_IDLE_DATA
    return ";".join(values)


def _parse_deadline(value: object, prefix: str) -> float | None:
    if not isinstance(value, str):
        return None
    for component in value.split(";"):
        if not component.startswith(prefix):
            continue
        raw_deadline = component[len(prefix) :]
        try:
            deadline = float(raw_deadline)
        except (TypeError, ValueError, OverflowError):
            return None
        return deadline if math.isfinite(deadline) and deadline > 0 else None
    return None


def _parse_refresh_deadline(value: object) -> float | None:
    return _parse_deadline(value, AUTO_REFRESH_DATA_PREFIX)


def _parse_error_notice(value: object) -> tuple[float | None, str]:
    if not isinstance(value, str):
        return None, ""
    for component in value.split(";"):
        if not component.startswith(ERROR_NOTICE_DATA_PREFIX):
            continue
        payload = component[len(ERROR_NOTICE_DATA_PREFIX) :]
        raw_deadline, separator, encoded_message = payload.partition(":")
        try:
            deadline = float(raw_deadline)
        except (TypeError, ValueError, OverflowError):
            return None, ""
        if not math.isfinite(deadline) or deadline <= 0:
            return None, ""
        if not separator:
            return deadline, ""
        try:
            return deadline, sanitize(unquote(encoded_message))
        except (UnicodeError, ValueError):
            return deadline, ""
    return None, ""


def _parse_continuation_state(value: object) -> ContinuationState:
    """Parse current and legacy Rofi continuation components together."""

    refresh_deadline = _parse_refresh_deadline(value)
    error_deadline, error_message = _parse_error_notice(value)
    return ContinuationState(
        navigation=_parse_navigation_state(value),
        refresh_deadline=refresh_deadline,
        error_deadline=error_deadline,
        error_message=error_message,
    )


# Keep a descriptive public spelling for focused callers and tests.
parse_continuation_state = _parse_continuation_state


def _render_continuation(
    snapshot: Mapping[str, Any] | None,
    state: ContinuationState,
    *,
    navigation: NavigationState | None = None,
    preserve: bool = False,
    clear_message: bool = True,
    continuation: bool = True,
) -> str:
    """Render navigation while retaining live refresh/notice state.

    ``ROFI_DATA`` is the only state that survives a script callback.  A
    navigation transition changes the prompt and rows, but must not silently
    stop a background worker or lose its bounded error notice.  Expired
    components are deliberately omitted and a timeout of zero clears Rofi's
    old timeout/theme when the last continuation has ended.
    """

    active = state.active()
    target = navigation or state.navigation
    if active.error_deadline is not None:
        message = active.error_message
    elif active.refresh_deadline is not None:
        message = "Refreshing in background"
    else:
        message = ""
    if active.has_lifecycle:
        timeout: bool | None = True
    elif state.has_lifecycle:
        # The callback received a continuation, but all its deadlines have
        # elapsed.  Explicitly clear the old timeout and carry only the new
        # navigation scope forward.
        timeout = False
    else:
        timeout = None
    return render_snapshot(
        snapshot,
        message=message,
        preserve=preserve,
        timeout=timeout,
        refresh_deadline=active.refresh_deadline,
        error_deadline=active.error_deadline,
        clear_message=clear_message,
        continuation=continuation,
        navigation=target,
    )


def render_snapshot(
    snapshot: Mapping[str, Any] | None,
    *,
    message: str = "",
    selected: Mapping[str, Any] | None = None,
    preserve: bool = False,
    now: float | None = None,
    continuation: bool = False,
    timeout: bool | None = None,
    refresh_deadline: float | None = None,
    error_deadline: float | None = None,
    clear_message: bool = False,
    navigation: NavigationState | None = None,
) -> str:
    """Render a snapshot as Rofi script headers and rows."""

    navigation_was_provided = navigation is not None
    navigation = navigation or NavigationState()
    sessions = _valid_sessions(snapshot)
    headers = [
        _protocol("prompt", _breadcrumb(navigation)),
        _protocol("no-custom", "true"),
        _protocol("use-hot-keys", "true"),
        _protocol("markup-rows", "true"),
    ]
    if preserve or selected is not None:
        # Rofi preserves the current filter and cursor across a script
        # callback when these headers are present.  This is especially useful
        # when a stale selection failed to open.
        headers.extend([_protocol("keep-selection", "true"), _protocol("keep-filter", "true")])
    effective_message = sanitize(message)
    if not effective_message and isinstance(snapshot, Mapping) and not clear_message:
        effective_message = summarize_errors(snapshot.get("errors", []))
    if effective_message or clear_message:
        headers.append(_protocol("message", effective_message))
    if timeout is not None:
        if timeout:
            if refresh_deadline is None and error_deadline is None:
                refresh_deadline = time.time() + AUTO_REFRESH_MAX_SECONDS
            if refresh_deadline is not None:
                timeout_delay = AUTO_REFRESH_POLL_SECONDS
            elif error_deadline is not None:
                timeout_delay = max(1, math.ceil(error_deadline - time.time()))
            else:
                timeout_delay = AUTO_REFRESH_POLL_SECONDS
        else:
            timeout_delay = 0
        headers.append(_protocol("theme", _timeout_theme(timeout_delay)))
        headers.append(
            _protocol(
                "data",
                _refresh_data(
                    refresh_deadline if timeout else None,
                    error_deadline if timeout else None,
                    effective_message if timeout and error_deadline is not None else "",
                    navigation=navigation,
                ),
            )
        )
    elif navigation_was_provided:
        # Continuation callbacks without a timeout (navigation and opening
        # failures) still need to carry the active scope to the next callback.
        # Explicitly emit ``idle`` for Recent so an older nested value cannot
        # leak across a root transition if Rofi retains the previous data.
        headers.append(_protocol("data", _refresh_data(navigation=navigation)))

    rendered_rows: list[str] = []
    emitted = 0
    if navigation.nested or navigation.view == "recent":
        rows = _sessions_for_navigation(sessions, navigation)
        for session in rows:
            kind = str(session.get("kind") or "")
            info = selection_payload(session)
            search_metadata = " ".join(
                (
                    *(
                        sanitize(session.get(field) or "")
                        for field in (
                            "name",
                            "kind",
                            "host",
                            "windowHost",
                            "connectHost",
                            "cwd",
                            "activityState",
                        )
                    ),
                    PROVIDER_LABELS[kind],
                    PROVIDER_SEARCH_TERMS[kind],
                )
            )
            options: list[tuple[str, object]] = [
                ("info", info),
                ("meta", search_metadata),
                ("icon", _provider_icon(kind)),
                ("display", _row_display(session, now)),
            ]
            if session.get("active"):
                options.append(("active", "true"))
            rendered_rows.append(_row_text(session, now) + _row_options(options))
            emitted += 1
    else:
        groups = (
            _host_groups(sessions) if navigation.view == "hosts" else _provider_groups(sessions)
        )
        for group in groups:
            group_type = str(group["groupType"])
            value = str(group["value"])
            label = sanitize(group.get("label") or value)
            info = json.dumps(
                {
                    "type": "group",
                    "groupType": group_type,
                    "value": value,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if group_type == "provider":
                search_metadata = " ".join(
                    (label, value, PROVIDER_LABELS[value], PROVIDER_SEARCH_TERMS[value])
                )
                icon = _provider_icon(value)
            else:
                search_metadata = " ".join((label, "host", "hosts"))
                icon = HOST_GROUP_ICON
            options = [
                ("info", info),
                ("meta", search_metadata),
                ("icon", icon),
                ("display", _group_display(group, now)),
            ]
            if any(bool(item.get("active")) for item in group["sessions"]):
                options.append(("active", "true"))
            rendered_rows.append(label + _row_options(options))
            emitted += 1

    if emitted == 0:
        status = "No agent sessions found"
        if effective_message:
            status = "No sessions · " + effective_message
        rendered_rows.append(status + _row_options([("nonselectable", "true"), ("urgent", "true")]))

    if continuation:
        return ROFI_RECORD_SEPARATOR.join((*headers, *rendered_rows)) + ROFI_RECORD_SEPARATOR

    # Rofi starts every script-mode process with LF records.  Change its
    # remembered delimiter in the final LF header, then use tabs for rows so a
    # literal newline can create a second visual line inside ``display``.
    headers.append(_protocol("delim", ROFI_DELIMITER_VALUE))
    return (
        "\n".join(headers)
        + "\n"
        + ROFI_RECORD_SEPARATOR.join(rendered_rows)
        + ROFI_RECORD_SEPARATOR
    )


def _parse_selection(raw: str | None) -> dict[str, Any]:
    if not raw:
        raise engine.PickerError("Rofi did not provide a session selection")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise engine.PickerError("Rofi selection metadata is invalid") from exc
    if not isinstance(payload, dict):
        raise engine.PickerError("Rofi selection metadata is not an object")
    kind = payload.get("kind")
    identifier = payload.get("id")
    if kind not in PROVIDER_LABELS or not isinstance(identifier, str):
        raise engine.PickerError("Rofi selection metadata is incomplete")
    if any(char in identifier for char in "\x00\n\r\t"):
        raise engine.PickerError("Rofi selection contains invalid control characters")
    if kind == "codex" or kind == "claude":
        if not engine.UUID_PATTERN.fullmatch(identifier):
            raise engine.PickerError("Rofi selection contains an invalid session id")
    elif not engine.OPENCODE_ID_PATTERN.fullmatch(identifier):
        raise engine.PickerError("Rofi selection contains an invalid session id")
    return payload


def _parse_group_selection(raw: str | None) -> dict[str, Any]:
    if not raw:
        raise engine.PickerError("Rofi did not provide a group selection")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise engine.PickerError("Rofi group metadata is invalid") from exc
    if not isinstance(payload, dict) or payload.get("type") != "group":
        raise engine.PickerError("Rofi group metadata is incomplete")
    group_type = payload.get("groupType")
    value = payload.get("value")
    if (
        not isinstance(group_type, str)
        or group_type not in {"host", "provider"}
        or not isinstance(value, str)
        or not value
    ):
        raise engine.PickerError("Rofi group metadata is incomplete")
    if any(char in value for char in "\x00\n\r\t") or len(value) > 256:
        raise engine.PickerError("Rofi group contains invalid text")
    if group_type == "provider" and value not in PROVIDER_LABELS:
        raise engine.PickerError("Rofi group contains an invalid provider")
    return payload


def _parse_row_selection(raw: str | None) -> tuple[str, dict[str, Any]]:
    """Parse typed row metadata without ever deriving identity from display text."""

    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and payload.get("type") == "group":
            return "group", _parse_group_selection(raw)
    return "session", _parse_selection(raw)


def _enter_group(
    navigation: NavigationState,
    group: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
) -> NavigationState:
    """Validate a selected root group against the current snapshot."""

    group_type = group.get("groupType")
    value = group.get("value")
    if (
        navigation.nested
        or not isinstance(group_type, str)
        or group_type
        not in {
            "host",
            "provider",
        }
    ):
        raise engine.PickerError("selected group is not available in this view")
    if group_type == "host" and navigation.view != "hosts":
        raise engine.PickerError("selected host is not available in this view")
    if group_type == "provider" and navigation.view != "providers":
        raise engine.PickerError("selected provider is not available in this view")
    sessions = _valid_sessions(snapshot)
    groups = _host_groups(sessions) if group_type == "host" else _provider_groups(sessions)
    if not any(item.get("value") == value for item in groups):
        raise engine.PickerError("selected group is no longer available")
    return NavigationState(navigation.view, group_type, value)


def _cycled_root(navigation: NavigationState, direction: int) -> NavigationState:
    views = ("recent", "hosts", "providers")
    try:
        index = views.index(navigation.view)
    except ValueError:
        index = 0
    return NavigationState(views[(index + direction) % len(views)])


def _open_selection(
    selection: Mapping[str, Any],
    config: PickerConfig,
    timeout: float = engine.DEFAULT_TIMEOUT,
) -> None:
    route_value = selection.get("route")
    host_value = route_value or selection.get("connectHost") or "local"
    if not isinstance(host_value, str):
        raise engine.PickerError("selected session has an invalid host")
    target = engine.resolve_host_target(engine.parse_host_target(host_value), config.ssh_policy)
    identifier = str(selection["id"])
    name = selection.get("name") if isinstance(selection.get("name"), str) else None
    cwd = selection.get("cwd") if isinstance(selection.get("cwd"), str) else None
    kind = str(selection["kind"])
    if kind == "codex":
        session = engine.resolve_open_target(
            target, identifier, name, cwd, timeout, config.ssh_policy
        )
    elif kind == "claude":
        session = engine.resolve_claude_open_target(
            target, identifier, name, cwd, timeout, config.ssh_policy
        )
    else:
        session = engine.resolve_opencode_open_target(
            target, identifier, name, cwd, timeout, config.ssh_policy
        )
    window_host = selection.get("windowHost")
    if not isinstance(window_host, str) or not window_host:
        window_host = target.connect_host or socket.gethostname()
    if engine.focus_existing_window(session, window_host, timeout):
        return
    # Rofi is itself the foreground UI.  The terminal must be detached so
    # returning from this function lets Rofi close immediately.
    engine.launch_attach(target, session, config.terminal, config.ssh_policy, detach=True)


def _background_command() -> list[str]:
    entrypoint = Path(__file__).resolve().parents[1] / "bin" / "rofi-agent-picker"
    if entrypoint.is_file():
        return [sys.executable, str(entrypoint), "refresh", "--background"]
    return [sys.executable, "-m", "rofi_agent_picker", "refresh", "--background"]


def _message_for_cache(
    store: CacheStore,
    snapshot: Mapping[str, Any],
    config: PickerConfig,
    *,
    fresh: bool | None = None,
) -> str:
    errors = summarize_errors(snapshot.get("errors", []))
    if fresh is None:
        fresh = store.is_fresh(snapshot, config.refresh_seconds)
    if not fresh:
        prefix = "Refreshing in background"
        return prefix + (" · " + errors if errors else "")
    return errors


def _render_error_notice(
    snapshot: Mapping[str, Any] | None,
    message: str,
    *,
    preserve: bool = False,
    continuation: bool = False,
    refresh_deadline: float | None = None,
    error_deadline: float | None = None,
    navigation: NavigationState | None = None,
) -> str:
    """Render a user-visible error with a bounded, self-clearing timeout."""

    return render_snapshot(
        snapshot,
        message=message,
        preserve=preserve,
        timeout=True,
        refresh_deadline=refresh_deadline,
        error_deadline=error_deadline or time.time() + ERROR_NOTICE_SECONDS,
        clear_message=True,
        continuation=continuation,
        navigation=navigation,
    )


def _start_background_refresh(
    store: CacheStore,
) -> tuple[bool, float | None]:
    """Claim the detached refresh and return whether polling should be enabled."""

    try:
        started = bool(store.spawn_background(_background_command()))
    except OSError:
        started = False
    if started:
        return True, time.time() + AUTO_REFRESH_MAX_SECONDS
    # Another picker invocation may already own the marker.  Continue polling
    # that worker, but never start a second one from this path.
    if store.background_active(max_age=AUTO_REFRESH_MAX_SECONDS):
        return True, time.time() + AUTO_REFRESH_MAX_SECONDS
    return False, None


def _auto_refresh_callback(
    environ: Mapping[str, str],
    store: CacheStore,
    config: PickerConfig,
) -> str:
    """Inspect cache state for the timeout callback without doing discovery."""

    snapshot = store.load(config.fingerprint)
    fresh = snapshot is not None and store.is_fresh(snapshot, config.refresh_seconds)
    rofi_data = environ.get("ROFI_DATA")
    continuation_state = _parse_continuation_state(rofi_data)
    navigation = continuation_state.navigation
    error_deadline = continuation_state.error_deadline
    error_message = continuation_state.error_message
    now = time.time()
    if fresh:
        errors = summarize_errors(snapshot.get("errors", []))
        if errors:
            # A completed background refresh can introduce errors after the
            # original one-second polling deadline was encoded.  Start a new
            # bounded notice for those current errors, then keep that same
            # deadline across subsequent callbacks.
            if error_deadline is not None and now >= error_deadline and error_message == errors:
                return render_snapshot(
                    snapshot,
                    preserve=True,
                    timeout=False,
                    clear_message=True,
                    continuation=True,
                    navigation=navigation,
                )
            if error_deadline is None or error_message != errors:
                error_deadline = now + ERROR_NOTICE_SECONDS
            if now < error_deadline:
                return render_snapshot(
                    snapshot,
                    message=errors,
                    preserve=True,
                    timeout=True,
                    error_deadline=error_deadline,
                    clear_message=True,
                    continuation=True,
                    navigation=navigation,
                )
            return render_snapshot(
                snapshot,
                preserve=True,
                timeout=False,
                clear_message=True,
                continuation=True,
                navigation=navigation,
            )

        # Foreground operation failures carry their message in continuation
        # data.  Keep that notice visible until its own deadline even when
        # the cache itself has no refresh errors.
        if error_deadline is not None and now < error_deadline and error_message:
            return render_snapshot(
                snapshot,
                message=error_message,
                preserve=True,
                timeout=True,
                error_deadline=error_deadline,
                clear_message=True,
                continuation=True,
                navigation=navigation,
            )
        return render_snapshot(
            snapshot,
            preserve=True,
            timeout=False,
            clear_message=True,
            continuation=True,
            navigation=navigation,
        )

    deadline = continuation_state.refresh_deadline
    timed_out = deadline is not None and now >= deadline
    marker_active = not timed_out and store.background_active(max_age=AUTO_REFRESH_MAX_SECONDS)
    if marker_active:
        if deadline is None:
            deadline = now + AUTO_REFRESH_MAX_SECONDS
        if snapshot is None:
            background_message = "Refreshing in background"
        else:
            # Errors in a stale snapshot belong to the previous refresh.  A
            # current error notice is emitted once the new worker snapshot is
            # fresh, so keep the polling status unambiguous here.
            background_message = "Refreshing in background"
        notice_active = error_deadline is not None and now < error_deadline and bool(error_message)
        return render_snapshot(
            snapshot,
            message=error_message if notice_active else background_message,
            preserve=True,
            timeout=True,
            refresh_deadline=deadline,
            error_deadline=error_deadline if notice_active else None,
            continuation=True,
            navigation=navigation,
        )

    # The worker writes the snapshot before removing its marker.  If both
    # operations happen between the first load and the marker check, take one
    # final read so a completed refresh wins over the stale stopped state.
    latest = store.load(config.fingerprint)
    if latest is not None and store.is_fresh(latest, config.refresh_seconds):
        latest_message = summarize_errors(latest.get("errors", []))
        if latest_message:
            return _render_error_notice(
                latest,
                latest_message,
                preserve=True,
                continuation=True,
                navigation=navigation,
            )
        return render_snapshot(
            latest,
            preserve=True,
            timeout=False,
            clear_message=True,
            continuation=True,
            navigation=navigation,
        )

    if error_deadline is not None and now < error_deadline and error_message:
        return render_snapshot(
            snapshot,
            message=error_message,
            preserve=True,
            timeout=True,
            error_deadline=error_deadline,
            clear_message=True,
            continuation=True,
            navigation=navigation,
        )
    return render_snapshot(
        snapshot,
        preserve=True,
        timeout=False,
        clear_message=True,
        continuation=True,
        navigation=navigation,
    )


def _forced_refresh(store: CacheStore, config: PickerConfig) -> dict[str, Any]:
    """Refresh with a hard foreground bound for the Alt+R callback."""

    if not hasattr(signal, "SIGALRM"):
        return store.refresh(config, force=True)

    def timeout_handler(_signum: int, _frame: object) -> None:
        raise engine.PickerError("refresh timed out")

    previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, FORCED_REFRESH_TIMEOUT_SECONDS)
    try:
        return store.refresh(config, force=True)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def run_rofi(
    environ: Mapping[str, str] | None = None,
    *,
    store: CacheStore | None = None,
    config: PickerConfig | None = None,
) -> int:
    """Process one Rofi script invocation."""

    environ = environ or os.environ
    try:
        retv = int(environ.get("ROFI_RETV", "0") or "0")
    except ValueError:
        retv = 0

    continuation_state = _parse_continuation_state(environ.get("ROFI_DATA"))
    navigation = continuation_state.navigation
    store = store or CacheStore()
    if retv == ROFI_RETV_CUSTOM_6 and not navigation.nested:
        # Escape is an unconditional root-level exit, including when loading
        # the configuration would otherwise produce an error row.
        return 0
    try:
        config = config or load_config()
    except ConfigError as exc:
        if retv == ROFI_RETV_CUSTOM_19:
            now = time.time()
            active = continuation_state.active(now)
            if (
                continuation_state.error_deadline is not None
                and continuation_state.error_deadline <= now
            ):
                # Preserve the old bounded-notice contract: once its deadline
                # has elapsed, clear it rather than starting another notice
                # merely because config loading still fails.  A still-live
                # background worker remains visible and keeps its poll alive.
                rendered = render_snapshot(
                    None,
                    message="Refreshing in background" if active.refresh_deadline else "",
                    preserve=True,
                    timeout=True if active.refresh_deadline else False,
                    refresh_deadline=active.refresh_deadline,
                    clear_message=True,
                    continuation=True,
                    navigation=navigation,
                )
            else:
                # A config failure is a new operation error.  Do not let an
                # unrelated active refresh (or its old notice text) hide it;
                # carry the refresh deadline alongside a fresh bounded notice.
                rendered = _render_error_notice(
                    None,
                    str(exc)
                    if active.refresh_deadline
                    else (continuation_state.error_message or str(exc)),
                    preserve=True,
                    continuation=True,
                    refresh_deadline=active.refresh_deadline,
                    error_deadline=active.error_deadline,
                    navigation=navigation,
                )
        else:
            rendered = _render_error_notice(
                None,
                str(exc),
                preserve=retv != 0,
                continuation=retv != 0,
                refresh_deadline=continuation_state.active().refresh_deadline,
                navigation=navigation,
            )
        print(rendered, end="")
        return 0

    if retv in {2, 3}:
        # ``no-custom`` normally prevents these callbacks.  If a user has a
        # global Rofi binding that still emits one, keep the list intact and
        # tell Rofi to preserve its current cursor/filter instead of treating
        # it as a mutation request.
        selected = None
        if retv == 3:
            try:
                selected = _parse_selection(environ.get("ROFI_INFO"))
            except engine.PickerError:
                selected = None
        snapshot = store.load(config.fingerprint)
        notice = "Custom input is disabled" if retv == 2 else "Deletion is disabled"
        print(
            render_snapshot(
                snapshot,
                message=notice,
                selected=selected,
                preserve=True,
                continuation=True,
                navigation=navigation,
            ),
            end="",
        )
        return 0

    if retv == ROFI_RETV_SELECTED:
        selected: dict[str, Any] | None = None
        row_type = "session"
        try:
            row_type, selected = _parse_row_selection(environ.get("ROFI_INFO"))
            if row_type == "group":
                snapshot = store.load(config.fingerprint)
                next_navigation = _enter_group(navigation, selected, snapshot)
                print(
                    _render_continuation(
                        snapshot,
                        continuation_state,
                        navigation=next_navigation,
                    ),
                    end="",
                )
                return 0
            _open_selection(selected, config)
            # No rows means Rofi closes after a successful action.
            return 0
        except (engine.PickerError, OSError, subprocess.SubprocessError) as exc:
            snapshot = store.load(config.fingerprint)
            operation = "open session" if row_type == "session" else "navigate"
            print(
                _render_error_notice(
                    snapshot,
                    message=f"Unable to {operation}: {sanitize(exc)}",
                    preserve=True,
                    continuation=True,
                    refresh_deadline=continuation_state.active().refresh_deadline,
                    navigation=navigation,
                ),
                end="",
            )
            return 0

    if retv in {ROFI_RETV_CUSTOM_2, ROFI_RETV_CUSTOM_3, ROFI_RETV_CUSTOM_4, ROFI_RETV_CUSTOM_5}:
        # Horizontal navigation always changes the top-level lens.  Nested
        # groups therefore switch directly to the adjacent root and discard
        # their scope, filter, and cursor while retaining live continuation
        # state.
        direction = 1 if retv in {ROFI_RETV_CUSTOM_2, ROFI_RETV_CUSTOM_4} else -1
        snapshot = store.load(config.fingerprint)
        print(
            _render_continuation(
                snapshot,
                continuation_state,
                navigation=_cycled_root(navigation, direction),
            ),
            end="",
        )
        return 0

    if retv == ROFI_RETV_CUSTOM_6:
        # Escape is Back inside a group and Exit at a root.  Returning no
        # records is the Rofi script-mode close signal, so do not render a
        # replacement list for the root case.
        snapshot = store.load(config.fingerprint)
        print(
            _render_continuation(
                snapshot,
                continuation_state,
                navigation=navigation.root(),
            ),
            end="",
        )
        return 0

    if retv == ROFI_RETV_CUSTOM_19:
        print(_auto_refresh_callback(environ, store, config), end="")
        return 0

    if retv == ROFI_RETV_CUSTOM_1:
        try:
            snapshot = _forced_refresh(store, config)
            fresh = store.is_fresh(snapshot, config.refresh_seconds)
            polling = False
            deadline = None
            if not fresh and store.background_active(max_age=AUTO_REFRESH_MAX_SECONDS):
                polling = True
                deadline = _parse_refresh_deadline(environ.get("ROFI_DATA"))
                if deadline is None:
                    deadline = time.time() + AUTO_REFRESH_MAX_SECONDS
            message = _message_for_cache(store, snapshot, config, fresh=fresh)
            if fresh and message:
                print(
                    _render_error_notice(
                        snapshot,
                        message,
                        preserve=True,
                        continuation=True,
                        refresh_deadline=continuation_state.active().refresh_deadline,
                        navigation=navigation,
                    ),
                    end="",
                )
                return 0
            if not fresh and not polling:
                message = summarize_errors(snapshot.get("errors", []))
            elif not fresh:
                # Errors in this snapshot belong to the previous refresh;
                # current provider errors are reported when the new snapshot
                # completes and receive their own bounded notice.
                message = "Refreshing in background"
            if not fresh and not polling and message:
                print(
                    _render_error_notice(
                        snapshot,
                        message,
                        preserve=True,
                        continuation=True,
                        refresh_deadline=continuation_state.active().refresh_deadline,
                        navigation=navigation,
                    ),
                    end="",
                )
                return 0
            print(
                render_snapshot(
                    snapshot,
                    message=message,
                    preserve=True,
                    timeout=polling,
                    refresh_deadline=deadline,
                    clear_message=True,
                    continuation=True,
                    navigation=navigation,
                ),
                end="",
            )
        except (engine.PickerError, OSError, subprocess.SubprocessError) as exc:
            snapshot = store.load(config.fingerprint)
            print(
                _render_error_notice(
                    snapshot,
                    message=f"Refresh failed: {sanitize(exc)}",
                    preserve=True,
                    continuation=True,
                    refresh_deadline=continuation_state.active().refresh_deadline,
                    navigation=navigation,
                ),
                end="",
            )
        return 0

    snapshot = store.load(config.fingerprint)
    polling = False
    refresh_deadline = None
    if snapshot is None:
        try:
            snapshot = store.refresh(config)
        except (engine.PickerError, OSError, subprocess.SubprocessError) as exc:
            print(
                _render_error_notice(
                    None,
                    f"Refresh failed: {sanitize(exc)}",
                    refresh_deadline=continuation_state.active().refresh_deadline,
                    navigation=navigation,
                ),
                end="",
            )
            return 0
    else:
        fresh = store.is_fresh(snapshot, config.refresh_seconds)
        if not fresh:
            polling, refresh_deadline = _start_background_refresh(store)
            if not polling:
                # If another worker finished between the first load and the
                # marker check, show its fresh snapshot instead of stopping
                # with rows that are already obsolete.
                latest = store.load(config.fingerprint)
                if latest is not None and store.is_fresh(latest, config.refresh_seconds):
                    latest_message = summarize_errors(latest.get("errors", []))
                    print(
                        _render_error_notice(latest, latest_message, navigation=navigation)
                        if latest_message
                        else render_snapshot(latest, navigation=navigation),
                        end="",
                    )
                    return 0
            message = (
                "Refreshing in background"
                if polling
                else summarize_errors(snapshot.get("errors", []))
            )
            if not polling and message:
                print(_render_error_notice(snapshot, message, navigation=navigation), end="")
                return 0
            print(
                render_snapshot(
                    snapshot,
                    message=message,
                    timeout=True if polling else None,
                    refresh_deadline=refresh_deadline,
                    navigation=navigation,
                ),
                end="",
            )
            return 0
    message = _message_for_cache(store, snapshot, config)
    if message:
        print(_render_error_notice(snapshot, message, navigation=navigation), end="")
    else:
        print(render_snapshot(snapshot, navigation=navigation), end="")
    return 0
