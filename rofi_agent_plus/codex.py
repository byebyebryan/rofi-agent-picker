"""Codex app-server transport for Agent Plus."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Any, Self


class AppServerClient:
    """Small JSON-RPC client for one Codex app-server process."""

    def __init__(
        self,
        command: Sequence[str],
        timeout: float,
        version: str,
        error: Callable[[str], Exception],
        *,
        stdout_limit: int | None = None,
        stderr_limit: int | None = None,
        reached_marker: bytes | None = None,
    ) -> None:
        self.timeout = timeout
        self.version = version
        self._error = error
        self._next_id = 1
        self._buffer = b""
        self._stdout_seen = 0
        self._stderr = bytearray()
        self._stdout_limit = stdout_limit
        self._stderr_limit = stderr_limit
        self._reached_marker = reached_marker
        self._marker_verified = reached_marker is None
        self._marker_diagnostic = ""
        self.process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise self._error("failed to open Codex app-server pipes")

        self._selector = selectors.DefaultSelector()
        self._selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
        if self.process.stderr is not None:
            self._selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")

    def initialize(self) -> None:
        self.call(
            "initialize",
            {
                "clientInfo": {
                    "name": "rofi-agent-plus",
                    "title": "Rofi Agent Plus",
                    "version": self.version,
                }
            },
        )
        self.notify("initialized", {})

    def call(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        return self._read_response(request_id)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def _send(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise self._error("Codex app-server stdin is closed")
        payload = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except BrokenPipeError as exc:
            raise self._error(self._process_error("Codex app-server exited")) from exc

    def _read_response(self, request_id: int) -> Any:
        deadline = time.monotonic() + self.timeout
        while True:
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    detail = message["error"]
                    if isinstance(detail, dict):
                        detail = detail.get("message", json.dumps(detail))
                    raise self._error(f"Codex app-server error: {detail}")
                return message.get("result")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._error(self._process_error("Codex app-server timed out"))

            events = self._selector.select(remaining)
            if not events:
                raise self._error(self._process_error("Codex app-server timed out"))

            for key, _ in events:
                closed_stdout = self._drain_event(key)
                if closed_stdout:
                    raise self._error(self._process_error("Codex app-server closed stdout"))

    def _process_error(self, prefix: str) -> str:
        stderr = self._stderr.decode(errors="replace").strip()
        return f"{prefix}: {stderr}" if stderr else prefix

    def marker_result(self) -> tuple[bool, str]:
        """Return an exact reached-marker verdict without trusting text alike.

        The marker is emitted before the remote ``codex app-server`` exec;
        callers inspect this immediately after ``initialize``.  Legacy
        clients pass no marker and retain their historical behavior.
        """

        if self._reached_marker is None or self._marker_verified:
            return True, self._marker_diagnostic or self._stderr.decode("utf-8", "replace")
        count = bytes(self._stderr).count(self._reached_marker)
        diagnostic = bytes(self._stderr).replace(self._reached_marker, b"", 1)
        return count == 1, diagnostic.decode("utf-8", "replace")

    def _drain_event(self, key: selectors.SelectorKey) -> bool:
        """Drain one ready pipe, returning whether stdout reached EOF."""

        stream = key.fileobj
        file_descriptor = stream if isinstance(stream, int) else stream.fileno()
        chunk = os.read(file_descriptor, 65536)
        if not chunk:
            try:
                self._selector.unregister(stream)
            except KeyError:
                pass
            return key.data == "stdout"
        if key.data == "stdout":
            self._buffer += chunk
            self._stdout_seen += len(chunk)
            if self._stdout_limit is not None and self._stdout_seen > self._stdout_limit:
                raise self._error("Codex app-server exceeded stdout limit")
        else:
            self._stderr.extend(chunk)
            if self._stderr_limit is not None and len(self._stderr) > self._stderr_limit:
                raise self._error("Codex app-server exceeded stderr limit")
        return False

    def wait_for_marker(self, timeout: float) -> tuple[bool, str]:
        """Boundedly await exactly one reached marker before any JSON-RPC.

        stdout is drained into the regular bounded JSON-RPC buffer while the
        marker arrives on stderr.  This handles the normal scheduler race in
        which SSH makes stdout readable before forwarding the marker pipe.
        """

        if self._reached_marker is None:
            return True, ""
        deadline = time.monotonic() + timeout
        while True:
            marker = self._reached_marker
            count = bytes(self._stderr).count(marker)
            if count == 1:
                diagnostic = bytes(self._stderr).replace(marker, b"", 1)
                self._marker_verified = True
                self._marker_diagnostic = diagnostic.decode("utf-8", "replace")
                return True, self._marker_diagnostic
            if count > 1:
                return False, bytes(self._stderr).decode("utf-8", "replace")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, bytes(self._stderr).decode("utf-8", "replace")
            events = self._selector.select(remaining)
            if not events:
                return False, bytes(self._stderr).decode("utf-8", "replace")
            for key, _ in events:
                self._drain_event(key)

    def close(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.wait(timeout=0.5)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            self.process.terminate()
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=0.5)
        finally:
            self._selector.close()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except BrokenPipeError:
                        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
