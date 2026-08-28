from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from rofi_agent_picker import VERSION, app, engine
from rofi_agent_picker.cache import CACHE_VERSION, CacheStore, build_snapshot
from rofi_agent_picker.config import ConfigError, PickerConfig, config_from_mapping, load_config
from rofi_agent_picker.rofi import (
    AUTO_REFRESH_DATA_PREFIX,
    ERROR_NOTICE_DATA_PREFIX,
    ERROR_NOTICE_SECONDS,
    FALLBACK_ICON_PATH,
    PROVIDER_ICON_PATHS,
    PROVIDER_LABELS,
    PROVIDER_SEARCH_TERMS,
    ROFI_DELIMITER_VALUE,
    ROFI_RECORD_SEPARATOR,
    ROFI_RETV_CUSTOM_19,
    ROW_SEPARATOR,
    _age,
    _background_command,
    _parse_error_notice,
    _parse_selection,
    _provider_icon,
    _refresh_data,
    render_snapshot,
    run_rofi,
)

THREAD_ID = "00000000-0000-0000-0000-000000000001"
OPENCODE_ID = "ses_0319af718ffegy8N1IoMEggx4B"


def session(
    kind: str = "codex", identifier: str = THREAD_ID, **values: object
) -> dict[str, object]:
    result: dict[str, object] = {
        "kind": kind,
        "id": identifier,
        "name": "hello\nworld\x00",
        "cwd": "/home/bryan/code/project",
        "host": "workstation",
        "windowHost": "workstation-vpn.example",
        "connectHost": "workstation-vpn.example",
        "recencyAt": 100,
        "updatedAt": 100,
        "active": False,
        "activityState": "idle",
    }
    result.update(values)
    return result


def parse_row_options(row: str) -> tuple[str, dict[str, str]]:
    visible, separator, encoded = row.partition("\x00")
    if not separator:
        raise AssertionError("row has no option separator")
    fields = encoded.split("\x1f")
    if len(fields) % 2:
        raise AssertionError("row options are not key/value pairs")
    return visible, dict(zip(fields[::2], fields[1::2], strict=True))


def parse_rendered_records(output: str) -> tuple[list[str], list[str]]:
    delimiter_header = f"\x00delim\x1f{ROFI_DELIMITER_VALUE}\n"
    if delimiter_header in output:
        header_text, record_text = output.split(delimiter_header, 1)
        headers = [*header_text.split("\n"), delimiter_header.removesuffix("\n")]
        records = record_text.removesuffix(ROFI_RECORD_SEPARATOR).split(ROFI_RECORD_SEPARATOR)
    else:
        records = output.removesuffix(ROFI_RECORD_SEPARATOR).split(ROFI_RECORD_SEPARATOR)
        headers = [record for record in records if record.startswith("\x00")]
        records = [record for record in records if not record.startswith("\x00")]
    return headers, [record for record in records if record]


class ConfigTest(unittest.TestCase):
    def test_missing_config_is_local_only_with_dms_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = load_config(Path(temporary) / "missing.toml")
        self.assertEqual((), config.hosts)
        self.assertEqual((), config.host_routes)
        self.assertEqual(40, config.max_sessions)
        self.assertEqual(30, config.refresh_seconds)
        self.assertEqual(2, config.ssh_connect_timeout)
        self.assertEqual(1, config.ssh_connection_attempts)

    def test_loads_routes_and_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                'hosts = ["ignored.lan"]\n'
                'host_routes = ["workstation=workstation-vpn.example|workstation.example"]\n'
                'aliases = ["LEGACY-HOST=Workstation"]\n'
                'terminal = "foot --class agent"\n'
                "max_sessions = 12\nrefresh_seconds = 60\n"
                "ssh_connect_timeout = 3\nssh_connection_attempts = 2\n"
            )
            config = load_config(path)
        self.assertEqual(("ignored.lan",), config.hosts)
        self.assertEqual(
            ("workstation=workstation-vpn.example|workstation.example",), config.host_routes
        )
        self.assertEqual("Workstation", config.aliases["legacy-host"])
        self.assertEqual("foot --class agent", config.terminal)
        self.assertEqual(
            ("workstation-vpn.example", "workstation.example"), config.routes[0].route_paths
        )

    def test_rejects_unknown_keys_wrong_types_and_bounds(self) -> None:
        for text in (
            "mystery = true\n",
            'hosts = "workstation"\n',
            "max_sessions = 0\n",
            "refresh_seconds = 301\n",
            "ssh_connect_timeout = true\n",
            'host_routes = ["bad route"]\n',
        ):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "config.toml"
                path.write_text(text)
                with self.assertRaises(ConfigError):
                    load_config(path)

    def test_cli_values_override_config_values(self) -> None:
        config = config_from_mapping(
            {
                "hosts": ["config-host"],
                "host_routes": ["config=config-host"],
                "aliases": ["config-host=Config"],
                "max_sessions": 12,
                "ssh_connect_timeout": 3,
            }
        )
        args = app.build_parser().parse_args(
            [
                "--ssh-connect-timeout",
                "5",
                "list",
                "--host",
                "cli-host",
                "--route",
                "cli=cli-host",
                "--alias",
                "cli-host=CLI",
                "--limit",
                "7",
            ]
        )
        merged = app._apply_cli_config(config, args)
        self.assertEqual(("cli-host",), merged.hosts)
        self.assertEqual(("cli=cli-host",), merged.host_routes)
        self.assertEqual({"cli-host": "CLI"}, merged.aliases)
        self.assertEqual(7, merged.max_sessions)
        self.assertEqual(5, merged.ssh_connect_timeout)

        host_only = app.build_parser().parse_args(["list", "--host", "host-only"])
        self.assertEqual((), app._apply_cli_config(config, host_only).host_routes)

    def test_diagnostic_limit_retains_legacy_200_row_bound(self) -> None:
        args = app.build_parser().parse_args(["list", "--limit", "200"])
        self.assertEqual(200, app._apply_cli_config(PickerConfig(), args).max_sessions)
        args = app.build_parser().parse_args(["list", "--limit", "201"])
        with self.assertRaises(engine.PickerError):
            app._apply_cli_config(PickerConfig(), args)

    def test_fingerprint_excludes_terminal_and_ttl_but_tracks_discovery(self) -> None:
        original = PickerConfig()
        self.assertEqual(
            original.fingerprint,
            original.with_overrides(terminal="foot", refresh_seconds=60).fingerprint,
        )
        self.assertNotEqual(
            original.fingerprint, original.with_overrides(hosts=["workstation"]).fingerprint
        )


class ProjectMetadataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def test_engine_package_and_project_versions_match(self) -> None:
        project = tomllib.loads((self.root / "pyproject.toml").read_text())
        self.assertEqual(engine.VERSION, project["project"]["version"])
        self.assertEqual(VERSION, engine.VERSION)
        self.assertEqual("0.1.0", engine.VERSION)
        self.assertIn(f"Version `{engine.VERSION}`", (self.root / "README.md").read_text())

    def test_ci_and_readme_describe_the_canonical_deployment_contract(self) -> None:
        readme = (self.root / "README.md").read_text()
        workflow = (self.root / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("config.toml", readme)
        self.assertIn("XDG_CACHE_HOME", readme)
        self.assertIn("Rofi script-mode", readme)
        self.assertIn("canonical implementation of the deployed Agent Picker", readme)
        self.assertIn("Current refresh/provider errors are shown for about three seconds", readme)
        self.assertIn("./scripts/check", workflow)

    def test_runtime_dependencies_are_empty_and_optional_providers_are_documented(self) -> None:
        project = tomllib.loads((self.root / "pyproject.toml").read_text())
        self.assertEqual([], project["project"]["dependencies"])
        readme = (self.root / "README.md").read_text().lower()
        self.assertIn("claude code and opencode are optional", readme)
        self.assertIn("codex cli", readme)


class CacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = CacheStore(Path(self.temporary.name) / "cache")
        self.config = PickerConfig(max_sessions=40)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def events(*sessions: dict[str, object], errors: list[dict[str, str]] | None = None):
        yield {"event": "refresh-started", "hosts": ["local"]}
        yield {
            "event": "host-complete",
            "host": "local",
            "sessions": list(sessions),
            "errors": errors or [],
        }
        yield {"event": "refresh-finished", "generatedAt": 100}

    def test_snapshot_is_versioned_private_and_atomic(self) -> None:
        snapshot = build_snapshot(self.config, self.events(session()), now=100)
        self.store.write(snapshot)
        self.assertEqual(CACHE_VERSION, self.store.load(self.config.fingerprint)["version"])
        self.assertEqual(0o700, stat.S_IMODE(self.store.root.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(self.store.snapshot_path.stat().st_mode))
        self.assertEqual([], list(self.store.root.glob(".snapshot.*.tmp")))

    def test_invalid_or_mismatched_cache_is_ignored(self) -> None:
        self.store.ensure_root()
        self.store.snapshot_path.write_text("not json")
        self.assertIsNone(self.store.load(self.config.fingerprint))
        self.store.write(
            {"version": CACHE_VERSION, "fingerprint": "other", "sessions": [], "hosts": {}}
        )
        self.assertIsNone(self.store.load(self.config.fingerprint))
        self.store.write(
            {
                "version": CACHE_VERSION,
                "fingerprint": self.config.fingerprint,
                "sessions": [],
                "hosts": {"local": "not-an-object"},
            }
        )
        self.assertIsNone(self.store.load(self.config.fingerprint))

    def test_partial_provider_failure_preserves_old_rows(self) -> None:
        old = session("codex", recencyAt=100)
        current = session("claude", THREAD_ID, recencyAt=200)
        previous = build_snapshot(self.config, self.events(old), now=100)
        current_snapshot = build_snapshot(
            self.config,
            self.events(
                current,
                errors=[{"host": "local", "stage": "threads", "message": "offline"}],
            ),
            previous,
            now=200,
        )
        self.assertEqual(
            {"codex", "claude"}, {item["kind"] for item in current_snapshot["sessions"]}
        )

    def test_fresh_cache_skips_discovery_and_stale_cache_refreshes(self) -> None:
        calls: list[int] = []

        def discover(_config: PickerConfig, _previous: object):
            calls.append(1)
            yield from self.events(session())

        first = self.store.refresh(self.config, discover=discover)
        self.assertEqual(1, len(calls))
        second = self.store.refresh(self.config, discover=discover)
        self.assertEqual(1, len(calls))
        self.assertEqual(first["generatedAt"], second["generatedAt"])
        stale = dict(second)
        stale["generatedAt"] = int(time.time()) - 100
        self.store.write(stale)
        self.store.refresh(self.config, discover=discover)
        self.assertEqual(2, len(calls))

    def test_lock_deduplicates_nonblocking_refresh(self) -> None:
        with self.store.lock() as acquired:
            self.assertTrue(acquired)
            with self.store.lock(blocking=False) as second:
                self.assertFalse(second)

    def test_background_refresh_marker_deduplicates_and_is_private(self) -> None:
        command = [sys.executable, "-c", "pass"]
        callback_environment = {
            "ROFI_DATA": "state",
            "ROFI_INFO": "selection",
            "ROFI_INPUT": "filter",
            "ROFI_OUTSIDE": "123",
            "ROFI_RETV": "0",
        }
        with (
            mock.patch.dict(os.environ, {**callback_environment, "PICKER_TEST_VALUE": "kept"}),
            mock.patch("rofi_agent_picker.cache.subprocess.Popen") as popen,
        ):
            self.assertTrue(self.store.spawn_background(command))
            self.assertFalse(self.store.spawn_background(command))
            popen.assert_called_once()
        child_environment = popen.call_args.kwargs["env"]
        self.assertEqual("kept", child_environment["PICKER_TEST_VALUE"])
        for key in callback_environment:
            self.assertNotIn(key, child_environment)
        self.assertEqual(0o600, stat.S_IMODE(self.store.background_path.stat().st_mode))
        self.store.clear_background_marker()

    def test_background_marker_query_reports_age_and_activity(self) -> None:
        self.store.ensure_root()
        self.store.background_path.write_text("worker")
        modified = self.store.background_path.stat().st_mtime
        self.assertAlmostEqual(2.0, self.store.background_age(now=modified + 2.0))
        self.assertTrue(self.store.background_active(max_age=2.0, now=modified + 2.0))
        self.assertFalse(self.store.background_active(max_age=1.0, now=modified + 2.0))
        self.store.clear_background_marker()

    def test_activity_failure_keeps_last_known_activity(self) -> None:
        old = session(active=True, activityState="active", tmuxSession="agent")
        previous = build_snapshot(self.config, self.events(old), now=100)
        current = build_snapshot(
            self.config,
            self.events(
                session(active=False, activityState="unknown"),
                errors=[{"host": "local", "stage": "active", "message": "offline"}],
            ),
            previous,
            now=200,
        )
        self.assertTrue(current["sessions"][0]["active"])
        self.assertEqual("agent", current["sessions"][0]["tmuxSession"])


class RofiProtocolTest(unittest.TestCase):
    def test_background_command_supports_checkout_and_installed_layouts(self) -> None:
        checkout = _background_command()
        self.assertEqual(sys.executable, checkout[0])
        self.assertTrue(checkout[1].endswith("/bin/rofi-agent-picker"))
        self.assertEqual(["refresh", "--background"], checkout[2:])

        with mock.patch(
            "rofi_agent_picker.rofi.__file__", "/opt/venv/lib/rofi_agent_picker/rofi.py"
        ):
            self.assertEqual(
                [sys.executable, "-m", "rofi_agent_picker", "refresh", "--background"],
                _background_command(),
            )

    def test_missing_or_invalid_timestamp_has_unknown_age(self) -> None:
        for timestamp in (None, 0, -1, "", "not-a-time"):
            with self.subTest(timestamp=timestamp):
                self.assertEqual("unknown", _age(timestamp, now=100))

    def test_rows_escape_protocol_controls_and_include_search_metadata(self) -> None:
        output = render_snapshot(
            {"sessions": [session(active=True, activityState="active")]}, now=100
        )
        self.assertIn("\x00prompt\x1fAgents", output)
        self.assertIn("\x00use-hot-keys\x1ftrue", output)
        self.assertIn("\x00markup-rows\x1ftrue", output)
        self.assertIn(f"\x00delim\x1f{ROFI_DELIMITER_VALUE}\n", output)
        self.assertNotIn("hello\nworld", output)
        _, rows = parse_rendered_records(output)
        row = next(record for record in rows if record.startswith("hello"))
        visible, options = parse_row_options(row)
        self.assertEqual(1, row.count("\x00"))
        self.assertIn("Codex", visible)
        self.assertEqual("true", options["active"])
        self.assertEqual(str(PROVIDER_ICON_PATHS["codex"]), options["icon"])
        display = options["display"]
        self.assertIn(f"<b>hello world</b>{ROW_SEPARATOR}", display)
        self.assertIn('<span size="smaller" alpha="75%">', display)
        self.assertIn("workstation  ·  ~/code/project  ·  0s  ·  active", display)
        decoded = json.loads(options["info"])
        self.assertEqual(THREAD_ID, decoded["id"])

    def test_display_escapes_markup_and_hides_provider_while_filtering_keeps_it(self) -> None:
        selected = session(
            kind="claude",
            name="A < & >",
            host="host",
            cwd="/srv/project",
            recencyAt=100,
            activityState="waiting",
        )
        output = render_snapshot({"sessions": [selected]}, now=100)
        _, rows = parse_rendered_records(output)
        row = next(record for record in rows if record.startswith("A < & >"))
        visible, options = parse_row_options(row)
        display = options["display"]
        meta = options["meta"]
        self.assertIn("<b>A &lt; &amp; &gt;</b>", display)
        self.assertNotIn("Claude Code", display)
        self.assertIn("Claude Code", visible)
        self.assertIn("claude claude-code claude code", meta)
        self.assertIn("host  ·  /srv/project  ·  0s  ·  waiting", display)

    def test_each_provider_uses_its_icon_and_retains_search_terms(self) -> None:
        for kind in PROVIDER_LABELS:
            identifier = OPENCODE_ID if kind == "opencode" else THREAD_ID
            with self.subTest(kind=kind):
                output = render_snapshot(
                    {"sessions": [session(kind, identifier, activityState="active")]}, now=100
                )
                _, rows = parse_rendered_records(output)
                row = next(record for record in rows if record.startswith("hello"))
                visible, options = parse_row_options(row)
                display = options["display"]
                meta = options["meta"]
                self.assertEqual(str(PROVIDER_ICON_PATHS[kind]), options["icon"])
                self.assertIn(PROVIDER_LABELS[kind], visible)
                self.assertNotIn(PROVIDER_LABELS[kind], display)
                self.assertIn(PROVIDER_SEARCH_TERMS[kind], meta)

    def test_provider_icons_are_bundled_absolute_paths_with_safe_fallback(self) -> None:
        for kind, path in PROVIDER_ICON_PATHS.items():
            with self.subTest(kind=kind):
                self.assertTrue(path.is_absolute())
                self.assertTrue(path.is_file())
                self.assertEqual(str(path), _provider_icon(kind))
        self.assertTrue(FALLBACK_ICON_PATH.is_absolute())
        self.assertTrue(FALLBACK_ICON_PATH.is_file())
        self.assertEqual(str(FALLBACK_ICON_PATH), _provider_icon("unknown"))
        with mock.patch.dict(PROVIDER_ICON_PATHS, {"codex": Path("/missing/codex.svg")}):
            self.assertEqual(str(FALLBACK_ICON_PATH), _provider_icon("codex"))

    def test_provider_icon_assets_are_declared_as_package_data(self) -> None:
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / "pyproject.toml").read_text())
        package_data = project["tool"]["setuptools"]["package-data"]["rofi_agent_picker"]
        self.assertIn("assets/providers/*.svg", package_data)
        self.assertEqual(
            {"claude.svg", "codex.svg", "generic.svg", "opencode.svg"},
            {path.name for path in (root / "rofi_agent_picker/assets/providers").glob("*.svg")},
        )

    def test_empty_snapshot_has_nonselectable_status_row(self) -> None:
        output = render_snapshot({"sessions": []}, message="No hosts reachable")
        _, rows = parse_rendered_records(output)
        row = next(record for record in rows if record.startswith("No sessions"))
        visible, options = parse_row_options(row)
        self.assertEqual("No sessions · No hosts reachable", visible)
        self.assertEqual({"nonselectable": "true", "urgent": "true"}, options)
        self.assertEqual(1, row.count("\x00"))

    def test_selection_parser_validates_provider_ids(self) -> None:
        payload = {"kind": "opencode", "id": OPENCODE_ID}
        self.assertEqual(payload, _parse_selection(json.dumps(payload)))
        with self.assertRaises(engine.PickerError):
            _parse_selection(json.dumps({"kind": "codex", "id": "bad"}))

    def test_initial_mode_refreshes_cache_and_alt_r_forces_refresh(self) -> None:
        store = mock.Mock(spec=CacheStore)
        store.load.return_value = None
        store.refresh.return_value = {"sessions": [session()], "errors": []}
        store.is_fresh.return_value = True
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            result = run_rofi({"ROFI_RETV": "0"}, store=store, config=self._config())
        self.assertEqual(0, result)
        store.refresh.assert_called_once()
        initial_output = output.getvalue()
        self.assertIn("Agents", initial_output)
        initial_headers, initial_rows = parse_rendered_records(initial_output)
        self.assertIn(f"\x00delim\x1f{ROFI_DELIMITER_VALUE}", initial_headers)
        self.assertEqual(1, len(initial_rows))
        self.assertIn(ROW_SEPARATOR, parse_row_options(initial_rows[0])[1]["display"])

        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            result = run_rofi({"ROFI_RETV": "10"}, store=store, config=self._config())
        self.assertEqual(0, result)
        self.assertEqual(2, store.refresh.call_count)
        callback_output = output.getvalue()
        self.assertIn("\x00keep-selection\x1ftrue", callback_output)
        self.assertIn("\x00keep-filter\x1ftrue", callback_output)
        callback_headers, callback_rows = parse_rendered_records(callback_output)
        self.assertNotIn(f"\x00delim\x1f{ROFI_DELIMITER_VALUE}", callback_headers)
        self.assertTrue(callback_output.endswith(ROFI_RECORD_SEPARATOR))
        self.assertEqual(1, len(callback_rows))
        self.assertIn(ROW_SEPARATOR, parse_row_options(callback_rows[0])[1]["display"])

        failing_store = mock.Mock(spec=CacheStore)
        failing_store.refresh.side_effect = engine.PickerError("offline")
        failing_store.load.return_value = {"sessions": [session()], "errors": []}
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            run_rofi({"ROFI_RETV": "10"}, store=failing_store, config=self._config())
        self.assertIn("Refresh failed", output.getvalue())
        self.assertIn("\x00keep-selection\x1ftrue", output.getvalue())
        self.assertIn("\x00keep-filter\x1ftrue", output.getvalue())

    def test_stale_mode_renders_immediately_and_starts_one_background_refresh(self) -> None:
        store = mock.Mock(spec=CacheStore)
        store.load.return_value = {"sessions": [session()], "errors": []}
        store.is_fresh.return_value = False
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            run_rofi({"ROFI_RETV": "0"}, store=store, config=self._config())
        store.spawn_background.assert_called_once()
        rendered = output.getvalue()
        self.assertIn("Refreshing in background", rendered)
        self.assertIn(
            '\x00theme\x1fconfiguration { timeout { delay: 1; action: "kb-custom-19"; } }', rendered
        )
        self.assertIn(f"\x00data\x1f{AUTO_REFRESH_DATA_PREFIX}", rendered)

    def test_stale_mode_only_shows_refresh_status_for_previous_errors(self) -> None:
        store = mock.Mock(spec=CacheStore)
        store.load.return_value = {
            "sessions": [session()],
            "errors": [{"host": "local", "stage": "threads", "message": "offline"}],
        }
        store.is_fresh.return_value = False
        store.spawn_background.return_value = True
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            run_rofi({"ROFI_RETV": "0"}, store=store, config=self._config())
        rendered = output.getvalue()
        self.assertIn("Refreshing in background", rendered)
        self.assertNotIn("Refresh errors: local/threads: offline", rendered)

    def test_stale_mode_clears_spawn_failure_status_without_enabling_polling(self) -> None:
        store = mock.Mock(spec=CacheStore)
        store.load.return_value = {"sessions": [session()], "errors": []}
        store.is_fresh.return_value = False
        store.spawn_background.return_value = False
        store.background_active.return_value = False
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            run_rofi({"ROFI_RETV": "0"}, store=store, config=self._config())
        store.spawn_background.assert_called_once()
        rendered = output.getvalue()
        self.assertNotIn("Refreshing in background", rendered)
        self.assertNotIn("Background refresh stopped", rendered)
        self.assertNotIn("\x00message\x1f", rendered)
        self.assertNotIn("\x00theme\x1f", rendered)

    def test_background_callback_polls_without_starting_another_refresh(self) -> None:
        store = mock.Mock(spec=CacheStore)
        store.load.return_value = {"sessions": [session()], "errors": []}
        store.is_fresh.return_value = False
        store.background_active.return_value = True
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            result = run_rofi(
                {
                    "ROFI_RETV": str(ROFI_RETV_CUSTOM_19),
                    "ROFI_DATA": f"{AUTO_REFRESH_DATA_PREFIX}{int(time.time()) + 10}",
                },
                store=store,
                config=self._config(),
            )
        self.assertEqual(0, result)
        store.refresh.assert_not_called()
        store.spawn_background.assert_not_called()
        rendered = output.getvalue()
        self.assertIn("Refreshing in background", rendered)
        self.assertIn("\x00keep-selection\x1ftrue", rendered)
        self.assertIn("\x00keep-filter\x1ftrue", rendered)
        self.assertIn(
            '\x00theme\x1fconfiguration { timeout { delay: 1; action: "kb-custom-19"; } }', rendered
        )

    def test_background_and_error_continuations_coexist_until_notice_expiry(self) -> None:
        snapshot = {"sessions": [session()], "errors": []}
        store = mock.Mock(spec=CacheStore)
        store.load.return_value = snapshot
        store.is_fresh.return_value = False
        store.background_active.return_value = True
        error_message = "Refresh errors: local/threads: offline"
        data = _refresh_data(1010, 1003, error_message)
        environ = {"ROFI_RETV": str(ROFI_RETV_CUSTOM_19), "ROFI_DATA": data}

        output = io.StringIO()
        with (
            mock.patch("sys.stdout", output),
            mock.patch("rofi_agent_picker.rofi.time.time", return_value=1000),
        ):
            result = run_rofi(environ, store=store, config=self._config())
        self.assertEqual(0, result)
        first = output.getvalue()
        self.assertIn(error_message, first)
        self.assertIn("hello", first)
        self.assertIn("\x00keep-selection\x1ftrue", first)
        self.assertIn("\x00keep-filter\x1ftrue", first)
        self.assertIn(
            '\x00theme\x1fconfiguration { timeout { delay: 1; action: "kb-custom-19"; } }',
            first,
        )
        continuation_data = first.split("\x00data\x1f", 1)[1].split("\t", 1)[0]
        self.assertTrue(continuation_data.startswith("background-refresh:1010;"))
        self.assertIn("error-notice:1003:", continuation_data)
        self.assertEqual((1003.0, error_message), _parse_error_notice(continuation_data))

        output = io.StringIO()
        with (
            mock.patch("sys.stdout", output),
            mock.patch("rofi_agent_picker.rofi.time.time", return_value=1003),
        ):
            result = run_rofi(
                {"ROFI_RETV": str(ROFI_RETV_CUSTOM_19), "ROFI_DATA": continuation_data},
                store=store,
                config=self._config(),
            )
        self.assertEqual(0, result)
        expired = output.getvalue()
        self.assertNotIn(error_message, expired)
        self.assertIn("Refreshing in background", expired)
        self.assertIn("\x00keep-selection\x1ftrue", expired)
        self.assertIn("\x00keep-filter\x1ftrue", expired)
        self.assertIn(
            '\x00theme\x1fconfiguration { timeout { delay: 1; action: "kb-custom-19"; } }',
            expired,
        )
        self.assertIn("\x00data\x1fbackground-refresh:1010", expired)
        self.assertNotIn("error-notice:", expired)
        store.refresh.assert_not_called()
        store.spawn_background.assert_not_called()

    def test_background_callback_renders_fresh_rows_and_disables_polling(self) -> None:
        fresh = session(name="fresh", recencyAt=int(time.time()))
        store = mock.Mock(spec=CacheStore)
        store.load.return_value = {"sessions": [fresh], "errors": []}
        store.is_fresh.return_value = True
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            result = run_rofi(
                {"ROFI_RETV": str(ROFI_RETV_CUSTOM_19)},
                store=store,
                config=self._config(),
            )
        self.assertEqual(0, result)
        store.refresh.assert_not_called()
        store.spawn_background.assert_not_called()
        store.background_active.assert_not_called()
        rendered = output.getvalue()
        self.assertIn("fresh", rendered)
        self.assertNotIn("Refreshing in background", rendered)
        self.assertIn("\x00message\x1f", rendered)
        self.assertIn(
            '\x00theme\x1fconfiguration { timeout { delay: 0; action: "kb-custom-19"; } }', rendered
        )
        self.assertIn("\x00data\x1fidle", rendered)

    def test_fresh_error_snapshot_uses_a_bounded_notice_timeout(self) -> None:
        snapshot = {
            "sessions": [session()],
            "errors": [{"host": "local", "stage": "threads", "message": "offline"}],
        }
        store = mock.Mock(spec=CacheStore)
        store.load.return_value = snapshot
        store.is_fresh.return_value = True
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            result = run_rofi({"ROFI_RETV": "0"}, store=store, config=self._config())
        self.assertEqual(0, result)
        rendered = output.getvalue()
        self.assertIn("Refresh errors: local/threads: offline", rendered)
        self.assertIn(
            f'\x00theme\x1fconfiguration {{ timeout {{ delay: {ERROR_NOTICE_SECONDS}; action: "kb-custom-19"; }} }}',
            rendered,
        )
        self.assertIn(f"\x00data\x1f{ERROR_NOTICE_DATA_PREFIX}", rendered)
        deadline, message = _parse_error_notice(
            rendered.split("\x00data\x1f", 1)[1].split("\n", 1)[0]
        )
        self.assertIsNotNone(deadline)
        self.assertEqual("Refresh errors: local/threads: offline", message)

    def test_error_notice_callback_clears_without_snapshot_error_fallback(self) -> None:
        snapshot = {
            "sessions": [session()],
            "errors": [{"host": "local", "stage": "threads", "message": "offline"}],
        }
        store = mock.Mock(spec=CacheStore)
        store.load.return_value = snapshot
        store.is_fresh.return_value = True
        expired = (
            f"{ERROR_NOTICE_DATA_PREFIX}{int(time.time()) - 1}:"
            "Refresh%20errors%3A%20local%2Fthreads%3A%20offline"
        )
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            result = run_rofi(
                {"ROFI_RETV": str(ROFI_RETV_CUSTOM_19), "ROFI_DATA": expired},
                store=store,
                config=self._config(),
            )
        self.assertEqual(0, result)
        rendered = output.getvalue()
        self.assertNotIn("Refresh errors: local/threads: offline", rendered)
        self.assertIn("\x00message\x1f\t", rendered)
        self.assertIn(
            '\x00theme\x1fconfiguration { timeout { delay: 0; action: "kb-custom-19"; } }',
            rendered,
        )
        self.assertIn("\x00data\x1fidle", rendered)

    def test_background_completion_with_errors_starts_bounded_notice(self) -> None:
        snapshot = {
            "sessions": [session()],
            "errors": [{"host": "local", "stage": "active", "message": "offline"}],
        }
        store = mock.Mock(spec=CacheStore)
        store.load.return_value = snapshot
        store.is_fresh.return_value = True
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            result = run_rofi(
                {
                    "ROFI_RETV": str(ROFI_RETV_CUSTOM_19),
                    "ROFI_DATA": f"{AUTO_REFRESH_DATA_PREFIX}{int(time.time()) + 10}",
                },
                store=store,
                config=self._config(),
            )
        self.assertEqual(0, result)
        rendered = output.getvalue()
        self.assertIn("Refresh errors: local/active: offline", rendered)
        self.assertIn(f"\x00data\x1f{ERROR_NOTICE_DATA_PREFIX}", rendered)
        self.assertNotIn(f"\x00data\x1f{AUTO_REFRESH_DATA_PREFIX}", rendered)

    def test_alt_r_error_uses_the_same_bounded_notice_contract(self) -> None:
        snapshot = {
            "sessions": [session()],
            "errors": [{"host": "local", "stage": "threads", "message": "offline"}],
        }
        store = mock.Mock(spec=CacheStore)
        store.refresh.return_value = snapshot
        store.is_fresh.return_value = True
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            result = run_rofi(
                {"ROFI_RETV": "10"},
                store=store,
                config=self._config(),
            )
        self.assertEqual(0, result)
        rendered = output.getvalue()
        self.assertIn("Refresh errors: local/threads: offline", rendered)
        self.assertIn(f"\x00data\x1f{ERROR_NOTICE_DATA_PREFIX}", rendered)
        self.assertIn("\x00keep-selection\x1ftrue", rendered)

    def test_background_callback_rereads_after_marker_stop_and_prefers_fresh_cache(self) -> None:
        stale = {"sessions": [session(name="stale")], "errors": []}
        fresh = {"sessions": [session(name="fresh")], "errors": []}
        store = mock.Mock(spec=CacheStore)
        store.load.side_effect = [stale, fresh]
        store.is_fresh.side_effect = [False, True]
        store.background_active.return_value = False
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            result = run_rofi(
                {"ROFI_RETV": str(ROFI_RETV_CUSTOM_19)},
                store=store,
                config=self._config(),
            )
        self.assertEqual(0, result)
        store.refresh.assert_not_called()
        store.spawn_background.assert_not_called()
        self.assertEqual(2, store.load.call_count)
        rendered = output.getvalue()
        self.assertIn("fresh", rendered)
        self.assertNotIn("Background refresh stopped", rendered)
        self.assertNotIn("Refreshing in background", rendered)
        self.assertIn("\x00message\x1f", rendered)
        self.assertIn(
            '\x00theme\x1fconfiguration { timeout { delay: 0; action: "kb-custom-19"; } }', rendered
        )

    def test_background_callback_stops_on_worker_failure_or_stall(self) -> None:
        stale = {"sessions": [session()], "errors": []}
        for data, marker_active in (
            (None, False),
            (f"{AUTO_REFRESH_DATA_PREFIX}{int(time.time()) - 1}", True),
        ):
            with self.subTest(data=data, marker_active=marker_active):
                store = mock.Mock(spec=CacheStore)
                store.load.return_value = stale
                store.is_fresh.return_value = False
                store.background_active.return_value = marker_active
                environ = {"ROFI_RETV": str(ROFI_RETV_CUSTOM_19)}
                if data is not None:
                    environ["ROFI_DATA"] = data
                output = io.StringIO()
                with mock.patch("sys.stdout", output):
                    result = run_rofi(environ, store=store, config=self._config())
                self.assertEqual(0, result)
                store.refresh.assert_not_called()
                store.spawn_background.assert_not_called()
                rendered = output.getvalue()
                self.assertNotIn("Background refresh stopped", rendered)
                self.assertNotIn("Refreshing in background", rendered)
                self.assertIn("\x00message\x1f\t", rendered)
                self.assertIn(
                    '\x00theme\x1fconfiguration { timeout { delay: 0; action: "kb-custom-19"; } }',
                    rendered,
                )

    def test_selection_success_closes_and_failure_rerenders(self) -> None:
        selected = session()
        store = mock.Mock(spec=CacheStore)
        store.load.return_value = {"sessions": [selected], "errors": []}
        with mock.patch("rofi_agent_picker.rofi._open_selection") as opener:
            self.assertEqual(
                0,
                run_rofi(
                    {"ROFI_RETV": "1", "ROFI_INFO": json.dumps(selected)},
                    store=store,
                    config=self._config(),
                ),
            )
            opener.assert_called_once()
        opener.side_effect = engine.PickerError("gone")
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            run_rofi(
                {"ROFI_RETV": "1", "ROFI_INFO": json.dumps(selected)},
                store=store,
                config=self._config(),
            )
        self.assertIn("Unable to open session", output.getvalue())
        self.assertIn("\x00keep-selection\x1ftrue", output.getvalue())
        self.assertIn("\x00keep-filter\x1ftrue", output.getvalue())
        self.assertIn(f"\x00data\x1f{ERROR_NOTICE_DATA_PREFIX}", output.getvalue())
        self.assertIn(
            f'\x00theme\x1fconfiguration {{ timeout {{ delay: {ERROR_NOTICE_SECONDS}; action: "kb-custom-19"; }} }}',
            output.getvalue(),
        )

        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            run_rofi(
                {"ROFI_RETV": "1", "ROFI_INFO": "not-json"},
                store=store,
                config=self._config(),
            )
        self.assertIn("\x00keep-selection\x1ftrue", output.getvalue())
        self.assertIn("\x00keep-filter\x1ftrue", output.getvalue())

    def test_custom_and_delete_callbacks_do_not_mutate_rows(self) -> None:
        selected = session()
        store = mock.Mock(spec=CacheStore)
        store.load.return_value = {"sessions": [selected], "errors": []}
        for retv, notice in (("2", "Custom input is disabled"), ("3", "Deletion is disabled")):
            output = io.StringIO()
            with mock.patch("sys.stdout", output):
                run_rofi(
                    {
                        "ROFI_RETV": retv,
                        "ROFI_INFO": json.dumps(selected),
                    },
                    store=store,
                    config=self._config(),
                )
            self.assertIn(notice, output.getvalue())
            self.assertIn("hello", output.getvalue())
            self.assertIn("\x00keep-selection\x1ftrue", output.getvalue())
            self.assertIn("\x00keep-filter\x1ftrue", output.getvalue())

    def test_selection_dispatches_each_provider_and_detaches_terminal(self) -> None:
        for kind, identifier, resolver in (
            ("codex", THREAD_ID, "resolve_open_target"),
            ("claude", THREAD_ID, "resolve_claude_open_target"),
            ("opencode", OPENCODE_ID, "resolve_opencode_open_target"),
        ):
            with self.subTest(kind=kind):
                selected = session(kind, identifier)
                with (
                    mock.patch.object(
                        engine, "resolve_host_target", return_value=engine.HostTarget(None)
                    ),
                    mock.patch.object(engine, resolver, return_value="agent-session") as opener,
                    mock.patch.object(engine, "focus_existing_window", return_value=False),
                    mock.patch.object(engine, "launch_attach") as launch,
                ):
                    from rofi_agent_picker.rofi import _open_selection

                    _open_selection(selected, PickerConfig())
                opener.assert_called_once()
                launch.assert_called_once()
                self.assertTrue(launch.call_args.kwargs["detach"])

    @staticmethod
    def _config() -> PickerConfig:
        return PickerConfig()


class EntrypointTest(unittest.TestCase):
    def test_rofi_selection_argv_is_not_parsed_as_a_diagnostic_command(self) -> None:
        with (
            mock.patch.dict(os.environ, {"ROFI_RETV": "1"}, clear=False),
            mock.patch("rofi_agent_picker.app.run_rofi", return_value=0) as rofi_mode,
        ):
            self.assertEqual(0, app.main(["visible row text"]))
        rofi_mode.assert_called_once()

    def test_direct_and_symlink_entrypoints_show_help_without_bytecode(self) -> None:
        root = Path(__file__).resolve().parents[1]
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        direct = subprocess.run(
            [str(root / "bin" / "rofi-agent-picker"), "--help"],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        self.assertEqual(0, direct.returncode)
        self.assertIn("open-opencode", direct.stdout)
        with tempfile.TemporaryDirectory() as temporary:
            link = Path(temporary) / "picker"
            link.symlink_to(root / "bin" / "rofi-agent-picker")
            linked = subprocess.run(
                [str(link), "--help"],
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )
        self.assertEqual(0, linked.returncode)
        self.assertFalse(any(root.rglob("__pycache__")))
