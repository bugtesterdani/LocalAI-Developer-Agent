"""Hardwareerkennung ohne feste Annahmen zu CPU oder GPU (R15).

GPU-Speicher über ``nvidia-smi`` (falls vorhanden, feste Abfrage ohne Modelleingaben), Arbeitsspeicher über
das Betriebssystem. Fehlt eine Quelle, bleibt der Wert unbekannt – die Kalibrierung entscheidet dann,
ob ein Profil freigegeben werden darf.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field

GIB = 1024 ** 3
MIB = 1024 ** 2


@dataclass
class Gpu:
    name: str
    total_bytes: int
    used_bytes: int
    driver: str = ""


@dataclass
class Hardware:
    cpu_count: int
    ram_total_bytes: int | None
    ram_available_bytes: int | None
    gpus: list[Gpu] = field(default_factory=list)
    gpu_source: str = "keine"
    remote_host: str | None = None

    @property
    def vram_total_bytes(self) -> int | None:
        return sum(g.total_bytes for g in self.gpus) if self.gpus else None

    def fingerprint(self) -> str:
        """Ändert sich, wenn GPU oder RAM-Ausstattung wechseln → Profile neu kalibrieren (AC14)."""
        if self.remote_host:
            # Ollama läuft auf einem anderen Rechner: dessen Hardware ist von hier nicht messbar.
            return hashlib.sha256(f"remote={self.remote_host}".encode()).hexdigest()[:16]
        ram_gib = round((self.ram_total_bytes or 0) / GIB)
        parts = [f"cpu={self.cpu_count}", f"ram={ram_gib}"] + [f"gpu={g.name}/{g.total_bytes // MIB}" for g in self.gpus]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    def describe(self) -> str:
        if self.remote_host:
            return f"Ollama auf {self.remote_host} (anderer Rechner – GPU/RAM dort nicht messbar)"
        ram = f"{self.ram_total_bytes / GIB:.1f} GiB RAM" if self.ram_total_bytes else "RAM unbekannt"
        if self.gpus:
            gpu = ", ".join(f"{g.name} ({g.total_bytes / GIB:.1f} GiB, belegt {g.used_bytes / GIB:.1f} GiB)" for g in self.gpus)
        else:
            gpu = "GPU-Speicher unbekannt (kein nvidia-smi)"
        return f"{self.cpu_count} logische CPUs, {ram}, {gpu}"

    def to_json(self) -> dict:
        data = asdict(self)
        data["fingerprint"] = self.fingerprint()
        return data


def _memory() -> tuple[int | None, int | None]:
    if sys.platform == "win32":  # pragma: no cover - nur Windows
        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys), int(status.ullAvailPhys)
        return None, None
    try:
        values: dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                key, rest = line.split(":", 1)
                values[key] = int(rest.split()[0]) * 1024
        return values.get("MemTotal"), values.get("MemAvailable")
    except (OSError, ValueError):
        return None, None


def _nvidia_smi() -> str | None:
    exe = shutil.which("nvidia-smi")
    if exe is None and sys.platform == "win32":  # pragma: no cover
        candidate = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
        exe = candidate if os.path.exists(candidate) else None
    return exe


def query_gpus(timeout: float = 5.0) -> list[Gpu] | None:
    exe = _nvidia_smi()
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,memory.used,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=timeout, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            try:
                gpus.append(Gpu(parts[0], int(float(parts[1])) * MIB, int(float(parts[2])) * MIB,
                                parts[3] if len(parts) > 3 else ""))
            except ValueError:
                continue
    return gpus


def detect(ollama_url: str | None = None) -> Hardware:
    """Hardware des Rechners, auf dem Ollama läuft. Bei entferntem Ollama wird hier nichts gemessen."""
    from localai.orchestrator.gateway import is_loopback_url

    if ollama_url and not is_loopback_url(ollama_url):
        import urllib.parse

        return Hardware(cpu_count=0, ram_total_bytes=None, ram_available_bytes=None, gpus=[], gpu_source="entfernt",
                        remote_host=urllib.parse.urlsplit(ollama_url).netloc)
    total, available = _memory()
    gpus = query_gpus()
    return Hardware(cpu_count=os.cpu_count() or 1, ram_total_bytes=total, ram_available_bytes=available,
                    gpus=gpus or [], gpu_source="nvidia-smi" if gpus else "keine")


class PeakSampler:
    """Misst während eines Requests den höchsten VRAM- und RAM-Verbrauch (systemweit)."""

    def __init__(self, interval: float = 0.25, gpu_query=query_gpus, memory=_memory) -> None:
        self.interval = interval
        self._gpu_query = gpu_query
        self._memory = memory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.vram_peak: int | None = None
        self.vram_total: int | None = None
        self.ram_peak_used: int | None = None
        self.ram_total: int | None = None
        self.samples = 0

    def _sample(self) -> None:
        gpus = self._gpu_query()
        if gpus:
            used = sum(g.used_bytes for g in gpus)
            self.vram_total = sum(g.total_bytes for g in gpus)
            self.vram_peak = max(self.vram_peak or 0, used)
        total, available = self._memory()
        if total and available is not None:
            self.ram_total = total
            self.ram_peak_used = max(self.ram_peak_used or 0, total - available)
        self.samples += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            self._sample()
            self._stop.wait(max(0.0, self.interval - (time.monotonic() - started)))

    def __enter__(self) -> PeakSampler:
        self._sample()
        self._thread = threading.Thread(target=self._run, name="peak-sampler", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._sample()
