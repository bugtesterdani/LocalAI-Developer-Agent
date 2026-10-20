"""M10: Linux-Host – Verbindungskanal im Gast (qemulink), virtio-Laufwerke, Link-Dienst, Webabruf mit bubblewrap,
Linux-Bausteine. Der echte Ablauf mit QEMU: tests/integration/test_m10_qemu.py."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from localai.guest import provision, qemulink
from localai.worker import kvp
from localai.worker.transfer import find_transfer_disk_linux

ROOT = pathlib.Path(__file__).resolve().parents[3]
# Gastseite und Linux-Host: Pipes, fcntl-Sperren des KVP-Pools und bubblewrap gibt es nur unter Linux.
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="nur unter Linux")


class Port:
    """Gegenstelle des virtio-serial-Ports: was der Gastdienst an den Host schreibt."""

    def __init__(self) -> None:
        self.r, self.w = os.pipe()
        os.set_blocking(self.r, False)

    def lines(self) -> list[str]:
        try:
            return os.read(self.r, 1 << 16).decode().splitlines()
        except BlockingIOError:
            return []


def _link(tmp_path: pathlib.Path) -> tuple[qemulink.Link, Port]:
    port = Port()
    return qemulink.Link(port.w, tmp_path / "inbox", tmp_path / "pool"), port


def _frame(name: str, data: bytes, sha: str | None = None) -> bytes:
    return f"LAIFILE1 {name} {len(data)} {sha or hashlib.sha256(data).hexdigest()}\n".encode() + data


def test_datei_landet_erst_nach_hashpruefung_im_eingang(tmp_path):
    link, port = _link(tmp_path)
    payload = b"\x00\x01binaer\nmit Zeilenumbruch LAIFILE1 x 1 y\n" * 1000
    frame = _frame("000000000001-job.payload.zip", payload)
    # In beliebigen Stücken, wie der Kanal sie liefert.
    for i in range(0, len(frame), 777):
        link.feed(frame[i:i + 777])
    assert (tmp_path / "inbox" / "000000000001-job.payload.zip").read_bytes() == payload
    assert oct((tmp_path / "inbox" / "000000000001-job.payload.zip").stat().st_mode & 0o777) == "0o600"
    assert not list((tmp_path / "inbox").glob(".*.part"))
    assert port.lines() == ["ack 000000000001-job.payload.zip ok"]


def test_falscher_hash_und_unbekannte_zeilen_werden_verworfen(tmp_path):
    link, port = _link(tmp_path)
    link.feed(b"irgendwas\n" + _frame("a.job.json", b"{}", sha="0" * 64) + _frame("b.ready", b"b"))
    assert not (tmp_path / "inbox" / "a.job.json").exists()
    assert (tmp_path / "inbox" / "b.ready").read_bytes() == b"b"
    assert port.lines() == ["ack a.job.json error Hash stimmt nicht", "ack b.ready ok"]
    assert not list((tmp_path / "inbox").glob(".*"))


@pytest.mark.parametrize("name", ["../böse", ".versteckt", "a/b", "x" * 201, "a b"])
def test_unzulaessige_namen_werden_nie_geschrieben(tmp_path, name):
    link, port = _link(tmp_path)
    link.feed(f"LAIFILE1 {name} 1 {'0' * 64}\n".encode() + b"x")
    assert not (tmp_path / "inbox").exists() or not any((tmp_path / "inbox").iterdir())
    assert not (tmp_path / "böse").exists()
    assert port.lines() == []


def test_ueberlanger_kopf_setzt_neu_auf_und_trennung_verwirft_teildatei(tmp_path):
    link, port = _link(tmp_path)
    link.feed(b"y" * 600)
    link.feed(_frame("ok.ready", b"1"))
    assert (tmp_path / "inbox" / "ok.ready").exists()
    link.feed(_frame("halb.zip", b"abcdef")[:-3])
    assert (tmp_path / "inbox" / ".halb.zip.part").exists()
    link.reset()
    assert not (tmp_path / "inbox" / ".halb.zip.part").exists()
    assert port.lines() == ["ack ok.ready ok"]


def test_kvp_werte_gehen_bei_aenderung_an_den_host(tmp_path):
    link, port = _link(tmp_path)
    link.forward_kvp(force=True)
    assert port.lines() == []
    kvp.write_value("LocalAiProgress", "network", tmp_path / "pool")
    kvp.write_value("Fremd", "x", tmp_path / "pool")
    link.forward_kvp()
    assert port.lines() == ["kvp LocalAiProgress\tnetwork"]
    link.forward_kvp()
    assert port.lines() == []
    kvp.write_value("LocalAiSignal", "result:3", tmp_path / "pool")
    link.forward_kvp()
    assert port.lines() == ["kvp LocalAiSignal\tresult:3"]
    link.forward_kvp(force=True)
    assert sorted(port.lines()) == ["kvp LocalAiProgress\tnetwork", "kvp LocalAiSignal\tresult:3"]


def test_virtio_laufwerk_wird_als_transfer_laufwerk_gefunden(tmp_path):
    size = 64 * 1024 * 1024
    sys_block, dev = tmp_path / "sys", tmp_path / "dev"
    dev.mkdir()
    for name, disk_size in (("vda", 8 * 1024 ** 3), ("vdb", 512 * 1024 * 1024), ("vdc", size)):
        (sys_block / name).mkdir(parents=True)
        (sys_block / name / "size").write_text(str(disk_size // 512))
        with open(dev / name, "wb") as f:
            f.truncate(disk_size)
    assert find_transfer_disk_linux(size, sys_block, dev) == str(dev / "vdc")


def test_link_dienst_statt_hyperv_diensten_unter_qemu(tmp_path, monkeypatch):
    paths = provision.Paths(root=tmp_path / "root", opt=tmp_path / "opt", systemd=tmp_path / "systemd")
    paths.systemd.mkdir()
    calls: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "active\n", "")

    monkeypatch.setattr(provision, "run", fake_run)
    monkeypatch.setattr(provision.time, "sleep", lambda _s: None)
    assert provision.start_daemons(paths, {"hypervisor": "Qemu"}) == []
    unit = (paths.systemd / provision.LINK_UNIT).read_text()
    assert "-m localai.guest.qemulink" in unit and "org.localai.link" in unit
    assert not (paths.systemd / provision.KVP_UNIT).exists()
    assert ["systemctl", "enable", "--now", provision.LINK_UNIT] in calls
    worker = provision.worker_unit({"hypervisor": "Qemu", "transferDiskBytes": 1}, paths)
    assert f"After=local-fs.target {provision.LINK_UNIT}" in worker
    assert f"After=local-fs.target {provision.KVP_UNIT}" in provision.worker_unit({"transferDiskBytes": 1}, paths)


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("bwrap") is None, reason="bubblewrap nötig")
def test_webabruf_unter_linux_mit_bubblewrap(tmp_path):
    from localai.web import __main__ as web

    work = tmp_path / "quarantaene"
    work.mkdir()
    outside = ROOT / "bwrap-probe.txt"  # außerhalb von /tmp (dort liegt im Sandkasten ein leeres tmpfs)
    offline = web.linux_isolation(work, network=False)
    assert "--unshare-all" in offline and "--share-net" not in offline
    assert "--share-net" in web.linux_isolation(work, network=True)
    script = (f"import pathlib\n"
              f"pathlib.Path({str(work / 'ok.txt')!r}).write_text('ja')\n"
              f"try:\n    pathlib.Path({str(outside)!r}).write_text('nein')\nexcept OSError:\n    print('gesperrt')\n"
              f"print(len(open('/proc/net/dev').read().splitlines()) - 2)\n")
    result = subprocess.run([*offline, sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert (work / "ok.txt").read_text() == "ja"
    assert not outside.exists()
    lines = result.stdout.split()
    assert lines[0] == "gesperrt"
    assert lines[1] == "1", "ohne Netz nur lo"


def test_linux_bausteine_manifest(tmp_path):
    spec = importlib.util.spec_from_file_location("linux_prerequisites", ROOT / "tools" / "prereq" / "linux_prerequisites.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    prereq = tmp_path / "linux-gast"
    (prereq / "wheels").mkdir(parents=True)
    (prereq / "debs").mkdir()
    (prereq / "cpython-3.13.9+1-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz").write_bytes(b"py")
    (prereq / "wheels" / "pydantic-2-py3-none-any.whl").write_bytes(b"w")
    path = module.manifest(prereq, "cpython-3.13.9+1-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz", "3.13.9")
    data = json.loads(path.read_text())
    assert data["debs"] == [] and data["wheels"] == ["wheels/pydantic-2-py3-none-any.whl"]
    assert data["sha256"]["wheels/pydantic-2-py3-none-any.whl"] == hashlib.sha256(b"w").hexdigest()
