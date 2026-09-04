"""Public Tmux Session v1 lifecycle consumer for Agent Plus.

This module deliberately consumes only the public ``rofi-tmux-plus`` JSON
commands.  It owns no SSH, terminal, Niri, or raw tmux behavior; those remain
behind the Tmux Session boundary.  The legacy engine never enters this module.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from . import engine
from .cache import CacheStore, PresentationContext
from .config import PickerConfig
from .contract_backend import CommandOutput

_SCHEMA_VERSION = 1
_CAPABILITY = "host-mesh-v1+tmux-session-v1"
_MAX_FIELD = 4096
_MAX_OUTPUT = 256 * 1024
_LIFECYCLE_SECONDS = 30.0
_MAX_CREATE_COLLISIONS = 8
_SESSION_ID = re.compile(r"\$[0-9]+", re.ASCII)
_HOST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*", re.ASCII)
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", re.ASCII)
_ERROR_CODES = frozenset(
    {
        "unknown_host",
        "stale_mesh",
        "host_unreachable",
        "tmux_missing",
        "session_not_found",
        "session_exists",
        "stale_session",
        "invalid_input",
        "invalid_cwd",
        "launch_failed",
        "operation_failed",
    }
)
_PROVIDER_OPTIONS = {
    "codex": ("@codex_thread_id", "@codex_name", "codex", "resume"),
    "claude": ("@claude_session_id", "@claude_name", "claude", "--resume"),
    "opencode": ("@opencode_session_id", "@opencode_name", "opencode", "--session"),
}


class LifecycleError(engine.PickerError):
    """A bounded, typed failure returned by Tmux Session v1."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _ContractBackend(Protocol):
    tmux_command: str

    def _run(self, argv: Sequence[str], **kwargs: object) -> CommandOutput: ...


@dataclass(frozen=True)
class StableReference:
    host_id: str
    mesh_revision: str
    server_generation: str
    session_id: str
    created_at: int
    observed_name: str | None

    def payload(self) -> dict[str, object]:
        return {
            "meshRevision": self.mesh_revision,
            "serverGeneration": self.server_generation,
            "sessionId": self.session_id,
            "createdAt": self.created_at,
            "observedName": self.observed_name,
        }


def _text(value: object, label: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_FIELD
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise LifecycleError("operation_failed", f"Tmux Session {label} is invalid")
    return value


def _nonnegative(value: object, label: str, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LifecycleError("operation_failed", f"Tmux Session {label} is invalid")
    return value


def _backend_identity(value: object) -> dict[str, object]:
    if (
        not isinstance(value, Mapping)
        or value.get("kind") != "contract"
        or value.get("capability") != _CAPABILITY
    ):
        raise LifecycleError("operation_failed", "contract action lacks a current authority")
    revision = _text(value.get("meshRevision"), "mesh revision")
    assert revision is not None
    if revision.strip() != revision or any(char.isspace() for char in revision):
        raise LifecycleError("operation_failed", "contract action has an invalid mesh revision")
    return {"kind": "contract", "capability": _CAPABILITY, "meshRevision": revision}


def _reference(value: object, host_id: str, revision: str) -> StableReference:
    if not isinstance(value, Mapping) or value.get("meshRevision") != revision:
        raise LifecycleError("operation_failed", "session no longer has a current tmux reference")
    generation = _text(value.get("serverGeneration"), "server generation")
    session_id = value.get("sessionId")
    created_at = _nonnegative(value.get("createdAt"), "creation time")
    observed_name = _text(value.get("observedName"), "observed name", nullable=True)
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise LifecycleError("operation_failed", "session no longer has a valid tmux reference")
    assert generation is not None and created_at is not None
    return StableReference(host_id, revision, generation, session_id, created_at, observed_name)


def _provider_row(
    snapshot: Mapping[str, object],
    identity: Mapping[str, object],
    selection: Mapping[str, object],
) -> dict[str, object]:
    """Resolve one fresh row solely by authority, host, provider, and ID."""

    if _backend_identity(selection.get("backend")) != dict(identity):
        raise LifecycleError(
            "operation_failed", "selected session belongs to an older Mesh revision"
        )
    host_id = selection.get("hostId")
    kind = selection.get("kind")
    identifier = selection.get("id")
    if (
        not isinstance(host_id, str)
        or not _HOST_ID.fullmatch(host_id)
        or kind not in _PROVIDER_OPTIONS
        or not isinstance(identifier, str)
    ):
        raise LifecycleError("operation_failed", "selected contract session is invalid")
    if snapshot.get("backend") != dict(identity):
        raise LifecycleError("operation_failed", "contract refresh changed authority")
    rows = snapshot.get("sessions")
    if not isinstance(rows, list):
        raise LifecycleError("operation_failed", "contract refresh returned invalid rows")
    matched = [
        dict(row)
        for row in rows
        if isinstance(row, Mapping)
        and row.get("contractMode") is True
        and row.get("backend") == dict(identity)
        and row.get("hostId") == host_id
        and row.get("kind") == kind
        and row.get("id") == identifier
    ]
    if len(matched) != 1:
        raise LifecycleError(
            "operation_failed", "selected provider session is no longer unambiguous"
        )
    row = matched[0]
    if row.get("tmuxStale") or row.get("tmuxAmbiguous"):
        raise LifecycleError(
            "operation_failed", "selected session has stale or ambiguous tmux state"
        )
    activity = row.get("activityState")
    if activity == "unknown":
        raise LifecycleError("operation_failed", "selected session has unknown provider activity")
    errors = snapshot.get("errors")
    stale_stages = {"codex": "threads", "claude": "claude", "opencode": "opencode"}
    if isinstance(errors, list):
        host_stages = {
            error.get("stage")
            for error in errors
            if isinstance(error, Mapping) and error.get("host") == host_id
        }
        # A normal ``tmux_missing`` observation is authoritative capability
        # data and a provider wrapper may safely be created there.  A generic
        # tmux-stage error is not: an absent association then means unknown,
        # not proof that no compatible wrapper already exists.
        if "tmux" in host_stages:
            raise LifecycleError("operation_failed", "selected host has unknown tmux inventory")
        if {"active", stale_stages[str(kind)]} & host_stages:
            raise LifecycleError("operation_failed", "selected session has stale provider state")
    return row


def _descriptor(value: object, host_id: str, revision: str) -> StableReference:
    """Validate the public complete descriptor and project its stable ref."""

    if not isinstance(value, Mapping) or value.get("hostId") != host_id:
        raise LifecycleError("operation_failed", "Tmux Session response has an invalid descriptor")
    generation = _text(value.get("serverGeneration"), "server generation")
    session_id = value.get("sessionId")
    created_at = _nonnegative(value.get("createdAt"), "creation time")
    name = _text(value.get("name"), "session name")
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise LifecycleError("operation_failed", "Tmux Session response has an invalid descriptor")
    for field in ("activityAt", "lastAttachedAt"):
        _nonnegative(value.get(field), field, nullable=True)
    for field in ("attachedClients", "windowCount"):
        _nonnegative(value.get(field), field)
    if not isinstance(value.get("pending"), bool):
        raise LifecycleError("operation_failed", "Tmux Session pending marker is invalid")
    for field in ("sessionPath", "currentWindow", "currentPath"):
        _text(value.get(field), field, nullable=True)
    assert generation is not None and created_at is not None and name is not None
    return StableReference(host_id, revision, generation, session_id, created_at, name)


def _error_response(output: CommandOutput) -> LifecycleError:
    if len(output.stdout.encode("utf-8", "replace")) > _MAX_OUTPUT:
        return LifecycleError("operation_failed", "Tmux Session response exceeded output limit")
    try:
        payload = json.loads(output.stdout)
    except json.JSONDecodeError:
        return LifecycleError("operation_failed", "Tmux Session command failed")
    if (
        not isinstance(payload, Mapping)
        or payload.get("schemaVersion") != _SCHEMA_VERSION
        or payload.get("ok") is not False
        or not isinstance(payload.get("error"), Mapping)
    ):
        return LifecycleError("operation_failed", "Tmux Session command failed")
    code = payload["error"].get("code")
    message = payload["error"].get("message")
    if code not in _ERROR_CODES or not isinstance(message, str):
        return LifecycleError("operation_failed", "Tmux Session command failed")
    try:
        clean = _text(message, "error message")
    except LifecycleError:
        return LifecycleError("operation_failed", "Tmux Session command failed")
    assert clean is not None
    return LifecycleError(str(code), clean)


def _success_response(
    output: CommandOutput,
    host_id: str,
    revision: str,
    *,
    opening: bool,
) -> StableReference:
    if len(output.stdout.encode("utf-8", "replace")) > _MAX_OUTPUT:
        raise LifecycleError("operation_failed", "Tmux Session response exceeded output limit")
    try:
        payload = json.loads(output.stdout)
    except json.JSONDecodeError as error:
        raise LifecycleError("operation_failed", "Tmux Session response is invalid") from error
    if (
        not isinstance(payload, Mapping)
        or payload.get("schemaVersion") != _SCHEMA_VERSION
        or payload.get("ok") is not True
        or payload.get("meshRevision") != revision
    ):
        raise LifecycleError("operation_failed", "Tmux Session response is invalid")
    reference = _descriptor(payload.get("session"), host_id, revision)
    if opening:
        focused = payload.get("focused")
        launched = payload.get("terminalLaunched")
        if not isinstance(focused, bool) or not isinstance(launched, bool) or focused == launched:
            raise LifecycleError("operation_failed", "Tmux Session open response is invalid")
    return reference


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise LifecycleError("operation_failed", "contract lifecycle timed out")
    return value


def _run_json(
    backend: _ContractBackend,
    argv: Sequence[str],
    deadline: float,
    host_id: str,
    revision: str,
    *,
    opening: bool,
) -> StableReference:
    output = backend._run(argv, timeout=min(15.0, _remaining(deadline)))
    if output.timed_out:
        raise LifecycleError("operation_failed", "Tmux Session command timed out")
    if output.returncode != 0:
        raise _error_response(output)
    return _success_response(output, host_id, revision, opening=opening)


def _safe_wrapper_name(kind: str, identifier: str, attempt: int) -> str:
    suffix = identifier[:12].casefold()
    base = f"agent-{kind}-{suffix}"
    candidate = base if attempt == 0 else f"{base}-{attempt + 1}"
    if not _NAME.fullmatch(candidate):
        raise LifecycleError("operation_failed", "cannot build a safe tmux wrapper name")
    return candidate


def _resume_argv(kind: str, identifier: str) -> tuple[str, ...]:
    try:
        executable, flag = _PROVIDER_OPTIONS[kind][2:]
    except KeyError as error:
        raise LifecycleError("operation_failed", "selected provider is invalid") from error
    return str(executable), str(flag), identifier


def _display_name(row: Mapping[str, object], identifier: str) -> str:
    value = row.get("name")
    if (
        isinstance(value, str)
        and value
        and len(value) <= _MAX_FIELD
        and not any(unicodedata.category(char).startswith("C") for char in value)
    ):
        return value
    return identifier


class ContractLifecycle:
    """Revalidate and hand an Agent Plus selection to Tmux Plus exactly once."""

    def __init__(
        self,
        store: CacheStore,
        config: PickerConfig,
        context: PresentationContext,
        *,
        timeout: float = _LIFECYCLE_SECONDS,
    ) -> None:
        if context.error is not None or context.selected is None:
            raise LifecycleError("operation_failed", "contract action has no selected authority")
        self.store = store
        self.config = config
        self.context = context
        self.identity = _backend_identity(context.backend)
        self.backend = context.selected
        # Protocols are intentionally static-only here.  Validate the public
        # executable seam at runtime instead of trusting an arbitrary object.
        if not (
            isinstance(getattr(self.backend, "tmux_command", None), str)
            and callable(getattr(self.backend, "_run", None))
        ):
            raise LifecycleError("operation_failed", "contract lifecycle backend is unavailable")
        self.deadline = time.monotonic() + timeout

    def _refresh_row(self, selection: Mapping[str, object]) -> dict[str, object]:
        try:
            snapshot = self.store.refresh(
                self.config,
                force=True,
                require_fresh=True,
                context=self.context,
                deadline=self.deadline,
            )
        except engine.PickerError as error:
            raise LifecycleError("operation_failed", str(error)) from error
        return _provider_row(snapshot, self.identity, selection)

    def _reconcile(
        self,
        selection: Mapping[str, object],
        reference: StableReference,
    ) -> None:
        self.store.reconcile_contract_reference(
            self.config,
            self.context,
            host_id=str(selection["hostId"]),
            kind=str(selection["kind"]),
            identifier=str(selection["id"]),
            reference=reference.payload(),
            deadline=self.deadline,
        )

    def _open(self, selection: Mapping[str, object], reference: StableReference) -> StableReference:
        argv = [
            self.backend.tmux_command,
            "open",
            "--json",
            "--host",
            reference.host_id,
            "--mesh-revision",
            reference.mesh_revision,
            "--server-generation",
            reference.server_generation,
            "--session-id",
            reference.session_id,
            "--created-at",
            str(reference.created_at),
        ]
        result = _run_json(
            self.backend,
            argv,
            self.deadline,
            reference.host_id,
            reference.mesh_revision,
            opening=True,
        )
        # Open may legitimately observe a new name, but it must never return
        # a different stable tmux identity than the one this typed provider
        # row selected.  Otherwise a malformed or confused producer response
        # could be reconciled into the wrong provider cache entry.
        if (
            result.host_id,
            result.mesh_revision,
            result.server_generation,
            result.session_id,
            result.created_at,
        ) != (
            reference.host_id,
            reference.mesh_revision,
            reference.server_generation,
            reference.session_id,
            reference.created_at,
        ):
            raise LifecycleError("operation_failed", "Tmux Session open returned another session")
        return result

    def _create(
        self, selection: Mapping[str, object], row: Mapping[str, object]
    ) -> StableReference:
        host_id = str(selection["hostId"])
        kind = str(selection["kind"])
        identifier = str(selection["id"])
        option_id, option_name, _executable, _flag = _PROVIDER_OPTIONS[kind]
        name_value = _display_name(row, identifier)
        cwd = row.get("cwd")
        cwd_value = (
            cwd
            if isinstance(cwd, str)
            and cwd
            and len(cwd) <= _MAX_FIELD
            and not any(unicodedata.category(char).startswith("C") for char in cwd)
            else None
        )
        invalid_cwd_retry = False
        for collision in range(_MAX_CREATE_COLLISIONS):
            wrapper_name = _safe_wrapper_name(kind, identifier, collision)
            argv = [
                self.backend.tmux_command,
                "create",
                "--json",
                "--host",
                host_id,
                "--mesh-revision",
                self.identity["meshRevision"],
                "--name",
                wrapper_name,
            ]
            if cwd_value is not None:
                argv.extend(("--cwd", cwd_value))
            argv.extend(
                (
                    "--set-option",
                    f"{option_id}={identifier}",
                    "--set-option",
                    f"{option_name}={name_value}",
                    "--defer-until-attached",
                    "--open",
                    "--",
                    *_resume_argv(kind, identifier),
                )
            )
            try:
                return _run_json(
                    self.backend,
                    argv,
                    self.deadline,
                    host_id,
                    str(self.identity["meshRevision"]),
                    opening=True,
                )
            except LifecycleError as error:
                if error.code == "invalid_cwd" and cwd_value is not None and not invalid_cwd_retry:
                    cwd_value = None
                    invalid_cwd_retry = True
                    continue
                if error.code == "session_exists" and collision + 1 < _MAX_CREATE_COLLISIONS:
                    continue
                raise
        raise LifecycleError(
            "operation_failed", "could not choose a collision-free tmux wrapper name"
        )

    def open_or_create(self, selection: Mapping[str, object]) -> None:
        row = self._refresh_row(selection)
        host_id = str(selection["hostId"])
        revision = str(self.identity["meshRevision"])
        tmux = row.get("tmux")
        if tmux is not None:
            reference = _reference(tmux, host_id, revision)
            try:
                result = self._open(selection, reference)
            except LifecycleError as error:
                if error.code != "stale_session":
                    raise
                retry_row = self._refresh_row(selection)
                retry_reference = _reference(retry_row.get("tmux"), host_id, revision)
                if retry_reference == reference:
                    raise
                result = self._open(selection, retry_reference)
            self._reconcile(selection, result)
            return

        if row.get("active") is True:
            raise LifecycleError(
                "operation_failed", "selected provider session is active outside tmux"
            )
        result = self._create(selection, row)
        self._reconcile(selection, result)
