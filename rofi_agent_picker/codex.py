"""Codex app-server transport for the Agent Picker."""

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
    ) -> None:
        self.timeout = timeout
        self.version = version
        self._error = error
        self._next_id = 1
        self._buffer = b""
        self._stderr = bytearray()
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
                    "name": "rofi-agent-picker",
                    "title": "Rofi Agent Picker",
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
                stream = key.fileobj
                file_descriptor = stream if isinstance(stream, int) else stream.fileno()
                chunk = os.read(file_descriptor, 65536)
                if not chunk:
                    try:
                        self._selector.unregister(stream)
                    except KeyError:
                        pass
                    if key.data == "stdout":
                        raise self._error(self._process_error("Codex app-server closed stdout"))
                    continue
                if key.data == "stdout":
                    self._buffer += chunk
                else:
                    self._stderr.extend(chunk)

    def _process_error(self, prefix: str) -> str:
        stderr = self._stderr.decode(errors="replace").strip()
        return f"{prefix}: {stderr}" if stderr else prefix

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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
