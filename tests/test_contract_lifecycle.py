"""Hermetic Tmux Session v1 lifecycle consumer tests."""

from __future__ import annotations

import copy
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from rofi_agent_plus.cache import CacheStore, PresentationContext
from rofi_agent_plus.config import PickerConfig
from rofi_agent_plus.contract_backend import CommandOutput
from rofi_agent_plus.contract_lifecycle import (
    ContractLifecycle,
    LifecycleError,
    _success_response,
)
from rofi_agent_plus.rofi import _open_selection, run_rofi, selection_payload

ROOT = Path(__file__).parent / "fixtures" / "contract" / "lifecycle"
THREAD = "11111111-1111-1111-1111-111111111111"
REVISION = "sha256:mesh-v1"
BACKEND = {
    "kind": "contract",
    "capability": "host-mesh-v1+tmux-session-v1",
    "meshRevision": REVISION,
}


def fixture(name: str) -> dict[str, object]:
    return json.loads((ROOT / name).read_text())


def descriptor(
    *,
    session_id: str = "$4",
    created_at: int = 5,
    name: str = "agent-codex-11111111",
    pending: bool = False,
) -> dict[str, object]:
    return {
        "hostId": "alpha",
        "serverGeneration": "tmux-v1:alpha",
        "sessionId": session_id,
        "createdAt": created_at,
        "name": name,
        "activityAt": 6,
        "lastAttachedAt": None,
        "attachedClients": 0,
        "pending": pending,
        "windowCount": 1,
        "sessionPath": "/work",
        "currentWindow": "shell",
        "currentPath": "/work",
    }


def success(session: dict[str, object]) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "ok": True,
        "meshRevision": REVISION,
        "session": session,
        "focused": False,
        "terminalLaunched": True,
    }


def failure(code: str, message: str = "failed") -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "ok": False,
        "error": {"code": code, "message": message, "hostId": "alpha"},
    }


def row(
    kind: str = "codex",
    *,
    tmux: bool = True,
    active: bool = False,
    activity: str = "idle",
    cwd: str = "/work",
    session_id: str = "$4",
    created_at: int = 5,
) -> dict[str, object]:
    item: dict[str, object] = {
        "contractMode": True,
        "backend": dict(BACKEND),
        "hostId": "alpha",
        "host": "Alpha",
        "kind": kind,
        "id": THREAD,
        "name": "native name",
        "cwd": cwd,
        "active": active,
        "activityState": activity,
        "recencyAt": 5,
    }
    if tmux:
        item["tmux"] = {
            "meshRevision": REVISION,
            "serverGeneration": "tmux-v1:alpha",
            "sessionId": session_id,
            "createdAt": created_at,
            "observedName": "old-name",
        }
        item["tmuxSession"] = "old-name"
    return item


class FakeBackend:
    tmux_command = "rofi-tmux-plus"

    def __init__(
        self, rows: list[dict[str, object]] | list[list[dict[str, object]]], responses: list[object]
    ):
        self.identity = dict(BACKEND)
        self._rows = rows if rows and isinstance(rows[0], list) else [rows]  # type: ignore[index]
        self._responses = list(responses)
        self.calls: list[tuple[list[str], float]] = []
        self.streams = 0
        self.errors: list[dict[str, object]] = []

    def prepare(self) -> None:
        return None

    def stream(self, _config: PickerConfig, **_kwargs: object):
        index = min(self.streams, len(self._rows) - 1)
        self.streams += 1
        rows = self._rows[index]
        return [
            {"event": "refresh-started", "hosts": ["alpha"], "backend": dict(BACKEND)},
            {
                "event": "host-complete",
                "host": "alpha",
                "sessions": copy.deepcopy(rows),
                "errors": copy.deepcopy(self.errors),
                "backend": dict(BACKEND),
            },
            {"event": "refresh-finished", "backend": dict(BACKEND)},
        ]

    def _run(self, argv: list[str], **kwargs: object) -> CommandOutput:
        self.calls.append((list(argv), float(kwargs["timeout"])))
        response = self._responses.pop(0)
        if isinstance(response, CommandOutput):
            return response
        if isinstance(response, tuple):
            code, payload = response
            return CommandOutput(tuple(argv), code, json.dumps(payload), "failure")
        return CommandOutput(tuple(argv), 0, json.dumps(response), "")


class ContractLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = PickerConfig(max_sessions=40)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def harness(
        self,
        rows: list[dict[str, object]] | list[list[dict[str, object]]],
        responses: list[object],
    ) -> tuple[ContractLifecycle, CacheStore, FakeBackend, PresentationContext]:
        backend = FakeBackend(rows, responses)
        store = CacheStore(Path(self.temporary.name) / "cache", backend_selector=lambda: backend)
        context = PresentationContext(self.config.fingerprint, dict(BACKEND), selected=backend)
        return ContractLifecycle(store, self.config, context), store, backend, context

    @staticmethod
    def selection(kind: str = "codex") -> dict[str, object]:
        return {
            "contractMode": True,
            "backend": dict(BACKEND),
            "hostId": "alpha",
            "kind": kind,
            "id": THREAD,
        }

    def test_open_uses_exact_reference_without_expected_name_and_reconciles_rename(self) -> None:
        lifecycle, store, backend, context = self.harness(
            [row()], [success(descriptor(name="renamed outside"))]
        )
        lifecycle.open_or_create(self.selection())
        argv, timeout = backend.calls[0]
        self.assertEqual(
            [
                "rofi-tmux-plus",
                "open",
                "--json",
                "--host",
                "alpha",
                "--mesh-revision",
                REVISION,
                "--server-generation",
                "tmux-v1:alpha",
                "--session-id",
                "$4",
                "--created-at",
                "5",
            ],
            argv,
        )
        self.assertNotIn("--expected-name", argv)
        self.assertGreater(timeout, 0)
        cached = store.load(self.config.fingerprint, BACKEND)
        assert cached is not None
        self.assertEqual("renamed outside", cached["sessions"][0]["tmuxSession"])
        self.assertEqual(REVISION, cached["sessions"][0]["tmux"]["meshRevision"])
        self.assertEqual(context.backend, cached["backend"])

    def test_open_rejects_a_success_response_for_another_stable_session(self) -> None:
        lifecycle, store, backend, _context = self.harness(
            [row()], [success(descriptor(session_id="$99", created_at=77))]
        )
        with self.assertRaisesRegex(LifecycleError, "another session"):
            lifecycle.open_or_create(self.selection())
        self.assertEqual(1, len(backend.calls))
        cached = store.load(self.config.fingerprint, BACKEND)
        assert cached is not None
        self.assertEqual("old-name", cached["sessions"][0]["tmuxSession"])

    def test_stale_session_retries_once_only_for_a_new_typed_reference(self) -> None:
        lifecycle, _store, backend, _context = self.harness(
            [[row(session_id="$4")], [row(session_id="$5", created_at=6)]],
            [
                (2, failure("stale_session", "changed")),
                success(descriptor(session_id="$5", created_at=6)),
            ],
        )
        lifecycle.open_or_create(self.selection())
        self.assertEqual(
            ["$4", "$5"], [call[0][call[0].index("--session-id") + 1] for call in backend.calls]
        )
        self.assertEqual(2, backend.streams)

    def test_nonstructured_or_unchanged_stale_response_never_retries_or_creates(self) -> None:
        for response, rows in (
            (
                (
                    2,
                    {
                        "schemaVersion": 1,
                        "ok": False,
                        "error": {"code": "operation_failed", "message": "stale_session text"},
                    },
                ),
                [row()],
            ),
            ((2, failure("stale_session", "changed")), [[row()], [row()]]),
        ):
            with self.subTest(response=response):
                lifecycle, _store, backend, _context = self.harness(rows, [response])
                with self.assertRaises(LifecycleError):
                    lifecycle.open_or_create(self.selection())
                self.assertEqual(1, len(backend.calls))
                self.assertEqual("open", backend.calls[0][0][1])

    def test_create_has_provider_argv_options_cwd_retry_and_collision_bound(self) -> None:
        invalid = fixture("invalid-cwd.json")["response"]
        collision = fixture("session-exists.json")["response"]
        for kind, resume in (
            ("codex", ["codex", "resume", THREAD]),
            ("claude", ["claude", "--resume", THREAD]),
            ("opencode", ["opencode", "--session", THREAD]),
        ):
            with self.subTest(kind=kind):
                lifecycle, _store, backend, _context = self.harness(
                    [row(kind, tmux=False, cwd="/gone")],
                    [(2, invalid), (2, collision), success(descriptor(pending=True))],
                )
                lifecycle.open_or_create(self.selection(kind))
                first, second, third = [call[0] for call in backend.calls]
                self.assertIn("--cwd", first)
                self.assertNotIn("--cwd", second)
                self.assertNotEqual(
                    second[second.index("--name") + 1], third[third.index("--name") + 1]
                )
                self.assertEqual(resume, third[third.index("--") + 1 :])
                self.assertIn("--defer-until-attached", third)
                self.assertIn("--open", third)
                self.assertNotIn("@agent_picker_waiting", " ".join(third))
                option_id = {
                    "codex": "@codex_thread_id",
                    "claude": "@claude_session_id",
                    "opencode": "@opencode_session_id",
                }[kind]
                self.assertIn(f"{option_id}={THREAD}", third)

    def test_create_collision_and_invalid_cwd_retries_are_bounded(self) -> None:
        collision = fixture("session-exists.json")["response"]
        lifecycle, _store, backend, _context = self.harness([row(tmux=False)], [(2, collision)] * 8)
        with self.assertRaisesRegex(LifecycleError, "exact name"):
            lifecycle.open_or_create(self.selection())
        self.assertEqual(8, len(backend.calls))
        base = f"agent-codex-{THREAD[:12]}"
        self.assertEqual(
            [
                base,
                f"{base}-2",
                f"{base}-3",
                f"{base}-4",
                f"{base}-5",
                f"{base}-6",
                f"{base}-7",
                f"{base}-8",
            ],
            [call[0][call[0].index("--name") + 1] for call in backend.calls],
        )

        invalid = fixture("invalid-cwd.json")["response"]
        lifecycle, _store, backend, _context = self.harness(
            [row(tmux=False, cwd="/gone")], [(2, invalid), (2, invalid)]
        )
        with self.assertRaisesRegex(LifecycleError, "cwd"):
            lifecycle.open_or_create(self.selection())
        self.assertEqual(2, len(backend.calls))
        self.assertIn("--cwd", backend.calls[0][0])
        self.assertNotIn("--cwd", backend.calls[1][0])

    def test_active_outside_or_unknown_provider_never_creates(self) -> None:
        for kind in ("codex", "claude", "opencode"):
            with self.subTest(kind=kind, state="outside"):
                lifecycle, _store, backend, _context = self.harness(
                    [row(kind, tmux=False, active=True)], []
                )
                with self.assertRaisesRegex(LifecycleError, "outside tmux"):
                    lifecycle.open_or_create(self.selection(kind))
                self.assertEqual([], backend.calls)
            with self.subTest(kind=kind, state="unknown"):
                lifecycle, _store, backend, _context = self.harness(
                    [row(kind, tmux=False, activity="unknown")], []
                )
                with self.assertRaisesRegex(LifecycleError, "unknown provider activity"):
                    lifecycle.open_or_create(self.selection(kind))
                self.assertEqual([], backend.calls)

    def test_unknown_tmux_inventory_never_treats_missing_ref_as_createable(self) -> None:
        lifecycle, _store, backend, _context = self.harness([row(tmux=False)], [])
        backend.errors = [{"host": "alpha", "stage": "tmux", "message": "inventory unavailable"}]
        with self.assertRaisesRegex(LifecycleError, "unknown tmux inventory"):
            lifecycle.open_or_create(self.selection())
        self.assertEqual([], backend.calls)

    def test_lifecycle_revalidation_does_not_use_stale_cache_while_lock_is_held(self) -> None:
        lifecycle, store, backend, context = self.harness(
            [row(tmux=False)], [success(descriptor())]
        )
        stale = store.refresh(self.config, force=True, context=context)
        stale["generatedAt"] = 0
        store.write(stale)
        lifecycle.deadline = time.monotonic() + 0.01
        with store.lock() as acquired:
            self.assertTrue(acquired)
            with self.assertRaisesRegex(LifecycleError, "revalidation is already in progress"):
                lifecycle.open_or_create(self.selection())
        self.assertEqual([], backend.calls)

    def test_malformed_and_rollback_responses_do_not_reconcile(self) -> None:
        rollback = fixture("setup-rollback.json")["response"]
        for response in (
            {"schemaVersion": 2, "ok": True},
            "{" + "x" * (256 * 1024) + "}",
            (2, rollback),
        ):
            with self.subTest(response_type=type(response).__name__):
                lifecycle, store, backend, _context = self.harness([row(tmux=False)], [response])
                if isinstance(response, str):
                    backend._responses = [CommandOutput((), 0, response, "")]
                with self.assertRaises(LifecycleError):
                    lifecycle.open_or_create(self.selection())
                self.assertEqual(1, len(backend.calls))
                cached = store.load(self.config.fingerprint, BACKEND)
                assert cached is not None
                self.assertNotIn("tmux", cached["sessions"][0])

    def test_timed_out_command_never_reconciles_even_with_json(self) -> None:
        lifecycle, store, backend, _context = self.harness(
            [row(tmux=False)],
            [CommandOutput((), 0, json.dumps(success(descriptor())), "", timed_out=True)],
        )
        with self.assertRaisesRegex(LifecycleError, "timed out"):
            lifecycle.open_or_create(self.selection())
        cached = store.load(self.config.fingerprint, BACKEND)
        assert cached is not None
        self.assertNotIn("tmux", cached["sessions"][0])

    def test_copied_lifecycle_fixtures_exercise_deferred_and_open_shapes(self) -> None:
        """Producer fixtures remain hermetic but drive the consumer validator."""

        deferred = fixture("create-deferred.json")["response"]
        assert isinstance(deferred, dict)
        deferred = copy.deepcopy(deferred)
        deferred["meshRevision"] = REVISION
        deferred_session = deferred["session"]
        assert isinstance(deferred_session, dict)
        deferred_session["hostId"] = "alpha"
        reference = _success_response(
            CommandOutput((), 0, json.dumps(deferred), ""),
            "alpha",
            REVISION,
            opening=False,
        )
        self.assertTrue(deferred_session["pending"])
        self.assertEqual("$8", reference.session_id)

        opened = fixture("external-rename.json")["open"]
        assert isinstance(opened, dict)
        opened = copy.deepcopy(opened)
        opened["meshRevision"] = REVISION
        opened_session = opened["session"]
        assert isinstance(opened_session, dict)
        opened_session["hostId"] = "alpha"
        renamed = _success_response(
            CommandOutput((), 0, json.dumps(opened), ""),
            "alpha",
            REVISION,
            opening=True,
        )
        self.assertEqual("renamed-outside", renamed.observed_name)

    def test_rendered_rofi_info_revalidates_current_authority_then_opens(self) -> None:
        backend = FakeBackend([row()], [success(descriptor())])
        store = CacheStore(Path(self.temporary.name) / "cache", backend_selector=lambda: backend)
        selected = json.loads(selection_payload(row()))
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.assertEqual(
                0,
                run_rofi(
                    {"ROFI_RETV": "1", "ROFI_INFO": json.dumps(selected)},
                    store=store,
                    config=self.config,
                ),
            )
        self.assertEqual("open", backend.calls[0][0][1])
        # The Rofi path gives contract lifecycle its 30-second action budget,
        # not the legacy four-second discovery default.
        self.assertGreater(backend.calls[0][1], 10.0)

    def test_explicit_open_timeout_is_honored_and_reconciliation_has_no_extra_wait(self) -> None:
        lifecycle, store, backend, context = self.harness([row()], [success(descriptor())])
        _open_selection(self.selection(), self.config, timeout=0.5, store=store, context=context)
        self.assertLessEqual(backend.calls[0][1], 0.5)

        snapshot = store.load(self.config.fingerprint, BACKEND)
        assert snapshot is not None
        started = time.monotonic()
        self.assertFalse(
            store.reconcile_contract_reference(
                self.config,
                context,
                host_id="alpha",
                kind="codex",
                identifier=THREAD,
                reference={
                    "meshRevision": REVISION,
                    "serverGeneration": "tmux-v1:alpha",
                    "sessionId": "$7",
                    "createdAt": 8,
                    "observedName": "late",
                },
                wait_seconds=1.0,
                deadline=started,
            )
        )
        self.assertLess(time.monotonic() - started, 0.1)

    def test_selection_must_match_the_prepared_authority(self) -> None:
        lifecycle, _store, backend, _context = self.harness([row()], [])
        selected = self.selection()
        selected["backend"] = {**BACKEND, "meshRevision": "sha256:old"}
        with self.assertRaisesRegex(LifecycleError, "older Mesh revision"):
            lifecycle.open_or_create(selected)
        self.assertEqual([], backend.calls)

    def test_reconciliation_preserves_freshness_and_rejects_changed_authority(self) -> None:
        lifecycle, store, _backend, context = self.harness([row()], [])
        snapshot = store.refresh(self.config, force=True, context=context)
        snapshot["generatedAt"] = 123
        store.write(snapshot)
        ref = {
            "meshRevision": REVISION,
            "serverGeneration": "tmux-v1:alpha",
            "sessionId": "$9",
            "createdAt": 10,
            "observedName": "new-name",
        }
        self.assertTrue(
            store.reconcile_contract_reference(
                self.config,
                context,
                host_id="alpha",
                kind="codex",
                identifier=THREAD,
                reference=ref,
            )
        )
        current = store.load(self.config.fingerprint, BACKEND)
        assert current is not None
        self.assertEqual(123, current["generatedAt"])
        changed = PresentationContext(
            self.config.fingerprint,
            {**BACKEND, "meshRevision": "sha256:new"},
            selected=lifecycle.backend,
        )
        self.assertFalse(
            store.reconcile_contract_reference(
                self.config,
                changed,
                host_id="alpha",
                kind="codex",
                identifier=THREAD,
                reference=ref,
            )
        )
        current = store.load(self.config.fingerprint, BACKEND)
        assert current is not None
        self.assertEqual("new-name", current["sessions"][0]["tmuxSession"])
