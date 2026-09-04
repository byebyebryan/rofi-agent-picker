"""Stale-while-revalidate cache for discovered agent sessions."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import engine
from .config import PickerConfig

CACHE_VERSION = 1
DEFAULT_CACHE_DIR = Path("rofi-agent-plus")
SNAPSHOT_NAME = "snapshot.json"
LOCK_NAME = "refresh.lock"
BACKGROUND_MARKER_NAME = "refresh.background"
LOCK_WAIT_SECONDS = 30.0
_PROVIDER_FOR_STAGE = {"threads": "codex", "claude": "claude", "opencode": "opencode"}
_ROFI_CALLBACK_ENVIRONMENT = (
    "ROFI_DATA",
    "ROFI_INFO",
    "ROFI_INPUT",
    "ROFI_OUTSIDE",
    "ROFI_RETV",
)


def cache_root() -> Path:
    value = os.environ.get("XDG_CACHE_HOME")
    root = Path(value) if value else Path.home() / ".cache"
    return root / DEFAULT_CACHE_DIR


def _safe_mode(path: Path, mode: int) -> None:
    try:
        current = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if current != mode:
        try:
            path.chmod(mode)
        except OSError:
            pass


def _as_session_key(session: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(session.get("windowHost") or session.get("host") or "local"),
        str(session.get("kind") or ""),
        str(session.get("id") or ""),
    )


def _provider_for_session(session: Mapping[str, Any]) -> str:
    kind = str(session.get("kind") or "")
    return kind if kind in {"codex", "claude", "opencode"} else ""


def _merge_host_snapshot(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge failed provider stages into the prior host snapshot.

    A successful provider result replaces its old rows.  Rows belonging to a
    provider that failed remain available until a later successful refresh.
    An activity failure preserves the last known activity fields while using
    current metadata for the row.
    """

    current_sessions = [
        dict(item) for item in current.get("sessions", []) if isinstance(item, dict)
    ]
    current_errors = [dict(item) for item in current.get("errors", []) if isinstance(item, dict)]
    failed_stages = {str(error.get("stage")) for error in current_errors if error.get("stage")}
    current_by_key = {_as_session_key(item): item for item in current_sessions}
    if previous and isinstance(previous.get("sessions"), list):
        for old in previous["sessions"]:
            if not isinstance(old, dict):
                continue
            key = _as_session_key(old)
            provider = _provider_for_session(old)
            preserve = any(_PROVIDER_FOR_STAGE.get(stage) == provider for stage in failed_stages)
            if "active" in failed_stages and key in current_by_key:
                fresh = current_by_key[key]
                for field in ("active", "activityState", "tmuxSession"):
                    if field in old:
                        fresh[field] = old[field]
            if preserve and key not in current_by_key:
                current_sessions.append(dict(old))

    current_sessions.sort(
        key=lambda item: (int(item.get("recencyAt") or 0), str(item.get("id") or "")),
        reverse=True,
    )
    result = {
        "generatedAt": int(current.get("generatedAt") or time.time()),
        "sessions": current_sessions,
        "errors": current_errors,
    }
    return result


def _flatten_hosts(hosts: Mapping[str, Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for host in hosts.values():
        for item in host.get("sessions", []):
            if not isinstance(item, dict):
                continue
            key = _as_session_key(item)
            if key in seen:
                continue
            seen.add(key)
            sessions.append(dict(item))
    sessions.sort(
        key=lambda item: (int(item.get("recencyAt") or 0), str(item.get("id") or "")),
        reverse=True,
    )
    return sessions[:limit]


def _flatten_errors(hosts: Mapping[str, Mapping[str, Any]]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for key, host in hosts.items():
        for error in host.get("errors", []):
            if not isinstance(error, dict):
                continue
            errors.append(
                {
                    "host": str(error.get("host") or key),
                    "stage": str(error.get("stage") or "refresh"),
                    "message": str(error.get("message") or "unknown error"),
                }
            )
    return errors


def build_snapshot(
    config: PickerConfig,
    events: Iterator[dict[str, Any]],
    previous: Mapping[str, Any] | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Build a versioned snapshot from the engine's per-host event stream."""

    hosts: dict[str, dict[str, Any]] = {}
    if previous and isinstance(previous.get("hosts"), dict):
        for key, value in previous["hosts"].items():
            if isinstance(key, str) and isinstance(value, dict):
                hosts[key] = dict(value)
    refresh_errors: list[dict[str, str]] = []
    try:
        for event in events:
            if not isinstance(event, dict):
                refresh_errors.append(
                    {"host": "local", "stage": "refresh", "message": "malformed refresh event"}
                )
                continue
            if event.get("event") == "host-complete":
                key = str(event.get("host") or "local")
                current = {
                    "generatedAt": int(event.get("generatedAt") or time.time()),
                    "sessions": event.get("sessions", []),
                    "errors": event.get("errors", []),
                }
                hosts[key] = _merge_host_snapshot(hosts.get(key), current)
            elif event.get("event") not in {"refresh-started", "refresh-finished"}:
                refresh_errors.append(
                    {"host": "local", "stage": "refresh", "message": "unknown refresh event"}
                )
    except Exception as exc:  # preserve prior hosts if a refresh aborts unexpectedly
        refresh_errors.append({"host": "local", "stage": "refresh", "message": str(exc)})

    generated_at = int(now if now is not None else time.time())
    errors = _flatten_errors(hosts)
    errors.extend(refresh_errors)
    return {
        "version": CACHE_VERSION,
        "fingerprint": config.fingerprint,
        "generatedAt": generated_at,
        "hosts": hosts,
        "sessions": _flatten_hosts(hosts, config.max_sessions),
        "errors": errors,
    }


class CacheStore:
    """Own cache files and serialized refresh operations."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or cache_root()
        self.snapshot_path = self.root / SNAPSHOT_NAME
        self.lock_path = self.root / LOCK_NAME
        self.background_path = self.root / BACKGROUND_MARKER_NAME

    def ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _safe_mode(self.root, 0o700)

    def load(self, fingerprint: str | None = None) -> dict[str, Any] | None:
        try:
            with self.snapshot_path.open(encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
            return None
        if fingerprint is not None and payload.get("fingerprint") != fingerprint:
            return None
        if (
            not isinstance(payload.get("sessions"), list)
            or not isinstance(payload.get("errors", []), list)
            or not isinstance(payload.get("hosts"), dict)
        ):
            return None
        for key, host in payload["hosts"].items():
            if not isinstance(key, str) or not isinstance(host, dict):
                return None
            if not isinstance(host.get("sessions", []), list) or not isinstance(
                host.get("errors", []), list
            ):
                return None
        _safe_mode(self.snapshot_path, 0o600)
        return payload

    def age(self, snapshot: Mapping[str, Any] | None, now: float | None = None) -> float:
        if not snapshot:
            return float("inf")
        try:
            generated = float(snapshot.get("generatedAt", 0))
        except (TypeError, ValueError):
            return float("inf")
        return max(0.0, (now if now is not None else time.time()) - generated)

    def is_fresh(
        self,
        snapshot: Mapping[str, Any] | None,
        refresh_seconds: int,
        now: float | None = None,
    ) -> bool:
        return self.age(snapshot, now) <= refresh_seconds

    def write(self, snapshot: Mapping[str, Any]) -> None:
        self.ensure_root()
        encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        descriptor, temporary = tempfile.mkstemp(prefix=".snapshot.", suffix=".tmp", dir=self.root)
        temporary_path = Path(temporary)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.snapshot_path)
            _safe_mode(self.snapshot_path, 0o600)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    @contextmanager
    def lock(self, blocking: bool = True) -> Iterator[bool]:
        self.ensure_root()
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            flags = fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(descriptor, flags)
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def refresh(
        self,
        config: PickerConfig,
        discover: Callable[[PickerConfig, Mapping[str, Any] | None], Iterator[dict[str, Any]]]
        | None = None,
        *,
        force: bool = False,
        wait_seconds: float = LOCK_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Synchronously refresh, with bounded lock waiting and safe fallback."""

        discover = discover or _discover
        previous = self.load(config.fingerprint)
        deadline = time.monotonic() + wait_seconds
        while True:
            with self.lock(blocking=False) as acquired:
                if acquired:
                    # Re-read after acquiring: another process may have
                    # completed the refresh while we were waiting.
                    previous = self.load(config.fingerprint)
                    if not force and self.is_fresh(previous, config.refresh_seconds):
                        return previous or _empty_snapshot(config)
                    snapshot = build_snapshot(config, discover(config, previous), previous)
                    self.write(snapshot)
                    return snapshot
            current = self.load(config.fingerprint)
            if current is not None and (not force or self.age(current) <= 1.0):
                return current
            if time.monotonic() >= deadline:
                if current is not None:
                    return current
                if previous is not None:
                    return previous
                raise engine.PickerError("Agent Plus refresh is already in progress")
            time.sleep(0.05)

    def spawn_background(self, command: list[str]) -> bool:
        """Start at most one detached refresh process using a marker claim."""

        self.ensure_root()
        try:
            descriptor = os.open(
                self.background_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            try:
                stale = time.time() - self.background_path.stat().st_mtime > LOCK_WAIT_SECONDS * 4
            except OSError:
                stale = False
            if stale:
                try:
                    self.background_path.unlink()
                except OSError:
                    return False
                return self.spawn_background(command)
            return False
        try:
            os.write(descriptor, str(os.getpid()).encode())
        finally:
            os.close(descriptor)
        try:
            environment = os.environ.copy()
            for key in _ROFI_CALLBACK_ENVIRONMENT:
                environment.pop(key, None)
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=environment,
            )
        except OSError:
            try:
                self.background_path.unlink()
            except OSError:
                pass
            return False
        return True

    def background_age(self, now: float | None = None) -> float | None:
        """Return the age of the detached-refresh marker, if it is valid."""

        try:
            metadata = self.background_path.stat()
        except OSError:
            return None
        if not stat.S_ISREG(metadata.st_mode):
            return None
        return max(0.0, (now if now is not None else time.time()) - metadata.st_mtime)

    def background_active(
        self,
        max_age: float = LOCK_WAIT_SECONDS * 4,
        now: float | None = None,
    ) -> bool:
        """Report whether a detached refresh marker is recent enough to poll."""

        age = self.background_age(now)
        return age is not None and age <= max_age

    def clear_background_marker(self) -> None:
        try:
            self.background_path.unlink()
        except FileNotFoundError:
            pass


def _empty_snapshot(config: PickerConfig) -> dict[str, Any]:
    return {
        "version": CACHE_VERSION,
        "fingerprint": config.fingerprint,
        "generatedAt": int(time.time()),
        "hosts": {},
        "sessions": [],
        "errors": [],
    }


def _discover(
    config: PickerConfig,
    _previous: Mapping[str, Any] | None,
) -> Iterator[dict[str, Any]]:
    yield from engine.stream_session_events(
        config.hosts,
        config.max_sessions,
        engine.DEFAULT_TIMEOUT,
        include_local=True,
        aliases=config.aliases,
        routes=config.routes,
        ssh_policy=config.ssh_policy,
    )
