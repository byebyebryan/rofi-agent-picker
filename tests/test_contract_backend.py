"""Hermetic Host Mesh/Tmux Session v1 consumer tests."""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from rofi_agent_plus.cache import CACHE_VERSION, CacheStore, build_snapshot
from rofi_agent_plus.codex import AppServerClient
from rofi_agent_plus.config import PickerConfig
from rofi_agent_plus.contract_backend import (
    _ACTIVE_PROBE,
    CommandOutput,
    ContractBackend,
    ContractError,
    StaleMeshError,
    _inventory,
    _inventory_args,
    _run_bounded,
    _tmux_association,
    _validate_active,
    parse_mesh,
    select_backend,
)
from rofi_agent_plus.engine import PickerError
from rofi_agent_plus.rofi import _open_selection, _parse_selection, render_snapshot, run_rofi

ROOT = Path(__file__).parent / "fixtures" / "contract"
THREAD = "11111111-1111-1111-1111-111111111111"
CONTRACT_CAPABILITY = "host-mesh-v1+tmux-session-v1"


def fixture(name: str) -> dict[str, object]:
    return json.loads((ROOT / name).read_text())


def output(argv: list[str], stdout: object, *, code: int = 0, stderr: str = "") -> CommandOutput:
    return CommandOutput(tuple(argv), code, json.dumps(stdout), stderr)


class MeshParseTest(unittest.TestCase):
    def test_alias_tokens_policy_bounds_and_emitted_route_order(self) -> None:
        payload = fixture("mesh-v1.json")
        remote = payload["hosts"][1]
        assert isinstance(remote, dict)
        remote["aliases"] = ["user@beta", "beta_native"]
        mesh = parse_mesh(payload)
        self.assertEqual(
            ("beta-first.test", "beta-fallback.test"),
            tuple(route.destination for route in mesh.hosts[1].routes),
        )

        for key, value in (("connectTimeoutSeconds", 61), ("connectionAttempts", 11)):
            invalid = copy.deepcopy(payload)
            invalid["sshPolicy"][key] = value
            with self.subTest(key=key), self.assertRaises(ContractError):
                parse_mesh(invalid)

    def test_same_host_alias_overlap_is_allowed_cross_host_is_not(self) -> None:
        payload = fixture("mesh-v1.json")
        payload["hosts"][0]["aliases"] = ["alpha"]
        self.assertEqual("alpha", parse_mesh(payload).local.host_id)
        invalid = copy.deepcopy(payload)
        invalid["hosts"][1]["aliases"] = ["alpha"]
        with self.assertRaisesRegex(ContractError, "ambiguous"):
            parse_mesh(invalid)

    def test_generated_revision_health_ttl_and_route_timestamps_are_strict(self) -> None:
        payload = fixture("mesh-v1.json")
        self.assertEqual(1722742999000, parse_mesh(payload).hosts[1].routes[0].last_reachable_at)
        mutations = (
            ("generatedAt", True),
            ("generatedAt", 2**63),
            ("meshRevision", "has space"),
        )
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                malformed = copy.deepcopy(payload)
                malformed[key] = value
                with self.assertRaises(ContractError):
                    parse_mesh(malformed)
        for value in (0, 86401):
            malformed = copy.deepcopy(payload)
            malformed["sshPolicy"]["routeHealthTtlSeconds"] = value
            with self.assertRaises(ContractError):
                parse_mesh(malformed)
        malformed = copy.deepcopy(payload)
        malformed["sshPolicy"]["executable"] = "ssh client"
        with self.assertRaises(ContractError):
            parse_mesh(malformed)
        for key, value in (("lastReachableAt", -1), ("lastUnreachableAt", True)):
            malformed = copy.deepcopy(payload)
            malformed["hosts"][1]["routes"][0][key] = value
            with self.assertRaises(ContractError):
                parse_mesh(malformed)

    def test_prepare_rejects_malformed_nonzero_and_unsupported_provider_output(self) -> None:
        for payload, code in (("not-json", 0), ({"schemaVersion": 2}, 0), ({}, 2)):
            with self.subTest(payload=payload, code=code):

                def runner(
                    argv: list[str],
                    _payload: object = payload,
                    _code: int = code,
                    **_kwargs: object,
                ) -> CommandOutput:
                    return (
                        CommandOutput(tuple(argv), _code, _payload, "failure")
                        if isinstance(_payload, str)
                        else output(argv, _payload, code=_code, stderr="failure")
                    )

                backend = ContractBackend(
                    "rofi-ssh-plus",
                    "rofi-tmux-plus",
                    runner=runner,
                )
                with self.assertRaises(ContractError):
                    backend.prepare()


class InventoryContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mesh = parse_mesh(fixture("mesh-v1.json"))
        self.payload = fixture("tmux-inventory-v1.json")

    def test_argv_pins_mesh_hosts_and_only_requested_options(self) -> None:
        argv = _inventory_args("rofi-tmux-plus", self.mesh)
        self.assertEqual(
            [
                "rofi-tmux-plus",
                "inventory",
                "--json",
                "--panes",
                "--mesh-revision",
                "sha256:mesh-v1",
                "--host",
                "alpha",
                "--host",
                "beta",
                "--session-option",
                "@codex_thread_id",
                "--session-option",
                "@codex_name",
                "--session-option",
                "@claude_session_id",
                "--session-option",
                "@claude_name",
                "--session-option",
                "@opencode_session_id",
                "--session-option",
                "@opencode_name",
                "--session-option",
                "@agent_picker_waiting",
            ],
            argv,
        )

    def test_eagerly_rejects_unreferenced_malformed_session_and_missing_option(self) -> None:
        malformed = copy.deepcopy(self.payload)
        beta = malformed["hosts"][1]
        beta.update(
            {
                "serverGeneration": "tmux-v1:beta",
                "sessions": [copy.deepcopy(malformed["hosts"][0]["sessions"][0])],
            }
        )
        beta["sessions"][0].update(
            {
                "hostId": "beta",
                "serverGeneration": "tmux-v1:beta",
                "sessionId": "$5",
                "createdAt": 9,
            }
        )
        beta["sessions"][0]["options"].pop("@opencode_name")
        with self.assertRaisesRegex(ContractError, "requested option"):
            _inventory(malformed, self.mesh)

        malformed = copy.deepcopy(self.payload)
        malformed["hosts"][1]["nativeHostname"] = "bad\nname"
        with self.assertRaises(ContractError):
            _inventory(malformed, self.mesh)

    def test_duplicate_option_and_process_conflict_are_ambiguous(self) -> None:
        host = self.payload["hosts"][0]
        duplicated = copy.deepcopy(host)
        second = copy.deepcopy(duplicated["sessions"][0])
        second.update({"sessionId": "$9", "createdAt": 99, "name": "other"})
        duplicated["sessions"].append(second)
        association, error = _tmux_association(duplicated, "codex", THREAD, {}, "sha256:mesh-v1")
        self.assertIsNone(association)
        self.assertEqual("ambiguous provider correlation", error)

        conflict = copy.deepcopy(host)
        second = copy.deepcopy(conflict["sessions"][0])
        second.update({"sessionId": "$9", "createdAt": 99, "name": "other"})
        second["options"]["@codex_thread_id"] = None
        second["panes"][0]["pid"] = 88
        conflict["sessions"].append(second)
        association, error = _tmux_association(
            conflict,
            "codex",
            THREAD,
            {"pid": 88, "ancestors": [88]},
            "sha256:mesh-v1",
        )
        self.assertIsNone(association)
        self.assertEqual("provider option conflicts with process correlation", error)

    def test_nullable_producer_descriptor_keeps_stable_reference(self) -> None:
        parsed = _inventory(fixture("tmux-inventory-nullable-v1.json"), self.mesh)
        association, error = _tmux_association(
            parsed["alpha"], "codex", THREAD, {}, "sha256:mesh-v1"
        )
        self.assertIsNone(error)
        assert association is not None
        self.assertEqual("$4", association["sessionId"])
        self.assertIsNone(association["observedName"])


class BackendSelectionAndTransportTest(unittest.TestCase):
    def test_pair_selection_never_mixes_one_external_owner(self) -> None:
        self.assertEqual("legacy", select_backend(which=lambda _name: None).kind)
        self.assertEqual(
            "legacy",
            select_backend(
                which=lambda name: "/tools/ssh" if name == "rofi-ssh-plus" else None
            ).kind,
        )
        self.assertEqual(
            "contract",
            select_backend(which=lambda name: f"/tools/{name}").kind,
        )

    def test_capability_disappearance_selects_legacy_without_mixing_cached_contract(self) -> None:
        available = {"rofi-ssh-plus": "/tools/ssh", "rofi-tmux-plus": "/tools/tmux"}

        def selector(name: str) -> str | None:
            return available.get(name)

        self.assertEqual("contract", select_backend(which=selector).kind)
        available.pop("rofi-tmux-plus")
        self.assertEqual("legacy", select_backend(which=selector).kind)

    def test_reached_marker_uses_emitted_route_order_and_reports_only_evidence(self) -> None:
        mesh = parse_mesh(fixture("mesh-v1.json"))
        calls: list[list[str]] = []

        def runner(argv: list[str], **_kwargs: object) -> CommandOutput:
            calls.append(argv)
            if argv[1:3] == ["mesh", "report-route"]:
                return output(argv, {"schemaVersion": 1, "ok": True})
            if argv[0] == "ssh" and argv[-2] == "beta-first.test":
                return CommandOutput(tuple(argv), 255, "", "Permission denied")
            remote = argv[-1]
            nonce = remote.split("rofi-plus-reached ")[1].split(" ", 1)[0].strip("'")
            marker = f"\x1eROFI_PLUS_REACHED_V1:{nonce}\x1f\n"
            payload = {
                "nativeHostname": "beta-native",
                "active": {},
                "claudeActive": {},
                "opencodeActive": {},
            }
            return output(argv, payload, stderr=marker)

        backend = ContractBackend(
            "rofi-ssh-plus", "rofi-tmux-plus", runner=runner, now_millis=lambda: 7
        )
        backend.mesh = mesh
        route, active = backend._remote_active(mesh.hosts[1], time.monotonic() + 5)
        self.assertEqual("beta-fallback.test", route)
        self.assertIsInstance(active, dict)
        ssh_routes = [call[-2] for call in calls if call[0] == "ssh"]
        self.assertEqual(["beta-first.test", "beta-fallback.test"], ssh_routes)
        reports = [call for call in calls if call[1:3] == ["mesh", "report-route"]]
        self.assertEqual(1, len(reports))
        self.assertIn("beta-fallback.test", reports[0])
        self.assertIn("reachable", reports[0])

    def test_marked_domain_attempt_retries_transport_and_keeps_report_failure_diagnostic(
        self,
    ) -> None:
        mesh = parse_mesh(fixture("mesh-v1.json"))
        calls: list[list[str]] = []

        def runner(argv: list[str], **_kwargs: object) -> CommandOutput:
            calls.append(argv)
            if argv[1:3] == ["mesh", "report-route"]:
                return CommandOutput(tuple(argv), 2, "{}", "report unavailable")
            if argv[-2] == "beta-first.test":
                return CommandOutput(tuple(argv), 255, "", "operation timed out")
            nonce = argv[-1].split("rofi-plus-reached ")[1].split(" ", 1)[0].strip("'")
            return output(
                argv,
                {"installed": False, "sessions": []},
                stderr=f"\x1eROFI_PLUS_REACHED_V1:{nonce}\x1f\n",
            )

        backend = ContractBackend("rofi-ssh-plus", "rofi-tmux-plus", runner=runner)
        backend.mesh = mesh
        route, completed = backend._remote_command(
            mesh.hosts[1],
            time.monotonic() + 4,
            remote_argv=("python3", "-", "40", ""),
            input_data=b"probe",
            label="provider query",
        )
        self.assertEqual("beta-fallback.test", route)
        self.assertIsInstance(completed, CommandOutput)
        self.assertEqual(
            ["beta-first.test", "beta-fallback.test"], [row[-2] for row in calls if row[0] == "ssh"]
        )
        self.assertTrue(backend._report_errors)
        remote = next(
            row[-1] for row in calls if row[0] == "ssh" and row[-2] == "beta-fallback.test"
        )
        self.assertIn("python3", remote)
        self.assertNotIn("ssh true", remote)

    def test_local_activity_uses_bounded_probe_instead_of_engine_ps(self) -> None:
        mesh = parse_mesh(fixture("mesh-v1.json"))
        backend = ContractBackend("rofi-ssh-plus", "rofi-tmux-plus", runner=lambda *_a, **_k: None)
        backend.mesh = mesh
        backend._run = mock.Mock(side_effect=ContractError("contract command timed out"))  # type: ignore[method-assign]
        with mock.patch("rofi_agent_plus.contract_backend.engine._process_table") as table:
            route, active = backend._active(mesh.local, time.monotonic() + 1)
        self.assertIsNone(route)
        self.assertIsInstance(active, ContractError)
        table.assert_not_called()

    def test_each_remote_provider_stage_uses_marked_domain_transport(self) -> None:
        mesh = parse_mesh(fixture("mesh-v1.json"))
        backend = ContractBackend("rofi-ssh-plus", "rofi-tmux-plus", runner=lambda *_a, **_k: None)
        backend.mesh = mesh
        calls: list[dict[str, object]] = []

        def marked(_host: object, _deadline: float, **kwargs: object):
            calls.append(kwargs)
            return "beta-first.test", output([], {"installed": False, "sessions": []})

        backend._remote_command = marked  # type: ignore[method-assign]
        backend._remote_codex_threads = mock.Mock(return_value=[])  # type: ignore[method-assign]
        codex, claude, opencode = backend._provider_results(
            mesh.hosts[1], PickerConfig(), time.monotonic() + 2
        )
        self.assertEqual([], codex)
        self.assertFalse(claude["installed"])
        self.assertFalse(opencode["installed"])
        self.assertEqual(2, len(calls))
        self.assertTrue(all(call["remote_argv"][:2] == ("python3", "-") for call in calls))
        backend._remote_codex_threads.assert_called_once()

    def test_bounded_process_rejects_output_and_reaps_timeout(self) -> None:
        noisy = [sys.executable, "-c", "import sys;sys.stdout.write('x'*100000)"]
        with self.assertRaisesRegex(ContractError, "stdout"):
            _run_bounded(noisy, timeout=2, stdout_limit=128)
        sleepy = [sys.executable, "-c", "import time;time.sleep(5)"]
        started = time.monotonic()
        with self.assertRaisesRegex(ContractError, "timed out"):
            _run_bounded(sleepy, timeout=0.05)
        self.assertLess(time.monotonic() - started, 1.0)

        blocked_stdin = [sys.executable, "-c", "import time;time.sleep(5)"]
        started = time.monotonic()
        with self.assertRaisesRegex(ContractError, "timed out"):
            _run_bounded(blocked_stdin, input_data=b"x" * (2 << 20), timeout=0.05)
        self.assertLess(time.monotonic() - started, 1.0)
        invalid_utf8 = [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'\\xff')"]
        with self.assertRaisesRegex(ContractError, "UTF-8"):
            _run_bounded(invalid_utf8, timeout=2)
        closed_stdin = [
            sys.executable,
            "-c",
            "import os,sys,time;os.close(0);sys.stderr.write('closed');sys.stderr.flush();time.sleep(.03)",
        ]
        completed = _run_bounded(closed_stdin, input_data=b"x" * (2 << 20), timeout=2)
        self.assertEqual(0, completed.returncode)
        self.assertIn("closed", completed.stderr)

    def test_codex_waits_for_marker_while_buffering_early_stdout(self) -> None:
        marker = b"\x1eROFI_PLUS_REACHED_V1:0123456789abcdef\x1f\n"

        def program(stderr: bytes) -> list[str]:
            source = (
                "import sys,time;"
                "sys.stdout.buffer.write(b'early-json-rpc\\n');sys.stdout.flush();"
                "time.sleep(.03);"
                f"sys.stderr.buffer.write({stderr!r});sys.stderr.flush();"
                "time.sleep(.05)"
            )
            return [sys.executable, "-c", source]

        with AppServerClient(
            program(marker),
            1,
            "test",
            ContractError,
            stdout_limit=1024,
            stderr_limit=1024,
            reached_marker=marker,
        ) as client:
            self.assertEqual((True, ""), client.wait_for_marker(0.5))
            self.assertIn(b"early-json-rpc", client._buffer)
        for candidate in (b"", marker.replace(b"0123456789abcdef", b"wrong"), marker + marker):
            with self.subTest(candidate=candidate):
                with AppServerClient(
                    program(candidate),
                    1,
                    "test",
                    ContractError,
                    stdout_limit=1024,
                    stderr_limit=1024,
                    reached_marker=marker,
                ) as client:
                    self.assertFalse(client.wait_for_marker(0.1)[0])

    def test_codex_stdout_limit_counts_discarded_jsonl_records(self) -> None:
        source = (
            "import sys,time;"
            "sys.stdin.readline();"
            "line='{\"id\":0,\"noise\":\"' + 'x'*48 + '\"}\\n';"
            "sys.stdout.write(line);sys.stdout.flush();time.sleep(.04);"
            "sys.stdout.write(line);sys.stdout.flush();time.sleep(.04);"
            'sys.stdout.write(\'{"id":1,"result":{}}\\n\');sys.stdout.flush()'
        )
        with AppServerClient(
            [sys.executable, "-c", source],
            1,
            "test",
            ContractError,
            stdout_limit=100,
            stderr_limit=1024,
        ) as client:
            with self.assertRaisesRegex(ContractError, "stdout limit"):
                client.call("test", {})

    def test_codex_app_server_uses_stdio_and_wrong_marker_falls_through_to_next_route(self) -> None:
        mesh = parse_mesh(fixture("mesh-v1.json"))
        local_commands: list[list[str]] = []
        remote_commands: list[list[str]] = []
        marker_timeouts: list[float] = []

        class LocalClient:
            def __init__(self, command: list[str], *_args: object, **_kwargs: object) -> None:
                local_commands.append(command)
                self.timeout = 0.0

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def initialize(self) -> None:
                return None

            def call(self, method: str, _params: object) -> object:
                return {"data": []} if method == "thread/list" else {}

        backend = ContractBackend(
            "rofi-ssh-plus",
            "rofi-tmux-plus",
            runner=lambda argv, **_kwargs: output(argv, {"installed": False, "sessions": []}),
        )
        backend.mesh = mesh
        with mock.patch("rofi_agent_plus.contract_backend.AppServerClient", LocalClient):
            backend._provider_results(mesh.local, PickerConfig(), time.monotonic() + 2)
        self.assertEqual([["codex", "app-server", "--stdio"]], local_commands)

        class RemoteClient:
            def __init__(self, command: list[str], *_args: object, **_kwargs: object) -> None:
                remote_commands.append(command)
                self.timeout = 0.0
                self._first = command[-2] == "beta-first.test"

            def wait_for_marker(self, timeout: float) -> tuple[bool, str]:
                marker_timeouts.append(timeout)
                return (False, "wrong reached marker") if self._first else (True, "")

            def marker_result(self) -> tuple[bool, str]:
                return self.wait_for_marker(0)

            def initialize(self) -> None:
                return None

            def call(self, method: str, _params: object) -> object:
                return {"data": []} if method == "thread/list" else {}

            def close(self) -> None:
                return None

        backend._report_hint = mock.Mock()  # type: ignore[method-assign]
        with mock.patch("rofi_agent_plus.contract_backend.AppServerClient", RemoteClient):
            self.assertEqual(
                [],
                backend._remote_codex_threads(mesh.hosts[1], PickerConfig(), time.monotonic() + 10),
            )
        self.assertEqual(
            ["beta-first.test", "beta-fallback.test"],
            [command[-2] for command in remote_commands],
        )
        self.assertTrue(
            all("codex app-server --stdio" in command[-1] for command in remote_commands)
        )
        self.assertEqual(2, len(marker_timeouts))
        self.assertTrue(
            all(
                mesh.connect_timeout + 3.5 <= timeout <= mesh.connect_timeout + 4
                for timeout in marker_timeouts
            )
        )
        backend._report_hint.assert_called_once_with(
            mesh.hosts[1], mesh.hosts[1].routes[1], "reachable", mock.ANY
        )

    def test_report_envelopes_are_typed_and_nonstale_hints_do_not_abort(self) -> None:
        mesh = parse_mesh(fixture("mesh-v1.json"))
        backend = ContractBackend("rofi-ssh-plus", "rofi-tmux-plus", runner=lambda *_a, **_k: None)
        backend.mesh = mesh
        host, route = mesh.hosts[1], mesh.hosts[1].routes[0]
        backend._run = lambda argv, **_kwargs: output(  # type: ignore[method-assign]
            argv, {"schemaVersion": 1, "ok": True, "accepted": False}
        )
        backend._report(host, route, "reachable", time.monotonic() + 2)
        backend._run = lambda argv, **_kwargs: output(  # type: ignore[method-assign]
            argv,
            {"schemaVersion": 1, "ok": False, "error": {"code": "stale_mesh"}},
            code=2,
        )
        with self.assertRaises(StaleMeshError):
            backend._report(host, route, "reachable", time.monotonic() + 2)
        backend._run = lambda argv, **_kwargs: CommandOutput(  # type: ignore[method-assign]
            tuple(argv), 2, "not-json", "ordinary stale_mesh wording"
        )
        with self.assertRaises(ContractError) as caught:
            backend._report(host, route, "reachable", time.monotonic() + 2)
        self.assertNotIsInstance(caught.exception, StaleMeshError)

    def test_remote_probe_keeps_root_codex_and_claude_fd_fallback_without_tmux(self) -> None:
        root_id = THREAD
        child_id = "22222222-2222-2222-2222-222222222222"
        claude_id = "33333333-3333-3333-3333-333333333333"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            proc = base / "proc"
            scripts = base / "bin"
            scripts.mkdir()
            ps = scripts / "ps"
            ps.write_text(
                "#!/bin/sh\nprintf '%s\\n' '10 1 codex' '11 1 claude' '12 1 codex' '13 1 codex'\n"
            )
            ps.chmod(0o700)
            commands = {
                10: b"codex\0",
                11: b"claude\0",
                12: b"codex\0app-server\0--listen\0" + b"127.0.0.1\0",
                13: b"codex\0",
            }
            for pid, command in commands.items():
                fd = proc / str(pid) / "fd"
                fd.mkdir(parents=True)
                (proc / str(pid) / "cmdline").write_bytes(command)
            root_rollout = base / f"rollout-{root_id}.jsonl"
            root_rollout.write_text('{"type":"session_meta","payload":{"source":{}}}\n')
            child_rollout = base / f"rollout-{child_id}.jsonl"
            child_rollout.write_text(
                '{"type":"session_meta","payload":{"source":{"subagent":true}}}\n'
            )
            projects = base / "projects"
            projects.mkdir()
            transcript = projects / f"{claude_id}.jsonl"
            transcript.write_text("{}\n")
            (proc / "10" / "fd" / "3").symlink_to(root_rollout)
            (proc / "10" / "fd" / "4").symlink_to(child_rollout)
            (proc / "13" / "fd" / "3").symlink_to(root_rollout)
            (proc / "11" / "fd" / "3").symlink_to(transcript)
            environment = {
                **os.environ,
                "PATH": str(scripts),
                "ROFI_AGENT_PLUS_PROC_ROOT": str(proc),
            }
            result = subprocess.run(
                [sys.executable, "-c", _ACTIVE_PROBE],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
        active = json.loads(result.stdout)
        self.assertEqual({root_id}, set(active["active"]))
        root_candidates = active["active"][root_id]["candidates"]
        self.assertEqual(2, len(root_candidates))
        self.assertEqual({10, 13}, {candidate["pid"] for candidate in root_candidates})
        self.assertEqual({claude_id}, set(active["claudeActive"]))
        validated = _validate_active(active)
        self.assertEqual(active["nativeHostname"], validated["nativeHostname"])
        self.assertEqual(active["active"], validated["active"])
        self.assertEqual(active["claudeActive"], validated["claudeActive"])
        self.assertEqual(active["opencodeActive"], validated["opencodeActive"])
        self.assertNotIn("tmux", _ACTIVE_PROBE)


class ContractBackendAssemblyTest(unittest.TestCase):
    def test_outside_tmux_active_is_preserved_and_inventory_is_subordinate(self) -> None:
        mesh_payload = fixture("mesh-v1.json")
        inventory_payload = fixture("tmux-inventory-v1.json")

        def runner(argv: list[str], **_kwargs: object) -> CommandOutput:
            if argv[1] == "inventory":
                return output(argv, inventory_payload)
            raise AssertionError(argv)

        backend = ContractBackend("rofi-ssh-plus", "rofi-tmux-plus", runner=runner)
        backend.mesh = parse_mesh(mesh_payload)
        active = {
            "nativeHostname": "alpha-native",
            "active": {
                THREAD: {"pid": 12345, "ancestors": [12345]},
                "22222222-2222-2222-2222-222222222222": {"pid": 9, "ancestors": [9]},
            },
            "claudeActive": {},
            "opencodeActive": {},
        }
        backend._active = lambda _host, _deadline: (None, active)  # type: ignore[method-assign]
        backend._provider_results = lambda *_args: (
            [{"id": THREAD, "name": "work", "cwd": "/work"}],
            {"installed": False, "sessions": []},
            {"installed": False, "sessions": []},
        )  # type: ignore[method-assign]
        events = backend._once(PickerConfig(max_sessions=40))
        rows = events[1]["sessions"]
        self.assertEqual(2, len(rows))
        correlated = next(row for row in rows if row["id"] == THREAD)
        self.assertEqual("$4", correlated["tmux"]["sessionId"])
        self.assertEqual("active", correlated["activityState"])
        outside = next(row for row in rows if row["id"].startswith("2222"))
        self.assertTrue(outside["active"])
        self.assertNotIn("tmux", outside)

    def test_stale_mesh_retries_once_before_any_events_escape(self) -> None:
        backend = ContractBackend("rofi-ssh-plus", "rofi-tmux-plus", runner=lambda *_a, **_k: None)
        backend.mesh = parse_mesh(fixture("mesh-v1.json"))
        events = [{"event": "refresh-started", "hosts": ["alpha"], "backend": backend.identity}]
        backend._once = mock.Mock(side_effect=[StaleMeshError("changed"), events])  # type: ignore[method-assign]
        backend.prepare = mock.Mock()  # type: ignore[method-assign]
        self.assertEqual(events, backend.stream(PickerConfig()))
        backend.prepare.assert_called_once()

    def test_only_typed_tmux_stale_envelope_retries_the_stream(self) -> None:
        mesh = parse_mesh(fixture("mesh-v1.json"))

        def runner(argv: list[str], **_kwargs: object) -> CommandOutput:
            if argv[1] != "inventory":
                raise AssertionError(argv)
            return output(
                argv,
                {"schemaVersion": 1, "ok": False, "error": {"code": "stale_mesh"}},
                code=2,
            )

        backend = ContractBackend("rofi-ssh-plus", "rofi-tmux-plus", runner=runner)
        backend.mesh = mesh
        backend._active = lambda _host, _deadline: (
            None,
            {  # type: ignore[method-assign]
                "nativeHostname": "native",
                "active": {},
                "claudeActive": {},
                "opencodeActive": {},
            },
        )
        backend._provider_results = lambda *_args: (  # type: ignore[method-assign]
            [],
            {"installed": False, "sessions": []},
            {"installed": False, "sessions": []},
        )
        with self.assertRaises(StaleMeshError):
            backend._once(PickerConfig())

        backend._run = lambda argv, **_kwargs: CommandOutput(  # type: ignore[method-assign]
            tuple(argv), 2, "{}", "a stale_mesh-looking diagnostic"
        )
        events = backend._once(PickerConfig())
        self.assertIn("tmux", str(events[1]["errors"]))

    def test_contract_rows_fail_closed_before_legacy_open(self) -> None:
        with mock.patch("rofi_agent_plus.rofi.engine.resolve_host_target") as resolve:
            with self.assertRaisesRegex(PickerError, "prepared authority"):
                _open_selection(
                    {"contractMode": True, "kind": "codex", "id": THREAD},
                    PickerConfig(),
                )
        resolve.assert_not_called()

    def test_rendered_contract_info_round_trips_and_cannot_fall_into_legacy_open(self) -> None:
        row = {
            "contractMode": True,
            "backend": {
                "kind": "contract",
                "capability": CONTRACT_CAPABILITY,
                "meshRevision": "sha256:mesh-v1",
            },
            "hostId": "beta",
            "kind": "codex",
            "id": THREAD,
            "name": "session",
            "host": "Beta",
            "cwd": "/work",
            "tmux": {
                "meshRevision": "sha256:mesh-v1",
                "serverGeneration": "tmux-v1:beta",
                "sessionId": "$4",
                "createdAt": 5,
                "observedName": None,
            },
        }
        rendered = render_snapshot({"sessions": [row], "errors": []})
        info = rendered.split("\x00info\x1f", 1)[1].split("\x1fmeta\x1f", 1)[0]
        selected = _parse_selection(info)
        self.assertTrue(selected["contractMode"])
        with mock.patch("rofi_agent_plus.rofi.engine.resolve_host_target") as resolve:
            with self.assertRaisesRegex(PickerError, "prepared authority"):
                _open_selection(selected, PickerConfig())
        resolve.assert_not_called()


class ContractCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = PickerConfig(max_sessions=40)

    def test_contract_prunes_removed_hosts_and_does_not_bless_partial_data(self) -> None:
        previous = {
            "version": CACHE_VERSION,
            "fingerprint": self.config.fingerprint,
            "generatedAt": 100,
            "backend": {
                "kind": "contract",
                "capability": CONTRACT_CAPABILITY,
                "meshRevision": "sha256:old",
            },
            "hosts": {
                "alpha": {
                    "sessions": [{"hostId": "alpha", "kind": "codex", "id": THREAD}],
                    "errors": [],
                },
                "removed": {"sessions": [], "errors": []},
            },
            "sessions": [],
            "errors": [],
        }
        events = iter(
            [
                {
                    "event": "refresh-started",
                    "hosts": ["alpha"],
                    "backend": {
                        "kind": "contract",
                        "capability": CONTRACT_CAPABILITY,
                        "meshRevision": "sha256:new",
                    },
                },
                {
                    "event": "host-complete",
                    "host": "alpha",
                    "sessions": [],
                    "errors": [],
                    "backend": {
                        "kind": "contract",
                        "capability": CONTRACT_CAPABILITY,
                        "meshRevision": "sha256:new",
                    },
                },
                {
                    "event": "refresh-finished",
                    "backend": {
                        "kind": "contract",
                        "capability": CONTRACT_CAPABILITY,
                        "meshRevision": "sha256:new",
                    },
                },
            ]
        )
        snapshot = build_snapshot(self.config, events, previous, now=200)
        self.assertEqual({"alpha"}, set(snapshot["hosts"]))
        self.assertEqual("sha256:new", snapshot["backend"]["meshRevision"])
        self.assertEqual(200, snapshot["generatedAt"])
        self.assertEqual([], snapshot["sessions"])

        partial = build_snapshot(
            self.config,
            iter(
                [
                    {
                        "event": "refresh-started",
                        "hosts": ["alpha"],
                        "backend": {
                            "kind": "contract",
                            "capability": CONTRACT_CAPABILITY,
                            "meshRevision": "sha256:old",
                        },
                    }
                ]
            ),
            previous,
            now=300,
        )
        self.assertEqual(100, partial["generatedAt"])
        self.assertIn("incomplete contract host coverage", json.dumps(partial["errors"]))

    def test_typed_tmx_failure_retains_only_a_marked_stale_subordinate_ref(self) -> None:
        old_session = {
            "contractMode": True,
            "hostId": "alpha",
            "kind": "codex",
            "id": THREAD,
            "active": False,
            "activityState": "idle",
            "tmuxSession": "old",
            "tmux": {"sessionId": "$1"},
        }
        previous = build_snapshot(
            self.config,
            iter(
                [
                    {"event": "refresh-started", "hosts": ["alpha"]},
                    {
                        "event": "host-complete",
                        "host": "alpha",
                        "sessions": [old_session],
                        "errors": [],
                    },
                    {"event": "refresh-finished"},
                ]
            ),
            now=100,
        )
        fresh = dict(old_session, active=True, activityState="active")
        fresh.pop("tmux")
        fresh.pop("tmuxSession")
        current = build_snapshot(
            self.config,
            iter(
                [
                    {"event": "refresh-started", "hosts": ["alpha"]},
                    {
                        "event": "host-complete",
                        "host": "alpha",
                        "sessions": [fresh],
                        "errors": [{"host": "alpha", "stage": "tmux", "message": "offline"}],
                    },
                    {"event": "refresh-finished"},
                ]
            ),
            previous,
            now=200,
        )
        row = current["sessions"][0]
        self.assertTrue(row["active"])
        self.assertEqual("active", row["activityState"])
        self.assertEqual("$1", row["tmux"]["sessionId"])
        self.assertTrue(row["tmuxStale"])

    def test_provider_failure_keeps_metadata_but_authoritative_tmux_clear_wins(self) -> None:
        backend = {
            "kind": "contract",
            "capability": CONTRACT_CAPABILITY,
            "meshRevision": "sha256:mesh-v1",
        }
        old = {
            "contractMode": True,
            "backend": backend,
            "hostId": "alpha",
            "kind": "codex",
            "id": THREAD,
            "name": "valuable name",
            "cwd": "/valuable",
            "active": False,
            "activityState": "idle",
            "tmux": {"sessionId": "$1"},
            "tmuxSession": "old",
        }
        previous = build_snapshot(
            self.config,
            iter(
                [
                    {"event": "refresh-started", "hosts": ["alpha"], "backend": backend},
                    {"event": "host-complete", "host": "alpha", "sessions": [old], "errors": []},
                    {"event": "refresh-finished", "backend": backend},
                ]
            ),
            now=100,
        )
        active_only = {
            "contractMode": True,
            "backend": backend,
            "hostId": "alpha",
            "kind": "codex",
            "id": THREAD,
            "name": THREAD[:8],
            "active": True,
            "activityState": "active",
        }
        current = build_snapshot(
            self.config,
            iter(
                [
                    {"event": "refresh-started", "hosts": ["alpha"], "backend": backend},
                    {
                        "event": "host-complete",
                        "host": "alpha",
                        "sessions": [active_only],
                        "errors": [{"host": "alpha", "stage": "threads", "message": "offline"}],
                    },
                    {"event": "refresh-finished", "backend": backend},
                ]
            ),
            previous,
            now=200,
        )
        row = current["sessions"][0]
        self.assertEqual("valuable name", row["name"])
        self.assertTrue(row["active"])
        self.assertNotIn("tmux", row)
        self.assertNotIn("tmuxSession", row)

    def test_active_failure_keeps_only_activity_when_fresh_tmux_evidence_changed(self) -> None:
        backend = {
            "kind": "contract",
            "capability": CONTRACT_CAPABILITY,
            "meshRevision": "sha256:mesh-v1",
        }
        old = {
            "contractMode": True,
            "backend": backend,
            "hostId": "alpha",
            "kind": "codex",
            "id": THREAD,
            "name": "provider name",
            "active": True,
            "activityState": "active",
            "tmux": {"sessionId": "$1", "createdAt": 1},
            "tmuxSession": "old-tmux-name",
        }
        previous = build_snapshot(
            self.config,
            iter(
                [
                    {"event": "refresh-started", "hosts": ["alpha"], "backend": backend},
                    {"event": "host-complete", "host": "alpha", "sessions": [old], "errors": []},
                    {"event": "refresh-finished", "backend": backend},
                ]
            ),
        )
        fresh = {
            "contractMode": True,
            "backend": backend,
            "hostId": "alpha",
            "kind": "codex",
            "id": THREAD,
            "name": "provider name",
            "active": False,
            "activityState": "idle",
            "tmux": {"sessionId": "$2", "createdAt": 2},
            "tmuxSession": "renamed-tmux-session",
        }
        current = build_snapshot(
            self.config,
            iter(
                [
                    {"event": "refresh-started", "hosts": ["alpha"], "backend": backend},
                    {
                        "event": "host-complete",
                        "host": "alpha",
                        "sessions": [fresh],
                        "errors": [{"host": "alpha", "stage": "active", "message": "ps failed"}],
                    },
                    {"event": "refresh-finished", "backend": backend},
                ]
            ),
            previous,
        )
        row = current["sessions"][0]
        self.assertTrue(row["active"])
        self.assertEqual("active", row["activityState"])
        self.assertEqual("$2", row["tmux"]["sessionId"])
        self.assertEqual("renamed-tmux-session", row["tmuxSession"])

    def test_typed_tmux_status_and_row_ambiguity_only_retain_stale_when_unavailable(self) -> None:
        backend = {
            "kind": "contract",
            "capability": CONTRACT_CAPABILITY,
            "meshRevision": "sha256:mesh-v1",
        }
        old = {
            "contractMode": True,
            "backend": backend,
            "hostId": "alpha",
            "kind": "codex",
            "id": THREAD,
            "tmux": {"sessionId": "$1"},
            "tmuxSession": "old",
        }
        previous = build_snapshot(
            self.config,
            iter(
                [
                    {"event": "refresh-started", "hosts": ["alpha"], "backend": backend},
                    {"event": "host-complete", "host": "alpha", "sessions": [old], "errors": []},
                    {"event": "refresh-finished", "backend": backend},
                ]
            ),
        )
        for stage, expect_stale in (
            ("tmux", True),
            ("tmux-missing", False),
            ("tmux-correlation", False),
        ):
            with self.subTest(stage=stage):
                fresh = {
                    key: value for key, value in old.items() if key not in {"tmux", "tmuxSession"}
                }
                if stage == "tmux-correlation":
                    fresh["tmuxAmbiguous"] = True
                snapshot = build_snapshot(
                    self.config,
                    iter(
                        [
                            {"event": "refresh-started", "hosts": ["alpha"], "backend": backend},
                            {
                                "event": "host-complete",
                                "host": "alpha",
                                "sessions": [fresh],
                                "errors": [{"host": "alpha", "stage": stage, "message": stage}],
                            },
                            {"event": "refresh-finished", "backend": backend},
                        ]
                    ),
                    previous,
                )
                row = snapshot["sessions"][0]
                self.assertEqual(expect_stale, bool(row.get("tmuxStale")))
                self.assertEqual(expect_stale, "tmux" in row)

    def test_cache_identity_and_background_marker_are_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CacheStore(Path(temporary) / "cache")
            snapshot = {
                "version": CACHE_VERSION,
                "fingerprint": self.config.fingerprint,
                "generatedAt": int(time.time()),
                "backend": {
                    "kind": "contract",
                    "capability": CONTRACT_CAPABILITY,
                    "meshRevision": "sha256:a",
                },
                "hosts": {},
                "sessions": [],
                "errors": [],
            }
            store.write(snapshot)
            self.assertIsNone(
                store.load(
                    self.config.fingerprint,
                    {"kind": "legacy", "capability": "legacy-v1", "meshRevision": None},
                )
            )
            self.assertIsNone(
                store.load(
                    self.config.fingerprint,
                    {
                        "kind": "contract",
                        "capability": CONTRACT_CAPABILITY,
                        "meshRevision": "sha256:b",
                    },
                )
            )
            with mock.patch("rofi_agent_plus.cache.subprocess.Popen") as popen:
                popen.return_value.pid = 7
                self.assertTrue(store.spawn_background(["fake"], scope={"fingerprint": "a"}))
                self.assertFalse(store.background_active(scope={"fingerprint": "b"}))
                self.assertTrue(store.spawn_background(["fake"], scope={"fingerprint": "b"}))
            self.assertTrue(store.background_active(scope={"fingerprint": "b"}))

    def test_background_owner_cannot_clear_a_stale_replacement_or_strand_spawn_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CacheStore(Path(temporary) / "cache")
            with mock.patch("rofi_agent_plus.cache.subprocess.Popen"):
                self.assertTrue(store.spawn_background(["fake"], scope={"fingerprint": "a"}))
                old = json.loads(store.background_path.read_text())
                os.utime(store.background_path, (0, 0))
                self.assertTrue(store.spawn_background(["fake"], scope={"fingerprint": "a"}))
            replacement = json.loads(store.background_path.read_text())
            old_worker = CacheStore(store.root)
            old_worker._background_owner = old["owner"]
            old_worker.clear_owned_background_marker()
            self.assertEqual(
                replacement["owner"], json.loads(store.background_path.read_text())["owner"]
            )
            store.clear_background_marker(owner=replacement["owner"], scope={"fingerprint": "a"})
            with mock.patch("rofi_agent_plus.cache.subprocess.Popen", side_effect=OSError("nope")):
                self.assertFalse(store.spawn_background(["fake"], scope={"fingerprint": "a"}))
            self.assertFalse(store.background_path.exists())

    def test_late_owner_cannot_write_after_authority_switch(self) -> None:
        old = {"kind": "contract", "capability": CONTRACT_CAPABILITY, "meshRevision": "sha256:old"}
        new = {"kind": "contract", "capability": CONTRACT_CAPABILITY, "meshRevision": "sha256:new"}

        class Backend:
            def __init__(self, identity: dict[str, object]) -> None:
                self.identity = identity

            def prepare(self) -> None:
                return None

            def stream(self, _config: PickerConfig):
                yield {"event": "refresh-started", "hosts": ["alpha"], "backend": old}
                yield {
                    "event": "host-complete",
                    "host": "alpha",
                    "sessions": [],
                    "errors": [],
                    "backend": old,
                }
                yield {"event": "refresh-finished", "backend": old}

        with tempfile.TemporaryDirectory() as temporary:
            selector = mock.Mock(side_effect=[Backend(old), Backend(new)])
            store = CacheStore(Path(temporary) / "cache", backend_selector=selector)
            returned = store.refresh(self.config, force=True)
            self.assertEqual(new, returned["backend"])
            self.assertEqual(0, returned["generatedAt"])
            self.assertIsNone(store.load(self.config.fingerprint, old))
            self.assertIsNone(store.load(self.config.fingerprint, new))

    def test_contract_prepare_error_is_cached_and_presentation_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CacheStore(
                Path(temporary) / "cache",
                backend_selector=mock.Mock(side_effect=ContractError("bad mesh")),
            )
            written = store.refresh(self.config, force=True)
            context = store.presentation_context(self.config)
            visible = store.load_current(self.config, context)
            self.assertEqual("contract-error", written["backend"]["kind"])
            self.assertEqual(written, visible)
            self.assertIn("bad mesh", json.dumps(visible["errors"]))

    def test_structural_rofi_callback_renders_a_present_contract_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CacheStore(
                Path(temporary) / "cache",
                backend_selector=mock.Mock(side_effect=ContractError("bad mesh now")),
            )
            rendered = io.StringIO()
            with mock.patch("sys.stdout", rendered):
                self.assertEqual(
                    0,
                    run_rofi(
                        {"ROFI_RETV": str(11), "ROFI_INFO": "host-selection"},
                        store=store,
                        config=self.config,
                    ),
                )
            self.assertIn("Contract refresh failed", rendered.getvalue())
            self.assertIn("bad mesh now", rendered.getvalue())

    def test_refresh_uses_the_prepared_presentation_context_for_its_stream(self) -> None:
        identity = {
            "kind": "contract",
            "capability": CONTRACT_CAPABILITY,
            "meshRevision": "sha256:mesh-v1",
        }

        class Backend:
            def __init__(self) -> None:
                self.prepared = 0
                self.streamed = 0
                self.identity = identity

            def prepare(self) -> None:
                self.prepared += 1

            def stream(self, _config: PickerConfig):
                self.streamed += 1
                yield {"event": "refresh-started", "hosts": ["alpha"], "backend": identity}
                yield {"event": "host-complete", "host": "alpha", "sessions": [], "errors": []}
                yield {"event": "refresh-finished", "backend": identity}

        with tempfile.TemporaryDirectory() as temporary:
            prepared = Backend()
            authority_check = Backend()
            store = CacheStore(
                Path(temporary) / "cache",
                backend_selector=mock.Mock(side_effect=[prepared, authority_check]),
            )
            context = store.presentation_context(self.config)
            store.refresh(self.config, force=True, context=context)
            self.assertEqual(1, prepared.streamed)
            self.assertEqual(1, prepared.prepared)
