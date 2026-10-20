"""M8 mit dem echten Broker-Prozess (ohne Hyper-V): Gastauswahl, mehrere VMs, Sammelzustand, Fehlermeldungen.

Unter Linux bzw. ohne Hyper-V sperrt jede VM mit „Hyper-V gibt es nur unter Windows“ – das genügt, um zu prüfen,
dass der Broker aus Projekt, Toolchain-Manifest und Konfiguration die richtigen VMs anlegt.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import time

import pytest
from conftest import ROOT, start_broker

pytestmark = pytest.mark.integration


def _config(tmp_path: pathlib.Path, linux: bool = True, default: str = "Windows") -> pathlib.Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "win.iso").write_text("x")
    (repo / "debian-13-generic-amd64.raw").write_text("x")
    sandbox = {"mode": "hyperv", "defaultGuestOs": default,
               "hyperv": {"imagePath": "win.iso", "imageName": "Windows 11 Pro", "vmDirectory": "vm"}}
    if linux:
        sandbox["hypervLinux"] = {"imagePath": "debian-13-generic-amd64.raw", "vmDirectory": "vm-linux"}
    config = repo / "config" / "localai.json"
    config.write_text(json.dumps({"sandbox": sandbox, "web": {"enabled": False}}), encoding="utf-8")
    return config


def _project(tmp_path: pathlib.Path, **extra) -> pathlib.Path:
    folder = tmp_path / "projekt"
    folder.mkdir(exist_ok=True)
    project = {"schemaVersion": 1, "projectId": "m8", "name": "M8", "toolchains": [],
               "grants": [{"grantId": "p", "root": str(folder), "rights": ["List", "Read"], "recursive": True}], **extra}
    path = tmp_path / "projekt.json"
    path.write_text(json.dumps(project), encoding="utf-8")
    return path


def _register(data: pathlib.Path, name: str) -> None:
    """Registrierung wie über die Oberfläche (für die Gastauswahl zählt das Manifest im registrierten Ordner)."""
    directory = ROOT / "toolchains" / name
    manifest = (directory / "toolchain.json").read_bytes()
    data.mkdir(parents=True, exist_ok=True)
    (data / "toolchains.json").write_text(json.dumps([{
        "name": name, "version": "1", "directory": str(directory), "manifestSha256": hashlib.sha256(manifest).hexdigest(),
        "digest": "0" * 64, "files": {}, "registeredAt": "2026-10-20T00:00:00.000Z"}]), encoding="utf-8")


def _settled(broker, parts: int) -> str:
    deadline = time.monotonic() + 30
    while True:
        status = broker.ui().status().get("sandbox") or ""
        if status.count("gesperrt") >= parts or time.monotonic() > deadline:
            return status
        time.sleep(0.2)


def test_linux_projekt_ohne_linux_konfiguration_startet_nicht(tmp_path):
    with pytest.raises(RuntimeError, match="sandbox.hypervLinux fehlt"):
        start_broker(tmp_path / "data", "--mode", "hyperv", "--config", str(_config(tmp_path, linux=False)),
                     "--project", str(_project(tmp_path, guestOs="Linux")))


def test_linux_projekt_bekommt_nur_die_linux_vm(tmp_path):
    broker = start_broker(tmp_path / "data", "--mode", "hyperv", "--config", str(_config(tmp_path)),
                          "--project", str(_project(tmp_path, guestOs="Linux")))
    try:
        status = _settled(broker, 1)
        assert "Windows:" not in status and "Linux:" not in status, status
        if sys.platform != "win32":
            assert status.startswith("gesperrt: Hyper-V gibt es nur unter Windows"), status
    finally:
        broker.stop()


def test_standardgast_aus_der_konfiguration(tmp_path):
    broker = start_broker(tmp_path / "data", "--mode", "hyperv", "--config", str(_config(tmp_path, default="Linux")),
                          "--project", str(_project(tmp_path)))
    try:
        _settled(broker, 1)
        assert (tmp_path / "repo" / "vm-linux").exists()
        assert not (tmp_path / "repo" / "vm").exists()
    finally:
        broker.stop()


def test_linux_toolchain_im_windows_projekt_bringt_zweite_vm(tmp_path):
    data = tmp_path / "data"
    _register(data, "python-linux")
    broker = start_broker(data, "--mode", "hyperv", "--config", str(_config(tmp_path)),
                          "--project", str(_project(tmp_path, guestOs="Windows", toolchains=["python-linux"])))
    try:
        status = _settled(broker, 2)
        assert status.startswith("Windows: ") and " | Linux: " in status, status
        # Hinweis der Gastauswahl im Ereignisprotokoll.
        events = broker.ui().wait_events(0, timeout_ms=2000)
        assert any(e.type == "sandbox.guest" and "python-linux" in (e.message or "") for e in events), events
        tools = broker.ui().toolchains_available()
        assert all(t.get("guest") in ("Windows", "Linux") for t in tools.get("toolchains", []))
    finally:
        broker.stop()


def test_ausdruecklicher_widerspruch_startet_nicht(tmp_path):
    data = tmp_path / "data"
    _register(data, "python-linux")
    with pytest.raises(RuntimeError, match="läuft laut toolchain.json nur unter Linux"):
        start_broker(data, "--mode", "hyperv", "--config", str(_config(tmp_path)),
                     "--project", str(_project(tmp_path, toolchains=["python-linux"], toolchainGuests={"python-linux": "Windows"})))
