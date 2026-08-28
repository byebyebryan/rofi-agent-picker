"""Rofi script-mode adapter for the standalone agent picker."""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import engine
from .cache import CacheStore
from .config import ConfigError, PickerConfig, load_config

ROFI_RETV_SELECTED = 1
ROFI_RETV_CUSTOM_1 = 10
MAX_MESSAGE_LENGTH = 360
FORCED_REFRESH_TIMEOUT_SECONDS = 30
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

PROVIDER_LABELS = {
    "codex": "Codex",
    "claude": "Claude",
    "opencode": "OpenCode",
}
ACTIVE_ICON = "utilities-terminal-symbolic"
HISTORY_ICON = "document-open-recent-symbolic"


def sanitize(value: object) -> str:
    """Remove control characters that could corrupt Rofi's script protocol."""

    text = str(value) if value is not None else ""
    return _CONTROL_CHARS.sub(" ", text).strip()


def _protocol(key: str, value: object) -> str:
    return "\0" + key + "\x1f" + sanitize(value)


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


def render_snapshot(
    snapshot: Mapping[str, Any] | None,
    *,
    message: str = "",
    selected: Mapping[str, Any] | None = None,
    preserve: bool = False,
    now: float | None = None,
) -> str:
    """Render a snapshot as Rofi script headers and rows."""

    rows = snapshot.get("sessions", []) if isinstance(snapshot, Mapping) else []
    if not isinstance(rows, list):
        rows = []
    output = [
        _protocol("prompt", "Agents"),
        _protocol("no-custom", "true"),
        _protocol("use-hot-keys", "true"),
    ]
    if preserve or selected is not None:
        # Rofi preserves the current filter and cursor across a script
        # callback when these headers are present.  This is especially useful
        # when a stale selection failed to open.
        output.extend([_protocol("keep-selection", "true"), _protocol("keep-filter", "true")])
    effective_message = sanitize(message)
    if not effective_message and isinstance(snapshot, Mapping):
        effective_message = summarize_errors(snapshot.get("errors", []))
    if effective_message:
        output.append(_protocol("message", effective_message))

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
        metadata = [
            _protocol("info", info),
            _protocol(
                "meta",
                " ".join(
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
            ),
            _protocol("icon", ACTIVE_ICON if session.get("active") else HISTORY_ICON),
        ]
        if session.get("active"):
            metadata.append(_protocol("active", "true"))
        output.append(_row_text(session, now) + "".join(metadata))
        emitted += 1

    if emitted == 0:
        status = "No agent sessions found"
        if effective_message:
            status = "No sessions · " + effective_message
        output.append(status + _protocol("nonselectable", "true") + _protocol("urgent", "true"))
    return "\n".join(output) + "\n"


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


def _message_for_cache(store: CacheStore, snapshot: Mapping[str, Any], config: PickerConfig) -> str:
    errors = summarize_errors(snapshot.get("errors", []))
    if not store.is_fresh(snapshot, config.refresh_seconds):
        prefix = "Refreshing in background"
        return prefix + (" · " + errors if errors else "")
    return errors


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
    store = store or CacheStore()
    try:
        config = config or load_config()
    except ConfigError as exc:
        print(render_snapshot(None, message=str(exc)), end="")
        return 0

    try:
        retv = int(environ.get("ROFI_RETV", "0") or "0")
    except ValueError:
        retv = 0

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
        print(render_snapshot(snapshot, message=notice, selected=selected, preserve=True), end="")
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
                ),
                end="",
            )
            return 0

    if retv == ROFI_RETV_CUSTOM_1:
        try:
            snapshot = _forced_refresh(store, config)
            print(render_snapshot(snapshot, preserve=True), end="")
        except (engine.PickerError, OSError, subprocess.SubprocessError) as exc:
            snapshot = store.load(config.fingerprint)
            print(
                render_snapshot(
                    snapshot,
                    message=f"Refresh failed: {sanitize(exc)}",
                    preserve=True,
                ),
                end="",
            )
        return 0

    snapshot = store.load(config.fingerprint)
    if snapshot is None:
        try:
            snapshot = store.refresh(config)
        except (engine.PickerError, OSError, subprocess.SubprocessError) as exc:
            print(render_snapshot(None, message=f"Refresh failed: {sanitize(exc)}"), end="")
            return 0
    elif not store.is_fresh(snapshot, config.refresh_seconds):
        store.spawn_background(_background_command())
    print(render_snapshot(snapshot, message=_message_for_cache(store, snapshot, config)), end="")
    return 0
