"""Eingangs-Laufwerk (``inbound: Disk``) – Jobs ohne Copy-VMFile in die VM bringen.

Auf manchen Rechnern ist die Gastdienstschnittstelle (Copy-VMFile) gesperrt, das Anhängen virtueller Festplatten an
die VM aber erlaubt. Dann schreibt der Broker die Dateien eines Jobs als tar in eine neue VHDX (ohne sie auf dem Host
einzubinden) und hängt sie an die VM. Der Worker findet das Laufwerk am Kopf, prüft Prüfsumme und Sequenz und legt
die Dateien in seinen Eingang – die ``.ready``-Datei zuletzt. Aufbau (C#: ``LocalAi.Sandbox.InboundDisk``)::

    0     8  b"LAIINBX1"
    8     4  Version (1, little endian)
    16    8  Sequenz
    24    8  Länge des tar
    32   32  SHA-256 des tar
    4096 …   tar (nur Dateien, flache Namen)
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import pathlib
import re
import struct
import sys
import tarfile
from collections.abc import Callable, Iterable

MAGIC = b"LAIINBX1"
HEADER_SIZE = 4096
SECTOR = 4096
MAX_TAR = 64 * 1024 ** 3
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class InboundError(Exception):
    pass


def parse_header(header: bytes) -> tuple[int, int, bytes] | None:
    """(Sequenz, tar-Länge, SHA-256) oder None, wenn das Laufwerk kein Eingangs-Laufwerk ist."""
    if len(header) < 64 or header[:8] != MAGIC:
        return None
    version, = struct.unpack_from("<I", header, 8)
    sequence, length = struct.unpack_from("<QQ", header, 16)
    if version != 1:
        raise InboundError(f"unbekannte Version {version}")
    if length > MAX_TAR:
        raise InboundError("Inhalt zu groß")
    return sequence, length, header[32:64]


def _read(handle, offset: int, length: int) -> bytes:
    """Liest sektorweise ausgerichtet am Seitencache vorbei: unter Linux mit O_DIRECT (Gerätenamen werden nach dem
    Abhängen wiederverwendet, alte Seiten dürfen nicht gelesen werden), unter Windows roh von \\\\.\\PhysicalDriveN."""
    start = offset - offset % SECTOR
    end = -(-(offset + length) // SECTOR) * SECTOR
    data = bytearray()
    if isinstance(handle, int):  # Linux: Dateideskriptor mit O_DIRECT
        import mmap
        buffer = mmap.mmap(-1, 1 << 20)
        try:
            position = start
            while position < end:
                size = min(1 << 20, end - position)
                with memoryview(buffer) as view, view[:size] as part:
                    read = os.preadv(handle, [part], position)
                if read <= 0:
                    break
                data += buffer[:read]
                position += read
        finally:
            buffer.close()
    else:
        handle.seek(start)
        while len(data) < end - start:
            chunk = handle.read(min(1 << 20, end - start - len(data)))
            if not chunk:
                break
            data += chunk
    return bytes(data[offset - start:offset - start + length])


class _Disk:
    """Öffnet ein Laufwerk nur lesend und ohne Seitencache."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.handle = None

    def __enter__(self):
        if sys.platform.startswith("linux"):
            try:
                self.handle = os.open(self.path, os.O_RDONLY | getattr(os, "O_DIRECT", 0))
            except OSError as err:
                if err.errno != errno.EINVAL:  # Dateisysteme ohne O_DIRECT (Tests mit Dateien)
                    raise
                self.handle = os.open(self.path, os.O_RDONLY)
        else:
            self.handle = open(self.path, "rb", buffering=0)
        return self.handle

    def __exit__(self, *exc) -> None:
        if isinstance(self.handle, int):
            os.close(self.handle)
        elif self.handle is not None:
            self.handle.close()


def _mounted_disks(mounts: pathlib.Path = pathlib.Path("/proc/mounts")) -> set[str]:
    """Namen der Laufwerke, von denen etwas eingebunden ist (Systemplatte) – die werden nie abgefragt."""
    names = set()
    try:
        for line in mounts.read_text().splitlines():
            device = line.split()[0]
            if device.startswith("/dev/"):
                names.add(re.sub(r"p?\d+$", "", device[5:]) if re.match(r"(sd|vd)[a-z]+\d+$", device[5:]) else device[5:])
    except OSError:
        pass
    return names


def linux_disks(sys_block: pathlib.Path = pathlib.Path("/sys/block"), dev: pathlib.Path = pathlib.Path("/dev"),
                mounts: pathlib.Path = pathlib.Path("/proc/mounts")) -> list[str]:
    mounted = _mounted_disks(mounts)
    return [str(dev / p.name) for p in sorted([*sys_block.glob("sd*"), *sys_block.glob("vd*")]) if p.name not in mounted]


def windows_disks(max_disks: int = 32) -> list[str]:  # pragma: no cover - nur Windows
    return [f"\\\\.\\PhysicalDrive{n}" for n in range(max_disks)]


def default_disks() -> list[str]:
    return windows_disks() if sys.platform == "win32" else linux_disks()


class InboundDisks:
    """Übernimmt neue Eingangs-Laufwerke in den Eingang; die zuletzt übernommene Sequenz bleibt in ``state_file``."""

    def __init__(self, state_file: pathlib.Path, disks: Callable[[], Iterable[str]] = default_disks) -> None:
        self.state_file = state_file
        self.disks = disks
        try:
            self.last = int(json.loads(state_file.read_text(encoding="utf-8")).get("lastSequence", -1))
        except (OSError, ValueError):
            self.last = -1

    def poll(self, inbox: pathlib.Path) -> list[str]:
        """Sucht neue Eingangs-Laufwerke; Rückgabe: übernommene Dateinamen."""
        taken: list[str] = []
        for path in self.disks():
            try:
                with _Disk(path) as handle:
                    parsed = parse_header(_read(handle, 0, HEADER_SIZE))
                    if parsed is None or parsed[0] <= self.last:
                        continue
                    sequence, length, digest = parsed
                    data = _read(handle, HEADER_SIZE, length)
            except (OSError, InboundError) as err:
                if isinstance(err, InboundError):
                    print(f"[Worker] Eingangs-Laufwerk {path} verworfen: {err}", flush=True)
                continue
            if len(data) != length or hashlib.sha256(data).digest() != digest:
                print(f"[Worker] Eingangs-Laufwerk {path}: Prüfsumme stimmt nicht – verworfen", flush=True)
                continue
            try:
                taken += self._extract(data, inbox)
            except (InboundError, tarfile.TarError, OSError) as err:
                print(f"[Worker] Eingangs-Laufwerk {path}: {err}", flush=True)
                continue
            self.last = sequence
            self.state_file.write_text(json.dumps({"lastSequence": sequence}), encoding="utf-8")
            print(f"[Worker] Eingangs-Laufwerk Sequenz {sequence}: {len(taken)} Datei(en) übernommen", flush=True)
        return taken

    @staticmethod
    def _extract(data: bytes, inbox: pathlib.Path) -> list[str]:
        inbox.mkdir(parents=True, exist_ok=True)
        staged: list[tuple[pathlib.Path, pathlib.Path]] = []
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            for member in tar.getmembers():
                if not member.isfile() or not SAFE_NAME.match(member.name):
                    raise InboundError(f"unzulässiger Eintrag: {member.name!r}")
                source = tar.extractfile(member)
                assert source is not None
                part = inbox / f".{member.name}.part"
                with open(part, "wb") as out:
                    out.write(source.read())
                staged.append((part, inbox / member.name))
        # .ready zuletzt: erst dann sieht der Worker den Job als vollständig an.
        staged.sort(key=lambda s: s[1].name.endswith(".ready"))
        for part, target in staged:
            os.replace(part, target)
        return [t.name for _, t in staged]
