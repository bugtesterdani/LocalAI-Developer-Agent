"""Verbindungskanal im Linux-Gast unter QEMU (M10) – ersetzt hv_kvp_daemon und hv_fcopy aus hyperv-daemons.

Läuft als root (systemd-Dienst ``localai-link.service``) und spricht über den virtio-serial-Port
``/dev/virtio-ports/org.localai.link`` mit dem Host:

* Gast → Host, nur Zeilen fester Form: ``hello`` (Dienst gestartet), ``hb`` (Lebenszeichen alle 2 s),
  ``kvp <Schlüssel>\\t<Wert>`` (Werte aus dem KVP-Pool, den Worker und Provisionierung wie unter Hyper-V schreiben;
  bei Änderung sofort, sonst alle 5 s erneut) und ``ack <Name> ok|error <Text>``.
* Host → Gast: Dateien als ``LAIFILE1 <Name> <Größe> <SHA-256>\\n`` plus Inhalt. Sie landen ausschließlich im Eingang
  des Workers (``/var/lib/localai/inbox``), erst nach Prüfung des Hashes und per Umbenennen (wie Copy-VMFile).

Der Rückweg für Ergebnisse bleibt das Transfer-Laufwerk; über diesen Kanal gehen nur Signale.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
import pathlib
import re
import select
import sys
import time

from localai.worker import kvp

PORT = pathlib.Path("/dev/virtio-ports/org.localai.link")
INBOX = pathlib.Path("/var/lib/localai/inbox")
FORWARDED_KEYS = ("LocalAiSignal", "LocalAiProgress")
HEADER = re.compile(rb"^LAIFILE1 ([A-Za-z0-9][A-Za-z0-9._-]{0,199}) ([0-9]{1,13}) ([0-9a-f]{64})$")
MAX_FILE = 16 * 1024 * 1024 * 1024
MAX_HEADER = 512
HEARTBEAT_SECONDS = 2.0
RESEND_SECONDS = 5.0


class Link:
    """Zustand des Kanals: eingehende Datei, zuletzt gesendete KVP-Werte."""

    def __init__(self, fd: int, inbox: pathlib.Path, pool: pathlib.Path) -> None:
        self.fd = fd
        self.inbox = inbox
        self.pool = pool
        self.buffer = b""
        self.receiving: tuple[str, int, str] | None = None
        self.part: pathlib.Path | None = None
        self.handle = None
        self.hasher = None
        self.remaining = 0
        self.sent: dict[str, str] = {}
        self.last_resend = 0.0

    # ------------------------------------------------------------------ senden

    def send(self, line: str, important: bool = False) -> None:
        """Eine Zeile an den Host; ohne verbundenen Host werden unwichtige Zeilen verworfen."""
        data = (line.replace("\n", " ") + "\n").encode("utf-8")
        deadline = time.monotonic() + (10 if important else 0.2)
        while data:
            try:
                written = os.write(self.fd, data)
                data = data[written:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    return
                select.select([], [self.fd], [], 0.2)
            except OSError as err:
                if err.errno in (errno.EAGAIN, errno.EIO, errno.ENODEV, errno.EPIPE):
                    return
                raise

    def forward_kvp(self, force: bool = False) -> None:
        try:
            values = kvp.read_pool(self.pool)
        except OSError:
            return
        now = time.monotonic()
        resend = force or now - self.last_resend >= RESEND_SECONDS
        if resend:
            self.last_resend = now
        for key in FORWARDED_KEYS:
            value = values.get(key)
            if value is None:
                continue
            if resend or self.sent.get(key) != value:
                self.send(f"kvp {key}\t{value}")
                self.sent[key] = value

    # ------------------------------------------------------------------ empfangen

    def feed(self, data: bytes) -> None:
        self.buffer += data
        while self.buffer:
            if self.receiving is None:
                newline = self.buffer.find(b"\n")
                if newline < 0:
                    if len(self.buffer) > MAX_HEADER:
                        self.buffer = b""  # kein gültiger Kopf: verwerfen und neu aufsetzen
                    return
                line, self.buffer = self.buffer[:newline], self.buffer[newline + 1:]
                match = HEADER.match(line.rstrip(b"\r"))
                if match is None:
                    continue  # Rest einer abgebrochenen Übertragung
                self.begin(match.group(1).decode("ascii"), int(match.group(2)), match.group(3).decode("ascii"))
                continue
            take = min(self.remaining, len(self.buffer))
            if take:
                chunk, self.buffer = self.buffer[:take], self.buffer[take:]
                if self.handle is not None:
                    self.handle.write(chunk)
                    self.hasher.update(chunk)
                self.remaining -= take
            if self.remaining == 0:
                self.finish()
            else:
                return

    def begin(self, name: str, size: int, sha: str) -> None:
        self.receiving = (name, size, sha)
        self.remaining = size
        self.hasher = hashlib.sha256()
        self.part = self.inbox / f".{name}.part"
        self.handle = None
        if size > MAX_FILE:
            return
        try:
            self.inbox.mkdir(parents=True, exist_ok=True)
            self.handle = open(self.part, "wb")  # noqa: SIM115 - bleibt über mehrere Lesevorgänge offen
            os.chmod(self.part, 0o600)
        except OSError:
            self.handle = None
        if size == 0:
            self.finish()

    def finish(self) -> None:
        name, size, sha = self.receiving  # type: ignore[misc]
        error = None
        try:
            if size > MAX_FILE:
                error = "zu groß"
            elif self.handle is None:
                error = "nicht schreibbar"
            else:
                self.handle.flush()
                os.fsync(self.handle.fileno())
                self.handle.close()
                self.handle = None
                if self.hasher.hexdigest() != sha:
                    error = "Hash stimmt nicht"
                else:
                    os.replace(self.part, self.inbox / name)
        except OSError as err:
            error = str(err)[:150]
        finally:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            if error is not None and self.part is not None:
                self.part.unlink(missing_ok=True)
            self.receiving = None
        self.send(f"ack {name} ok" if error is None else f"ack {name} error {error}", important=True)

    def reset(self) -> None:
        """Host getrennt: halbe Datei verwerfen."""
        if self.handle is not None:
            self.handle.close()
            self.handle = None
        if self.part is not None and self.receiving is not None:
            self.part.unlink(missing_ok=True)
        self.receiving = None
        self.buffer = b""


def open_port(path: pathlib.Path, wait: float = 300.0) -> int:
    deadline = time.monotonic() + wait
    while True:
        try:
            return os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def run(port: pathlib.Path = PORT, inbox: pathlib.Path = INBOX, pool: pathlib.Path = kvp.GUEST_POOL,
        stop_after: float | None = None) -> int:
    fd = open_port(port)
    link = Link(fd, inbox, pool)
    link.send("hello", important=True)
    link.forward_kvp(force=True)
    last_heartbeat = 0.0
    started = time.monotonic()
    try:
        while stop_after is None or time.monotonic() - started < stop_after:
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                link.send("hb")
                last_heartbeat = now
            link.forward_kvp()
            readable, _, _ = select.select([fd], [], [], 0.25)
            if not readable:
                continue
            try:
                data = os.read(fd, 1 << 16)
            except BlockingIOError:
                continue
            except OSError as err:
                if err.errno in (errno.EIO, errno.EPIPE):
                    link.reset()
                    time.sleep(0.5)
                    continue
                raise
            if not data:
                # Host nicht verbunden (POLLHUP): kurz warten, Teilübertragung verwerfen.
                link.reset()
                time.sleep(0.5)
                continue
            link.feed(data)
    finally:
        os.close(fd)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LocalAi: Verbindungskanal zum Host (QEMU)")
    parser.add_argument("--port", type=pathlib.Path, default=PORT)
    parser.add_argument("--inbox", type=pathlib.Path, default=INBOX)
    parser.add_argument("--kvp-pool", type=pathlib.Path, default=kvp.GUEST_POOL)
    args = parser.parse_args(argv)
    return run(args.port, args.inbox, args.kvp_pool)


if __name__ == "__main__":
    sys.exit(main())
