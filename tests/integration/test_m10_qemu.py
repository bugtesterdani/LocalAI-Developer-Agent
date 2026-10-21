"""M10 mit echtem Broker und echtem QEMU auf einem Linux-Host: Aufbau ohne root, Verbindungskanal, Snapshot und Lesen
im Gast, Transfer-Laufwerk nur während der Jobs, Leerlauf-Abschaltung mit Start bei Bedarf, Not-Aus.

Statt des Debian-Abbilds startet ein Test-Gast (tests/qemu: Linux-Kern + initramfs mit Python, Worker und
``localai.guest.qemulink``) über ``sandbox.qemu.directBoot``. Alles andere ist der echte Weg: QemuVmHost baut
Systemfestplatte (qemu-img), Seed-Laufwerk (mkfs.fat/mcopy) und Transfer-Laufwerk, startet QEMU ohne Netzwerkgerät
und prüft das per QMP.

Kern: Umgebungsvariable ``LOCALAI_TEST_KERNEL`` oder ``/opt/testkernel/x/boot/vmlinuz-*`` (z. B. aus dem Paket
linux-image-unsigned-*-generic, entpackt). Ohne Kern, QEMU oder Werkzeuge wird übersprungen.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest
from conftest import start_broker
from test_sandbox_dev_local import approve_and_wait, req

from localai.contracts import ProposalState, ToolTarget

ROOT = pathlib.Path(__file__).resolve().parents[2]
KERNEL = os.environ.get("LOCALAI_TEST_KERNEL") or next(iter(sorted(glob.glob("/opt/testkernel/x/boot/vmlinuz-*"))), None)
TOOLS = ["qemu-system-x86_64", "qemu-img", "mkfs.fat", "mcopy", "cpio", "busybox"]
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(sys.platform != "linux" or KERNEL is None or any(shutil.which(t) is None for t in TOOLS),
                                 reason="Linux, QEMU, qemu-img, dosfstools, mtools, cpio, busybox und ein Test-Kern nötig")]


@pytest.fixture(scope="module")
def initrd(tmp_path_factory):
    out = tmp_path_factory.mktemp("gast") / "guest.gz"
    subprocess.run([sys.executable, str(ROOT / "tests" / "qemu" / "build_test_guest.py"), "--out", str(out), "--kernel", KERNEL],
                   check=True)
    return out


def _prerequisites(directory: pathlib.Path) -> None:
    """Gültige, aber leere Linux-Bausteine (der Test-Gast bringt seine Laufzeit selbst mit)."""
    files = {"cpython-3.13.9+20251014-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz": b"python",
             "wheels/pydantic-2.12.0-py3-none-any.whl": b"w1",
             "wheels/pydantic_core-2.41.0-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl": b"w2",
             "wheels/annotated_types-0.7.0-py3-none-any.whl": b"w3",
             "wheels/typing_extensions-4.15.0-py3-none-any.whl": b"w4",
             "wheels/typing_inspection-0.4.2-py3-none-any.whl": b"w5"}
    for name, data in files.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(data)
    manifest = {"schemaVersion": 1, "python": next(iter(files)), "pythonVersion": "3.13.9",
                "wheels": [n for n in files if n.startswith("wheels/")], "debs": [],
                "sha256": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}}
    (directory / "prerequisites.json").write_text(json.dumps(manifest), encoding="utf-8")


def _setup(tmp_path: pathlib.Path, initrd: pathlib.Path, idle_minutes: int = 0):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    for part in ("src", "scripts", "contracts"):
        (repo / part).symlink_to(ROOT / part)  # Gastdateien (Worker, Skripte) aus dem echten Repository
    (repo / "linux" / "linux-gast").mkdir(parents=True)
    _prerequisites(repo / "linux" / "linux-gast")
    (repo / "linux" / "debian-13-generic-amd64.raw").write_bytes(b"\0" * (1024 * 1024))
    config = {"sandbox": {"mode": "qemu", "qemu": {
        "imagePath": "linux/debian-13-generic-amd64.raw", "vmDirectory": "vm", "memoryMB": 1024, "processors": 2,
        "osDiskGB": 8, "buildTimeoutMinutes": 10, "accel": "Tcg", "idleMinutes": idle_minutes,
        "directBoot": {"kernel": KERNEL, "initrd": str(initrd), "append": "console=ttyS0 quiet panic=-1"}}},
        "web": {"enabled": False}}
    (repo / "config" / "localai.json").write_text(json.dumps(config), encoding="utf-8")
    project_dir = tmp_path / "projekt"
    (project_dir / "src").mkdir(parents=True)
    (project_dir / "README.md").write_bytes(b"# QEMU-Test\n")
    (project_dir / "src" / "main.c").write_bytes(b"int main(void) { return 0; }\n")
    project = {"schemaVersion": 1, "projectId": "m10", "name": "M10", "guestOs": "Linux",
               "grants": [{"grantId": "p", "root": str(project_dir), "rights": ["List", "Read"], "recursive": True}]}
    (tmp_path / "projekt.json").write_text(json.dumps(project), encoding="utf-8")
    return repo, project_dir


def _wait_status(broker, predicate, timeout: float, vm_dir: pathlib.Path) -> str:
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        status = broker.ui().status().get("sandbox") or ""
        if predicate(status):
            return status
        if status.startswith("gesperrt"):
            break
        time.sleep(0.5)
    console = (vm_dir / "console.log").read_text(errors="replace")[-3000:] if (vm_dir / "console.log").exists() else ""
    pytest.fail(f"Zustand: {status}\n--- Konsole ---\n{console}")


def test_qemu_sandbox_ende_zu_ende(tmp_path, initrd):
    repo, project_dir = _setup(tmp_path, initrd)
    data = tmp_path / "data"
    broker = start_broker(data, "--mode", "qemu", "--config", str(repo / "config" / "localai.json"),
                          "--project", str(tmp_path / "projekt.json"))
    vm = repo / "vm"
    try:
        status = _wait_status(broker, lambda s: s.startswith("Offline-VM bereit"), 300, vm)
        assert "Selbsttest bestanden" in status, status
        assert (os.stat(data).st_mode & 0o777) == 0o700
        # QEMU läuft ohne Netzwerkgerät und mit seccomp; Steuer-Sockets nur für diesen Benutzer.
        pid = int((vm / "qemu.pid").read_text().strip())
        cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        assert b"-nodefaults" in cmdline and cmdline[cmdline.index(b"-nic") + 1] == b"none"
        assert any(a.startswith(b"on,obsolete=deny") for a in cmdline)

        ui, orch = broker.ui(), broker.orchestrator()
        snap = approve_and_wait(ui, orch, orch.submit(req("Workspace.Snapshot", {"grantId": "p", "root": str(project_dir)})), timeout=120)
        assert snap.state == ProposalState.Succeeded, snap
        snapshot = snap.result.target_results[0].text
        assert snap.result.target_results[0].message.startswith("2 Dateien")
        view = orch.submit(req("File.ReadMany", {"grantId": "p"}, [ToolTarget(target_id="t1", path="src/main.c")], snapshot))
        outcome = approve_and_wait(ui, orch, view, timeout=120)
        assert outcome.state == ProposalState.Succeeded, outcome
        assert outcome.result.target_results[0].text == "int main(void) { return 0; }\n"
        # Jede Rückgabe kam über ein frisch angehängtes Transfer-Laufwerk (Rohformat); zwischen den Jobs hängt keins an
        # der VM (Gerät auf einen leeren null-co-Knoten umgestellt, Datei abgeholt und gelöscht).
        assert not list(vm.glob("transfer-0*.raw"))
        assert json.loads((vm / "vm.json").read_text())["transferDisk"] is None
        assert (vm / "console.log").exists() and "LocalAi-Testgast" in (vm / "console.log").read_text(errors="replace")
        assert ui.status().get("sandbox", "").startswith("Offline-VM bereit")
    finally:
        broker.stop()
    # Programmende: QEMU ist beendet, keine VM-Dateien bleiben (ohne reuseVm).
    deadline = time.monotonic() + 30
    while pathlib.Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not pathlib.Path(f"/proc/{pid}").exists()
    assert not list(vm.glob("*.qcow2"))


def test_leerlauf_und_start_bei_bedarf(tmp_path, initrd):
    repo, project_dir = _setup(tmp_path, initrd)
    config_path = repo / "config" / "localai.json"
    config = json.loads(config_path.read_text())
    config["sandbox"]["qemu"]["reuseVm"] = True
    config_path.write_text(json.dumps(config))
    data = tmp_path / "data"
    vm = repo / "vm"
    broker = start_broker(data, "--mode", "qemu", "--config", str(config_path), "--project", str(tmp_path / "projekt.json"))
    try:
        _wait_status(broker, lambda s: s.startswith("Offline-VM bereit"), 300, vm)
    finally:
        broker.stop()
    # reuseVm: heruntergefahren statt gelöscht, Seed (mit Schlüssel) wird beim nächsten Start entfernt.
    assert list(vm.glob("*.qcow2"))
    broker = start_broker(data, "--mode", "qemu", "--config", str(config_path), "--project", str(tmp_path / "projekt.json"))
    try:
        status = _wait_status(broker, lambda s: "ausgeschaltet" in s, 60, vm)
        assert "startet beim nächsten Job" in status
        ui, orch = broker.ui(), broker.orchestrator()
        snap = approve_and_wait(ui, orch, orch.submit(req("Workspace.Snapshot", {"grantId": "p", "root": str(project_dir)})), timeout=300)
        assert snap.state == ProposalState.Succeeded, snap
        assert not list(vm.glob("*-seed.img"))
        assert ui.status().get("sandbox", "").startswith("Offline-VM bereit")
    finally:
        broker.stop()
