"""Rückweg VM → Host über die Transfer-VHDX (Spezifikation §8).

Die Transfer-VHDX hat kein Dateisystem. Der Worker schreibt genau einen Container ab Sektor 0
(Format identisch zu ``LocalAi.Sandbox.TransferContainer``), flusht und meldet die Fertigstellung über
das Hyper-V-KVP-Register. Der Host hängt das Laufwerk danach ab, liest es ohne Einbinden und ersetzt es
durch ein leeres.

Ein Laufwerk wird nur beschrieben, wenn seine ersten 4096 Bytes leer sind (frisch vom Host) und seine
Größe genau stimmt – so kann der Worker nie das Systemlaufwerk überschreiben und jedes Laufwerk nur
einmal benutzen.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import struct
import sys
import time
from dataclasses import dataclass
from enum import IntEnum

HEADER_SIZE = 4096
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
MAGIC = b"LAIXFER1"
KVP_KEY = r"SOFTWARE\Microsoft\Virtual Machine\Auto"
KVP_VALUE = "LocalAiSignal"


class TransferKind(IntEnum):
    RESULT = 1
    REJECTED = 2
    REPORT = 3


class TransferError(Exception):
    """Transfer-Laufwerk nicht nutzbar."""


def build_container(kind: TransferKind, sequence: int, payload: bytes) -> bytes:
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise TransferError(f"Nutzlast zu groß ({len(payload)} Bytes)")
    padded = -(-len(payload) // HEADER_SIZE) * HEADER_SIZE
    header = MAGIC + struct.pack("<IIQQ", 1, int(kind), sequence, len(payload)) + hashlib.sha256(payload).digest()
    return header.ljust(HEADER_SIZE, b"\0") + payload.ljust(padded, b"\0")


def parse_container(image: bytes) -> tuple[TransferKind, int, bytes] | None:
    """Gegenstück für Tests (im Betrieb liest nur der Host)."""
    if not any(image[:8]):
        return None
    if image[:8] != MAGIC:
        raise TransferError("kein Container")
    version, kind, sequence, length = struct.unpack_from("<IIQQ", image, 8)
    if version != 1 or kind not in (1, 2, 3) or length > MAX_PAYLOAD_BYTES:
        raise TransferError("Kopf ungültig")
    payload = image[HEADER_SIZE:HEADER_SIZE + length]
    if len(payload) != length or hashlib.sha256(payload).digest() != image[32:64]:
        raise TransferError("Prüfsumme stimmt nicht")
    return TransferKind(kind), sequence, payload


def _device_size(handle_file) -> int:  # pragma: no cover - nur Windows
    import ctypes
    import msvcrt
    from ctypes import wintypes

    ioctl_disk_get_length_info = 0x0007405C
    handle = msvcrt.get_osfhandle(handle_file.fileno())
    size = ctypes.c_longlong(0)
    returned = wintypes.DWORD(0)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ok = kernel32.DeviceIoControl(
        wintypes.HANDLE(handle), ioctl_disk_get_length_info, None, 0,
        ctypes.byref(size), ctypes.sizeof(size), ctypes.byref(returned), None)
    if not ok:
        raise OSError(ctypes.get_last_error(), "IOCTL_DISK_GET_LENGTH_INFO fehlgeschlagen")
    return size.value


def _size_of(path: str, f) -> int:
    if path.startswith("\\\\.\\"):
        return _device_size(f)
    return os.fstat(f.fileno()).st_size


@dataclass
class TransferDisk:
    """``path``: Gerät (``\\\\.\\PhysicalDriveN``) oder – in Tests – eine Datei; ``"auto"`` sucht das Laufwerk."""

    path: str
    expected_size: int

    def resolve(self, attempts: int = 40, delay: float = 0.5) -> str:
        if self.path != "auto":
            return self.path
        # Nach dem Austausch durch den Host taucht das neue Laufwerk erst nach kurzer Zeit im Gast auf.
        for attempt in range(attempts):
            try:
                return find_transfer_disk(self.expected_size)
            except TransferError:
                if attempt == attempts - 1:
                    raise
                time.sleep(delay)
        raise TransferError("Transfer-Laufwerk nicht gefunden")  # pragma: no cover

    def write(self, kind: TransferKind, sequence: int, payload: bytes) -> None:
        image = build_container(kind, sequence, payload)
        path = self.resolve()
        with open(path, "r+b", buffering=0) as f:
            size = _size_of(path, f)
            if size != self.expected_size:
                raise TransferError(f"Transfer-Laufwerk hat {size} statt {self.expected_size} Bytes")
            if len(image) > size:
                raise TransferError("Container passt nicht auf das Transfer-Laufwerk")
            f.seek(0)
            first = f.read(HEADER_SIZE)
            if any(first):
                raise TransferError("Transfer-Laufwerk ist nicht leer – wartet auf Austausch durch den Host")
            # Nutzlast zuerst, Kopf zuletzt: ein halb geschriebener Container hat keinen gültigen Kopf.
            f.seek(HEADER_SIZE)
            f.write(image[HEADER_SIZE:])
            os.fsync(f.fileno())
            f.seek(0)
            f.write(image[:HEADER_SIZE])
            os.fsync(f.fileno())


def find_transfer_disk(expected_size: int, max_disks: int = 32) -> str:  # pragma: no cover - nur Windows
    candidates = []
    for n in range(max_disks):
        path = f"\\\\.\\PhysicalDrive{n}"
        try:
            with open(path, "rb", buffering=0) as f:
                if _device_size(f) != expected_size:
                    continue
                if not any(f.read(HEADER_SIZE)):
                    candidates.append(path)
        except OSError:
            continue
    if len(candidates) != 1:
        raise TransferError(f"Transfer-Laufwerk nicht eindeutig gefunden ({len(candidates)} Kandidaten)")
    return candidates[0]


class Signal:
    """Meldet dem Host „fertig“: in der VM über KVP, in Tests über eine Datei."""

    def __init__(self, signal_file: pathlib.Path | None) -> None:
        self.signal_file = signal_file

    def send(self, value: str) -> None:
        if self.signal_file is not None:
            tmp = self.signal_file.with_name(self.signal_file.name + ".tmp")
            tmp.write_text(value, encoding="ascii")
            tmp.replace(self.signal_file)
            return
        if sys.platform != "win32":  # pragma: no cover
            raise TransferError("KVP-Signal gibt es nur in einer Windows-VM")
        import winreg  # pragma: no cover

        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, KVP_KEY, 0, winreg.KEY_SET_VALUE) as key:  # pragma: no cover
            winreg.SetValueEx(key, KVP_VALUE, 0, winreg.REG_SZ, value)
