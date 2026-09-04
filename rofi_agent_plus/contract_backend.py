"""Process-only Host Mesh/Tmux Session discovery backend.

This module deliberately knows only the public JSON contracts.  The legacy
engine remains intact and is selected whenever the complete capability pair is
not on ``PATH``.  It has no imports from the sibling Plus repositories and no
knowledge of their private files or Python modules.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from threading import Lock
from typing import Any

from . import engine
from .claude_probe import SESSION_PROBE as CLAUDE_SESSION_PROBE
from .codex import AppServerClient
from .opencode_probe import SESSION_PROBE as OPENCODE_SESSION_PROBE

_MAX_STDOUT = 1 << 20
_MAX_STDERR = 1 << 16
_MAX_FIELD = 16 * 1024
_MAX_HOSTS = 128
_HOST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", re.ASCII)
_SESSION_ID = re.compile(r"^\$[0-9]+$", re.ASCII)
_PANE_ID = re.compile(r"^%[0-9]+$", re.ASCII)
_UUID = engine.UUID_PATTERN
_OPENCODE = engine.OPENCODE_ID_PATTERN
_MARKER_PREFIX = "\x1eROFI_PLUS_REACHED_V1:"
_MARKER_SUFFIX = "\x1f\n"
_TRANSPORT_TEXT = (
    "could not resolve hostname",
    "name or service not known",
    "temporary failure in name resolution",
    "connection refused",
    "connection timed out",
    "network is unreachable",
    "no route to host",
    "connection reset",
    "operation timed out",
    "kex_exchange_identification",
)
_OPTIONS = (
    "@codex_thread_id",
    "@codex_name",
    "@claude_session_id",
    "@claude_name",
    "@opencode_session_id",
    "@opencode_name",
    "@agent_picker_waiting",
)
_MAX_HOST_WORKERS = 8
_WHOLE_REFRESH_MIN_SECONDS = 12.0
_WHOLE_REFRESH_MAX_SECONDS = 30.0


class ContractError(engine.PickerError):
    """Visible public-contract failure; never a legacy fallback trigger."""


class StaleMeshError(ContractError):
    """A structured producer stale-mesh envelope, never a text heuristic."""


@dataclass(frozen=True)
class CommandOutput:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _bounded_text(value: object, limit: int = _MAX_FIELD) -> str:
    text = str(value)
    text = "".join(" " if unicodedata.category(char).startswith("C") else char for char in text)
    return text[:limit]


def _run_bounded(
    argv: Sequence[str],
    *,
    input_data: bytes | None = None,
    timeout: float,
    stdout_limit: int = _MAX_STDOUT,
    stderr_limit: int = _MAX_STDERR,
) -> CommandOutput:
    """Run a process without unbounded ``communicate`` capture.

    Output is drained incrementally.  Any timeout or cap breach terminates and
    reaps the exact child before a visible error is raised.
    """

    poller: selectors.BaseSelector | None = None
    process: subprocess.Popen[bytes] | None = None

    def terminate() -> None:
        if process is None or process.poll() is not None:
            return
        try:
            # The local probe itself may have a child ``ps`` process.  Keeping
            # it in a new session lets a deadline reap that whole tiny tree.
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait()

    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as error:
        raise ContractError(f"cannot run contract command: {_bounded_text(error)}") from error
    try:
        assert process.stdout is not None and process.stderr is not None
        poller = selectors.DefaultSelector()
        poller.register(process.stdout, selectors.EVENT_READ, "stdout")
        poller.register(process.stderr, selectors.EVENT_READ, "stderr")
        pending_input = memoryview(input_data or b"")
        if pending_input:
            assert process.stdin is not None
            os.set_blocking(process.stdin.fileno(), False)
            poller.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        elif process.stdin is not None:
            process.stdin.close()
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + timeout
        while poller.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            for key, _events in poller.select(remaining):
                if key.data == "stdin":
                    try:
                        written = os.write(key.fileobj.fileno(), pending_input)
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        written = 0
                    except OSError as error:
                        if error.errno != errno.EPIPE:
                            raise
                        written = 0
                    if written == 0:
                        poller.unregister(key.fileobj)
                        key.fileobj.close()
                        pending_input = memoryview(b"")
                        continue
                    pending_input = pending_input[written:]
                    if not pending_input:
                        poller.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    poller.unregister(key.fileobj)
                    continue
                buffers[key.data].extend(chunk)
                cap = stdout_limit if key.data == "stdout" else stderr_limit
                if len(buffers[key.data]) > cap:
                    raise BufferError(key.data)
        try:
            returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise TimeoutError from error
        try:
            stdout = buffers["stdout"].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContractError("contract command returned invalid UTF-8 stdout") from error
        return CommandOutput(
            tuple(str(value) for value in argv),
            returncode,
            stdout,
            buffers["stderr"].decode("utf-8", "replace"),
        )
    except (TimeoutError, BufferError) as error:
        terminate()
        if isinstance(error, BufferError):
            raise ContractError(f"contract command exceeded {error.args[0]} limit") from error
        raise ContractError("contract command timed out") from error
    finally:
        terminate()
        if poller is not None:
            poller.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


Runner = Callable[..., CommandOutput]


@dataclass(frozen=True)
class Route:
    destination: str
    configured_index: int
    last_reachable_at: int | None = None
    last_unreachable_at: int | None = None


@dataclass(frozen=True)
class MeshHost:
    host_id: str
    display: str
    local: bool
    aliases: tuple[str, ...]
    routes: tuple[Route, ...]


@dataclass(frozen=True)
class Mesh:
    revision: str
    executable: str
    connect_timeout: int
    attempts: int
    hosts: tuple[MeshHost, ...]

    @property
    def local(self) -> MeshHost:
        return next(host for host in self.hosts if host.local)


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not empty and not value)
        or len(value) > _MAX_FIELD
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ContractError(f"Host Mesh {label} is invalid")
    return value


def _host_id(value: object, label: str = "host id") -> str:
    result = _text(value, label)
    if result.startswith("-") or not _HOST_ID.fullmatch(result):
        raise ContractError(f"Host Mesh {label} is invalid")
    return result.casefold()


def _mesh_token(value: object, label: str) -> str:
    """Match Host Mesh's non-identifier token rule for aliases/routes."""

    result = _text(value, label)
    if result.startswith("-") or result.strip() != result or any(char.isspace() for char in result):
        raise ContractError(f"Host Mesh {label} is invalid")
    return result


def _timestamp(value: object, label: str, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise ContractError(f"{label} is invalid")
    return value


def parse_mesh(payload: object) -> Mesh:
    if not isinstance(payload, Mapping) or payload.get("schemaVersion") != 1:
        raise ContractError("Host Mesh returned an unsupported schema")
    _timestamp(payload.get("generatedAt"), "Host Mesh generatedAt")
    revision = _mesh_token(payload.get("meshRevision"), "revision")
    local_id = _host_id(payload.get("localHostId"), "local host id")
    policy = payload.get("sshPolicy")
    hosts_value = payload.get("hosts")
    if not isinstance(policy, Mapping) or not isinstance(hosts_value, list):
        raise ContractError("Host Mesh returned malformed inventory")
    executable = _mesh_token(policy.get("executable"), "SSH executable")
    timeout = policy.get("connectTimeoutSeconds")
    attempts = policy.get("connectionAttempts")
    route_health_ttl = policy.get("routeHealthTtlSeconds")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= 60
        or isinstance(attempts, bool)
        or not isinstance(attempts, int)
        or not 1 <= attempts <= 10
        or isinstance(route_health_ttl, bool)
        or not isinstance(route_health_ttl, int)
        or not 1 <= route_health_ttl <= 86400
        or not 1 <= len(hosts_value) <= _MAX_HOSTS
    ):
        raise ContractError("Host Mesh SSH policy is invalid")
    hosts: list[MeshHost] = []
    owners: dict[str, str] = {}
    for item in hosts_value:
        if not isinstance(item, Mapping):
            raise ContractError("Host Mesh host is invalid")
        host_id = _host_id(item.get("id"))
        display = _text(item.get("display"), "host display")
        local = item.get("local")
        aliases = item.get("aliases")
        routes_value = item.get("routes")
        if (
            not isinstance(local, bool)
            or not isinstance(aliases, list)
            or not isinstance(routes_value, list)
        ):
            raise ContractError("Host Mesh host is invalid")
        for token, label in ((host_id, "host id"), *((alias, "host alias") for alias in aliases)):
            safe = (
                _host_id(token, label)
                if label == "host id"
                else _mesh_token(token, label).casefold()
            )
            owner = owners.setdefault(safe, host_id)
            if owner != host_id:
                raise ContractError("Host Mesh identities are ambiguous")
        routes: list[Route] = []
        route_indices: set[int] = set()
        for route_value in routes_value:
            if not isinstance(route_value, Mapping):
                raise ContractError("Host Mesh route is invalid")
            destination = _mesh_token(route_value.get("destination"), "route")
            index = route_value.get("configuredIndex")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or index < 0
                or index in route_indices
            ):
                raise ContractError("Host Mesh route is invalid")
            route_indices.add(index)
            key = destination.casefold()
            owner = owners.setdefault(key, host_id)
            if owner != host_id:
                raise ContractError("Host Mesh identities are ambiguous")
            if "lastReachableAt" not in route_value or "lastUnreachableAt" not in route_value:
                raise ContractError("Host Mesh route is invalid")
            routes.append(
                Route(
                    destination,
                    index,
                    _timestamp(
                        route_value.get("lastReachableAt"),
                        "Host Mesh route timestamp",
                        nullable=True,
                    ),
                    _timestamp(
                        route_value.get("lastUnreachableAt"),
                        "Host Mesh route timestamp",
                        nullable=True,
                    ),
                )
            )
        if local and routes:
            raise ContractError("Host Mesh local host has routes")
        if not local and not routes:
            raise ContractError("Host Mesh remote host has no routes")
        hosts.append(MeshHost(host_id, display, local, tuple(aliases), tuple(routes)))
    if sum(host.local for host in hosts) != 1 or hosts[0].host_id != local_id or not hosts[0].local:
        raise ContractError("Host Mesh must declare its local host first")
    if len({host.host_id.casefold() for host in hosts}) != len(hosts):
        raise ContractError("Host Mesh host ids are ambiguous")
    return Mesh(revision, executable, timeout, attempts, tuple(hosts))


def _json(command: CommandOutput, source: str) -> Mapping[str, object]:
    if command.returncode != 0:
        raise ContractError(f"{source} failed: {_bounded_text(command.stderr or command.stdout)}")
    try:
        payload = json.loads(command.stdout)
    except json.JSONDecodeError as error:
        raise ContractError(f"{source} returned invalid JSON") from error
    if not isinstance(payload, Mapping):
        raise ContractError(f"{source} returned invalid JSON")
    return payload


def _error_envelope_code(command: CommandOutput) -> str | None:
    """Return only a structurally valid v1 error code.

    Producer diagnostics are untrusted text.  In particular, a sentence that
    happens to contain ``stale_mesh`` cannot change the refresh transaction.
    """

    try:
        payload = json.loads(command.stdout)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(payload, Mapping)
        or payload.get("schemaVersion") != 1
        or payload.get("ok") is not False
        or not isinstance(payload.get("error"), Mapping)
    ):
        return None
    code = payload["error"].get("code")
    if not isinstance(code, str):
        return None
    try:
        return _mesh_token(code, "error code")
    except ContractError:
        return None


def _raise_command_failure(command: CommandOutput, source: str) -> None:
    code = _error_envelope_code(command)
    if code == "stale_mesh":
        raise StaleMeshError("Host Mesh revision changed")
    suffix = f" [{code}]" if code else ""
    raise ContractError(
        f"{source} failed{suffix}: {_bounded_text(command.stderr or command.stdout)}"
    )


def _marker(nonce: str) -> str:
    return f"{_MARKER_PREFIX}{nonce}{_MARKER_SUFFIX}"


def parse_reached_marker(stderr: str, nonce: str) -> tuple[bool, str]:
    marker = _marker(nonce)
    return (True, stderr.replace(marker, "", 1)) if stderr.count(marker) == 1 else (False, stderr)


def _transport_failure(stderr: str, timed_out: bool = False) -> bool:
    return timed_out or any(token in stderr.casefold() for token in _TRANSPORT_TEXT)


def _report_argv(
    mesh_command: str,
    mesh: Mesh,
    host: MeshHost,
    route: Route,
    status: str,
    observed_at: int | None = None,
) -> list[str]:
    return [
        mesh_command,
        "mesh",
        "report-route",
        "--json",
        "--host",
        host.host_id,
        "--route",
        route.destination,
        "--status",
        status,
        "--source",
        "rofi-agent-plus",
        "--mesh-revision",
        mesh.revision,
        "--observed-at",
        str(observed_at if observed_at is not None else time.time_ns() // 1_000_000),
    ]


def _ssh_argv(
    mesh: Mesh,
    route: Route,
    nonce: str,
    remote_argv: Sequence[str] = ("python3", "-"),
) -> list[str]:
    wrapper = r'''printf '\036ROFI_PLUS_REACHED_V1:%s\037\n' "$1" >&2
shift
exec "$@"'''
    remote = " ".join(
        shlex.quote(value)
        for value in ("sh", "-c", wrapper, "rofi-plus-reached", nonce, *remote_argv)
    )
    return [
        mesh.executable,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"ConnectTimeout={mesh.connect_timeout}",
        "-o",
        f"ConnectionAttempts={mesh.attempts}",
        route.destination,
        remote,
    ]


# This is provider/process discovery only.  It intentionally contains no
# tmux invocation or parsing: pane ownership comes solely from Tmux Plus.
_ACTIVE_PROBE = r"""
import json, os, re, socket, subprocess
from pathlib import Path
uuid=re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
opencode_re=re.compile(r"ses_[A-Za-z0-9]+")
root=Path(os.environ.get("ROFI_AGENT_PLUS_PROC_ROOT","/proc")); parents={}; rows=[]
try: output=subprocess.run(["ps","-u",str(os.getuid()),"-o","pid=,ppid=,comm="],capture_output=True,text=True,check=True).stdout
except Exception as error: raise SystemExit("provider process table failed: %s" % error)
for line in output.splitlines():
 p=line.split(None,2)
 if len(p)!=3: continue
 try: pid,ppid=int(p[0]),int(p[1])
 except ValueError: continue
 parents[pid]=ppid; rows.append((pid,p[2].lower()))
def args(pid):
 try: return (root/str(pid)/"cmdline").read_bytes().decode(errors="replace").split("\0")
 except OSError: return []
def ancestry(pid):
 out=[]; seen=set()
 while pid>1 and pid not in seen:
  seen.add(pid); out.append(pid); pid=parents.get(pid,0)
 return out[:128]
def shared(arguments):
 try: begin=arguments.index("app-server")
 except ValueError: return False
 values=arguments[begin+1:]
 for i,value in enumerate(values):
  endpoint=values[i+1] if value=="--listen" and i+1<len(values) else value.partition("=")[2] if value.startswith("--listen=") else None
  if endpoint is not None: return endpoint not in {"stdio://","off"}
 return False
def subagent(path):
 try:
  record=json.loads(Path(path).open(encoding="utf-8").readline())
 except (OSError,UnicodeError,json.JSONDecodeError): return None
 payload=record.get("payload") if isinstance(record,dict) else None
 source=payload.get("source") if isinstance(payload,dict) else None
 return isinstance(source,dict) and "subagent" in source
def codex_id(pid):
 values={}
 try: entries=sorted((root/str(pid)/"fd").iterdir(),key=lambda item:item.name)
 except OSError: return None
 for entry in entries:
  try: target=os.readlink(entry)
  except OSError: continue
  match=uuid.search(target)
  if "rollout-" not in target or ".jsonl" not in target or not match: continue
  ident=match.group(1).lower(); child=subagent(target)
  if ident not in values or (values[ident] is True and child is not True): values[ident]=child
 for child in (False,None):
  found=sorted(ident for ident,value in values.items() if value is child)
  if found: return found[0]
 return None
def claude_id(pid,arguments):
 for i,value in enumerate(arguments):
  if value in ("--resume","-r","--session-id") and i+1<len(arguments) and uuid.fullmatch(arguments[i+1].lower()): return arguments[i+1].lower()
  for flag in ("--resume=","--session-id="):
   if value.startswith(flag) and uuid.fullmatch(value[len(flag):].lower()): return value[len(flag):].lower()
 try: entries=(root/str(pid)/"fd").iterdir()
 except OSError: return None
 for entry in entries:
  try: path=Path(os.readlink(entry))
  except OSError: continue
  if path.suffix==".jsonl" and "projects" in path.parts and uuid.fullmatch(path.stem.lower()): return path.stem.lower()
 return None
def open_id(arguments):
 for i,value in enumerate(arguments):
  if value in ("--session","-s") and i+1<len(arguments) and opencode_re.fullmatch(arguments[i+1]): return arguments[i+1]
  if value.startswith("--session=") and opencode_re.fullmatch(value.partition("=")[2]): return value.partition("=")[2]
 return None
def put(dst,ident,pid):
 if ident: dst.setdefault(ident,[]).append({"pid":pid,"ancestors":ancestry(pid)})
codex={}; claude={}; opencode={}
for pid,command in rows:
 a=args(pid)
 if "codex" in command and not shared(a): put(codex,codex_id(pid),pid)
 if "claude" in command: put(claude,claude_id(pid,a),pid)
 if "opencode" in command: put(opencode,open_id(a),pid)
print(json.dumps({"nativeHostname":socket.gethostname(),"parents":parents,"active":codex,"claudeActive":claude,"opencodeActive":opencode},separators=(",",":")))
"""


def _local_active() -> dict[str, object]:
    """Compatibility helper for direct unit callers.

    Contract discovery itself invokes the fixed probe through ``_run_bounded``
    so its ``ps`` read shares the whole-refresh deadline.
    """

    parents, codex, claude, opencode = engine._process_table()

    def entry(pid: int) -> dict[str, object]:
        chain: list[int] = []
        current = pid
        while current > 1 and current not in chain and len(chain) < 128:
            chain.append(current)
            current = parents.get(current, 0)
        return {"pid": pid, "ancestors": chain}

    def candidates(pids: set[int], resolver: Callable[[int], str | None]) -> dict[str, object]:
        result: dict[str, list[dict[str, object]]] = {}
        for pid in sorted(pids):
            if identifier := resolver(pid):
                result.setdefault(identifier, []).append(entry(pid))
        return {identifier: {"candidates": values} for identifier, values in result.items()}

    return {
        "nativeHostname": socket.gethostname(),
        "parents": parents,
        "active": candidates(codex, engine._thread_id_for_process),
        "claudeActive": candidates(claude, engine._claude_session_id_for_process),
        "opencodeActive": candidates(opencode, engine._opencode_session_id_for_process),
    }


def _validate_active(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise ContractError("provider activity probe returned invalid JSON")
    native = _text(payload.get("nativeHostname"), "native hostname")
    result: dict[str, object] = {"nativeHostname": native}
    for key, pattern in (("active", _UUID), ("claudeActive", _UUID), ("opencodeActive", _OPENCODE)):
        value = payload.get(key)
        if not isinstance(value, Mapping):
            raise ContractError("provider activity probe returned invalid data")
        clean: dict[str, dict[str, object]] = {}
        for identifier, details in value.items():
            if not isinstance(identifier, str) or not pattern.fullmatch(identifier):
                raise ContractError("provider activity probe returned invalid data")
            if isinstance(details, Mapping) and isinstance(details.get("candidates"), list):
                raw_candidates = details["candidates"]
            elif isinstance(details, Mapping):
                # Accept the first P5a draft's single-candidate probe shape
                # while normalizing it before correlation.
                raw_candidates = [details]
            else:
                raise ContractError("provider activity probe returned invalid data")
            if not 1 <= len(raw_candidates) <= 128:
                raise ContractError("provider activity probe returned invalid data")
            candidates: list[dict[str, object]] = []
            seen_pids: set[int] = set()
            for candidate in raw_candidates:
                if not isinstance(candidate, Mapping):
                    raise ContractError("provider activity probe returned invalid data")
                pid = candidate.get("pid")
                ancestors = candidate.get("ancestors")
                if (
                    isinstance(pid, bool)
                    or not isinstance(pid, int)
                    or pid < 1
                    or pid in seen_pids
                    or not isinstance(ancestors, list)
                ):
                    raise ContractError("provider activity probe returned invalid data")
                chain = [
                    item
                    for item in ancestors
                    if isinstance(item, int) and not isinstance(item, bool) and item > 0
                ]
                if len(chain) != len(ancestors) or len(chain) > 128:
                    raise ContractError("provider activity probe returned invalid data")
                seen_pids.add(pid)
                candidates.append({"pid": pid, "ancestors": chain})
            clean[identifier.lower() if key != "opencodeActive" else identifier] = {
                "candidates": candidates,
            }
        result[key] = clean
    return result


def _inventory_args(tmux_command: str, mesh: Mesh) -> list[str]:
    argv = [tmux_command, "inventory", "--json", "--panes", "--mesh-revision", mesh.revision]
    for host in mesh.hosts:
        argv.extend(("--host", host.host_id))
    for option in _OPTIONS:
        argv.extend(("--session-option", option))
    return argv


def _inventory_text(value: object, label: str, *, empty: bool = True) -> str:
    return _text(value, f"Tmux Session {label}", empty=empty)


def _nonnegative(value: object, label: str, *, nullable: bool = False) -> int | None:
    try:
        return _timestamp(value, f"Tmux Session inventory {label}", nullable=nullable)
    except ContractError as error:
        raise ContractError(f"Tmux Session inventory {label} is invalid") from error


def _nullable_inventory_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _inventory_text(value, label)


def _validate_inventory_session(session: object, host: MeshHost, generation: str) -> None:
    if not isinstance(session, Mapping):
        raise ContractError("Tmux Session inventory session is invalid")
    if session.get("hostId") != host.host_id or session.get("serverGeneration") != generation:
        raise ContractError("Tmux Session inventory session is invalid")
    session_id = session.get("sessionId")
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ContractError("Tmux Session inventory session is invalid")
    _nonnegative(session.get("createdAt"), "createdAt")
    _nullable_inventory_text(session.get("name"), "session name")
    _nonnegative(session.get("activityAt"), "activityAt", nullable=True)
    _nonnegative(session.get("lastAttachedAt"), "lastAttachedAt", nullable=True)
    _nonnegative(session.get("attachedClients"), "attachedClients", nullable=True)
    _nonnegative(session.get("windowCount"), "windowCount", nullable=True)
    if not isinstance(session.get("pending"), bool):
        raise ContractError("Tmux Session inventory pending state is invalid")
    for field in ("sessionPath", "currentWindow", "currentPath"):
        _nullable_inventory_text(session.get(field), field)
    panes = session.get("panes")
    options = session.get("options")
    if not isinstance(panes, list) or not isinstance(options, Mapping):
        raise ContractError("Tmux Session inventory omitted requested pane metadata")
    seen_panes: set[str] = set()
    for pane in panes:
        if not isinstance(pane, Mapping):
            raise ContractError("Tmux Session pane is invalid")
        pane_id = pane.get("paneId")
        if not isinstance(pane_id, str) or not _PANE_ID.fullmatch(pane_id) or pane_id in seen_panes:
            raise ContractError("Tmux Session pane is invalid")
        seen_panes.add(pane_id)
        _nonnegative(pane.get("pid"), "pane pid", nullable=True)
        _nullable_inventory_text(pane.get("currentPath"), "pane path")
        _nullable_inventory_text(pane.get("currentCommand"), "pane command")
    for option in _OPTIONS:
        if option not in options:
            raise ContractError("Tmux Session inventory omitted requested option")
        if options[option] is not None:
            _inventory_text(options[option], "session option")


def _inventory(payload: object, mesh: Mesh) -> dict[str, dict[str, object]]:
    if (
        not isinstance(payload, Mapping)
        or payload.get("schemaVersion") != 1
        or payload.get("meshRevision") != mesh.revision
    ):
        raise ContractError("Tmux Session inventory returned an unsupported schema")
    _nonnegative(payload.get("generatedAt"), "generatedAt")
    hosts = payload.get("hosts")
    if not isinstance(hosts, list) or len(hosts) != len(mesh.hosts):
        raise ContractError("Tmux Session inventory host coverage is invalid")
    result: dict[str, dict[str, object]] = {}
    for expected, value in zip(mesh.hosts, hosts, strict=True):
        if (
            not isinstance(value, Mapping)
            or value.get("hostId") != expected.host_id
            or value.get("display") != expected.display
            or value.get("local") is not expected.local
        ):
            raise ContractError("Tmux Session inventory host coverage is invalid")
        status = value.get("status")
        sessions = value.get("sessions")
        if not isinstance(status, str) or not isinstance(sessions, list):
            raise ContractError("Tmux Session inventory host is invalid")
        _nonnegative(value.get("observedAt"), "observedAt")
        native = value.get("nativeHostname")
        if native is not None:
            _mesh_token(native, "native hostname")
        route = value.get("route")
        if expected.local:
            if route is not None:
                raise ContractError("Tmux Session inventory local route is invalid")
        elif status == "unreachable":
            if route is not None:
                raise ContractError("Tmux Session inventory unreachable route is invalid")
        elif route not in {item.destination for item in expected.routes}:
            raise ContractError("Tmux Session inventory route is invalid")
        if status == "ok":
            generation = value.get("serverGeneration")
            if generation is None and sessions:
                raise ContractError("Tmux Session server generation is invalid")
            if generation is not None and (
                not isinstance(generation, str)
                or not generation
                or len(generation) > _MAX_FIELD
                or any(unicodedata.category(char).startswith("C") for char in generation)
            ):
                raise ContractError("Tmux Session server generation is invalid")
        elif status not in {"unreachable", "error", "tmux_missing"}:
            raise ContractError("Tmux Session inventory host status is invalid")
        else:
            if sessions or value.get("serverGeneration") is not None:
                raise ContractError("Tmux Session inventory failure host is invalid")
            error = value.get("error")
            if not isinstance(error, Mapping):
                raise ContractError("Tmux Session inventory error is invalid")
            _inventory_text(error.get("code"), "error code", empty=False)
            _inventory_text(error.get("message"), "error message", empty=False)
        if status == "ok" and value.get("error") is not None:
            raise ContractError("Tmux Session inventory ok host is invalid")
        if isinstance(generation := value.get("serverGeneration"), str):
            seen_refs: set[tuple[str, int]] = set()
            for session in sessions:
                _validate_inventory_session(session, expected, generation)
                assert isinstance(session, Mapping)
                reference = (str(session["sessionId"]), int(session["createdAt"]))
                if reference in seen_refs:
                    raise ContractError("Tmux Session inventory duplicate session reference")
                seen_refs.add(reference)
        result[expected.host_id] = dict(value)
    return result


def _tmux_reference(session: Mapping[str, object], revision: str) -> dict[str, object]:
    """Validate and project a session into the only tmux action identity.

    The provider owns the primary row identity.  This deliberately keeps the
    tmux record subordinate, complete, and impossible to reconstruct from a
    display name later in the UI.
    """

    reference = (
        session.get("serverGeneration"),
        session.get("sessionId"),
        session.get("createdAt"),
    )
    if (
        not isinstance(reference[0], str)
        or not reference[0]
        or not isinstance(reference[1], str)
        or not _SESSION_ID.fullmatch(reference[1])
        or isinstance(reference[2], bool)
        or not isinstance(reference[2], int)
        or reference[2] < 0
    ):
        raise ContractError("Tmux Session inventory session reference is invalid")
    observed_name = session.get("name")
    if observed_name is not None:
        _inventory_text(observed_name, "session name")
    return {
        "meshRevision": revision,
        "serverGeneration": reference[0],
        "sessionId": reference[1],
        "createdAt": reference[2],
        "observedName": observed_name,
    }


def _tmux_association(
    host: Mapping[str, object],
    provider: str,
    identifier: str,
    active: Mapping[str, object],
    revision: str,
) -> tuple[dict[str, object] | None, str | None]:
    """Return one conservative association or an ambiguity diagnostic.

    Options and process ancestry are independent evidence.  We never choose a
    first claimant: duplicated option claims, duplicated pane claims, or a
    disagreement between those sources are explicitly ambiguous.
    """

    option = {
        "codex": "@codex_thread_id",
        "claude": "@claude_session_id",
        "opencode": "@opencode_session_id",
    }[provider]
    active_ancestors: set[int] = set()
    if isinstance(active, Mapping):
        candidates = active.get("candidates")
        if not isinstance(candidates, list):
            candidates = [active]
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                active_ancestors.update(
                    item
                    for item in candidate.get("ancestors", [])
                    if isinstance(item, int) and not isinstance(item, bool) and item > 0
                )
    option_claims: dict[tuple[str, str, int], dict[str, object]] = {}
    process_claims: dict[tuple[str, str, int], dict[str, object]] = {}
    for session in host.get("sessions", []):
        if not isinstance(session, Mapping):
            raise ContractError("Tmux Session inventory session is invalid")
        reference = _tmux_reference(session, revision)
        options = session.get("options")
        panes = session.get("panes")
        if (
            not isinstance(options, Mapping)
            or not isinstance(panes, list)
            or set(_OPTIONS) - set(options)
        ):
            raise ContractError("Tmux Session inventory omitted requested pane metadata")
        key = (
            str(reference["serverGeneration"]),
            str(reference["sessionId"]),
            int(reference["createdAt"]),
        )
        candidate = dict(reference)
        candidate["pending"] = (
            session.get("pending") is True or options.get("@agent_picker_waiting") == "1"
        )
        if options.get(option) == identifier:
            option_claims[key] = candidate
        for pane in panes:
            if not isinstance(pane, Mapping) or not _PANE_ID.fullmatch(
                str(pane.get("paneId") or "")
            ):
                raise ContractError("Tmux Session pane is invalid")
            pid = pane.get("pid")
            if isinstance(pid, int) and not isinstance(pid, bool) and pid in active_ancestors:
                process_claims[key] = candidate

    option_keys = set(option_claims)
    process_keys = set(process_claims)
    if len(option_keys) > 1 or len(process_keys) > 1:
        return None, "ambiguous provider correlation"
    if option_keys and process_keys and option_keys != process_keys:
        return None, "provider option conflicts with process correlation"
    key = next(iter(option_keys or process_keys), None)
    if key is None:
        return None, None
    return option_claims.get(key) or process_claims.get(key), None


@dataclass
class LegacyBackend:
    kind: str = "legacy"
    mesh_revision: str | None = None

    @property
    def identity(self) -> dict[str, object]:
        return {"kind": self.kind, "capability": "legacy-v1", "meshRevision": None}

    def prepare(self) -> None:
        return None

    def stream(self, config: Any) -> Sequence[dict[str, object]]:
        return list(
            engine.stream_session_events(
                config.hosts,
                config.max_sessions,
                engine.DEFAULT_TIMEOUT,
                include_local=True,
                aliases=config.aliases,
                routes=config.routes,
                ssh_policy=config.ssh_policy,
            )
        )


class ContractBackend:
    """Contract-backed discovery with one bounded Mesh retry on stale state."""

    kind = "contract"

    def __init__(
        self,
        ssh_command: str,
        tmux_command: str,
        *,
        runner: Runner = _run_bounded,
        which: Callable[[str], str | None] = shutil.which,
        now_millis: Callable[[], int] | None = None,
    ) -> None:
        self.ssh_command = ssh_command
        self.tmux_command = tmux_command
        self._run = runner
        self._which = which
        self._now_millis = now_millis or (lambda: time.time_ns() // 1_000_000)
        self.mesh: Mesh | None = None
        self._report_errors: list[dict[str, str]] = []
        self._report_error_lock = Lock()
        self._stream_deadline: float | None = None

    @property
    def identity(self) -> dict[str, object]:
        return {
            "kind": "contract",
            "capability": "host-mesh-v1+tmux-session-v1",
            "meshRevision": self.mesh.revision if self.mesh else None,
        }

    def prepare(self, *, deadline: float | None = None) -> None:
        """Observe Host Mesh without extending an enclosing action deadline.

        Browse preparation normally gets the regular refresh budget.  A
        lifecycle action, however, has already spent time revalidating its
        provider row; its final authority re-observation must consume the
        same absolute budget rather than quietly starting another one.
        """

        if deadline is not None:
            if self._stream_deadline is None or deadline < self._stream_deadline:
                self._stream_deadline = deadline
        elif self._stream_deadline is None:
            self._stream_deadline = time.monotonic() + _WHOLE_REFRESH_MAX_SECONDS
        output = self._run(
            [self.ssh_command, "mesh", "list", "--json"],
            timeout=min(5.0, self._remaining(self._stream_deadline)),
        )
        if output.returncode != 0:
            _raise_command_failure(output, "Host Mesh")
        self.mesh = parse_mesh(_json(output, "Host Mesh"))

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ContractError("contract refresh timed out")
        return remaining

    def _report(
        self,
        host: MeshHost,
        route: Route,
        status: str,
        deadline: float,
    ) -> None:
        assert self.mesh is not None
        output = self._run(
            _report_argv(
                self.ssh_command,
                self.mesh,
                host,
                route,
                status,
                self._now_millis(),
            ),
            timeout=min(5.0, self._remaining(deadline)),
        )
        if output.returncode != 0:
            _raise_command_failure(output, "Host Mesh route report")
        response = _json(output, "Host Mesh route report")
        if (
            response.get("schemaVersion") != 1
            or response.get("ok") is not True
            or not isinstance(response.get("accepted"), bool)
        ):
            raise ContractError("Host Mesh route report returned invalid data")

    def _record_report_error(self, host: MeshHost, error: ContractError) -> None:
        with self._report_error_lock:
            self._report_errors.append(
                {
                    "host": host.host_id,
                    "stage": "route-health",
                    "message": _bounded_text(error),
                }
            )

    def _report_hint(self, host: MeshHost, route: Route, status: str, deadline: float) -> None:
        """Send a best-effort health hint without discarding domain truth."""

        try:
            self._report(host, route, status, deadline)
        except StaleMeshError:
            raise
        except ContractError as error:
            self._record_report_error(host, error)

    def _remote_command(
        self,
        host: MeshHost,
        deadline: float,
        *,
        remote_argv: Sequence[str],
        input_data: bytes | None,
        label: str,
    ) -> tuple[str | None, CommandOutput | ContractError]:
        """Run one real remote domain command through every eligible route.

        A marker ends selection even on a domain failure.  Before a marker,
        only classified transport evidence receives an unavailable hint; a
        non-stale report failure remains diagnostic and never destroys a
        reached result or prevents the next candidate.
        """

        assert self.mesh is not None
        unclassified: str | None = None
        for route in host.routes:
            nonce = secrets.token_hex(16)
            try:
                output = self._run(
                    _ssh_argv(self.mesh, route, nonce, remote_argv),
                    input_data=input_data,
                    timeout=min(float(self.mesh.connect_timeout + 4), self._remaining(deadline)),
                )
            except ContractError as error:
                if _transport_failure(str(error), "timed out" in str(error)):
                    self._report_hint(host, route, "unreachable", deadline)
                else:
                    unclassified = _bounded_text(error)
                continue
            reached, stderr = parse_reached_marker(output.stderr, nonce)
            if reached:
                self._report_hint(host, route, "reachable", deadline)
                # Deliberately return the completed domain command even when
                # nonzero: the marker makes it authoritative and terminal.
                return route.destination, CommandOutput(
                    output.argv, output.returncode, output.stdout, stderr, output.timed_out
                )
            if _transport_failure(output.stderr):
                self._report_hint(host, route, "unreachable", deadline)
            else:
                unclassified = _bounded_text(output.stderr or f"SSH exited {output.returncode}")
        return None, ContractError(unclassified or f"{label} could not reach host")

    def _remote_active(
        self, host: MeshHost, deadline: float
    ) -> tuple[str | None, dict[str, object] | Exception]:
        route, result = self._remote_command(
            host,
            deadline,
            remote_argv=("python3", "-"),
            input_data=_ACTIVE_PROBE.encode(),
            label="provider activity probe",
        )
        if isinstance(result, ContractError):
            return route, result
        if result.returncode != 0:
            return route, ContractError(
                f"provider activity probe failed: {_bounded_text(result.stderr)}"
            )
        try:
            return route, _validate_active(_json(result, "provider activity probe"))
        except ContractError as error:
            return route, error

    def _active(
        self, host: MeshHost, deadline: float
    ) -> tuple[str | None, dict[str, object] | Exception]:
        if host.local:
            try:
                output = self._run(
                    [sys.executable, "-"],
                    input_data=_ACTIVE_PROBE.encode(),
                    timeout=self._remaining(deadline),
                )
                return None, _validate_active(_json(output, "provider activity probe"))
            except Exception as error:  # noqa: BLE001 - provider stage remains isolated
                return None, error
        return self._remote_active(host, deadline)

    def _remote_codex_threads(self, host: MeshHost, config: Any, deadline: float) -> object:
        """Discover Codex through marked app-server attempts, never bare SSH."""

        assert self.mesh is not None
        unclassified: str | None = None
        for route in host.routes:
            nonce = secrets.token_hex(16)
            marker = _marker(nonce).encode()
            client: AppServerClient | None = None
            reached = False
            reported_reachable = False
            diagnostic = ""
            try:
                client = AppServerClient(
                    _ssh_argv(self.mesh, route, nonce, ("codex", "app-server", "--stdio")),
                    min(engine.DEFAULT_TIMEOUT, self._remaining(deadline)),
                    engine.VERSION,
                    ContractError,
                    stdout_limit=_MAX_STDOUT,
                    stderr_limit=_MAX_STDERR,
                    reached_marker=marker,
                )
                # The reached-host handshake is the first bounded remote
                # discovery stage.  Its per-route allowance mirrors the
                # other marked SSH commands, retaining enough time for the
                # configured connection timeout without letting one route
                # consume the whole refresh budget and suppress fallback.
                reached, diagnostic = client.wait_for_marker(
                    min(float(self.mesh.connect_timeout + 4), self._remaining(deadline))
                )
                if not reached:
                    if _transport_failure(diagnostic):
                        self._report_hint(host, route, "unreachable", deadline)
                    else:
                        unclassified = _bounded_text(diagnostic or "missing reached-host marker")
                    continue
                self._report_hint(host, route, "reachable", deadline)
                reported_reachable = True
                client.timeout = self._remaining(deadline)
                client.initialize()
                client.timeout = self._remaining(deadline)
                payload = client.call(
                    "thread/list",
                    {
                        "archived": False,
                        "limit": config.max_sessions,
                        "sortDirection": "desc",
                        "sortKey": "recency_at",
                        "sourceKinds": ["cli"],
                        "useStateDbOnly": True,
                    },
                )
                if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
                    raise ContractError("Codex app-server returned invalid thread list")
                return [item for item in payload["data"] if isinstance(item, dict)]
            except StaleMeshError:
                raise
            except ContractError as error:
                if client is not None:
                    reached, diagnostic = client.marker_result()
                if reached:
                    # Domain failure after an exact marker is final and does
                    # not receive a second route attempt.
                    if not reported_reachable:
                        try:
                            self._report_hint(host, route, "reachable", deadline)
                        except StaleMeshError:
                            raise
                    raise error
                if _transport_failure(diagnostic or str(error), "timed out" in str(error)):
                    self._report_hint(host, route, "unreachable", deadline)
                else:
                    unclassified = _bounded_text(diagnostic or error)
            finally:
                if client is not None:
                    client.close()
        raise ContractError(unclassified or "Codex app-server could not reach host")

    def _provider_results(
        self, host: MeshHost, config: Any, deadline: float
    ) -> tuple[object, object, object]:
        assert self.mesh is not None

        def session_probe(probe: str, label: str) -> object:
            timeout = min(engine.DEFAULT_TIMEOUT, self._remaining(deadline))
            arguments = [str(config.max_sessions), ""]
            if host.local:
                argv = [sys.executable, "-", *arguments]
                command = self._run(argv, input_data=probe.encode(), timeout=timeout)
            else:
                route, command = self._remote_command(
                    host,
                    deadline,
                    remote_argv=("python3", "-", *arguments),
                    input_data=probe.encode(),
                    label=f"{label} session query",
                )
                if isinstance(command, ContractError):
                    raise command
                # A reached domain failure is final: do not send a second
                # provider query to another route merely because it failed.
                if route is None:
                    raise ContractError(f"{label} session query could not reach host")
            payload = _json(
                command,
                f"{label} session query",
            )
            if not isinstance(payload.get("installed"), bool) or not isinstance(
                payload.get("sessions"), list
            ):
                raise ContractError(f"{label} session query returned invalid data")
            return dict(payload)

        def codex_threads() -> object:
            timeout = min(engine.DEFAULT_TIMEOUT, self._remaining(deadline))
            if host.local:
                command = ["codex", "app-server", "--stdio"]
            else:
                # Codex's JSON-RPC stream itself is the marked domain command;
                # the marker is verified immediately after initialize before
                # thread/list is allowed to consume its result.
                return self._remote_codex_threads(host, config, deadline)
            with AppServerClient(
                command,
                timeout,
                engine.VERSION,
                ContractError,
                stdout_limit=_MAX_STDOUT,
                stderr_limit=_MAX_STDERR,
            ) as client:
                client.timeout = self._remaining(deadline)
                client.initialize()
                client.timeout = self._remaining(deadline)
                payload = client.call(
                    "thread/list",
                    {
                        "archived": False,
                        "limit": config.max_sessions,
                        "sortDirection": "desc",
                        "sortKey": "recency_at",
                        "sourceKinds": ["cli"],
                        "useStateDbOnly": True,
                    },
                )
            if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
                raise ContractError("Codex app-server returned invalid thread list")
            return [item for item in payload["data"] if isinstance(item, dict)]

        def result(call: Callable[[], object]) -> object:
            try:
                return call()
            except Exception as error:  # noqa: BLE001 - independent provider stages
                return error

        # Provider failures are independent rows, not a reason to discard a
        # reached Host Mesh identity or a successful sibling provider.
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="rofi-agent-provider") as pool:
            futures = [
                pool.submit(result, call)
                for call in (
                    codex_threads,
                    lambda: session_probe(CLAUDE_SESSION_PROBE, "Claude"),
                    lambda: session_probe(OPENCODE_SESSION_PROBE, "opencode"),
                )
            ]
            return tuple(future.result() for future in futures)  # type: ignore[return-value]

    @staticmethod
    def _append_active_only_rows(
        sessions: list[dict[str, object]],
        active: object,
    ) -> None:
        """Keep a provider-active process visible even when history is absent.

        It is intentionally not given a guessed tmux reference.  The later
        correlation pass can add one only when Tmux Plus provides unambiguous
        evidence for this exact provider identifier.
        """

        if not isinstance(active, Mapping):
            return
        present = {(str(row.get("kind")), str(row.get("id"))) for row in sessions}
        for kind, key in (
            ("codex", "active"),
            ("claude", "claudeActive"),
            ("opencode", "opencodeActive"),
        ):
            values = active.get(key)
            if not isinstance(values, Mapping):
                continue
            for identifier in values:
                if not isinstance(identifier, str) or (kind, identifier) in present:
                    continue
                sessions.append(
                    {
                        "kind": kind,
                        "id": identifier,
                        "name": identifier[:8] if kind != "opencode" else identifier,
                        "cwd": "",
                        "recencyAt": 0,
                        "updatedAt": 0,
                        "active": True,
                        "activityState": "active",
                        "tmuxSession": None,
                    }
                )
                present.add((kind, identifier))

    def _once(self, config: Any, deadline: float | None = None) -> list[dict[str, object]]:
        assert self.mesh is not None
        deadline = deadline or self._stream_deadline
        if deadline is None:
            whole_seconds = max(
                _WHOLE_REFRESH_MIN_SECONDS,
                min(
                    _WHOLE_REFRESH_MAX_SECONDS,
                    float(self.mesh.connect_timeout * self.mesh.attempts + 12),
                ),
            )
            deadline = time.monotonic() + whole_seconds
        self._remaining(deadline)
        with self._report_error_lock:
            self._report_errors = []
        stages: dict[str, tuple[str | None, object, object, object, object]] = {}

        def host_stage(
            host: MeshHost,
        ) -> tuple[str, tuple[str | None, object, object, object, object]]:
            route, active = self._active(host, deadline)
            if isinstance(active, StaleMeshError):
                raise active
            if not host.local and route is None:
                unavailable = (
                    active
                    if isinstance(active, Exception)
                    else ContractError("host is unreachable")
                )
                return host.host_id, (route, unavailable, unavailable, unavailable, active)
            codex, claude, opencode = self._provider_results(host, config, deadline)
            for stage in (codex, claude, opencode):
                if isinstance(stage, StaleMeshError):
                    raise stage
            return host.host_id, (route, codex, claude, opencode, active)

        # All host and provider workers finish before this function can return.
        # Each external child receives the shared remaining deadline, so joining
        # cannot leave post-return reporting or subprocess work behind.
        with ThreadPoolExecutor(
            max_workers=min(_MAX_HOST_WORKERS, len(self.mesh.hosts)),
            thread_name_prefix="rofi-agent-host",
        ) as pool:
            futures = {pool.submit(host_stage, host): host.host_id for host in self.mesh.hosts}
            try:
                for future in as_completed(futures, timeout=self._remaining(deadline)):
                    host_id, stage = future.result()
                    stages[host_id] = stage
            except FuturesTimeoutError as error:
                for future in futures:
                    future.cancel()
                raise ContractError("contract refresh timed out") from error
        try:
            inventory_command = self._run(
                _inventory_args(self.tmux_command, self.mesh),
                timeout=min(15.0, self._remaining(deadline)),
            )
            if inventory_command.returncode != 0:
                _raise_command_failure(inventory_command, "Tmux Session inventory")
            inventory_payload = _json(inventory_command, "Tmux Session inventory")
            tmux = _inventory(inventory_payload, self.mesh)
            tmux_error: ContractError | None = None
        except StaleMeshError:
            raise
        except ContractError as error:
            tmux = {}
            tmux_error = error
        events: list[dict[str, object]] = [
            {
                "event": "refresh-started",
                "hosts": [host.host_id for host in self.mesh.hosts],
                "backend": self.identity,
            }
        ]
        for host in self.mesh.hosts:
            route, codex, claude, opencode, active = stages[host.host_id]
            target = engine.HostTarget(None if host.local else route, host.host_id)
            merged = engine.merge_host_results(
                [(target, codex, claude, opencode, active)], config.max_sessions
            )
            self._append_active_only_rows(merged["sessions"], active)
            errors = list(merged["errors"])
            with self._report_error_lock:
                errors.extend(
                    error for error in self._report_errors if error["host"] == host.host_id
                )
            tmux_row = tmux.get(host.host_id)
            if tmux_error is not None:
                errors.append(
                    {"host": host.host_id, "stage": "tmux", "message": _bounded_text(tmux_error)}
                )
            elif not isinstance(tmux_row, Mapping):
                errors.append(
                    {
                        "host": host.host_id,
                        "stage": "tmux",
                        "message": "missing",
                    }
                )
            elif tmux_row.get("status") in {"unreachable", "error"}:
                errors.append(
                    {
                        "host": host.host_id,
                        "stage": "tmux",
                        "message": _bounded_text(tmux_row.get("status")),
                    }
                )
            elif tmux_row.get("status") == "tmux_missing":
                # Reached capability truth is authoritative: it clears an old
                # reference rather than making it look currently attachable.
                errors.append(
                    {
                        "host": host.host_id,
                        "stage": "tmux-missing",
                        "message": "tmux_missing",
                    }
                )
            for row in merged["sessions"]:
                row["contractMode"] = True
                row["backend"] = dict(self.identity)
                row["host"] = host.display
                row["hostId"] = host.host_id
                row["windowHost"] = (
                    active.get("nativeHostname", host.host_id)
                    if isinstance(active, Mapping)
                    else host.host_id
                )
                row["connectHost"] = route or "local"
                activity = (
                    active.get(
                        {"codex": "active", "claude": "claudeActive", "opencode": "opencodeActive"}[
                            row["kind"]
                        ],
                        {},
                    )
                    if isinstance(active, Mapping)
                    else {}
                )
                info = activity.get(row["id"]) if isinstance(activity, Mapping) else None
                association: dict[str, object] | None = None
                ambiguity: str | None = None
                if isinstance(tmux_row, Mapping) and tmux_row.get("status") == "ok":
                    try:
                        association, ambiguity = _tmux_association(
                            tmux_row,
                            str(row["kind"]),
                            str(row["id"]),
                            info if isinstance(info, Mapping) else {},
                            self.mesh.revision,
                        )
                    except ContractError as error:
                        errors.append(
                            {"host": host.host_id, "stage": "tmux", "message": _bounded_text(error)}
                        )
                if association is not None:
                    row["tmux"] = association
                    row["tmuxSession"] = association["observedName"]
                    if association["pending"] and not row.get("active"):
                        row["activityState"] = "waiting"
                elif ambiguity:
                    row["tmuxAmbiguous"] = True
                    errors.append(
                        {"host": host.host_id, "stage": "tmux-correlation", "message": ambiguity}
                    )
            events.append(
                {
                    "event": "host-complete",
                    "host": host.host_id,
                    "sessions": merged["sessions"],
                    "errors": errors,
                    "backend": self.identity,
                }
            )
        events.append(
            {"event": "refresh-finished", "generatedAt": int(time.time()), "backend": self.identity}
        )
        return events

    def stream(
        self,
        config: Any,
        *,
        deadline: float | None = None,
    ) -> Sequence[dict[str, object]]:
        if deadline is not None:
            self._stream_deadline = deadline
        elif self._stream_deadline is None:
            self._stream_deadline = time.monotonic() + _WHOLE_REFRESH_MAX_SECONDS
        try:
            for attempt in range(2):
                try:
                    return self._once(config, self._stream_deadline)
                except StaleMeshError:
                    if attempt:
                        raise
                    self.prepare()
            raise AssertionError("unreachable")
        finally:
            self._stream_deadline = None


def select_backend(
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Runner = _run_bounded,
) -> LegacyBackend | ContractBackend:
    """Choose the pair atomically: one external command is never enough."""

    ssh_command = which("rofi-ssh-plus")
    tmux_command = which("rofi-tmux-plus")
    if ssh_command is None or tmux_command is None:
        return LegacyBackend()
    return ContractBackend(ssh_command, tmux_command, runner=runner, which=which)
