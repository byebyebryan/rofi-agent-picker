"""Aggregate and open Codex, Claude Code, and opencode sessions from local and SSH hosts."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from .claude_probe import SESSION_PROBE as CLAUDE_SESSION_PROBE
from .codex import AppServerClient
from .opencode_probe import SESSION_PROBE as OPENCODE_SESSION_PROBE

DEFAULT_LIMIT = 40
DEFAULT_TIMEOUT = 4.0
DEFAULT_SSH_CONNECT_TIMEOUT = 2
DEFAULT_SSH_CONNECTION_ATTEMPTS = 1
SESSION_ATTACH_TIMEOUT_SECONDS = 60
VERSION = "0.2.0"
UUID_PATTERN = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
OPENCODE_ID_PATTERN = re.compile(r"ses_[A-Za-z0-9]+")


class PickerError(RuntimeError):
    """An expected failure that can be reported without a traceback."""


@dataclass(frozen=True)
class HostTarget:
    connect_host: str | None
    route_key: str | None = None
    route_paths: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.route_key or self.connect_host or "local"

    @property
    def route_spec(self) -> str | None:
        if not self.route_key or not self.route_paths:
            return None
        return self.route_key + "=" + "|".join(self.route_paths)

    def with_connect_host(self, connect_host: str) -> Self:
        return HostTarget(connect_host, self.route_key, self.route_paths)


@dataclass(frozen=True)
class SshPolicy:
    connect_timeout: int = DEFAULT_SSH_CONNECT_TIMEOUT
    connection_attempts: int = DEFAULT_SSH_CONNECTION_ATTEMPTS
    # Contract mode receives the executable from Host Mesh.  Keeping the
    # default preserves every legacy call site and its existing argv shape.
    executable: str = "ssh"


DEFAULT_SSH_POLICY = SshPolicy()

HostResult = tuple[
    HostTarget,
    list[dict[str, Any]] | Exception,
    dict[str, Any] | Exception,
    dict[str, Any] | Exception,
    dict[str, Any] | Exception,
]


def _short_hostname(host: str) -> str:
    return host.rstrip(".").split(".", 1)[0].casefold()


def _validate_host(host: str) -> str:
    if host.startswith("-"):
        raise PickerError(f"invalid host {host!r}")
    return host


def parse_host_aliases(values: Sequence[str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for value in values:
        for entry in value.split(","):
            entry = entry.strip()
            if not entry:
                continue
            source, separator, display = entry.partition("=")
            source = source.strip()
            display = display.strip()
            if not separator or not source or not display:
                raise PickerError(f"invalid host alias {entry!r}; expected source=display")
            aliases[_short_hostname(source)] = display
    return aliases


def parse_host_routes(values: Sequence[str]) -> list[HostTarget]:
    """Parse logical-host routes in ``name=endpoint|fallback`` form."""

    routes: list[HostTarget] = []
    seen_keys: set[str] = set()
    for value in values:
        for entry in value.replace("\n", ",").split(","):
            entry = entry.strip()
            if not entry:
                continue
            name, separator, raw_paths = entry.partition("=")
            name = name.strip()
            paths = tuple(path.strip() for path in raw_paths.split("|") if path.strip())
            if not separator or not name or not paths:
                raise PickerError(f"invalid host route {entry!r}; expected name=endpoint|fallback")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
                raise PickerError(f"invalid host route name {name!r}")
            if any(path.startswith("-") or re.search(r"[\s,|=]", path) for path in paths):
                raise PickerError(f"invalid host route endpoint in {entry!r}")
            normalized_key = name.casefold()
            if normalized_key in seen_keys:
                raise PickerError(f"duplicate host route {name!r}")
            seen_keys.add(normalized_key)
            routes.append(HostTarget(paths[0], name, paths))
    return routes


def parse_host_target(value: str) -> HostTarget:
    value = value.strip()
    if value in {"", "local"}:
        return HostTarget(None)
    if "=" in value:
        return parse_host_routes([value])[0]
    return HostTarget(_validate_host(value))


def _ssh_prefix(policy: SshPolicy) -> list[str]:
    return [
        policy.executable,
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={policy.connect_timeout}",
        "-o",
        f"ConnectionAttempts={policy.connection_attempts}",
        "-o",
        "LogLevel=ERROR",
    ]


def resolve_host_target(
    target: HostTarget,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> HostTarget:
    """Choose the first reachable path for a logical host route.

    Legacy single-host targets keep their existing no-preflight behavior. A
    route pays for at most one successful SSH handshake before its regular
    Codex, Claude, and activity probes begin.
    """

    if not target.route_paths:
        return target

    failures: list[str] = []
    for path in target.route_paths:
        command = _ssh_prefix(ssh_policy) + [path, "true"]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=ssh_policy.connect_timeout + 2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{path}: {exc}")
            continue
        if result.returncode == 0:
            return target.with_connect_host(path)
        detail = result.stderr.strip() or f"ssh exited with {result.returncode}"
        failures.append(f"{path}: {detail}")

    detail = "; ".join(failures)
    raise PickerError(f"no reachable connection path for {target.key}: {detail}")


def _local_scope_command(command: list[str]) -> list[str]:
    systemd_run = shutil.which("systemd-run")
    if systemd_run is None:
        return command
    return [
        systemd_run,
        "--user",
        "--scope",
        "--collect",
        "--quiet",
        "--",
    ] + command


def _tmux_creation_command(
    target: HostTarget,
    script: str,
    ssh_policy: SshPolicy,
) -> list[str]:
    if target.connect_host is not None:
        return _ssh_prefix(ssh_policy) + [
            target.connect_host,
            "sh -lc " + shlex.quote(script),
        ]
    return _local_scope_command(["sh", "-lc", script])


def _app_server_command(target: HostTarget, ssh_policy: SshPolicy) -> list[str]:
    if target.connect_host is None:
        codex = shutil.which("codex")
        if not codex:
            raise PickerError("codex is not installed")
        return [codex, "app-server", "--stdio"]
    return _ssh_prefix(ssh_policy) + [target.connect_host, "codex app-server --stdio"]


def list_codex_threads(
    target: HostTarget,
    limit: int,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> list[dict[str, Any]]:
    with AppServerClient(
        _app_server_command(target, ssh_policy), timeout, VERSION, PickerError
    ) as client:
        client.initialize()
        result = client.call(
            "thread/list",
            {
                "archived": False,
                "limit": limit,
                "sortDirection": "desc",
                "sortKey": "recency_at",
                "sourceKinds": ["cli"],
                "useStateDbOnly": True,
            },
        )
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise PickerError("Codex app-server returned an invalid thread list")
    return [item for item in result["data"] if isinstance(item, dict)]


def read_codex_thread(
    target: HostTarget,
    thread_id: str,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> dict[str, Any]:
    with AppServerClient(
        _app_server_command(target, ssh_policy), timeout, VERSION, PickerError
    ) as client:
        client.initialize()
        result = client.call("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = result.get("thread") if isinstance(result, dict) else None
    if not isinstance(thread, dict):
        raise PickerError(f"Codex session {thread_id} was not found")
    return thread


def _query_session_probe(
    target: HostTarget,
    limit: int,
    timeout: float,
    ssh_policy: SshPolicy,
    probe: str,
    provider: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    arguments = [str(limit), session_id or ""]
    if target.connect_host is None:
        command = [sys.executable, "-"] + arguments
    else:
        remote_command = "python3 - " + " ".join(shlex.quote(value) for value in arguments)
        command = _ssh_prefix(ssh_policy) + [target.connect_host, remote_command]
    try:
        result = subprocess.run(
            command,
            input=probe,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PickerError(f"{provider} session query timed out on {target.key}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise PickerError(f"{provider} session query failed on {target.key}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PickerError(
            f"{provider} session query returned invalid JSON on {target.key}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list):
        raise PickerError(f"{provider} session query returned invalid data on {target.key}")
    return payload


def query_claude_sessions(
    target: HostTarget,
    limit: int,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
    session_id: str | None = None,
) -> dict[str, Any]:
    return _query_session_probe(
        target,
        limit,
        timeout,
        ssh_policy,
        CLAUDE_SESSION_PROBE,
        "Claude",
        session_id,
    )


def query_opencode_sessions(
    target: HostTarget,
    limit: int,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
    session_id: str | None = None,
) -> dict[str, Any]:
    return _query_session_probe(
        target,
        limit,
        timeout,
        ssh_policy,
        OPENCODE_SESSION_PROBE,
        "opencode",
        session_id,
    )


def list_claude_sessions(
    target: HostTarget,
    limit: int,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> dict[str, Any]:
    return query_claude_sessions(target, limit, timeout, ssh_policy)


def list_opencode_sessions(
    target: HostTarget,
    limit: int,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> dict[str, Any]:
    return query_opencode_sessions(target, limit, timeout, ssh_policy)


def read_claude_session(
    target: HostTarget,
    session_id: str,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> dict[str, Any]:
    result = query_claude_sessions(target, 1, timeout, ssh_policy, session_id)
    if not result.get("installed"):
        raise PickerError(f"Claude Code is not installed on {target.key}")
    sessions = result["sessions"]
    if not sessions or not isinstance(sessions[0], dict):
        raise PickerError(f"Claude session {session_id} was not found on {target.key}")
    return sessions[0]


def read_opencode_session(
    target: HostTarget,
    session_id: str,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> dict[str, Any]:
    result = query_opencode_sessions(target, 1, timeout, ssh_policy, session_id)
    if not result.get("installed"):
        raise PickerError(f"opencode is not installed on {target.key}")
    sessions = result["sessions"]
    if not sessions or not isinstance(sessions[0], dict):
        raise PickerError(f"opencode session {session_id} was not found on {target.key}")
    return sessions[0]


def _process_arguments(pid: int) -> list[str]:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except (FileNotFoundError, PermissionError, OSError):
        return []


def _is_shared_codex_app_server(arguments: Sequence[str]) -> bool:
    try:
        app_server_index = arguments.index("app-server")
    except ValueError:
        return False

    server_arguments = arguments[app_server_index + 1 :]
    for index, argument in enumerate(server_arguments):
        endpoint: str | None = None
        if argument == "--listen" and index + 1 < len(server_arguments):
            endpoint = server_arguments[index + 1]
        elif argument.startswith("--listen="):
            endpoint = argument.partition("=")[2]
        if endpoint is not None:
            return endpoint not in {"stdio://", "off"}
    return False


def _process_table() -> tuple[dict[int, int], set[int], set[int], set[int]]:
    result = subprocess.run(
        ["ps", "-u", str(os.getuid()), "-o", "pid=,ppid=,comm="],
        check=True,
        capture_output=True,
        text=True,
    )
    parents: dict[int, int] = {}
    codex_pids: set[int] = set()
    claude_pids: set[int] = set()
    opencode_pids: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        pid, parent = int(parts[0]), int(parts[1])
        parents[pid] = parent
        command = parts[2].lower()
        if "codex" in command:
            if _is_shared_codex_app_server(_process_arguments(pid)):
                continue
            codex_pids.add(pid)
        if "claude" in command:
            claude_pids.add(pid)
        if "opencode" in command:
            opencode_pids.add(pid)
    return parents, codex_pids, claude_pids, opencode_pids


def _rollout_is_subagent(path: str) -> bool | None:
    """Return whether a rollout's session metadata identifies a subagent.

    A process can keep several Codex rollout files open at once.  The first
    line is the session metadata for current and recent files, while older or
    racing files may be unreadable.  ``None`` deliberately means unknown so a
    legacy or inaccessible rollout can still be used as a best-effort
    fallback.
    """

    try:
        with Path(path).open(encoding="utf-8") as stream:
            record = json.loads(stream.readline())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, Mapping) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return None
    source = payload.get("source")
    return isinstance(source, Mapping) and "subagent" in source


def _rollout_candidates_for_process(
    pid: int, proc_root: str | Path = "/proc"
) -> list[tuple[str, bool | None]]:
    fd_dir = Path(proc_root) / str(pid) / "fd"
    try:
        entries = sorted(fd_dir.iterdir(), key=lambda entry: entry.name)
    except (FileNotFoundError, PermissionError, OSError):
        return []

    candidates: dict[str, bool | None] = {}
    for entry in entries:
        try:
            target = os.readlink(entry)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if "rollout-" not in target or ".jsonl" not in target:
            continue
        match = UUID_PATTERN.search(target)
        if not match:
            continue
        thread_id = match.group(1).lower()
        is_subagent = _rollout_is_subagent(target)
        if thread_id not in candidates or (
            candidates[thread_id] is True and is_subagent is not True
        ):
            candidates[thread_id] = is_subagent
    return sorted(candidates.items())


def _thread_id_for_process(pid: int, proc_root: str | Path = "/proc") -> str | None:
    candidates = _rollout_candidates_for_process(pid, proc_root)
    for is_subagent in (False, None):
        roots = [
            thread_id
            for thread_id, candidate_is_subagent in candidates
            if candidate_is_subagent is is_subagent
        ]
        if roots:
            return roots[0]
    return None


def _tmux_panes() -> tuple[dict[int, str], dict[str, str], dict[str, str], dict[str, str]]:
    result = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{pane_pid}\t#{@codex_thread_id}\t#{@claude_session_id}\t#{@opencode_session_id}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {}, {}, {}, {}

    pane_sessions: dict[int, str] = {}
    codex_option_sessions: dict[str, str] = {}
    claude_option_sessions: dict[str, str] = {}
    opencode_option_sessions: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t", 4)
        if len(parts) < 2:
            continue
        try:
            pane_sessions[int(parts[1])] = parts[0]
        except ValueError:
            continue
        if len(parts) >= 3 and UUID_PATTERN.fullmatch(parts[2]):
            codex_option_sessions[parts[2].lower()] = parts[0]
        if len(parts) >= 4 and UUID_PATTERN.fullmatch(parts[3]):
            claude_option_sessions[parts[3].lower()] = parts[0]
        if len(parts) == 5 and OPENCODE_ID_PATTERN.fullmatch(parts[4]):
            opencode_option_sessions[parts[4]] = parts[0]
    return pane_sessions, codex_option_sessions, claude_option_sessions, opencode_option_sessions


def _tmux_session_for_process(
    pid: int, parents: Mapping[int, int], pane_sessions: Mapping[int, str]
) -> str | None:
    current = pid
    visited: set[int] = set()
    while current > 1 and current not in visited:
        visited.add(current)
        tmux_session = pane_sessions.get(current)
        if tmux_session is not None:
            return tmux_session
        current = parents.get(current, 0)
    return None


def _claude_session_id_from_args(arguments: Sequence[str]) -> str | None:
    value_flags = {"--resume", "-r", "--session-id"}
    for index, argument in enumerate(arguments):
        if argument in value_flags and index + 1 < len(arguments):
            candidate = arguments[index + 1].lower()
            if UUID_PATTERN.fullmatch(candidate):
                return candidate
        for flag in ("--resume=", "--session-id="):
            if argument.startswith(flag):
                candidate = argument[len(flag) :].lower()
                if UUID_PATTERN.fullmatch(candidate):
                    return candidate
    return None


def _claude_session_id_for_process(pid: int) -> str | None:
    try:
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except (FileNotFoundError, PermissionError, OSError):
        arguments = []
    session_id = _claude_session_id_from_args(arguments)
    if session_id is not None:
        return session_id

    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        entries = list(fd_dir.iterdir())
    except (FileNotFoundError, PermissionError):
        return None
    for entry in entries:
        try:
            target = os.readlink(entry)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        path = Path(target)
        if path.suffix != ".jsonl" or "projects" not in path.parts:
            continue
        candidate = path.stem.lower()
        if UUID_PATTERN.fullmatch(candidate):
            return candidate
    return None


def _opencode_session_id_from_args(arguments: Sequence[str]) -> str | None:
    value_flags = {"--session", "-s"}
    for index, argument in enumerate(arguments):
        if argument in value_flags and index + 1 < len(arguments):
            candidate = arguments[index + 1]
            if OPENCODE_ID_PATTERN.fullmatch(candidate):
                return candidate
        if argument.startswith("--session="):
            candidate = argument.partition("=")[2]
            if OPENCODE_ID_PATTERN.fullmatch(candidate):
                return candidate
    return None


def _opencode_session_id_for_process(pid: int) -> str | None:
    try:
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except (FileNotFoundError, PermissionError, OSError):
        arguments = []
    return _opencode_session_id_from_args(arguments)


def _opencode_active_sessions(
    parents: Mapping[int, int],
    opencode_pids: set[int],
    pane_sessions: Mapping[int, str],
    option_sessions: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    option_ids = {tmux_session: session_id for session_id, tmux_session in option_sessions.items()}
    active: dict[str, dict[str, Any]] = {}
    for pid in opencode_pids:
        tmux_session = _tmux_session_for_process(pid, parents, pane_sessions)
        session_id = option_ids.get(tmux_session or "")
        if session_id is None:
            session_id = _opencode_session_id_for_process(pid)
        if session_id is None:
            continue
        item = {"pid": pid, "tmuxSession": tmux_session}
        previous = active.get(session_id)
        if previous is None or (previous.get("tmuxSession") is None and tmux_session):
            active[session_id] = item
    return active


def _claude_active_sessions(
    parents: Mapping[int, int],
    claude_pids: set[int],
    pane_sessions: Mapping[int, str],
    option_sessions: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    option_ids = {tmux_session: session_id for session_id, tmux_session in option_sessions.items()}
    active: dict[str, dict[str, Any]] = {}
    for pid in claude_pids:
        tmux_session = _tmux_session_for_process(pid, parents, pane_sessions)
        session_id = option_ids.get(tmux_session or "")
        if session_id is None:
            session_id = _claude_session_id_for_process(pid)
        if session_id is None:
            continue
        item = {"pid": pid, "tmuxSession": tmux_session}
        previous = active.get(session_id)
        if previous is None or (previous.get("tmuxSession") is None and tmux_session):
            active[session_id] = item
    return active


def active_snapshot() -> dict[str, Any]:
    parents, codex_pids, claude_pids, opencode_pids = _process_table()
    pane_sessions, option_sessions, claude_option_sessions, opencode_option_sessions = _tmux_panes()
    active: dict[str, dict[str, Any]] = {}
    option_ids = {tmux_session: thread_id for thread_id, tmux_session in option_sessions.items()}

    for pid in codex_pids:
        tmux_session = _tmux_session_for_process(pid, parents, pane_sessions)
        thread_id = option_ids.get(tmux_session or "")
        if thread_id is None:
            thread_id = _thread_id_for_process(pid)
        if thread_id is None:
            continue

        if tmux_session is None:
            tmux_session = option_sessions.get(thread_id)

        item = {"pid": pid, "tmuxSession": tmux_session}
        previous = active.get(thread_id)
        if previous is None or (previous.get("tmuxSession") is None and tmux_session):
            active[thread_id] = item

    return {
        "host": socket.gethostname(),
        "active": active,
        "claudeInstalled": shutil.which("claude") is not None,
        "claudeActive": _claude_active_sessions(
            parents,
            claude_pids,
            pane_sessions,
            claude_option_sessions,
        ),
        "opencodeInstalled": shutil.which("opencode") is not None,
        "opencodeActive": _opencode_active_sessions(
            parents,
            opencode_pids,
            pane_sessions,
            opencode_option_sessions,
        ),
    }


ACTIVE_PROBE = r"""
import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

uuid_pattern = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
proc_root = Path(
    os.environ.get(
        "ROFI_AGENT_PLUS_PROC_ROOT",
        os.environ.get("DMS_AGENT_PICKER_PROC_ROOT", "/proc"),
    )
)

def is_shared_codex_app_server(arguments):
    try:
        app_server_index = arguments.index("app-server")
    except ValueError:
        return False
    server_arguments = arguments[app_server_index + 1:]
    for index, argument in enumerate(server_arguments):
        endpoint = None
        if argument == "--listen" and index + 1 < len(server_arguments):
            endpoint = server_arguments[index + 1]
        elif argument.startswith("--listen="):
            endpoint = argument.partition("=")[2]
        if endpoint is not None:
            return endpoint not in {"stdio://", "off"}
    return False

ps = subprocess.run(
    ["ps", "-u", str(os.getuid()), "-o", "pid=,ppid=,comm="],
    check=True,
    capture_output=True,
    text=True,
)
parents = {}
codex_pids = set()
claude_pids = set()
opencode_pids = set()
for line in ps.stdout.splitlines():
    parts = line.split(None, 2)
    if len(parts) != 3:
        continue
    pid, parent = int(parts[0]), int(parts[1])
    parents[pid] = parent
    command = parts[2].lower()
    if "codex" in command:
        try:
            arguments = (proc_root / str(pid) / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except (FileNotFoundError, PermissionError, OSError):
            arguments = []
        if is_shared_codex_app_server(arguments):
            continue
        codex_pids.add(pid)
    if "claude" in command:
        claude_pids.add(pid)
    if "opencode" in command:
        opencode_pids.add(pid)

panes = subprocess.run(
    [
        "tmux",
        "list-panes",
        "-a",
        "-F",
        "#{session_name}\t#{pane_pid}\t#{@codex_thread_id}\t#{@claude_session_id}\t#{@opencode_session_id}",
    ],
    capture_output=True,
    text=True,
)
pane_sessions = {}
option_sessions = {}
claude_option_sessions = {}
opencode_option_sessions = {}
if panes.returncode == 0:
    for line in panes.stdout.splitlines():
        parts = line.split("\t", 4)
        if len(parts) < 2:
            continue
        try:
            pane_sessions[int(parts[1])] = parts[0]
        except ValueError:
            continue
        if len(parts) >= 3 and uuid_pattern.fullmatch(parts[2]):
            option_sessions[parts[2].lower()] = parts[0]
        if len(parts) >= 4 and uuid_pattern.fullmatch(parts[3]):
            claude_option_sessions[parts[3].lower()] = parts[0]
        if len(parts) == 5 and parts[4].startswith("ses_"):
            opencode_option_sessions[parts[4]] = parts[0]

active = {}
def tmux_session_for_process(pid):
    current = pid
    visited = set()
    while current > 1 and current not in visited:
        visited.add(current)
        tmux_session = pane_sessions.get(current)
        if tmux_session is not None:
            return tmux_session
        current = parents.get(current, 0)
    return None

def rollout_is_subagent(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            record = json.loads(stream.readline())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    source = payload.get("source")
    return isinstance(source, dict) and "subagent" in source

def thread_id_for_process(pid):
    fd_dir = proc_root / str(pid) / "fd"
    try:
        entries = sorted(fd_dir.iterdir(), key=lambda entry: entry.name)
    except (FileNotFoundError, PermissionError, OSError):
        return None

    candidates = {}
    for entry in entries:
        try:
            target = os.readlink(entry)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if "rollout-" not in target or ".jsonl" not in target:
            continue
        match = uuid_pattern.search(target)
        if not match:
            continue
        thread_id = match.group(1).lower()
        is_subagent = rollout_is_subagent(target)
        if thread_id not in candidates or (
            candidates[thread_id] is True and is_subagent is not True
        ):
            candidates[thread_id] = is_subagent

    for is_subagent in (False, None):
        roots = sorted(
            thread_id
            for thread_id, candidate_is_subagent in candidates.items()
            if candidate_is_subagent is is_subagent
        )
        if roots:
            return roots[0]
    return None

option_ids = {
    tmux_session: thread_id for thread_id, tmux_session in option_sessions.items()
}
for pid in codex_pids:
    tmux_session = tmux_session_for_process(pid)
    thread_id = option_ids.get(tmux_session or "")
    if thread_id is None:
        thread_id = thread_id_for_process(pid)
    if thread_id is None:
        continue

    if tmux_session is None:
        tmux_session = option_sessions.get(thread_id)
    item = {"pid": pid, "tmuxSession": tmux_session}
    previous = active.get(thread_id)
    if previous is None or (previous.get("tmuxSession") is None and tmux_session):
        active[thread_id] = item

def claude_session_id_from_args(arguments):
    for index, argument in enumerate(arguments):
        if argument in {"--resume", "-r", "--session-id"} and index + 1 < len(arguments):
            candidate = arguments[index + 1].lower()
            if uuid_pattern.fullmatch(candidate):
                return candidate
        for flag in ("--resume=", "--session-id="):
            if argument.startswith(flag):
                candidate = argument[len(flag):].lower()
                if uuid_pattern.fullmatch(candidate):
                    return candidate
    return None

def claude_session_id_for_process(pid):
    try:
        arguments = (proc_root / str(pid) / "cmdline").read_bytes().decode(errors="replace").split("\0")
    except (FileNotFoundError, PermissionError, OSError):
        arguments = []
    session_id = claude_session_id_from_args(arguments)
    if session_id is not None:
        return session_id
    try:
        entries = list((proc_root / str(pid) / "fd").iterdir())
    except (FileNotFoundError, PermissionError):
        entries = []
    for entry in entries:
        try:
            target = os.readlink(entry)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        path = Path(target)
        if path.suffix != ".jsonl" or "projects" not in path.parts:
            continue
        candidate = path.stem.lower()
        if uuid_pattern.fullmatch(candidate):
            return candidate
    return None

claude_active = {}
claude_option_ids = {
    tmux_session: session_id for session_id, tmux_session in claude_option_sessions.items()
}
for pid in claude_pids:
    tmux_session = tmux_session_for_process(pid)
    session_id = claude_option_ids.get(tmux_session or "")
    if session_id is None:
        session_id = claude_session_id_for_process(pid)
    if session_id is None:
        continue
    item = {"pid": pid, "tmuxSession": tmux_session}
    previous = claude_active.get(session_id)
    if previous is None or (previous.get("tmuxSession") is None and tmux_session):
        claude_active[session_id] = item

def opencode_session_id_from_args(arguments):
    for index, argument in enumerate(arguments):
        if argument in {"--session", "-s"} and index + 1 < len(arguments):
            candidate = arguments[index + 1]
            if candidate.startswith("ses_"):
                return candidate
        if argument.startswith("--session="):
            candidate = argument.partition("=")[2]
            if candidate.startswith("ses_"):
                return candidate
    return None

def opencode_session_id_for_process(pid):
    try:
        arguments = (proc_root / str(pid) / "cmdline").read_bytes().decode(errors="replace").split("\0")
    except (FileNotFoundError, PermissionError, OSError):
        arguments = []
    return opencode_session_id_from_args(arguments)

opencode_active = {}
opencode_option_ids = {
    tmux_session: session_id for session_id, tmux_session in opencode_option_sessions.items()
}
for pid in opencode_pids:
    tmux_session = tmux_session_for_process(pid)
    session_id = opencode_option_ids.get(tmux_session or "")
    if session_id is None:
        session_id = opencode_session_id_for_process(pid)
    if session_id is None:
        continue
    item = {"pid": pid, "tmuxSession": tmux_session}
    previous = opencode_active.get(session_id)
    if previous is None or (previous.get("tmuxSession") is None and tmux_session):
        opencode_active[session_id] = item

print(json.dumps(
    {
        "host": socket.gethostname(),
        "active": active,
        "claudeInstalled": shutil.which("claude") is not None,
        "claudeActive": claude_active,
        "opencodeInstalled": shutil.which("opencode") is not None,
        "opencodeActive": opencode_active,
    },
    separators=(",", ":"),
))
"""


def remote_active_snapshot(
    host: str,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            _ssh_prefix(ssh_policy) + [host, "python3 -"],
            input=ACTIVE_PROBE,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PickerError(f"active-session probe timed out on {host}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise PickerError(f"active-session probe failed on {host}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PickerError(f"active-session probe returned invalid JSON on {host}") from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("active"), dict)
        or not isinstance(payload.get("claudeActive"), dict)
        or not isinstance(payload.get("opencodeActive"), dict)
    ):
        raise PickerError(f"active-session probe returned invalid data on {host}")
    return payload


def get_active_snapshot(
    target: HostTarget,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> dict[str, Any]:
    if target.connect_host is None:
        return active_snapshot()
    return remote_active_snapshot(target.connect_host, timeout, ssh_policy)


def merge_host_results(
    host_results: Sequence[HostResult],
    limit: int,
    aliases: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    aliases = aliases or {}
    sessions: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for target, threads_result, claude_result, opencode_result, active_result in host_results:
        if isinstance(active_result, Exception):
            errors.append({"host": target.key, "stage": "active", "message": str(active_result)})
            canonical_host = target.key
            active_map: dict[str, Any] = {}
            claude_active_map: dict[str, Any] = {}
            opencode_active_map: dict[str, Any] = {}
            activity_known = False
        else:
            canonical_host = str(active_result.get("host") or target.key)
            active_map = active_result.get("active", {})
            claude_active_map = active_result.get("claudeActive", {})
            opencode_active_map = active_result.get("opencodeActive", {})
            activity_known = True
        display_host = target.route_key or aliases.get(
            _short_hostname(canonical_host), canonical_host
        )

        if isinstance(threads_result, Exception):
            errors.append({"host": target.key, "stage": "threads", "message": str(threads_result)})
        else:
            for thread in threads_result:
                thread_id = str(thread.get("id") or "").lower()
                if not UUID_PATTERN.fullmatch(thread_id):
                    continue
                active_info = active_map.get(thread_id)
                name = str(thread.get("name") or "").strip()
                cwd = str(thread.get("cwd") or "").strip()
                session = {
                    "kind": "codex",
                    "id": thread_id,
                    "name": name or Path(cwd).name or thread_id[:8],
                    "cwd": cwd,
                    "host": display_host,
                    "windowHost": canonical_host,
                    "connectHost": target.connect_host or "local",
                    "recencyAt": int(thread.get("recencyAt") or 0),
                    "updatedAt": int(thread.get("updatedAt") or 0),
                    "active": active_info is not None,
                    "activityState": (
                        "active"
                        if active_info is not None
                        else "idle"
                        if activity_known
                        else "unknown"
                    ),
                    "tmuxSession": (
                        active_info.get("tmuxSession") if isinstance(active_info, dict) else None
                    ),
                }
                if target.route_spec:
                    session["route"] = target.route_spec
                sessions.append(session)

        if isinstance(claude_result, Exception):
            errors.append({"host": target.key, "stage": "claude", "message": str(claude_result)})
        elif claude_result.get("installed"):
            for conversation in claude_result.get("sessions", []):
                if not isinstance(conversation, dict):
                    continue
                session_id = str(conversation.get("id") or "").lower()
                if not UUID_PATTERN.fullmatch(session_id):
                    continue
                active_info = claude_active_map.get(session_id)
                name = str(conversation.get("name") or "").strip()
                cwd = str(conversation.get("cwd") or "").strip()
                session = {
                    "kind": "claude",
                    "id": session_id,
                    "name": name or Path(cwd).name or session_id[:8],
                    "cwd": cwd,
                    "host": display_host,
                    "windowHost": canonical_host,
                    "connectHost": target.connect_host or "local",
                    "recencyAt": int(conversation.get("recencyAt") or 0),
                    "updatedAt": int(conversation.get("updatedAt") or 0),
                    "active": active_info is not None,
                    "activityState": (
                        "active"
                        if active_info is not None
                        else "idle"
                        if activity_known
                        else "unknown"
                    ),
                    "tmuxSession": (
                        active_info.get("tmuxSession") if isinstance(active_info, dict) else None
                    ),
                }
                if target.route_spec:
                    session["route"] = target.route_spec
                sessions.append(session)

        if isinstance(opencode_result, Exception):
            errors.append(
                {"host": target.key, "stage": "opencode", "message": str(opencode_result)}
            )
        elif opencode_result.get("installed"):
            for conversation in opencode_result.get("sessions", []):
                if not isinstance(conversation, dict):
                    continue
                session_id = str(conversation.get("id") or "")
                if not OPENCODE_ID_PATTERN.fullmatch(session_id):
                    continue
                active_info = opencode_active_map.get(session_id)
                name = str(conversation.get("name") or "").strip()
                cwd = str(conversation.get("cwd") or "").strip()
                session = {
                    "kind": "opencode",
                    "id": session_id,
                    "name": name or Path(cwd).name or session_id,
                    "cwd": cwd,
                    "host": display_host,
                    "windowHost": canonical_host,
                    "connectHost": target.connect_host or "local",
                    "recencyAt": int(conversation.get("recencyAt") or 0),
                    "updatedAt": int(conversation.get("updatedAt") or 0),
                    "active": active_info is not None,
                    "activityState": (
                        "active"
                        if active_info is not None
                        else "idle"
                        if activity_known
                        else "unknown"
                    ),
                    "tmuxSession": (
                        active_info.get("tmuxSession") if isinstance(active_info, dict) else None
                    ),
                }
                if target.route_spec:
                    session["route"] = target.route_spec
                sessions.append(session)

    sessions.sort(key=lambda item: (item["recencyAt"], item["id"]), reverse=True)
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for session in sessions:
        key = (session["windowHost"], session["kind"], session["id"])
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(session)
        if len(deduplicated) >= limit:
            break

    return {
        "generatedAt": int(time.time()),
        "sessions": deduplicated,
        "errors": errors,
    }


def build_host_targets(
    hosts: Sequence[str],
    include_local: bool = True,
    aliases: Mapping[str, str] | None = None,
    routes: Sequence[HostTarget] = (),
) -> list[HostTarget]:
    aliases = aliases or {}
    targets: list[HostTarget] = []
    if include_local:
        targets.append(HostTarget(None))
    local_names = {_short_hostname(socket.gethostname())}
    local_alias = aliases.get(_short_hostname(socket.gethostname()))
    if local_alias:
        local_names.add(_short_hostname(local_alias))
    if routes:
        for route in routes:
            route_names = {_short_hostname(route.key)}
            route_names.update(_short_hostname(path) for path in route.route_paths)
            if route_names.isdisjoint(local_names):
                targets.append(route)
        return targets
    targets.extend(
        HostTarget(_validate_host(host.strip()))
        for host in hosts
        if host.strip() and _short_hostname(host.strip()) not in local_names
    )
    return targets


def _future_result(future: Any) -> Any:
    try:
        return future.result()
    except (PickerError, OSError, subprocess.SubprocessError) as exc:
        return exc


def _iter_host_results(
    targets: Sequence[HostTarget],
    limit: int,
    timeout: float,
    ssh_policy: SshPolicy,
) -> Iterator[tuple[int, HostResult]]:
    workers = max(1, min(4, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures: dict[Any, int] = {}
        for index, target in enumerate(targets):
            futures[pool.submit(_collect_host_result, target, limit, timeout, ssh_policy)] = index
        for future in as_completed(futures):
            yield futures[future], future.result()


def _collect_host_result(
    target: HostTarget,
    limit: int,
    timeout: float,
    ssh_policy: SshPolicy,
) -> HostResult:
    try:
        resolved_target = resolve_host_target(target, ssh_policy)
    except (PickerError, OSError, subprocess.SubprocessError) as exc:
        return target, exc, exc, exc, exc

    with ThreadPoolExecutor(max_workers=4) as pool:
        threads = pool.submit(list_codex_threads, resolved_target, limit, timeout, ssh_policy)
        claude = pool.submit(list_claude_sessions, resolved_target, limit, timeout, ssh_policy)
        opencode = pool.submit(list_opencode_sessions, resolved_target, limit, timeout, ssh_policy)
        active = pool.submit(get_active_snapshot, resolved_target, timeout, ssh_policy)
        return (
            resolved_target,
            _future_result(threads),
            _future_result(claude),
            _future_result(opencode),
            _future_result(active),
        )


def aggregate_sessions(
    hosts: Sequence[str],
    limit: int,
    timeout: float,
    include_local: bool = True,
    aliases: Mapping[str, str] | None = None,
    routes: Sequence[HostTarget] = (),
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> dict[str, Any]:
    aliases = aliases or {}
    targets = build_host_targets(
        hosts,
        include_local=include_local,
        aliases=aliases,
        routes=routes,
    )
    completed: dict[int, HostResult] = {}
    for index, result in _iter_host_results(targets, limit, timeout, ssh_policy):
        completed[index] = result

    return merge_host_results(
        [completed[index] for index in range(len(targets))],
        limit,
        aliases,
    )


def stream_session_events(
    hosts: Sequence[str],
    limit: int,
    timeout: float,
    include_local: bool = True,
    aliases: Mapping[str, str] | None = None,
    routes: Sequence[HostTarget] = (),
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> Iterator[dict[str, Any]]:
    aliases = aliases or {}
    targets = build_host_targets(
        hosts,
        include_local=include_local,
        aliases=aliases,
        routes=routes,
    )
    yield {"event": "refresh-started", "hosts": [target.key for target in targets]}

    for _, result in _iter_host_results(targets, limit, timeout, ssh_policy):
        target = result[0]
        host_payload = merge_host_results([result], limit, aliases)
        yield {
            "event": "host-complete",
            "host": target.key,
            "sessions": host_payload["sessions"],
            "errors": host_payload["errors"],
        }

    yield {"event": "refresh-finished", "generatedAt": int(time.time())}


def _safe_tmux_name(name: str, thread_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-")
    return cleaned[:48] or f"agent-{thread_id[:8]}"


def _tmux_client_wait_script() -> str:
    return (
        "session=$1; shift; "
        f"deadline=$(( $(date +%s) + {SESSION_ATTACH_TIMEOUT_SECONDS} )); "
        'while :; do attached=$(tmux display-message -p -t "$TMUX_PANE" '
        '"#{session_attached}" 2>/dev/null || true); '
        'case "$attached" in ""|0) '
        'if [ "$(date +%s)" -ge "$deadline" ]; then '
        'printf "%s\\n" "rofi-agent-plus: terminal did not attach in time" >&2; '
        'tmux kill-session -t "=$session" 2>/dev/null || true; exit 1; fi; '
        "sleep 0.05 ;; *) break ;; esac; done; "
        'tmux set-option -t "=$session" @agent_picker_waiting 0; '
        'sleep 0.05; exec "$@"'
    )


def _ensure_session_script(thread_id: str, name: str, cwd: str) -> str:
    if not UUID_PATTERN.fullmatch(thread_id):
        raise PickerError("invalid Codex session id")
    base = _safe_tmux_name(name, thread_id)
    short_id = thread_id[:8]
    wait_script = _tmux_client_wait_script()
    return f"""set -eu
thread_id={shlex.quote(thread_id)}
display_name={shlex.quote(name)}
requested_cwd={shlex.quote(cwd)}
base={shlex.quote(base)}
short_id={shlex.quote(short_id)}
wait_script={shlex.quote(wait_script)}
codex_bin=$(command -v codex)
if [ -z "$requested_cwd" ] || [ ! -d "$requested_cwd" ]; then
    requested_cwd=$HOME
fi
existing=$(tmux list-panes -a -F '#{{session_name}}\t#{{@codex_thread_id}}\t#{{@agent_picker_waiting}}' 2>/dev/null | awk -F '\t' -v id="$thread_id" '$2 == id && $3 == "1" {{ print $1; exit }}')
if [ -n "$existing" ]; then
    printf '%s\n' "$existing"
    exit 0
fi
candidate=$base
counter=0
while tmux has-session -t "=$candidate" 2>/dev/null; do
    counter=$((counter + 1))
    if [ "$counter" -eq 1 ]; then
        candidate="${{base}}-${{short_id}}"
    else
        candidate="${{base}}-${{short_id}}-$counter"
    fi
done
tmux new-session -d -s "$candidate" -c "$requested_cwd"
tmux set-option -t "$candidate" @codex_thread_id "$thread_id"
tmux set-option -t "$candidate" @codex_name "$display_name"
tmux set-option -t "$candidate" @agent_picker_waiting 1
codex_command="exec sh -c '$wait_script' sh \"$candidate\" \"$codex_bin\" resume \"$thread_id\""
tmux respawn-pane -k -t "$candidate:0.0" -c "$requested_cwd" "$codex_command"
printf '%s\n' "$candidate"
"""


def ensure_tmux_session(
    target: HostTarget,
    thread_id: str,
    name: str,
    cwd: str,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> str:
    script = _ensure_session_script(thread_id, name, cwd)
    command = _tmux_creation_command(target, script, ssh_policy)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise PickerError(f"timed out creating tmux session on {target.key}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise PickerError(f"failed to create tmux session on {target.key}: {detail}")
    session = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not session:
        raise PickerError(f"tmux did not return a session name on {target.key}")
    return session


def resolve_open_target(
    target: HostTarget,
    thread_id: str,
    name: str | None,
    cwd: str | None,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> str:
    snapshot = get_active_snapshot(target, timeout, ssh_policy)
    active_info = snapshot.get("active", {}).get(thread_id)
    if isinstance(active_info, dict):
        tmux_session = active_info.get("tmuxSession")
        if tmux_session:
            return str(tmux_session)
        raise PickerError(f"Codex session {thread_id[:8]} is active on {target.key} outside tmux")

    if not name or cwd is None:
        thread = read_codex_thread(target, thread_id, timeout, ssh_policy)
        name = name or str(thread.get("name") or "")
        cwd = cwd if cwd is not None else str(thread.get("cwd") or "")
    return ensure_tmux_session(
        target,
        thread_id,
        name or "codex",
        cwd or "",
        timeout,
        ssh_policy,
    )


def _ensure_claude_session_script(session_id: str, name: str, cwd: str) -> str:
    if not UUID_PATTERN.fullmatch(session_id):
        raise PickerError("invalid Claude session id")
    base = _safe_tmux_name(name, session_id)
    short_id = session_id[:8]
    wait_script = _tmux_client_wait_script()
    return f"""set -eu
session_id={shlex.quote(session_id)}
display_name={shlex.quote(name)}
requested_cwd={shlex.quote(cwd)}
base={shlex.quote(base)}
short_id={shlex.quote(short_id)}
wait_script={shlex.quote(wait_script)}
claude_bin=$(command -v claude || true)
if [ -z "$claude_bin" ]; then
    printf '%s\n' 'claude is not installed' >&2
    exit 1
fi
if [ -z "$requested_cwd" ] || [ ! -d "$requested_cwd" ]; then
    requested_cwd=$HOME
fi
existing=$(tmux list-panes -a -F '#{{session_name}}\t#{{@claude_session_id}}\t#{{@agent_picker_waiting}}' 2>/dev/null | awk -F '\t' -v id="$session_id" '$2 == id && $3 == "1" {{ print $1; exit }}')
if [ -n "$existing" ]; then
    printf '%s\n' "$existing"
    exit 0
fi
candidate=$base
counter=0
while tmux has-session -t "=$candidate" 2>/dev/null; do
    counter=$((counter + 1))
    if [ "$counter" -eq 1 ]; then
        candidate="${{base}}-${{short_id}}"
    else
        candidate="${{base}}-${{short_id}}-$counter"
    fi
done
tmux new-session -d -s "$candidate" -c "$requested_cwd"
tmux set-option -t "$candidate" @claude_session_id "$session_id"
tmux set-option -t "$candidate" @claude_name "$display_name"
tmux set-option -t "$candidate" @agent_picker_waiting 1
claude_command="exec sh -c '$wait_script' sh \"$candidate\" \"$claude_bin\" --resume \"$session_id\""
tmux respawn-pane -k -t "$candidate:0.0" -c "$requested_cwd" "$claude_command"
printf '%s\n' "$candidate"
"""


def ensure_claude_tmux_session(
    target: HostTarget,
    session_id: str,
    name: str,
    cwd: str,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> str:
    script = _ensure_claude_session_script(session_id, name, cwd)
    command = _tmux_creation_command(target, script, ssh_policy)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise PickerError(f"timed out creating Claude session on {target.key}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise PickerError(f"failed to create Claude session on {target.key}: {detail}")
    session = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not session:
        raise PickerError(f"tmux did not return a Claude session on {target.key}")
    return session


def resolve_claude_open_target(
    target: HostTarget,
    session_id: str,
    name: str | None,
    cwd: str | None,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> str:
    snapshot = get_active_snapshot(target, timeout, ssh_policy)
    if not snapshot.get("claudeInstalled"):
        raise PickerError(f"Claude Code is not installed on {target.key}")

    active_info = snapshot.get("claudeActive", {}).get(session_id)
    if isinstance(active_info, dict):
        tmux_session = active_info.get("tmuxSession")
        if tmux_session:
            return str(tmux_session)
        raise PickerError(f"Claude session {session_id[:8]} is active on {target.key} outside tmux")

    if not name or cwd is None:
        conversation = read_claude_session(target, session_id, timeout, ssh_policy)
        name = name or str(conversation.get("name") or "")
        cwd = cwd if cwd is not None else str(conversation.get("cwd") or "")
    return ensure_claude_tmux_session(
        target,
        session_id,
        name or "claude",
        cwd or "",
        timeout,
        ssh_policy,
    )


def _ensure_opencode_session_script(session_id: str, name: str, cwd: str) -> str:
    if not OPENCODE_ID_PATTERN.fullmatch(session_id):
        raise PickerError("invalid opencode session id")
    base = _safe_tmux_name(name, session_id)
    short_id = session_id[:8]
    wait_script = _tmux_client_wait_script()
    return f"""set -eu
session_id={shlex.quote(session_id)}
display_name={shlex.quote(name)}
requested_cwd={shlex.quote(cwd)}
base={shlex.quote(base)}
short_id={shlex.quote(short_id)}
wait_script={shlex.quote(wait_script)}
opencode_bin=$(command -v opencode || true)
if [ -z "$opencode_bin" ]; then
    printf '%s\n' 'opencode is not installed' >&2
    exit 1
fi
if [ -z "$requested_cwd" ] || [ ! -d "$requested_cwd" ]; then
    requested_cwd=$HOME
fi
existing=$(tmux list-panes -a -F '#{{session_name}}\t#{{@opencode_session_id}}\t#{{@agent_picker_waiting}}' 2>/dev/null | awk -F '\t' -v id="$session_id" '$2 == id && $3 == "1" {{ print $1; exit }}')
if [ -n "$existing" ]; then
    printf '%s\n' "$existing"
    exit 0
fi
candidate=$base
counter=0
while tmux has-session -t "=$candidate" 2>/dev/null; do
    counter=$((counter + 1))
    if [ "$counter" -eq 1 ]; then
        candidate="${{base}}-${{short_id}}"
    else
        candidate="${{base}}-${{short_id}}-$counter"
    fi
done
tmux new-session -d -s "$candidate" -c "$requested_cwd"
tmux set-option -t "$candidate" @opencode_session_id "$session_id"
tmux set-option -t "$candidate" @opencode_name "$display_name"
tmux set-option -t "$candidate" @agent_picker_waiting 1
opencode_command="exec sh -c '$wait_script' sh \"$candidate\" \"$opencode_bin\" --session \"$session_id\""
tmux respawn-pane -k -t "$candidate:0.0" -c "$requested_cwd" "$opencode_command"
printf '%s\n' "$candidate"
"""


def ensure_opencode_tmux_session(
    target: HostTarget,
    session_id: str,
    name: str,
    cwd: str,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> str:
    script = _ensure_opencode_session_script(session_id, name, cwd)
    command = _tmux_creation_command(target, script, ssh_policy)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise PickerError(f"timed out creating opencode session on {target.key}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise PickerError(f"failed to create opencode session on {target.key}: {detail}")
    session = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not session:
        raise PickerError(f"tmux did not return an opencode session on {target.key}")
    return session


def resolve_opencode_open_target(
    target: HostTarget,
    session_id: str,
    name: str | None,
    cwd: str | None,
    timeout: float,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
) -> str:
    snapshot = get_active_snapshot(target, timeout, ssh_policy)
    if not snapshot.get("opencodeInstalled"):
        raise PickerError(f"opencode is not installed on {target.key}")

    active_info = snapshot.get("opencodeActive", {}).get(session_id)
    if isinstance(active_info, dict):
        tmux_session = active_info.get("tmuxSession")
        if tmux_session:
            return str(tmux_session)
        raise PickerError(
            f"opencode session {session_id[:8]} is active on {target.key} outside tmux"
        )

    if not name or cwd is None:
        conversation = read_opencode_session(target, session_id, timeout, ssh_policy)
        name = name or str(conversation.get("name") or "")
        cwd = cwd if cwd is not None else str(conversation.get("cwd") or "")
    return ensure_opencode_tmux_session(
        target,
        session_id,
        name or "opencode",
        cwd or "",
        timeout,
        ssh_policy,
    )


def _terminal_command(terminal: str, inner: list[str]) -> list[str]:
    command = shlex.split(terminal)
    if not command:
        raise PickerError("terminal command is empty")
    executable = shutil.which(command[0])
    if executable is None:
        raise PickerError(f"terminal is not installed: {command[0]}")
    command[0] = executable
    if "-e" not in command:
        command.append("-e")
    return command + inner


def _remote_attach_command(session: str) -> str:
    # zsh expands an unquoted leading "=" as a command path. tmux uses it to
    # request an exact session-name match, so force quotes even for safe names.
    target = "'" + f"={session}".replace("'", "'\"'\"'") + "'"
    # Noninteractive SSH commands may have no locale; force a UTF-8 client so
    # tmux does not replace Unicode cells with underscores while attaching.
    return "exec tmux -u attach-session -t " + target


def _matching_niri_window_id(windows: Sequence[object], session: str, host: str) -> int | None:
    session_prefix = f"{session}:"
    expected_host = _short_hostname(host)
    for window in windows:
        if not isinstance(window, dict):
            continue
        title = window.get("title")
        window_id = window.get("id")
        if not isinstance(title, str) or not isinstance(window_id, int):
            continue
        if not title.startswith(session_prefix) or " @ " not in title:
            continue
        title_host = title.rsplit(" @ ", 1)[1]
        if _short_hostname(title_host) == expected_host:
            return window_id
    return None


def focus_existing_window(session: str, host: str, timeout: float) -> bool:
    niri = shutil.which("niri")
    if not niri or not os.environ.get("NIRI_SOCKET"):
        return False
    command_timeout = min(max(timeout, 0.5), 2.0)
    try:
        result = subprocess.run(
            [niri, "msg", "--json", "windows"],
            capture_output=True,
            text=True,
            timeout=command_timeout,
            check=False,
        )
        if result.returncode != 0:
            return False
        windows = json.loads(result.stdout)
        if not isinstance(windows, list):
            return False
        window_id = _matching_niri_window_id(windows, session, host)
        if window_id is None:
            return False
        focused = subprocess.run(
            [niri, "msg", "action", "focus-window", "--id", str(window_id)],
            capture_output=True,
            text=True,
            timeout=command_timeout,
            check=False,
        )
        return focused.returncode == 0
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired):
        return False


def launch_attach(
    target: HostTarget,
    session: str,
    terminal: str,
    ssh_policy: SshPolicy = DEFAULT_SSH_POLICY,
    detach: bool = False,
) -> None:
    if target.connect_host is None:
        inner = ["tmux", "attach-session", "-t", f"={session}"]
    else:
        remote_command = _remote_attach_command(session)
        inner = _ssh_prefix(ssh_policy) + ["-t", target.connect_host, remote_command]

    command = _terminal_command(terminal, inner)
    command = _local_scope_command(command)
    if detach:
        try:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise PickerError(f"failed to launch terminal: {exc}") from exc
        return
    os.execvp(command[0], command)


def _valid_thread_id(value: str) -> str:
    value = value.lower()
    if not UUID_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("expected a session UUID")
    return value


def _valid_opencode_id(value: str) -> str:
    if not OPENCODE_ID_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("expected an opencode session id")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--ssh-connect-timeout",
        type=int,
        default=DEFAULT_SSH_CONNECT_TIMEOUT,
    )
    parser.add_argument(
        "--ssh-connection-attempts",
        type=int,
        default=DEFAULT_SSH_CONNECTION_ATTEMPTS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list agent sessions as JSON")
    list_parser.add_argument("--host", action="append", default=[])
    list_parser.add_argument(
        "--route",
        action="append",
        default=[],
        help="logical host route in name=endpoint|fallback form",
    )
    list_parser.add_argument(
        "--alias",
        action="append",
        default=[],
        help="display hostname mapping in source=display form",
    )
    list_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    list_parser.add_argument("--no-local", action="store_true")
    list_parser.add_argument(
        "--stream",
        action="store_true",
        help="emit per-host discovery events as JSONL",
    )

    active_parser = subparsers.add_parser("active", help="show active local agent sessions as JSON")
    active_parser.set_defaults(command="active")

    open_parser = subparsers.add_parser("open", help="open or resume a Codex session")
    open_parser.add_argument("--host", default="local")
    open_parser.add_argument("--window-host")
    open_parser.add_argument("--id", required=True, type=_valid_thread_id)
    open_parser.add_argument("--name")
    open_parser.add_argument("--cwd")
    open_parser.add_argument("--terminal", default=os.environ.get("TERMINAL", "ghostty"))
    open_parser.add_argument(
        "--detach",
        action="store_true",
        help="detach the terminal after resolving the session",
    )

    claude_parser = subparsers.add_parser("open-claude", help="open or resume a Claude session")
    claude_parser.add_argument("--host", default="local")
    claude_parser.add_argument("--window-host")
    claude_parser.add_argument("--id", required=True, type=_valid_thread_id)
    claude_parser.add_argument("--name")
    claude_parser.add_argument("--cwd")
    claude_parser.add_argument("--terminal", default=os.environ.get("TERMINAL", "ghostty"))
    claude_parser.add_argument(
        "--detach",
        action="store_true",
        help="detach the terminal after resolving the session",
    )

    opencode_parser = subparsers.add_parser(
        "open-opencode", help="open or resume an opencode session"
    )
    opencode_parser.add_argument("--host", default="local")
    opencode_parser.add_argument("--window-host")
    opencode_parser.add_argument("--id", required=True, type=_valid_opencode_id)
    opencode_parser.add_argument("--name")
    opencode_parser.add_argument("--cwd")
    opencode_parser.add_argument("--terminal", default=os.environ.get("TERMINAL", "ghostty"))
    opencode_parser.add_argument(
        "--detach",
        action="store_true",
        help="detach the terminal after resolving the session",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.timeout <= 0:
            raise PickerError("timeout must be positive")
        if args.ssh_connect_timeout < 1 or args.ssh_connect_timeout > 30:
            raise PickerError("SSH connect timeout must be between 1 and 30 seconds")
        if args.ssh_connection_attempts < 1 or args.ssh_connection_attempts > 5:
            raise PickerError("SSH connection attempts must be between 1 and 5")
        ssh_policy = SshPolicy(args.ssh_connect_timeout, args.ssh_connection_attempts)

        if args.command == "list":
            if args.limit < 1 or args.limit > 200:
                raise PickerError("limit must be between 1 and 200")
            aliases = parse_host_aliases(args.alias)
            routes = parse_host_routes(args.route)
            if args.stream:
                for event in stream_session_events(
                    args.host,
                    args.limit,
                    args.timeout,
                    include_local=not args.no_local,
                    aliases=aliases,
                    routes=routes,
                    ssh_policy=ssh_policy,
                ):
                    print(json.dumps(event, separators=(",", ":")), flush=True)
                return 0
            payload = aggregate_sessions(
                args.host,
                args.limit,
                args.timeout,
                include_local=not args.no_local,
                aliases=aliases,
                routes=routes,
                ssh_policy=ssh_policy,
            )
            print(json.dumps(payload, separators=(",", ":")))
            return 0

        if args.command == "active":
            print(json.dumps(active_snapshot(), separators=(",", ":")))
            return 0

        target = resolve_host_target(parse_host_target(args.host), ssh_policy)
        if args.command == "open-opencode":
            session = resolve_opencode_open_target(
                target,
                args.id,
                args.name,
                args.cwd,
                args.timeout,
                ssh_policy,
            )
        elif args.command == "open-claude":
            session = resolve_claude_open_target(
                target,
                args.id,
                args.name,
                args.cwd,
                args.timeout,
                ssh_policy,
            )
        else:
            session = resolve_open_target(
                target,
                args.id,
                args.name,
                args.cwd,
                args.timeout,
                ssh_policy,
            )
        window_host = args.window_host or target.connect_host or socket.gethostname()
        if focus_existing_window(session, window_host, args.timeout):
            return 0
        launch_attach(target, session, args.terminal, ssh_policy, detach=args.detach)
        return 0
    except PickerError as exc:
        print(f"rofi-agent-plus: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
