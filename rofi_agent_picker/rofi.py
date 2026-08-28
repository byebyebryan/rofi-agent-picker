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
from html import escape
from pathlib import Path
from typing import Any

from . import engine
from .cache import CacheStore
from .config import ConfigError, PickerConfig, load_config

ROFI_RETV_SELECTED = 1
ROFI_RETV_CUSTOM_1 = 10
ROFI_RETV_CUSTOM_19 = 28
MAX_MESSAGE_LENGTH = 360
FORCED_REFRESH_TIMEOUT_SECONDS = 30
AUTO_REFRESH_POLL_SECONDS = 1
AUTO_REFRESH_MAX_SECONDS = 30
AUTO_REFRESH_DATA_PREFIX = "background-refresh:"
AUTO_REFRESH_IDLE_DATA = "idle"
AUTO_REFRESH_STOPPED_MESSAGE = "Background refresh stopped · press Alt+R to retry"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_DISPLAY_CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")

PROVIDER_LABELS = {
    "codex": "Codex",
    "claude": "Claude Code",
    "opencode": "OpenCode",
}
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
ROW_SEPARATOR = "\n"
ROFI_RECORD_SEPARATOR = "\t"
ROFI_DELIMITER_VALUE = r"\t"


def sanitize(value: object) -> str:
    """Remove control characters that could corrupt Rofi's script protocol."""

    text = str(value) if value is not None else ""
    return _CONTROL_CHARS.sub(" ", text).strip()


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


def _timeout_theme(enabled: bool) -> str:
    """Build the per-dialog timeout configuration used by script mode."""

    delay = AUTO_REFRESH_POLL_SECONDS if enabled else 0
    return f'configuration {{ timeout {{ delay: {delay}; action: "kb-custom-19"; }} }}'


def _refresh_data(deadline: float | None) -> str:
    """Encode the bounded polling deadline in Rofi's continuation data."""

    if deadline is None:
        return AUTO_REFRESH_IDLE_DATA
    return f"{AUTO_REFRESH_DATA_PREFIX}{max(0, int(deadline))}"


def _parse_refresh_deadline(value: object) -> float | None:
    if not isinstance(value, str) or not value.startswith(AUTO_REFRESH_DATA_PREFIX):
        return None
    raw_deadline = value[len(AUTO_REFRESH_DATA_PREFIX) :]
    try:
        deadline = float(raw_deadline)
    except (TypeError, ValueError, OverflowError):
        return None
    return deadline if math.isfinite(deadline) and deadline > 0 else None


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
    clear_message: bool = False,
) -> str:
    """Render a snapshot as Rofi script headers and rows."""

    rows = snapshot.get("sessions", []) if isinstance(snapshot, Mapping) else []
    if not isinstance(rows, list):
        rows = []
    headers = [
        _protocol("prompt", "Agents"),
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
    if not effective_message and isinstance(snapshot, Mapping):
        effective_message = summarize_errors(snapshot.get("errors", []))
    if effective_message or clear_message:
        headers.append(_protocol("message", effective_message))
    if timeout is not None:
        headers.append(_protocol("theme", _timeout_theme(timeout)))
        if timeout and refresh_deadline is None:
            refresh_deadline = time.time() + AUTO_REFRESH_MAX_SECONDS
        headers.append(_protocol("data", _refresh_data(refresh_deadline if timeout else None)))

    rendered_rows: list[str] = []
    emitted = 0
    for session in rows:
        if not isinstance(session, Mapping):
            continue
        # JSON ensures selection data is not taken from display text.  Invalid
        # rows are simply omitted; the status row below keeps Rofi usable.
        kind = str(session.get("kind") or "")
        identifier = str(session.get("id") or "")
        if kind not in PROVIDER_LABELS or not identifier:
            continue
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
    if fresh:
        return render_snapshot(
            snapshot,
            message=summarize_errors(snapshot.get("errors", [])),
            preserve=True,
            timeout=False,
            clear_message=True,
            continuation=True,
        )

    deadline = _parse_refresh_deadline(environ.get("ROFI_DATA"))
    timed_out = deadline is not None and time.time() >= deadline
    marker_active = not timed_out and store.background_active(max_age=AUTO_REFRESH_MAX_SECONDS)
    if marker_active:
        if snapshot is None:
            message = "Refreshing in background"
        else:
            message = _message_for_cache(store, snapshot, config, fresh=False)
        return render_snapshot(
            snapshot,
            message=message,
            preserve=True,
            timeout=True,
            refresh_deadline=deadline,
            continuation=True,
        )

    # The worker writes the snapshot before removing its marker.  If both
    # operations happen between the first load and the marker check, take one
    # final read so a completed refresh wins over the stale stopped state.
    latest = store.load(config.fingerprint)
    if latest is not None and store.is_fresh(latest, config.refresh_seconds):
        return render_snapshot(
            latest,
            message=summarize_errors(latest.get("errors", [])),
            preserve=True,
            timeout=False,
            clear_message=True,
            continuation=True,
        )

    return render_snapshot(
        snapshot,
        message=AUTO_REFRESH_STOPPED_MESSAGE,
        preserve=True,
        timeout=False,
        continuation=True,
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

    store = store or CacheStore()
    try:
        config = config or load_config()
    except ConfigError as exc:
        print(
            render_snapshot(
                None,
                message=str(exc),
                timeout=False if retv != 0 else None,
                continuation=retv != 0,
            ),
            end="",
        )
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
            ),
            end="",
        )
        return 0

    if retv == ROFI_RETV_SELECTED:
        selected: dict[str, Any] | None = None
        try:
            selected = _parse_selection(environ.get("ROFI_INFO"))
            _open_selection(selected, config)
            # No rows means Rofi closes after a successful action.
            return 0
        except (engine.PickerError, OSError, subprocess.SubprocessError) as exc:
            snapshot = store.load(config.fingerprint)
            print(
                render_snapshot(
                    snapshot,
                    message=f"Unable to open session: {sanitize(exc)}",
                    selected=selected,
                    preserve=True,
                    continuation=True,
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
            if not fresh and not polling:
                message = AUTO_REFRESH_STOPPED_MESSAGE
            print(
                render_snapshot(
                    snapshot,
                    message=message,
                    preserve=True,
                    timeout=polling,
                    refresh_deadline=deadline,
                    clear_message=True,
                    continuation=True,
                ),
                end="",
            )
        except (engine.PickerError, OSError, subprocess.SubprocessError) as exc:
            snapshot = store.load(config.fingerprint)
            print(
                render_snapshot(
                    snapshot,
                    message=f"Refresh failed: {sanitize(exc)}",
                    preserve=True,
                    timeout=False,
                    continuation=True,
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
            print(render_snapshot(None, message=f"Refresh failed: {sanitize(exc)}"), end="")
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
                    print(
                        render_snapshot(
                            latest,
                            message=summarize_errors(latest.get("errors", [])),
                        ),
                        end="",
                    )
                    return 0
            message = (
                _message_for_cache(store, snapshot, config, fresh=False)
                if polling
                else AUTO_REFRESH_STOPPED_MESSAGE
            )
            print(
                render_snapshot(
                    snapshot,
                    message=message,
                    timeout=True if polling else None,
                    refresh_deadline=refresh_deadline,
                ),
                end="",
            )
            return 0
    print(render_snapshot(snapshot, message=_message_for_cache(store, snapshot, config)), end="")
    return 0
