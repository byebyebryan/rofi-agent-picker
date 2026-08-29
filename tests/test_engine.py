from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from rofi_agent_picker import engine as picker

THREAD_A = "00000000-0000-0000-0000-000000000001"
THREAD_B = "00000000-0000-0000-0000-000000000002"
THREAD_C = "00000000-0000-0000-0000-000000000003"
THREAD_D = "00000000-0000-0000-0000-000000000004"
OPENCODE_ID = "ses_0319af718ffegy8N1IoMEggx4B"
EMPTY_OPCODE = {"installed": False, "sessions": []}


class CodexThreadTest(unittest.TestCase):
    def test_lists_only_dedicated_cli_sessions(self) -> None:
        client = mock.MagicMock()
        client.call.return_value = {"data": [{"id": THREAD_A}]}
        context = mock.MagicMock()
        context.__enter__.return_value = client

        with (
            mock.patch.object(picker, "_app_server_command", return_value=["codex"]),
            mock.patch.object(picker, "AppServerClient", return_value=context) as app_server_client,
        ):
            result = picker.list_codex_threads(picker.HostTarget(None), 20, 1.0)

        self.assertEqual([{"id": THREAD_A}], result)
        app_server_client.assert_called_once_with(
            ["codex"],
            1.0,
            picker.VERSION,
            picker.PickerError,
        )
        client.call.assert_called_once_with(
            "thread/list",
            {
                "archived": False,
                "limit": 20,
                "sortDirection": "desc",
                "sortKey": "recency_at",
                "sourceKinds": ["cli"],
                "useStateDbOnly": True,
            },
        )

    def test_detects_only_persistent_app_server_transports(self) -> None:
        self.assertTrue(
            picker._is_shared_codex_app_server(["codex", "app-server", "--listen", "unix://"])
        )
        self.assertTrue(
            picker._is_shared_codex_app_server(
                ["codex", "app-server", "--listen=ws://127.0.0.1:4500"]
            )
        )
        self.assertFalse(picker._is_shared_codex_app_server(["codex", "app-server", "--stdio"]))
        self.assertFalse(
            picker._is_shared_codex_app_server(["codex", "app-server", "--listen", "stdio://"])
        )

    def test_active_probe_ignores_shared_app_server(self) -> None:
        process_table = mock.Mock(stdout="100 1 codex\n200 1 codex\n300 1 claude\n")
        with (
            mock.patch.object(picker.subprocess, "run", return_value=process_table),
            mock.patch.object(
                picker,
                "_process_arguments",
                side_effect=lambda pid: (
                    ["codex", "app-server", "--listen", "unix://"] if pid == 100 else ["codex"]
                ),
            ),
        ):
            parents, codex_pids, claude_pids, opencode_pids = picker._process_table()

        self.assertEqual({100: 1, 200: 1, 300: 1}, parents)
        self.assertEqual({200}, codex_pids)
        self.assertEqual({300}, claude_pids)
        self.assertEqual(set(), opencode_pids)

    def test_thread_id_for_process_prefers_root_rollout_over_subagent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            proc_root = root / "proc"
            fd_dir = proc_root / "200" / "fd"
            fd_dir.mkdir(parents=True)

            root_rollout = root / f"rollout-2026-07-21T00-00-00-{THREAD_A}.jsonl"
            root_rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"source": "cli"}}) + "\n"
            )
            subagent_rollout = root / f"rollout-2026-07-21T00-00-01-{THREAD_B}.jsonl"
            subagent_rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "source": {"subagent": {"thread_spawn": {"parent_thread_id": THREAD_A}}}
                        },
                    }
                )
                + "\n"
            )
            (fd_dir / "3").symlink_to(subagent_rollout)
            (fd_dir / "4").symlink_to(root_rollout)

            self.assertEqual(THREAD_A, picker._thread_id_for_process(200, proc_root))

    def test_active_snapshot_prefers_managed_tmux_thread_id(self) -> None:
        with (
            mock.patch.object(
                picker,
                "_process_table",
                return_value=({200: 400}, {200}, set(), set()),
            ),
            mock.patch.object(
                picker,
                "_tmux_panes",
                return_value=({400: "codex-picker"}, {THREAD_A: "codex-picker"}, {}, {}),
            ),
            mock.patch.object(picker, "_thread_id_for_process", return_value=THREAD_B) as detect,
        ):
            active = picker.active_snapshot()

        self.assertEqual(
            {THREAD_A: {"pid": 200, "tmuxSession": "codex-picker"}},
            active["active"],
        )
        detect.assert_not_called()

    def test_remote_active_probe_matches_managed_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            proc_root = root / "proc"
            bin_dir = root / "bin"
            bin_dir.mkdir()

            def write_cmdline(pid: int, arguments: list[str]) -> None:
                process_dir = proc_root / str(pid)
                process_dir.mkdir(parents=True)
                (process_dir / "cmdline").write_bytes("\0".join(arguments).encode() + b"\0")

            def link_fd(pid: int, descriptor: int, target: Path) -> None:
                fd_dir = proc_root / str(pid) / "fd"
                fd_dir.mkdir(parents=True, exist_ok=True)
                (fd_dir / str(descriptor)).symlink_to(target)

            write_cmdline(100, ["codex", "app-server", "--listen", "unix://"])
            write_cmdline(200, ["codex"])
            write_cmdline(300, ["claude", "--resume", THREAD_C])
            write_cmdline(700, ["opencode", "--session", OPENCODE_ID])
            link_fd(200, 3, root / f"rollout-2026-07-21T00-00-00-{THREAD_A}.jsonl")

            (bin_dir / "ps").write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '100 1 codex' '200 400 codex' '300 500 claude' '700 800 opencode'\n"
            )
            (bin_dir / "tmux").write_text(
                "#!/bin/sh\n"
                f"printf 'codex-picker\\t400\\t{THREAD_A}\\t\\n'\n"
                f"printf 'claude-picker\\t500\\t\\t{THREAD_C}\\n'\n"
                f"printf 'opencode-picker\\t800\\t\\t\\t{OPENCODE_ID}\\n'\n"
            )
            (bin_dir / "claude").write_text("#!/bin/sh\nexit 0\n")
            (bin_dir / "opencode").write_text("#!/bin/sh\nexit 0\n")
            for executable in bin_dir.iterdir():
                executable.chmod(0o755)

            environment = {
                **os.environ,
                "DMS_AGENT_PICKER_PROC_ROOT": str(proc_root),
                "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            }
            result = subprocess.run(
                [sys.executable, "-c", picker.ACTIVE_PROBE],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual({THREAD_A: {"pid": 200, "tmuxSession": "codex-picker"}}, payload["active"])
        self.assertEqual(
            {THREAD_C: {"pid": 300, "tmuxSession": "claude-picker"}},
            payload["claudeActive"],
        )
        self.assertTrue(payload["claudeInstalled"])
        self.assertEqual(
            {OPENCODE_ID: {"pid": 700, "tmuxSession": "opencode-picker"}},
            payload["opencodeActive"],
        )
        self.assertTrue(payload["opencodeInstalled"])

    def test_remote_active_probe_prefers_managed_id_over_fd_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            proc_root = root / "proc"
            bin_dir = root / "bin"
            bin_dir.mkdir()

            process_dir = proc_root / "200"
            fd_dir = process_dir / "fd"
            fd_dir.mkdir(parents=True)
            (process_dir / "cmdline").write_bytes(b"codex\0")
            (fd_dir / "3").symlink_to(root / f"rollout-2026-07-21T00-00-00-{THREAD_B}.jsonl")

            (bin_dir / "ps").write_text("#!/bin/sh\nprintf '%s\\n' '200 400 codex'\n")
            (bin_dir / "tmux").write_text(
                f"#!/bin/sh\nprintf 'codex-session\\t400\\t{THREAD_A}\\t\\n'\n"
            )
            for executable in (bin_dir / "ps", bin_dir / "tmux"):
                executable.chmod(0o755)

            environment = {
                **os.environ,
                "DMS_AGENT_PICKER_PROC_ROOT": str(proc_root),
                "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            }
            result = subprocess.run(
                [sys.executable, "-c", picker.ACTIVE_PROBE],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            {THREAD_A: {"pid": 200, "tmuxSession": "codex-session"}},
            payload["active"],
        )

    def test_remote_active_probe_prefers_root_rollout_over_subagent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            proc_root = root / "proc"
            bin_dir = root / "bin"
            bin_dir.mkdir()

            process_dir = proc_root / "200"
            fd_dir = process_dir / "fd"
            fd_dir.mkdir(parents=True)
            (process_dir / "cmdline").write_bytes(b"codex\0")

            root_rollout = root / f"rollout-2026-07-21T00-00-00-{THREAD_A}.jsonl"
            root_rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"source": "cli"}}) + "\n"
            )
            subagent_rollout = root / f"rollout-2026-07-21T00-00-01-{THREAD_B}.jsonl"
            subagent_rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "source": {"subagent": {"thread_spawn": {"parent_thread_id": THREAD_A}}}
                        },
                    }
                )
                + "\n"
            )
            (fd_dir / "3").symlink_to(subagent_rollout)
            (fd_dir / "4").symlink_to(root_rollout)

            (bin_dir / "ps").write_text("#!/bin/sh\nprintf '%s\\n' '200 400 codex'\n")
            (bin_dir / "tmux").write_text("#!/bin/sh\nprintf 'codex-session\\t400\\t\\n'\n")
            for executable in (bin_dir / "ps", bin_dir / "tmux"):
                executable.chmod(0o755)

            environment = {
                **os.environ,
                "DMS_AGENT_PICKER_PROC_ROOT": str(proc_root),
                "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            }
            result = subprocess.run(
                [sys.executable, "-c", picker.ACTIVE_PROBE],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            {THREAD_A: {"pid": 200, "tmuxSession": "codex-session"}},
            payload["active"],
        )


class StreamingSessionTest(unittest.TestCase):
    def test_list_parser_defaults_to_40_sessions(self) -> None:
        args = picker.build_parser().parse_args(["list"])

        self.assertEqual(40, args.limit)

    def test_stream_emits_completed_hosts_in_completion_order(self) -> None:
        def sleep_for_local(target: picker.HostTarget) -> None:
            if target.key == "local":
                time.sleep(0.15)

        def threads(
            target: picker.HostTarget,
            _limit: int,
            _timeout: float,
            _policy: picker.SshPolicy,
        ) -> list[dict[str, object]]:
            sleep_for_local(target)
            return [
                {
                    "id": THREAD_B if target.key == "local" else THREAD_A,
                    "name": target.key,
                    "cwd": "/home/test",
                    "recencyAt": 1,
                }
            ]

        def claude(
            target: picker.HostTarget,
            _limit: int,
            _timeout: float,
            _policy: picker.SshPolicy,
        ) -> dict[str, object]:
            sleep_for_local(target)
            return {"installed": False, "sessions": []}

        def active(
            target: picker.HostTarget,
            _timeout: float,
            _policy: picker.SshPolicy,
        ) -> dict[str, object]:
            sleep_for_local(target)
            return {"host": target.key, "active": {}, "claudeActive": {}, "opencodeActive": {}}

        with (
            mock.patch.object(picker, "list_codex_threads", side_effect=threads),
            mock.patch.object(picker, "list_claude_sessions", side_effect=claude),
            mock.patch.object(
                picker,
                "list_opencode_sessions",
                return_value={"installed": False, "sessions": []},
            ),
            mock.patch.object(picker, "get_active_snapshot", side_effect=active),
        ):
            events = list(picker.stream_session_events(["fast.lan"], limit=20, timeout=1.0))

        self.assertEqual({"event": "refresh-started", "hosts": ["local", "fast.lan"]}, events[0])
        self.assertEqual(["fast.lan", "local"], [event["host"] for event in events[1:3]])
        self.assertEqual("host-complete", events[1]["event"])
        self.assertEqual(THREAD_A, events[1]["sessions"][0]["id"])
        self.assertEqual("refresh-finished", events[3]["event"])
        self.assertIn("generatedAt", events[3])

    def test_stream_preserves_host_partial_failures(self) -> None:
        with (
            mock.patch.object(
                picker,
                "list_codex_threads",
                side_effect=picker.PickerError("codex unavailable"),
            ),
            mock.patch.object(
                picker,
                "list_claude_sessions",
                return_value={
                    "installed": True,
                    "sessions": [
                        {
                            "id": THREAD_C,
                            "name": "Claude task",
                            "cwd": "/home/test",
                            "recencyAt": 2,
                        }
                    ],
                },
            ),
            mock.patch.object(
                picker,
                "list_opencode_sessions",
                return_value={"installed": False, "sessions": []},
            ),
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "desktop",
                    "active": {},
                    "claudeActive": {},
                    "opencodeActive": {},
                },
            ),
        ):
            events = list(
                picker.stream_session_events([], limit=20, timeout=1.0, include_local=True)
            )

        event = events[1]
        self.assertEqual("host-complete", event["event"])
        self.assertEqual("local", event["host"])
        self.assertEqual([THREAD_C], [session["id"] for session in event["sessions"]])
        self.assertEqual("threads", event["errors"][0]["stage"])

    def test_main_streams_jsonl_only_when_requested(self) -> None:
        events = [
            {"event": "refresh-started", "hosts": ["local"]},
            {"event": "refresh-finished", "generatedAt": 1},
        ]
        output = io.StringIO()
        with (
            mock.patch.object(picker, "stream_session_events", return_value=iter(events)),
            mock.patch.object(sys, "stdout", output),
        ):
            result = picker.main(["list", "--stream"])

        self.assertEqual(0, result)
        self.assertEqual(events, [json.loads(line) for line in output.getvalue().splitlines()])

    def test_main_list_keeps_single_json_payload(self) -> None:
        payload = {"generatedAt": 1, "sessions": [], "errors": []}
        output = io.StringIO()
        with (
            mock.patch.object(picker, "aggregate_sessions", return_value=payload),
            mock.patch.object(sys, "stdout", output),
        ):
            result = picker.main(["list"])

        self.assertEqual(0, result)
        self.assertEqual(payload, json.loads(output.getvalue()))


class MergeHostResultsTest(unittest.TestCase):
    def test_sorts_by_recency_marks_active_and_deduplicates_aliases(self) -> None:
        laptop_threads = [
            {
                "id": THREAD_A,
                "name": "cubey",
                "cwd": "/home/test/code/cubey",
                "recencyAt": 20,
                "updatedAt": 30,
            }
        ]
        local_threads = [
            {
                "id": THREAD_B,
                "name": "system",
                "cwd": "/home/test",
                "recencyAt": 40,
                "updatedAt": 50,
            }
        ]
        laptop_active = {
            "host": "laptop",
            "active": {THREAD_A: {"pid": 42, "tmuxSession": "cubey"}},
        }

        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget(None),
                    local_threads,
                    {"installed": False, "sessions": []},
                    EMPTY_OPCODE,
                    {"host": "desktop", "active": {}},
                ),
                (
                    picker.HostTarget("laptop.lan"),
                    laptop_threads,
                    {"installed": False, "sessions": []},
                    EMPTY_OPCODE,
                    laptop_active,
                ),
                (
                    picker.HostTarget("laptop-alias"),
                    laptop_threads,
                    {"installed": False, "sessions": []},
                    EMPTY_OPCODE,
                    laptop_active,
                ),
            ],
            limit=20,
        )

        self.assertEqual([THREAD_B, THREAD_A], [item["id"] for item in result["sessions"]])
        self.assertTrue(result["sessions"][1]["active"])
        self.assertEqual("cubey", result["sessions"][1]["tmuxSession"])
        self.assertEqual("laptop.lan", result["sessions"][1]["connectHost"])
        self.assertEqual("laptop", result["sessions"][1]["windowHost"])

    def test_remote_active_failure_keeps_sessions_idle(self) -> None:
        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget("laptop"),
                    [
                        {
                            "id": THREAD_A,
                            "name": "codex",
                            "cwd": "/home/test",
                            "recencyAt": 5,
                            "updatedAt": 7,
                        }
                    ],
                    {"installed": False, "sessions": []},
                    EMPTY_OPCODE,
                    picker.PickerError("probe failed"),
                )
            ],
            limit=20,
        )

        self.assertFalse(result["sessions"][0]["active"])
        self.assertEqual("unknown", result["sessions"][0]["activityState"])
        self.assertEqual("active", result["errors"][0]["stage"])

    def test_claude_session_survives_codex_list_failure(self) -> None:
        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget("workstation.example"),
                    picker.PickerError("codex unavailable"),
                    {
                        "installed": True,
                        "sessions": [
                            {
                                "id": THREAD_C,
                                "name": "Improve auth flow",
                                "cwd": "/home/test/code/app",
                                "recencyAt": 80,
                                "updatedAt": 80,
                            }
                        ],
                    },
                    EMPTY_OPCODE,
                    {
                        "host": "LEGACY-HOST",
                        "active": {},
                        "claudeActive": {THREAD_C: {"pid": 42, "tmuxSession": "improve-auth-flow"}},
                        "opencodeActive": {},
                    },
                )
            ],
            limit=20,
            aliases={"legacy-host": "workstation"},
        )

        self.assertEqual(
            {
                "kind": "claude",
                "id": THREAD_C,
                "name": "Improve auth flow",
                "cwd": "/home/test/code/app",
                "host": "workstation",
                "windowHost": "LEGACY-HOST",
                "connectHost": "workstation.example",
                "recencyAt": 80,
                "updatedAt": 80,
                "active": True,
                "activityState": "active",
                "tmuxSession": "improve-auth-flow",
            },
            result["sessions"][0],
        )
        self.assertEqual("threads", result["errors"][0]["stage"])

    def test_unavailable_claude_sessions_are_omitted(self) -> None:
        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget(None),
                    [],
                    {"installed": False, "sessions": [{"id": THREAD_C}]},
                    EMPTY_OPCODE,
                    {
                        "host": "desktop",
                        "active": {},
                        "claudeActive": {},
                        "opencodeActive": {},
                    },
                )
            ],
            limit=20,
        )

        self.assertEqual([], result["sessions"])

    def test_codex_and_claude_sessions_share_recency_order(self) -> None:
        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget(None),
                    [
                        {
                            "id": THREAD_A,
                            "name": "Codex task",
                            "cwd": "/home/test/code/app",
                            "recencyAt": 10,
                        }
                    ],
                    {
                        "installed": True,
                        "sessions": [
                            {
                                "id": THREAD_C,
                                "name": "Claude task",
                                "cwd": "/home/test/code/app",
                                "recencyAt": 20,
                            }
                        ],
                    },
                    EMPTY_OPCODE,
                    {"host": "desktop", "active": {}, "claudeActive": {}, "opencodeActive": {}},
                )
            ],
            limit=20,
        )

        self.assertEqual(["claude", "codex"], [item["kind"] for item in result["sessions"]])


class OpenTargetTest(unittest.TestCase):
    def test_active_tmux_session_is_reused(self) -> None:
        with (
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "desktop",
                    "active": {THREAD_A: {"pid": 10, "tmuxSession": "cubey"}},
                },
            ),
            mock.patch.object(picker, "ensure_tmux_session") as ensure,
        ):
            session = picker.resolve_open_target(
                picker.HostTarget(None), THREAD_A, "cubey", "/tmp", 1.0
            )

        self.assertEqual("cubey", session)
        ensure.assert_not_called()

    def test_main_detaches_after_resolving_a_session(self) -> None:
        with (
            mock.patch.object(picker, "resolve_open_target", return_value="picker"),
            mock.patch.object(picker, "focus_existing_window", return_value=False),
            mock.patch.object(picker, "launch_attach") as launch_attach,
        ):
            exit_code = picker.main(["open", "--id", THREAD_A, "--detach"])

        self.assertEqual(0, exit_code)
        self.assertTrue(launch_attach.call_args.kwargs["detach"])

    def test_active_session_outside_tmux_is_not_duplicated(self) -> None:
        with (
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "desktop",
                    "active": {THREAD_A: {"pid": 10, "tmuxSession": None}},
                },
            ),
            self.assertRaisesRegex(picker.PickerError, "outside tmux"),
        ):
            picker.resolve_open_target(picker.HostTarget(None), THREAD_A, "cubey", "/tmp", 1.0)


class ClaudeSessionTest(unittest.TestCase):
    def test_discovers_named_session_from_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            claude = bin_dir / "claude"
            claude.write_text("#!/bin/sh\nexit 0\n")
            claude.chmod(0o755)

            config_dir = root / ".claude"
            project_dir = config_dir / "projects" / "-home-test-code-app"
            project_dir.mkdir(parents=True)
            transcript = project_dir / f"{THREAD_C}.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "sessionId": THREAD_C,
                                "cwd": "/home/test/code/app",
                                "entrypoint": "cli",
                                "message": {"role": "user", "content": "Initial request"},
                            }
                        ),
                        "not valid json",
                        json.dumps(
                            {
                                "type": "custom-title",
                                "sessionId": THREAD_C,
                                "customTitle": "Improve auth flow",
                            }
                        ),
                    ]
                )
                + "\n"
            )
            (project_dir / f"{THREAD_D}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": THREAD_D,
                        "cwd": "/home/test/.claude-mem/observer-sessions",
                        "entrypoint": "sdk-cli",
                        "message": {"role": "user", "content": "Observe sessions"},
                    }
                )
                + "\n"
            )
            (config_dir / "history.jsonl").write_text(
                json.dumps(
                    {
                        "display": "Initial request",
                        "project": "/home/test/code/app",
                        "sessionId": THREAD_C,
                        "timestamp": 2_000_000_000_000,
                    }
                )
                + "\n"
            )

            with mock.patch.dict(
                os.environ,
                {
                    "CLAUDE_CONFIG_DIR": str(config_dir),
                    "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                },
            ):
                result = picker.list_claude_sessions(picker.HostTarget(None), 20, 2.0)

        self.assertTrue(result["installed"])
        self.assertEqual(1, len(result["sessions"]))
        self.assertEqual(THREAD_C, result["sessions"][0]["id"])
        self.assertEqual("Improve auth flow", result["sessions"][0]["name"])
        self.assertEqual("/home/test/code/app", result["sessions"][0]["cwd"])
        self.assertEqual(2_000_000_000, result["sessions"][0]["recencyAt"])

    def test_latest_custom_title_wins_in_transcript_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            claude = bin_dir / "claude"
            claude.write_text("#!/bin/sh\nexit 0\n")
            claude.chmod(0o755)

            config_dir = root / ".claude"
            project_dir = config_dir / "projects" / "-home-test-code-app"
            project_dir.mkdir(parents=True)
            transcript = project_dir / f"{THREAD_C}.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "sessionId": THREAD_C,
                                "cwd": "/home/test/code/app",
                                "entrypoint": "cli",
                                "message": {"role": "user", "content": "Initial request"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "custom-title",
                                "sessionId": THREAD_C,
                                "customTitle": "Initial title",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "custom-title",
                                "sessionId": THREAD_C,
                                "customTitle": "Latest title",
                            }
                        ),
                        '{"type":"custom-title","customTitle":',
                        json.dumps(
                            {
                                "type": "custom-title",
                                "sessionId": THREAD_C,
                                "customTitle": "",
                            }
                        ),
                    ]
                )
                + "\n"
            )

            with mock.patch.dict(
                os.environ,
                {
                    "CLAUDE_CONFIG_DIR": str(config_dir),
                    "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                },
            ):
                result = picker.list_claude_sessions(picker.HostTarget(None), 20, 2.0)

        self.assertEqual("Latest title", result["sessions"][0]["name"])

    def test_finds_custom_title_beyond_old_head_region(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            claude = bin_dir / "claude"
            claude.write_text("#!/bin/sh\nexit 0\n")
            claude.chmod(0o755)

            config_dir = root / ".claude"
            project_dir = config_dir / "projects" / "-home-test-code-app"
            project_dir.mkdir(parents=True)
            transcript = project_dir / f"{THREAD_C}.jsonl"
            lines = [
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": THREAD_C,
                        "cwd": "/home/test/code/app",
                        "entrypoint": "cli",
                        "message": {"role": "user", "content": "Start"},
                    }
                )
            ]
            big = "x" * 300_000
            for _ in range(4):
                lines.append(
                    json.dumps(
                        {
                            "type": "user",
                            "sessionId": THREAD_C,
                            "message": {"role": "user", "content": big},
                        }
                    )
                )
            lines.append(
                json.dumps(
                    {
                        "type": "custom-title",
                        "sessionId": THREAD_C,
                        "customTitle": "Middle rename",
                    }
                )
            )
            transcript.write_text("\n".join(lines) + "\n")
            transcript_size = transcript.stat().st_size

            with mock.patch.dict(
                os.environ,
                {
                    "CLAUDE_CONFIG_DIR": str(config_dir),
                    "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                },
            ):
                result = picker.list_claude_sessions(picker.HostTarget(None), 20, 2.0)

        self.assertGreater(transcript_size, 1_000_000)
        self.assertEqual("Middle rename", result["sessions"][0]["name"])

    def test_active_snapshot_uses_managed_tmux_session_id(self) -> None:
        with mock.patch.object(picker, "_claude_session_id_for_process", return_value=None):
            active = picker._claude_active_sessions(
                parents={300: 200, 200: 1},
                claude_pids={300},
                pane_sessions={200: "auth-flow"},
                option_sessions={THREAD_C: "auth-flow"},
            )

        self.assertEqual({THREAD_C: {"pid": 300, "tmuxSession": "auth-flow"}}, active)

    def test_extracts_session_id_from_resume_arguments(self) -> None:
        self.assertEqual(
            THREAD_C,
            picker._claude_session_id_from_args(["claude", "--resume", THREAD_C]),
        )
        self.assertEqual(
            THREAD_C,
            picker._claude_session_id_from_args(["claude", f"--session-id={THREAD_C}"]),
        )

    def test_open_reuses_existing_claude_tmux_session(self) -> None:
        with (
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "LEGACY-HOST",
                    "active": {},
                    "claudeInstalled": True,
                    "claudeActive": {THREAD_C: {"pid": 42, "tmuxSession": "auth-flow"}},
                },
            ),
            mock.patch.object(picker, "ensure_claude_tmux_session") as ensure,
        ):
            session = picker.resolve_claude_open_target(
                picker.HostTarget("workstation.example"),
                THREAD_C,
                "Improve auth flow",
                "/home/test/code/app",
                1.0,
            )

        self.assertEqual("auth-flow", session)
        ensure.assert_not_called()

    def test_active_claude_outside_tmux_is_not_duplicated(self) -> None:
        with (
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "desktop",
                    "active": {},
                    "claudeInstalled": True,
                    "claudeActive": {THREAD_C: {"pid": 42, "tmuxSession": None}},
                },
            ),
            self.assertRaisesRegex(picker.PickerError, "outside tmux"),
        ):
            picker.resolve_claude_open_target(
                picker.HostTarget(None), THREAD_C, "Improve auth flow", "/tmp", 1.0
            )

    def test_inactive_claude_creates_managed_session(self) -> None:
        with (
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "desktop",
                    "active": {},
                    "claudeInstalled": True,
                    "claudeActive": {},
                },
            ),
            mock.patch.object(
                picker, "ensure_claude_tmux_session", return_value="improve-auth-flow"
            ) as ensure,
        ):
            session = picker.resolve_claude_open_target(
                picker.HostTarget(None),
                THREAD_C,
                "Improve auth flow",
                "/home/test/code/app",
                1.0,
            )

        self.assertEqual("improve-auth-flow", session)
        ensure.assert_called_once()

    def test_managed_session_resumes_exact_id_in_recorded_directory(self) -> None:
        script = picker._ensure_claude_session_script(
            THREAD_C, "Improve auth flow", "/home/test/code/app"
        )

        self.assertIn("requested_cwd=/home/test/code/app", script)
        self.assertIn("--resume", script)
        self.assertIn('"$session_id"', script)
        self.assertIn("@claude_session_id", script)


class TmuxNameTest(unittest.TestCase):
    def test_sanitizes_tmux_target_characters(self) -> None:
        self.assertEqual(
            "project-name-test",
            picker._safe_tmux_name("project:name.test", THREAD_A),
        )

    def test_rejects_invalid_ids_in_session_scripts(self) -> None:
        with self.assertRaisesRegex(picker.PickerError, "invalid Codex session id"):
            picker._ensure_session_script("not-a-uuid", "project", "/home/test")
        with self.assertRaisesRegex(picker.PickerError, "invalid Claude session id"):
            picker._ensure_claude_session_script("not-a-uuid", "project", "/home/test")
        with self.assertRaisesRegex(picker.PickerError, "invalid opencode session id"):
            picker._ensure_opencode_session_script(
                "00000000-0000-0000-0000-000000000001", "p", "/home"
            )

    def test_agent_start_waits_for_attached_tmux_client(self) -> None:
        wait_script = picker._tmux_client_wait_script()

        self.assertIn("$TMUX_PANE", wait_script)
        self.assertIn("#{session_attached}", wait_script)
        self.assertIn("terminal did not attach in time", wait_script)
        self.assertIn("tmux kill-session", wait_script)
        self.assertIn('exec "$@"', wait_script)

        codex_script = picker._ensure_session_script(THREAD_A, "project", "/home/test/code/project")
        claude_script = picker._ensure_claude_session_script(
            THREAD_C, "project", "/home/test/code/project"
        )
        opencode_script = picker._ensure_opencode_session_script(
            OPENCODE_ID, "project", "/home/test/code/project"
        )
        self.assertIn("codex_command=\"exec sh -c '$wait_script'", codex_script)
        self.assertIn("claude_command=\"exec sh -c '$wait_script'", claude_script)
        self.assertIn("opencode_command=\"exec sh -c '$wait_script'", opencode_script)
        self.assertIn("@agent_picker_waiting 1", codex_script)
        self.assertIn("@agent_picker_waiting 1", claude_script)
        self.assertIn("@agent_picker_waiting 1", opencode_script)
        self.assertIn("existing=$(tmux list-panes", codex_script)
        self.assertIn("existing=$(tmux list-panes", claude_script)
        self.assertIn("existing=$(tmux list-panes", opencode_script)

    def test_remote_attach_quotes_exact_target_for_zsh(self) -> None:
        self.assertEqual(
            "exec tmux -u attach-session -t '=desktop-config'",
            picker._remote_attach_command("desktop-config"),
        )

    def test_open_parser_accepts_detached_launches(self) -> None:
        args = picker.build_parser().parse_args(["open", "--id", THREAD_A, "--detach"])

        self.assertTrue(args.detach)


class TmuxProcessBoundaryTest(unittest.TestCase):
    def test_local_tmux_creation_uses_a_systemd_scope(self) -> None:
        with mock.patch.object(picker.shutil, "which", return_value="/usr/bin/systemd-run"):
            command = picker._tmux_creation_command(
                picker.HostTarget(None), "echo local", picker.DEFAULT_SSH_POLICY
            )

        self.assertEqual(
            [
                "/usr/bin/systemd-run",
                "--user",
                "--scope",
                "--collect",
                "--quiet",
                "--",
                "sh",
                "-lc",
                "echo local",
            ],
            command,
        )

    def test_local_tmux_creation_falls_back_without_systemd_run(self) -> None:
        with mock.patch.object(picker.shutil, "which", return_value=None):
            command = picker._tmux_creation_command(
                picker.HostTarget(None), "echo local", picker.DEFAULT_SSH_POLICY
            )

        self.assertEqual(["sh", "-lc", "echo local"], command)

    def test_remote_tmux_creation_stays_inside_ssh(self) -> None:
        with (
            mock.patch.object(picker, "_ssh_prefix", return_value=["ssh-prefix"]),
            mock.patch.object(picker.shutil, "which") as which,
        ):
            command = picker._tmux_creation_command(
                picker.HostTarget("remote.lan"),
                "echo remote",
                picker.DEFAULT_SSH_POLICY,
            )

        self.assertEqual(["ssh-prefix", "remote.lan", "sh -lc 'echo remote'"], command)
        which.assert_not_called()

    def test_detached_attach_starts_terminal_in_a_new_session(self) -> None:
        command = ["ghostty", "-e", "tmux", "attach-session", "-t", "=picker"]
        with (
            mock.patch.object(picker, "_terminal_command", return_value=command),
            mock.patch.object(picker, "_local_scope_command", return_value=command),
            mock.patch.object(picker.subprocess, "Popen") as popen,
        ):
            picker.launch_attach(picker.HostTarget(None), "picker", "ghostty", detach=True)

        popen.assert_called_once_with(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def test_attached_launch_replaces_the_helper_process(self) -> None:
        command = ["ghostty", "-e", "tmux", "attach-session", "-t", "=picker"]
        with (
            mock.patch.object(picker, "_terminal_command", return_value=command),
            mock.patch.object(picker, "_local_scope_command", return_value=command),
            mock.patch.object(picker.os, "execvp") as execvp,
        ):
            picker.launch_attach(picker.HostTarget(None), "picker", "ghostty")

        execvp.assert_called_once_with(command[0], command)


class NiriWindowTest(unittest.TestCase):
    def test_matches_exact_session_and_short_hostname(self) -> None:
        windows = [
            {
                "id": 42,
                "title": "desktop-config:0 codex | bryan @ LEGACY-HOST",
            }
        ]

        self.assertEqual(
            42,
            picker._matching_niri_window_id(windows, "desktop-config", "legacy-host.example"),
        )

    def test_rejects_same_session_on_another_host(self) -> None:
        windows = [
            {
                "id": 42,
                "title": "cubey:0 codex | cubey @ desktop",
            }
        ]

        self.assertIsNone(picker._matching_niri_window_id(windows, "cubey", "laptop"))


class HostConfigTest(unittest.TestCase):
    def test_ssh_policy_builds_noninteractive_bounded_prefix(self) -> None:
        self.assertEqual(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=2",
                "-o",
                "ConnectionAttempts=1",
                "-o",
                "LogLevel=ERROR",
            ],
            picker._ssh_prefix(picker.SshPolicy()),
        )

    def test_parses_case_insensitive_alias_source(self) -> None:
        self.assertEqual(
            {"legacy-host": "workstation"},
            picker.parse_host_aliases(["LEGACY-HOST=workstation"]),
        )

    def test_rejects_invalid_alias(self) -> None:
        with self.assertRaisesRegex(picker.PickerError, "expected source=display"):
            picker.parse_host_aliases(["workstation"])

    def test_parses_logical_route_with_fallback_paths(self) -> None:
        self.assertEqual(
            [
                picker.HostTarget(
                    "workstation-vpn.example",
                    route_key="workstation",
                    route_paths=("workstation-vpn.example", "workstation.example"),
                )
            ],
            picker.parse_host_routes(["workstation=workstation-vpn.example|workstation.example"]),
        )

    def test_rejects_invalid_or_duplicate_logical_routes(self) -> None:
        with self.assertRaisesRegex(picker.PickerError, "expected name=endpoint"):
            picker.parse_host_routes(["workstation"])
        with self.assertRaisesRegex(picker.PickerError, "duplicate host route"):
            picker.parse_host_routes(
                ["workstation=workstation-vpn.example,workstation=workstation.example"]
            )

    def test_rejects_hosts_and_endpoints_starting_with_dash(self) -> None:
        with self.assertRaisesRegex(picker.PickerError, "invalid host"):
            picker.parse_host_target("-oProxyCommand=evil")
        with self.assertRaisesRegex(picker.PickerError, "invalid host"):
            picker.build_host_targets(["-oProxyCommand=evil"], include_local=False)
        with self.assertRaisesRegex(picker.PickerError, "invalid host route endpoint"):
            picker.parse_host_routes(["workstation=-oProxyCommand=evil"])

    def test_uses_first_reachable_route_path(self) -> None:
        route = picker.parse_host_routes(
            ["workstation=workstation-vpn.example|workstation.example"]
        )[0]
        with mock.patch.object(
            picker.subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess([], 255, "", "network unreachable"),
                subprocess.CompletedProcess([], 0, "", ""),
            ],
        ) as run:
            resolved = picker.resolve_host_target(route, picker.SshPolicy())

        self.assertEqual("workstation", resolved.key)
        self.assertEqual("workstation.example", resolved.connect_host)
        self.assertEqual(
            "workstation=workstation-vpn.example|workstation.example", resolved.route_spec
        )
        self.assertEqual("workstation-vpn.example", run.call_args_list[0].args[0][-2])
        self.assertEqual("workstation.example", run.call_args_list[1].args[0][-2])

    def test_route_results_keep_logical_name_and_reconnect_spec(self) -> None:
        route = picker.HostTarget(
            "workstation.example",
            route_key="workstation",
            route_paths=("workstation-vpn.example", "workstation.example"),
        )
        result = picker.merge_host_results(
            [
                (
                    route,
                    [{"id": THREAD_A, "name": "dotfiles", "cwd": "/home/test"}],
                    {"installed": False, "sessions": []},
                    EMPTY_OPCODE,
                    {"host": "LEGACY-HOST", "active": {}, "opencodeActive": {}},
                )
            ],
            limit=20,
        )

        self.assertEqual("workstation", result["sessions"][0]["host"])
        self.assertEqual("workstation.example", result["sessions"][0]["connectHost"])
        self.assertEqual(
            "workstation=workstation-vpn.example|workstation.example",
            result["sessions"][0]["route"],
        )

    def test_alias_changes_display_host_but_preserves_window_host(self) -> None:
        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget("workstation.example"),
                    [{"id": THREAD_A, "name": "dotfiles", "cwd": "/home/test"}],
                    {"installed": False, "sessions": []},
                    EMPTY_OPCODE,
                    {"host": "LEGACY-HOST", "active": {}, "opencodeActive": {}},
                )
            ],
            limit=20,
            aliases={"legacy-host": "workstation"},
        )

        self.assertEqual("workstation", result["sessions"][0]["host"])
        self.assertEqual("LEGACY-HOST", result["sessions"][0]["windowHost"])

    def test_shared_host_list_skips_local_alias(self) -> None:
        with (
            mock.patch.object(picker.socket, "gethostname", return_value="LEGACY-HOST"),
            mock.patch.object(picker, "list_codex_threads", return_value=[]),
            mock.patch.object(
                picker,
                "list_claude_sessions",
                return_value={"installed": False, "sessions": []},
            ),
            mock.patch.object(
                picker,
                "list_opencode_sessions",
                return_value={"installed": False, "sessions": []},
            ),
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "unused",
                    "active": {},
                    "opencodeActive": {},
                },
            ) as active,
        ):
            picker.aggregate_sessions(
                ["desktop.example", "workstation.example", "laptop.example"],
                limit=20,
                timeout=1.0,
                aliases={"legacy-host": "workstation"},
            )

        queried_hosts = {call.args[0].key for call in active.call_args_list}
        self.assertEqual({"local", "desktop.example", "laptop.example"}, queried_hosts)

    def test_routes_skip_the_current_logical_host(self) -> None:
        routes = picker.parse_host_routes(
            ["workstation=workstation-vpn.example|workstation.example", "desktop=desktop.example"]
        )
        with mock.patch.object(picker.socket, "gethostname", return_value="LEGACY-HOST"):
            targets = picker.build_host_targets(
                [],
                aliases={"legacy-host": "workstation"},
                routes=routes,
            )

        self.assertEqual(["local", "desktop"], [target.key for target in targets])


class OpencodeSessionTest(unittest.TestCase):
    def _database(self, root: Path, sessions: list[tuple[object, ...]]) -> Path:
        db_dir = root / "data" / "opencode"
        db_dir.mkdir(parents=True)
        database = db_dir / "opencode.db"
        conn = sqlite3.connect(database)
        conn.execute(
            "CREATE TABLE session ("
            "id text PRIMARY KEY, title text NOT NULL, directory text NOT NULL, "
            "time_updated integer NOT NULL, time_archived integer, parent_id text"
            ")"
        )
        conn.executemany(
            "INSERT INTO session (id, title, directory, time_updated, time_archived, parent_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            sessions,
        )
        conn.commit()
        conn.close()
        return database

    def _environment(self, root: Path, with_opencode: bool = True) -> dict[str, str]:
        environment = dict(os.environ)
        environment["XDG_DATA_HOME"] = str(root / "data")
        bin_dir = root / "bin"
        bin_dir.mkdir(exist_ok=True)
        if with_opencode:
            executable = bin_dir / "opencode"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
        environment["PATH"] = str(bin_dir)
        return environment

    def test_discovers_sessions_from_sqlite_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._database(
                root,
                [
                    (
                        OPENCODE_ID,
                        "Improve auth flow",
                        "/home/test/code/app",
                        2_000_000_000_000,
                        None,
                        None,
                    ),
                    (
                        "ses_archived",
                        "Old work",
                        "/home/test",
                        3_000_000_000_000,
                        3_000_000_000_001,
                        None,
                    ),
                    (
                        "ses_child",
                        "Internal task",
                        "/home/test/code/app",
                        4_000_000_000_000,
                        None,
                        OPENCODE_ID,
                    ),
                ],
            )
            with mock.patch.dict(os.environ, self._environment(root)):
                result = picker.list_opencode_sessions(picker.HostTarget(None), 20, 2.0)

        self.assertTrue(result["installed"])
        self.assertEqual(1, len(result["sessions"]))
        self.assertEqual(
            {
                "id": OPENCODE_ID,
                "name": "Improve auth flow",
                "cwd": "/home/test/code/app",
                "recencyAt": 2_000_000_000,
                "updatedAt": 2_000_000_000,
            },
            result["sessions"][0],
        )

    def test_reads_one_session_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._database(
                root, [(OPENCODE_ID, "Title", "/home/test", 2_000_000_000_000, None, None)]
            )
            with mock.patch.dict(os.environ, self._environment(root)):
                session = picker.read_opencode_session(picker.HostTarget(None), OPENCODE_ID, 2.0)

        self.assertEqual(OPENCODE_ID, session["id"])
        self.assertEqual("Title", session["name"])

    def test_does_not_read_child_session_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._database(
                root,
                [
                    (OPENCODE_ID, "Title", "/home/test", 2_000_000_000_000, None, None),
                    (
                        "ses_child",
                        "Internal task",
                        "/home/test",
                        3_000_000_000_000,
                        None,
                        OPENCODE_ID,
                    ),
                ],
            )
            with (
                mock.patch.dict(os.environ, self._environment(root)),
                self.assertRaisesRegex(picker.PickerError, "was not found"),
            ):
                picker.read_opencode_session(picker.HostTarget(None), "ses_child", 2.0)

    def test_surfaces_database_query_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            db_dir = root / "data" / "opencode"
            db_dir.mkdir(parents=True)
            database = db_dir / "opencode.db"
            conn = sqlite3.connect(database)
            conn.execute("CREATE TABLE session (id text PRIMARY KEY)")
            conn.commit()
            conn.close()
            with (
                mock.patch.dict(os.environ, self._environment(root)),
                self.assertRaisesRegex(picker.PickerError, "opencode session query failed"),
            ):
                picker.list_opencode_sessions(picker.HostTarget(None), 20, 2.0)

    def test_probe_reports_not_installed_without_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._database(
                root, [(OPENCODE_ID, "Title", "/home/test", 2_000_000_000_000, None, None)]
            )
            with mock.patch.dict(os.environ, self._environment(root, with_opencode=False)):
                result = picker.list_opencode_sessions(picker.HostTarget(None), 20, 2.0)

        self.assertFalse(result["installed"])
        self.assertEqual(1, len(result["sessions"]))

    def test_extracts_session_id_from_arguments(self) -> None:
        self.assertEqual(
            OPENCODE_ID,
            picker._opencode_session_id_from_args(["opencode", "--session", OPENCODE_ID]),
        )
        self.assertEqual(
            OPENCODE_ID,
            picker._opencode_session_id_from_args(["opencode", "-s", OPENCODE_ID]),
        )
        self.assertEqual(
            OPENCODE_ID,
            picker._opencode_session_id_from_args(["opencode", f"--session={OPENCODE_ID}"]),
        )
        self.assertIsNone(picker._opencode_session_id_from_args(["opencode"]))

    def test_active_snapshot_maps_managed_tmux_session_id(self) -> None:
        with mock.patch.object(picker, "_opencode_session_id_for_process", return_value=None):
            active = picker._opencode_active_sessions(
                parents={700: 800, 800: 1},
                opencode_pids={700},
                pane_sessions={800: "improve-auth-flow"},
                option_sessions={OPENCODE_ID: "improve-auth-flow"},
            )

        self.assertEqual({OPENCODE_ID: {"pid": 700, "tmuxSession": "improve-auth-flow"}}, active)

    def test_merge_includes_active_opencode_sessions(self) -> None:
        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget("laptop.lan"),
                    [],
                    {"installed": False, "sessions": []},
                    {
                        "installed": True,
                        "sessions": [
                            {
                                "id": OPENCODE_ID,
                                "name": "Fix auth flow",
                                "cwd": "/home/test/code/app",
                                "recencyAt": 90,
                                "updatedAt": 90,
                            }
                        ],
                    },
                    {
                        "host": "laptop",
                        "active": {},
                        "claudeActive": {},
                        "opencodeActive": {OPENCODE_ID: {"pid": 42, "tmuxSession": "auth-flow"}},
                    },
                )
            ],
            limit=20,
        )

        session = result["sessions"][0]
        self.assertEqual("opencode", session["kind"])
        self.assertEqual(OPENCODE_ID, session["id"])
        self.assertTrue(session["active"])
        self.assertEqual("auth-flow", session["tmuxSession"])
        self.assertEqual("laptop.lan", session["connectHost"])

    def test_open_parser_accepts_opencode_session_ids(self) -> None:
        args = picker.build_parser().parse_args(["open-opencode", "--id", OPENCODE_ID, "--detach"])

        self.assertEqual("open-opencode", args.command)
        self.assertEqual(OPENCODE_ID, args.id)
        self.assertTrue(args.detach)

    def test_open_parser_rejects_uuid_for_opencode(self) -> None:
        with self.assertRaises(SystemExit):
            picker.build_parser().parse_args(["open-opencode", "--id", THREAD_A])

    def test_open_reuses_existing_opencode_tmux_session(self) -> None:
        with (
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "laptop",
                    "active": {},
                    "claudeActive": {},
                    "opencodeInstalled": True,
                    "opencodeActive": {OPENCODE_ID: {"pid": 42, "tmuxSession": "auth-flow"}},
                },
            ),
            mock.patch.object(picker, "ensure_opencode_tmux_session") as ensure,
        ):
            session = picker.resolve_opencode_open_target(
                picker.HostTarget("laptop.lan"),
                OPENCODE_ID,
                "Fix auth flow",
                "/home/test/code/app",
                1.0,
            )

        self.assertEqual("auth-flow", session)
        ensure.assert_not_called()

    def test_active_opencode_outside_tmux_is_not_duplicated(self) -> None:
        with (
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "desktop",
                    "active": {},
                    "claudeActive": {},
                    "opencodeInstalled": True,
                    "opencodeActive": {OPENCODE_ID: {"pid": 42, "tmuxSession": None}},
                },
            ),
            self.assertRaisesRegex(picker.PickerError, "outside tmux"),
        ):
            picker.resolve_opencode_open_target(
                picker.HostTarget(None), OPENCODE_ID, "Fix auth flow", "/tmp", 1.0
            )

    def test_inactive_opencode_creates_managed_session(self) -> None:
        with (
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "desktop",
                    "active": {},
                    "claudeActive": {},
                    "opencodeInstalled": True,
                    "opencodeActive": {},
                },
            ),
            mock.patch.object(
                picker, "ensure_opencode_tmux_session", return_value="auth-flow"
            ) as ensure,
        ):
            session = picker.resolve_opencode_open_target(
                picker.HostTarget(None),
                OPENCODE_ID,
                "Fix auth flow",
                "/home/test/code/app",
                1.0,
            )

        self.assertEqual("auth-flow", session)
        ensure.assert_called_once()

    def test_opencode_not_installed_raises(self) -> None:
        with (
            mock.patch.object(
                picker,
                "get_active_snapshot",
                return_value={
                    "host": "desktop",
                    "active": {},
                    "claudeActive": {},
                    "opencodeInstalled": False,
                    "opencodeActive": {},
                },
            ),
            self.assertRaisesRegex(picker.PickerError, "not installed"),
        ):
            picker.resolve_opencode_open_target(
                picker.HostTarget(None), OPENCODE_ID, "Fix auth flow", "/tmp", 1.0
            )

    def test_unavailable_opencode_sessions_are_omitted(self) -> None:
        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget(None),
                    [],
                    {"installed": False, "sessions": []},
                    {"installed": False, "sessions": [{"id": OPENCODE_ID}]},
                    {"host": "desktop", "active": {}, "claudeActive": {}, "opencodeActive": {}},
                )
            ],
            limit=20,
        )

        self.assertEqual([], result["sessions"])

    def test_opencode_list_failure_emits_stage_error(self) -> None:
        result = picker.merge_host_results(
            [
                (
                    picker.HostTarget(None),
                    [],
                    {"installed": False, "sessions": []},
                    picker.PickerError("opencode probe failed"),
                    {"host": "desktop", "active": {}, "claudeActive": {}, "opencodeActive": {}},
                )
            ],
            limit=20,
        )

        self.assertEqual([], result["sessions"])
        self.assertEqual("opencode", result["errors"][0]["stage"])
        self.assertIn("opencode probe failed", result["errors"][0]["message"])

    def test_managed_session_resumes_exact_id_in_recorded_directory(self) -> None:
        script = picker._ensure_opencode_session_script(
            OPENCODE_ID, "Fix auth flow", "/home/test/code/app"
        )

        self.assertIn("requested_cwd=/home/test/code/app", script)
        self.assertIn('"$opencode_bin" --session "$session_id"', script)
        self.assertIn("@opencode_session_id", script)
        self.assertIn("@agent_picker_waiting 1", script)


if __name__ == "__main__":
    unittest.main()
