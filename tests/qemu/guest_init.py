"""Init des Test-Gasts (M10): Provisionierung im Kleinen, dann der echte Worker.

Wie im echten Linux-Gast unter QEMU: Sitzungsschlüssel und Konfiguration vom Seed-Laufwerk (CIDATA), Verbindungskanal
``localai.guest.qemulink``, Bericht über das Transfer-Laufwerk mit Signal ``report:0`` im KVP-Pool, danach der Worker
mit ``--transfer-disk auto``. Netzwerkprüfungen: der Test-Gast hat kein Netzwerkgerät; geprüft wird wie im Gast.
"""

from __future__ import annotations

import json
import os
import pathlib
import struct
import subprocess
import sys
import threading
import time

SEED = pathlib.Path("/mnt/localai-seed/localai")
ROOT = pathlib.Path("/var/lib/localai")
PY = "/opt/localai/python/bin/python3"


def log(text: str) -> None:
    print(f"[testgast {time.strftime('%H:%M:%S')}] {text}", flush=True)


def power_button() -> None:
    """ACPI-Ausschalter (system_powerdown des Hosts) → sofort ausschalten; im echten Gast erledigt das systemd."""
    for name_file in pathlib.Path("/sys/class/input").glob("event*/device/name"):
        if "Power Button" in name_file.read_text():
            device = f"/dev/input/{name_file.parent.parent.name}"
            with open(device, "rb") as events:
                while True:
                    _, _, kind, code, value = struct.unpack("llHHi", events.read(24))
                    if kind == 1 and code == 116 and value == 1:
                        log("Ausschalter gedrückt – fahre herunter")
                        os.sync()
                        subprocess.run(["/bin/busybox", "poweroff", "-f"], check=False)


def provisioned_state() -> tuple[str, dict, bool]:
    """Erster Start: Schlüssel und Konfiguration vom Seed, dauerhaft auf /dev/vda (Systemfestplatte) ablegen.
    Späterer Start (Start bei Bedarf, Seed entfernt): von dort lesen – wie die Provisionierung auf der Platte des echten Gasts."""
    if (SEED / "session.key").exists():
        key = (SEED / "session.key").read_text()
        config = json.loads((SEED / "guest-config.json").read_text(encoding="utf-8-sig"))
        blob = json.dumps({"key": key, "config": config}).encode()
        with open("/dev/vda", "r+b") as disk:
            disk.write(struct.pack("<I", len(blob)) + blob)
            os.fsync(disk.fileno())
        return key, config, True
    with open("/dev/vda", "rb") as disk:
        length = struct.unpack("<I", disk.read(4))[0]
        state = json.loads(disk.read(length))
    return state["key"], state["config"], False


def main() -> int:
    for d in ("inbox", "work", "logs"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    threading.Thread(target=power_button, daemon=True).start()
    key, config, first = provisioned_state()
    (ROOT / "session.key").write_text(key)
    log(f"Konfiguration: hypervisor={config.get('hypervisor')} transfer={config['transferDiskBytes']} erster Start={first}")
    link = subprocess.Popen([PY, "-I", "-m", "localai.guest.qemulink"])
    from localai.guest import provision
    from localai.worker.transfer import Signal, TransferDisk, TransferKind

    if not first:
        return run_worker(link, config)
    report = provision.Report()
    report.data["networkAdapters"] = provision.count_adapters(pathlib.Path("/sys/class/net"))
    report.data["probes"] = provision.probes()
    report.data["worker"] = {"running": True, "python": sys.version.split()[0]}
    data = report.finish()
    TransferDisk("auto", int(config["transferDiskBytes"])).write(TransferKind.REPORT, 0, data)
    Signal(None).send("report:0")
    log("Bericht übergeben, Worker startet")
    return run_worker(link, config)


def run_worker(link, config) -> int:
    worker = subprocess.Popen([PY, "-I", "-m", "localai.worker", "--inbox", str(ROOT / "inbox"), "--work", str(ROOT / "work"),
                               "--key-file", str(ROOT / "session.key"), "--transfer-disk", "auto",
                               "--transfer-size", str(int(config["transferDiskBytes"])), "--require-ready", "--no-low-integrity"])
    while True:
        if worker.poll() is not None:
            log(f"Worker beendet ({worker.returncode}), Neustart")
            return main_restart(worker, config)
        if link.poll() is not None:
            log(f"Verbindungskanal beendet ({link.returncode}), Neustart")
            link = subprocess.Popen([PY, "-I", "-m", "localai.guest.qemulink"])
        time.sleep(1)


def main_restart(worker, config) -> int:  # pragma: no cover - nur bei Fehlern im Test-Gast
    os.execv(PY, [PY, "-I", "-m", "localai.worker", "--inbox", str(ROOT / "inbox"), "--work", str(ROOT / "work"),
                  "--key-file", str(ROOT / "session.key"), "--transfer-disk", "auto",
                  "--transfer-size", str(int(config["transferDiskBytes"])), "--require-ready", "--no-low-integrity"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException as err:  # noqa: BLE001 - als PID 1 nie still enden
        log(f"FEHLER: {err!r}")
        time.sleep(3600)
