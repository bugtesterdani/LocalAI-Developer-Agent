"""Named-Pipe-Client für den C#-Broker.

Rahmen: 4 Byte Länge (Little Endian) + UTF-8-JSON, identisch zu ``PipeProtocol`` in C#.
Windows: ``\\\\.\\pipe\\<name>``. Linux/macOS: .NET bildet Named Pipes auf Unix-Sockets
``<TMPDIR>/CoreFxPipe_<name>`` ab (nur für Tests ausserhalb von Windows).
"""

from __future__ import annotations

import itertools
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
from typing import Any

MAX_FRAME_BYTES = 8 * 1024 * 1024


class BrokerError(Exception):
    """Fehlerantwort des Brokers (Code in Anlehnung an HTTP)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


ERROR_PIPE_BUSY = 231


def _open_windows_pipe(path: str, timeout: float):
    """Öffnet eine Named Pipe unter Windows; wartet, solange alle Serverinstanzen belegt sind."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return open(path, "r+b", buffering=0)  # noqa: SIM115
        except OSError as err:
            busy = getattr(err, "winerror", None) == ERROR_PIPE_BUSY
            if not busy or time.monotonic() > deadline:
                raise
            time.sleep(0.02)


class _Transport:
    def __init__(self, name: str, timeout: float | None) -> None:
        if sys.platform == "win32":
            path = rf"\\.\pipe\{name}"
            self._file = _open_windows_pipe(path, timeout or 10.0)
            self._sock = None
        else:
            path = os.path.join(tempfile.gettempdir(), f"CoreFxPipe_{name}")
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.settimeout(timeout)
            self._sock.connect(path)
            self._file = None

    def send(self, data: bytes) -> None:
        if self._sock is not None:
            self._sock.sendall(data)
        else:
            assert self._file is not None
            self._file.write(data)

    def recv_exact(self, n: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < n:
            if self._sock is not None:
                chunk = self._sock.recv(n - len(chunks))
            else:
                assert self._file is not None
                chunk = self._file.read(n - len(chunks))
            if not chunk:
                raise ConnectionError("Pipe vom Broker geschlossen")
            chunks.extend(chunk)
        return bytes(chunks)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
        if self._file is not None:
            self._file.close()


class PipeClient:
    """Eine Verbindung zum Broker; Anfragen werden serialisiert (eine Anfrage gleichzeitig)."""

    def __init__(self, pipe_name: str, token: str, timeout: float | None = 60.0) -> None:
        self._token = token
        self._transport = _Transport(pipe_name, timeout)
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = next(self._ids)
        payload = json.dumps(
            {"id": request_id, "method": method, "token": self._token, "params": params or {}},
            ensure_ascii=False,
        ).encode("utf-8")
        if len(payload) > MAX_FRAME_BYTES:
            raise ValueError("Anfrage zu groß")
        with self._lock:
            self._transport.send(struct.pack("<i", len(payload)) + payload)
            (length,) = struct.unpack("<i", self._transport.recv_exact(4))
            if length < 0 or length > MAX_FRAME_BYTES:
                raise ConnectionError(f"Ungültige Rahmenlänge: {length}")
            response = json.loads(self._transport.recv_exact(length).decode("utf-8"))
        if response.get("id") != request_id:
            raise ConnectionError("Antwort passt nicht zur Anfrage")
        if not response.get("ok"):
            error = response.get("error") or {}
            raise BrokerError(int(error.get("code", 500)), str(error.get("message", "Unbekannter Fehler")))
        return response.get("result")

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> PipeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
