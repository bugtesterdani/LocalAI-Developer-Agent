"""Hyper-V-Datenaustausch (KVP) im Linux-Gast (M8).

Der Dienst ``hv_kvp_daemon`` (Paket hyperv-daemons) spiegelt die Pool-Dateien ``/var/lib/hyperv/.kvp_pool_<n>``
mit dem Host. Pool 1 („Guest“) liest der Host als ``GuestExchangeItems`` – dasselbe, was unter Windows der
Registrierungsschlüssel ``HKLM\\SOFTWARE\\Microsoft\\Virtual Machine\\Guest`` ist. Jeder Eintrag ist ein Datensatz
fester Länge: 512 Bytes Schlüssel und 2048 Bytes Wert, jeweils mit Nullen aufgefüllt (UTF-8). Der Dienst sperrt die
Datei mit ``fcntl``-Sperren; dieselbe Sperre wird hier beim Schreiben gehalten.
"""

from __future__ import annotations

import os
import pathlib

KEY_SIZE = 512
VALUE_SIZE = 2048
RECORD_SIZE = KEY_SIZE + VALUE_SIZE
GUEST_POOL = pathlib.Path("/var/lib/hyperv/.kvp_pool_1")
MAX_RECORDS = 1024


class KvpError(Exception):
    pass


def _field(text: str, size: int) -> bytes:
    data = text.encode("utf-8")
    if len(data) >= size or b"\0" in data:
        raise KvpError("Schlüssel oder Wert zu lang bzw. ungültig")
    return data.ljust(size, b"\0")


def read_pool(path: pathlib.Path = GUEST_POOL) -> dict[str, str]:
    if not path.exists():
        return {}
    data = path.read_bytes()
    result: dict[str, str] = {}
    for offset in range(0, len(data) - len(data) % RECORD_SIZE, RECORD_SIZE):
        key = data[offset:offset + KEY_SIZE].split(b"\0", 1)[0].decode("utf-8", errors="replace")
        value = data[offset + KEY_SIZE:offset + RECORD_SIZE].split(b"\0", 1)[0].decode("utf-8", errors="replace")
        if key:
            result[key] = value
    return result


def write_value(key: str, value: str, path: pathlib.Path = GUEST_POOL) -> None:
    """Setzt bzw. ersetzt ``key`` im Gast-Pool (unter der Sperre des KVP-Dienstes)."""
    import fcntl

    record = _field(key, KEY_SIZE) + _field(value, VALUE_SIZE)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX)
        try:
            size = os.fstat(fd).st_size
            data = os.pread(fd, size, 0) if size else b""
            records = [data[i:i + RECORD_SIZE] for i in range(0, len(data) - len(data) % RECORD_SIZE, RECORD_SIZE)]
            wanted = record[:KEY_SIZE]
            for index, existing in enumerate(records):
                if existing[:KEY_SIZE] == wanted:
                    os.pwrite(fd, record, index * RECORD_SIZE)
                    break
            else:
                if len(records) >= MAX_RECORDS:
                    raise KvpError("KVP-Pool ist voll")
                os.pwrite(fd, record, len(records) * RECORD_SIZE)
                os.ftruncate(fd, (len(records) + 1) * RECORD_SIZE)
            os.fsync(fd)
        finally:
            fcntl.lockf(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
