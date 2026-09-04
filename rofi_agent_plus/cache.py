"""Stale-while-revalidate cache for discovered agent sessions."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import engine
from .config import PickerConfig

# v2 adds the discovery authority identity.  A Host Mesh revision is part of
# cache validity, so a legacy or older-Mesh snapshot can never be consumed as
# current contract data.
CACHE_VERSION = 2
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
_BACKGROUND_OWNER_ENV = "ROFI_AGENT_PLUS_REFRESH_OWNER"


@dataclass(frozen=True)
class PresentationContext:
    """One capability/revision observation reused by a Rofi callback."""

    fingerprint: str
    backend: dict[str, object]
    error: str | None = None
    selected: object | None = None


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
        str(session.get("hostId") or session.get("windowHost") or session.get("host") or "local"),
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
    tmux_failed = "tmux" in failed_stages

    def retain_tmux(
        old: Mapping[str, Any],
        fresh: dict[str, Any],
        *,
        current_had_tmux: bool,
    ) -> None:
        """Keep old tmux evidence only when the current tmux stage failed.

        An authoritative ``ok``/empty or ``tmux_missing`` result has already
        disproved a fresh old association.  A failed/unreachable/error stage
        may retain it, but only with an explicit stale marker.
        """

        if tmux_failed:
            if isinstance(old.get("tmux"), dict) and "tmux" not in fresh:
                fresh["tmux"] = dict(old["tmux"])
                fresh["tmuxSession"] = old.get("tmuxSession")
            if "tmux" in fresh:
                fresh["tmuxStale"] = True
            return
        if not current_had_tmux:
            fresh.pop("tmux", None)
            fresh.pop("tmuxSession", None)
            fresh.pop("tmuxStale", None)

    if previous and isinstance(previous.get("sessions"), list):
        for old in previous["sessions"]:
            if not isinstance(old, dict):
                continue
            key = _as_session_key(old)
            provider = _provider_for_session(old)
            preserve = any(_PROVIDER_FOR_STAGE.get(stage) == provider for stage in failed_stages)
            fresh = current_by_key.get(key)
            current_had_tmux = fresh is not None and "tmux" in fresh
            if preserve and fresh is not None:
                # The provider's list/details stage failed, but activity is a
                # fresh independent observation.  Start from old provider
                # metadata and overlay only current identity/activity and
                # current tmux evidence; do not replace a useful name/cwd
                # with an active-probe placeholder.
                retained = dict(old)
                for field in (
                    "kind",
                    "id",
                    "host",
                    "hostId",
                    "windowHost",
                    "connectHost",
                    "route",
                    "contractMode",
                    "backend",
                    "active",
                    "activityState",
                ):
                    if field in fresh:
                        retained[field] = fresh[field]
                if "tmux" in fresh:
                    retained["tmux"] = fresh["tmux"]
                    retained["tmuxSession"] = fresh.get("tmuxSession")
                fresh.clear()
                fresh.update(retained)
            elif preserve and fresh is None:
                fresh = dict(old)
                current_sessions.append(fresh)
                current_by_key[key] = fresh
            if "active" in failed_stages and key in current_by_key:
                fresh = current_by_key[key]
                # Activity is independent process evidence.  It may retain
                # only its own state; an old tmux display name without the
                # matching current nested reference would be false authority.
                activity_fields = ("active", "activityState")
                # The contract path owns tmux authority separately.  Keep
                # legacy's pre-existing broad activity fallback unchanged as
                # its rollback behavior still carries that private field.
                if old.get("contractMode") is not True and fresh.get("contractMode") is not True:
                    activity_fields += ("tmuxSession",)
                for field in activity_fields:
                    if field in old:
                        fresh[field] = old[field]
            if key in current_by_key and (
                old.get("contractMode") is True or current_by_key[key].get("contractMode") is True
            ):
                retain_tmux(old, current_by_key[key], current_had_tmux=current_had_tmux)

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


def _backend_identity(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    kind = value.get("kind")
    capability = value.get("capability")
    revision = value.get("meshRevision")
    expected_capability = {
        "legacy": "legacy-v1",
        "contract": "host-mesh-v1+tmux-session-v1",
        "contract-error": "host-mesh-v1+tmux-session-v1",
    }.get(kind)
    if expected_capability is None or capability != expected_capability:
        return None
    if revision is not None and (
        not isinstance(revision, str)
        or not revision
        or revision.strip() != revision
        or any(char.isspace() or ord(char) < 32 for char in revision)
    ):
        return None
    if kind in {"legacy", "contract-error"} and revision is not None:
        return None
    if kind == "contract" and revision is None:
        return None
    return {"kind": kind, "capability": capability, "meshRevision": revision}


def _failed_contract_snapshot(
    config: PickerConfig,
    previous: Mapping[str, Any] | None,
    backend: Mapping[str, object],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    """Retain only compatible cache data without advancing freshness."""

    prior = (
        previous
        if previous is not None and _backend_identity(previous.get("backend")) == dict(backend)
        else None
    )
    hosts = dict(prior.get("hosts", {})) if isinstance(prior, Mapping) else {}
    snapshot = {
        "version": CACHE_VERSION,
        "fingerprint": config.fingerprint,
        # Never bless a partial/stale contract result as freshly generated.
        "generatedAt": int(prior.get("generatedAt", 0)) if isinstance(prior, Mapping) else 0,
        "backend": dict(backend),
        "hosts": hosts,
        "sessions": _flatten_hosts(hosts, config.max_sessions),
        "errors": _flatten_errors(hosts) + errors,
    }
    return snapshot


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
    refresh_errors: list[dict[str, str]] = []
    backend: dict[str, object] = {
        "kind": "legacy",
        "capability": "legacy-v1",
        "meshRevision": None,
    }
    expected_hosts: set[str] | None = None
    completed_hosts: set[str] = set()
    finished = False
    contract_aborted = False

    def preserve_previous(expected: set[str] | None = None) -> None:
        if not previous or not isinstance(previous.get("hosts"), dict):
            return
        for key, value in previous["hosts"].items():
            if (
                isinstance(key, str)
                and isinstance(value, dict)
                and (expected is None or key in expected)
            ):
                hosts[key] = dict(value)

    try:
        for event in events:
            if not isinstance(event, dict):
                refresh_errors.append(
                    {"host": "local", "stage": "refresh", "message": "malformed refresh event"}
                )
                continue
            kind = event.get("event")
            if kind == "refresh-started":
                declared = _backend_identity(event.get("backend"))
                if event.get("backend") is not None and declared is None:
                    refresh_errors.append(
                        {"host": "local", "stage": "refresh", "message": "invalid backend identity"}
                    )
                    contract_aborted = True
                    continue
                backend = declared or backend
                names = event.get("hosts")
                if (
                    not isinstance(names, list)
                    or any(not isinstance(name, str) or not name for name in names)
                    or len(set(names)) != len(names)
                ):
                    refresh_errors.append(
                        {"host": "local", "stage": "refresh", "message": "invalid refresh host set"}
                    )
                    contract_aborted = backend["kind"] == "contract"
                    continue
                expected_hosts = set(names)
                # Contract Mesh is authoritative for host membership.  Start
                # from compatible old rows only for currently declared hosts,
                # which prunes removed hosts before any cache merge.
                if backend["kind"] != "contract" or (
                    previous is not None and _backend_identity(previous.get("backend")) == backend
                ):
                    preserve_previous(expected_hosts if backend["kind"] == "contract" else None)
            elif kind == "host-complete":
                key = str(event.get("host") or "local")
                if expected_hosts is not None and key not in expected_hosts:
                    refresh_errors.append(
                        {"host": key, "stage": "refresh", "message": "unexpected refresh host"}
                    )
                    contract_aborted = backend["kind"] == "contract"
                    continue
                current = {
                    "generatedAt": int(event.get("generatedAt") or time.time()),
                    "sessions": event.get("sessions", []),
                    "errors": event.get("errors", []),
                }
                hosts[key] = _merge_host_snapshot(hosts.get(key), current)
                completed_hosts.add(key)
            elif kind == "refresh-finished":
                declared = _backend_identity(event.get("backend"))
                if event.get("backend") is not None and declared != backend:
                    refresh_errors.append(
                        {
                            "host": "local",
                            "stage": "refresh",
                            "message": "backend changed during refresh",
                        }
                    )
                    contract_aborted = backend["kind"] == "contract"
                finished = True
            else:
                refresh_errors.append(
                    {"host": "local", "stage": "refresh", "message": "unknown refresh event"}
                )
    except Exception as exc:  # preserve prior hosts if a refresh aborts unexpectedly
        refresh_errors.append({"host": "local", "stage": "refresh", "message": str(exc)})
        contract_aborted = backend["kind"] == "contract"

    if backend["kind"] == "contract" and (
        contract_aborted
        or not finished
        or expected_hosts is None
        or completed_hosts != expected_hosts
    ):
        if expected_hosts is not None and completed_hosts != expected_hosts:
            refresh_errors.append(
                {
                    "host": "local",
                    "stage": "refresh",
                    "message": "incomplete contract host coverage",
                }
            )
        return _failed_contract_snapshot(config, previous, backend, refresh_errors)

    generated_at = int(now if now is not None else time.time())
    errors = _flatten_errors(hosts)
    errors.extend(refresh_errors)
    return {
        "version": CACHE_VERSION,
        "fingerprint": config.fingerprint,
        "generatedAt": generated_at,
        "backend": backend,
        "hosts": hosts,
        "sessions": _flatten_hosts(hosts, config.max_sessions),
        "errors": errors,
    }


class CacheStore:
    """Own cache files and serialized refresh operations."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        backend_selector: Callable[[], object] | None = None,
    ) -> None:
        self.root = root or cache_root()
        self.snapshot_path = self.root / SNAPSHOT_NAME
        self.lock_path = self.root / LOCK_NAME
        self.background_path = self.root / BACKGROUND_MARKER_NAME
        self._backend_selector = backend_selector
        self._last_refresh_scope: dict[str, object] | None = None
        self._background_owner = os.environ.get(_BACKGROUND_OWNER_ENV)

    def ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _safe_mode(self.root, 0o700)

    def load(
        self,
        fingerprint: str | None = None,
        backend: Mapping[str, object] | None = None,
    ) -> dict[str, Any] | None:
        try:
            with self.snapshot_path.open(encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
            return None
        if fingerprint is not None and payload.get("fingerprint") != fingerprint:
            return None
        stored_backend = _backend_identity(payload.get("backend"))
        if stored_backend is None:
            return None
        if backend is not None and stored_backend != _backend_identity(backend):
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

    def _select_backend(self, *, deadline: float | None = None) -> tuple[object, dict[str, object]]:
        if self._backend_selector is None:
            from .contract_backend import select_backend

            selected = select_backend()
        else:
            selected = self._backend_selector()
        # Only the concrete public-contract consumer supports the optional
        # deadline parameter.  Keep injected legacy/fake seams byte-for-byte
        # compatible while ensuring a lifecycle authority re-check cannot
        # create a second independent Host Mesh budget.
        if deadline is not None:
            from .contract_backend import ContractBackend

            if isinstance(selected, ContractBackend):
                selected.prepare(deadline=deadline)
            else:
                selected.prepare()  # type: ignore[union-attr]
        else:
            selected.prepare()  # type: ignore[union-attr]
        identity = selected.identity  # type: ignore[union-attr]
        if not isinstance(identity, Mapping) or _backend_identity(identity) is None:
            raise engine.PickerError("Agent Plus backend returned an invalid identity")
        return selected, dict(identity)

    def presentation_context(self, config: PickerConfig) -> PresentationContext:
        """Resolve exactly one presentation authority for a callback."""

        try:
            selected, identity = self._select_backend()
        except Exception as error:
            return PresentationContext(
                config.fingerprint,
                {
                    "kind": "contract-error",
                    "capability": "host-mesh-v1+tmux-session-v1",
                    "meshRevision": None,
                },
                str(error)[:1024],
            )
        return PresentationContext(config.fingerprint, identity, selected=selected)

    def load_current(
        self,
        config: PickerConfig,
        context: PresentationContext | None = None,
    ) -> dict[str, Any] | None:
        """Load only data from the currently selected capability/revision."""

        context = context or self.presentation_context(config)
        return self.load(config.fingerprint, context.backend)

    def cache_scope(
        self,
        config: PickerConfig,
        context: PresentationContext | None = None,
    ) -> dict[str, object]:
        """Return the marker scope for the current capability observation."""

        context = context or self.presentation_context(config)
        return {"fingerprint": config.fingerprint, "backend": context.backend}

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

    def reconcile_contract_reference(
        self,
        config: PickerConfig,
        context: PresentationContext,
        *,
        host_id: str,
        kind: str,
        identifier: str,
        reference: Mapping[str, object],
        wait_seconds: float = 1.0,
        deadline: float | None = None,
    ) -> bool:
        """Atomically retain one successful public lifecycle descriptor.

        This is deliberately a narrow, non-authoritative cache update.  It
        neither manufactures provider data nor advances ``generatedAt``.  The
        cache is re-read under the lock and must still have the same capability
        and Mesh revision, so a result from an older authority cannot overwrite
        a newer discovery snapshot.
        """

        if context.error is not None or _backend_identity(context.backend) is None:
            return False
        wanted_backend = _backend_identity(context.backend)
        assert wanted_backend is not None
        wanted = (host_id, kind, identifier)
        wait_deadline = time.monotonic() + max(0.0, wait_seconds)
        if deadline is not None:
            wait_deadline = min(wait_deadline, deadline)
        while True:
            if time.monotonic() >= wait_deadline:
                return False
            with self.lock(blocking=False) as acquired:
                if acquired:
                    snapshot = self.load(config.fingerprint, wanted_backend)
                    if snapshot is None:
                        return False
                    # JSON round-tripping gives a deep copy without carrying
                    # references from callers into the atomic persisted value.
                    candidate = json.loads(json.dumps(snapshot, ensure_ascii=False))
                    rows = candidate.get("sessions")
                    hosts = candidate.get("hosts")
                    if not isinstance(rows, list) or not isinstance(hosts, dict):
                        return False

                    def matches(row: object) -> bool:
                        return (
                            isinstance(row, dict)
                            and row.get("contractMode") is True
                            and row.get("backend") == wanted_backend
                            and _as_session_key(row) == wanted
                        )

                    flattened = [row for row in rows if matches(row)]
                    host = hosts.get(host_id)
                    host_rows = host.get("sessions") if isinstance(host, dict) else None
                    retained = (
                        [row for row in host_rows if matches(row)]
                        if isinstance(host_rows, list)
                        else []
                    )
                    if len(flattened) != 1 or len(retained) != 1:
                        return False
                    for row in (*flattened, *retained):
                        assert isinstance(row, dict)
                        row["tmux"] = dict(reference)
                        row["tmuxSession"] = reference.get("observedName")
                        row.pop("tmuxStale", None)
                        row.pop("tmuxAmbiguous", None)
                    self.write(candidate)
                    return True
            if time.monotonic() >= wait_deadline:
                return False
            time.sleep(0.05)

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
        require_fresh: bool = False,
        wait_seconds: float = LOCK_WAIT_SECONDS,
        context: PresentationContext | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Synchronously refresh, with bounded lock waiting and safe fallback."""

        uses_selected_backend = discover is None
        selected_backend: object | None = None
        backend_identity: Mapping[str, object] | None = None
        if discover is None:
            if context is not None and context.error is not None:
                failure_backend = {
                    "kind": "contract-error",
                    "capability": "host-mesh-v1+tmux-session-v1",
                    "meshRevision": None,
                }
                snapshot = _failed_contract_snapshot(
                    config,
                    None,
                    failure_backend,
                    [{"host": "local", "stage": "contract", "message": context.error[:1024]}],
                )
                self.write(snapshot)
                self._last_refresh_scope = {
                    "fingerprint": config.fingerprint,
                    "backend": failure_backend,
                }
                return snapshot
            try:
                if context is not None:
                    selected_backend = context.selected
                    identity = context.backend
                    if selected_backend is None:
                        raise engine.PickerError("Agent Plus backend context is unavailable")
                else:
                    selected_backend, identity = self._select_backend()
            except Exception as error:  # never quietly fall back from a selected pair
                failure_backend = {
                    "kind": "contract-error",
                    "capability": "host-mesh-v1+tmux-session-v1",
                    "meshRevision": None,
                }
                snapshot = _failed_contract_snapshot(
                    config,
                    None,
                    failure_backend,
                    [{"host": "local", "stage": "contract", "message": str(error)[:1024]}],
                )
                self.write(snapshot)
                self._last_refresh_scope = {
                    "fingerprint": config.fingerprint,
                    "backend": failure_backend,
                }
                return snapshot
            backend_identity = dict(identity)

            def backend_discover(
                discovery_config: PickerConfig,
                _previous: Mapping[str, Any] | None,
            ) -> Iterator[dict[str, Any]]:
                assert selected_backend is not None
                if (
                    deadline is not None
                    and backend_identity is not None
                    and backend_identity.get("kind") == "contract"
                ):
                    return iter(selected_backend.stream(discovery_config, deadline=deadline))  # type: ignore[union-attr]
                return iter(selected_backend.stream(discovery_config))  # type: ignore[union-attr]

            discover = backend_discover
        previous = self.load(config.fingerprint, backend_identity)
        lock_deadline = time.monotonic() + wait_seconds
        if deadline is not None:
            lock_deadline = min(lock_deadline, deadline)
        while True:
            with self.lock(blocking=False) as acquired:
                if acquired:
                    # Re-read after acquiring: another process may have
                    # completed the refresh while we were waiting.
                    previous = self.load(config.fingerprint, backend_identity)
                    if not force and self.is_fresh(previous, config.refresh_seconds):
                        return previous or _empty_snapshot(config)
                    snapshot = build_snapshot(config, discover(config, previous), previous)
                    if uses_selected_backend:
                        # An old detached owner may finish after Mesh/capability
                        # changed.  Re-observe while still holding the mutation
                        # lock and never let its stale discovery overwrite the
                        # current authority's snapshot.
                        try:
                            _current_backend, current_identity = self._select_backend(
                                deadline=deadline
                            )
                        except Exception:
                            current_identity = {
                                "kind": "contract-error",
                                "capability": "host-mesh-v1+tmux-session-v1",
                                "meshRevision": None,
                            }
                        if _backend_identity(snapshot.get("backend")) != current_identity:
                            current = self.load(config.fingerprint, current_identity)
                            return current or {
                                "version": CACHE_VERSION,
                                "fingerprint": config.fingerprint,
                                "generatedAt": 0,
                                "backend": current_identity,
                                "hosts": {},
                                "sessions": [],
                                "errors": [
                                    {
                                        "host": "local",
                                        "stage": "contract",
                                        "message": "discovery authority changed; refresh again",
                                    }
                                ],
                            }
                    self.write(snapshot)
                    backend = _backend_identity(snapshot.get("backend"))
                    self._last_refresh_scope = (
                        {"fingerprint": config.fingerprint, "backend": backend}
                        if backend is not None
                        else None
                    )
                    return snapshot
            current = self.load(config.fingerprint, backend_identity)
            # Lifecycle actions must not reinterpret a lock-contended retained
            # snapshot as a synchronous revalidation.  A just-completed,
            # same-authority concurrent refresh is safe to consume; anything
            # older remains ordinary picker fallback data only.
            if current is not None and (
                (require_fresh and self.age(current) <= 1.0)
                or (not require_fresh and (not force or self.age(current) <= 1.0))
            ):
                return current
            if time.monotonic() >= lock_deadline:
                if require_fresh:
                    raise engine.PickerError(
                        "Agent Plus lifecycle revalidation is already in progress"
                    )
                if current is not None:
                    return current
                if previous is not None:
                    return previous
                raise engine.PickerError("Agent Plus refresh is already in progress")
            time.sleep(0.05)

    @staticmethod
    def _marker_matches(
        payload: object,
        scope: Mapping[str, object] | None,
        owner: str | None = None,
    ) -> bool:
        if scope is None and owner is None:
            # Preserve the legacy unscoped status seam; production Rofi passes
            # a typed capability scope and owned cleanup always passes owner.
            return True
        if not isinstance(payload, Mapping):
            return False
        if scope is not None and payload.get("scope") != dict(scope):
            return False
        return owner is None or payload.get("owner") == owner

    def _read_marker(self) -> object | None:
        try:
            with self.background_path.open(encoding="utf-8") as stream:
                return json.load(stream)
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _write_marker(self, payload: Mapping[str, object]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".refresh.", suffix=".tmp", dir=self.root)
        temporary_path = Path(temporary)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.background_path)
            _safe_mode(self.background_path, 0o600)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def _clear_marker_if_owned(self, owner: str, scope: Mapping[str, object] | None) -> None:
        payload = self._read_marker()
        if not self._marker_matches(payload, scope, owner):
            return
        try:
            self.background_path.unlink()
        except FileNotFoundError:
            pass

    def spawn_background(
        self,
        command: list[str],
        *,
        scope: Mapping[str, object] | None = None,
    ) -> bool:
        """Start at most one detached refresh process using a marker claim."""

        self.ensure_root()
        with self.lock(blocking=False) as acquired:
            if not acquired:
                return False
            payload = self._read_marker()
            try:
                stale = (
                    self.background_path.exists()
                    and time.time() - self.background_path.stat().st_mtime > LOCK_WAIT_SECONDS * 4
                )
            except OSError:
                stale = False
            if payload is not None and self._marker_matches(payload, scope) and not stale:
                return False
            # Replace atomically while holding the refresh lock.  There is no
            # unlink/create gap in which another callback can claim the same
            # scope.  A prior worker cannot clear this new marker because its
            # owner token differs.
            owner = secrets.token_hex(16)
            self._write_marker({"pid": os.getpid(), "scope": dict(scope or {}), "owner": owner})
        try:
            environment = os.environ.copy()
            for key in _ROFI_CALLBACK_ENVIRONMENT:
                environment.pop(key, None)
            environment[_BACKGROUND_OWNER_ENV] = owner
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=environment,
            )
        except OSError:
            self._clear_marker_if_owned(owner, scope)
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
        scope: Mapping[str, object] | None = None,
    ) -> bool:
        """Report whether a detached refresh marker is recent enough to poll."""

        if not self._marker_matches(self._read_marker(), scope):
            return False
        age = self.background_age(now)
        return age is not None and age <= max_age

    def clear_background_marker(
        self,
        *,
        scope: Mapping[str, object] | None = None,
        owner: str | None = None,
    ) -> None:
        if scope is None:
            scope = self._last_refresh_scope
        owner = owner or self._background_owner
        payload = self._read_marker()
        if owner is not None:
            if not self._marker_matches(payload, scope, owner):
                return
        elif isinstance(payload, Mapping) and payload.get("pid") != os.getpid():
            return
        try:
            self.background_path.unlink()
        except FileNotFoundError:
            pass

    def clear_owned_background_marker(self) -> None:
        """Clear only a marker whose authority this refresh observed."""

        if self._background_owner is not None:
            self.clear_background_marker(owner=self._background_owner)
        elif self._last_refresh_scope is not None:
            self.clear_background_marker(scope=self._last_refresh_scope)


def _empty_snapshot(config: PickerConfig) -> dict[str, Any]:
    return {
        "version": CACHE_VERSION,
        "fingerprint": config.fingerprint,
        "generatedAt": int(time.time()),
        "backend": {"kind": "legacy", "capability": "legacy-v1", "meshRevision": None},
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
