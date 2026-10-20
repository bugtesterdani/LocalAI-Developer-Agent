"""M9: Betriebssystem-Debugging mit QEMU – mit echtem QEMU und einem kleinen Bootsektor (nasm)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time

import pytest
from localai.contracts import ResultStatus
from localai.worker import linproc
from localai.worker.execution import Executor, ToolchainCatalog
from localai.worker.osdebug import (
    MachineConfig,
    OsDebugError,
    OsDebugManager,
    cpu_mode,
    qemu_command,
)
from localai.worker.snapshot import SnapshotStore
from localai.worker.tools import ToolRunner
from tests.test_worker import job, make_snapshot, proposal

QEMU = shutil.which("qemu-system-x86_64")
NASM = shutil.which("nasm")
needs_qemu = pytest.mark.skipif(QEMU is None or NASM is None, reason="qemu-system-x86_64 und nasm nötig")
root_only = pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="braucht root")

BOOT = """bits 16
org 0x7c00
start:
    cli
    xor ax, ax
    mov ds, ax
    mov ss, ax
    mov sp, 0x7c00
    mov ax, 0xb800
    mov es, ax
    mov si, msg
    xor di, di
.print:
    lodsb
    test al, al
    jz marker
    mov ah, 0x0f
    stosw
    mov dx, 0x3f8
    out dx, al
    jmp .print
marker:
    sti
.key:
    xor ah, ah
    int 0x16
    cmp al, 'x'
    je crash
    mov dx, 0x3f8
    out dx, al
    jmp .key
crash:
    lidt [nullidt]
    int3
nullidt: dw 0
         dd 0
msg: db "Hallo DaniOS", 13, 10, 0
times 510-($-$$) db 0
dw 0xaa55
"""
SESSION = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(scope="module")
def floppy(tmp_path_factory) -> tuple[bytes, int]:
    if NASM is None:
        pytest.skip("nasm fehlt")
    d = tmp_path_factory.mktemp("os")
    (d / "boot.asm").write_text(BOOT)
    subprocess.run([NASM, "-f", "bin", str(d / "boot.asm"), "-o", str(d / "boot.bin"), "-l", str(d / "boot.lst")], check=True)
    marker = next(int(line.split()[1], 16) for line in (d / "boot.lst").read_text().splitlines()
                  if line.split()[1:2] and line.split()[-1:] == ["sti"])
    image = (d / "boot.bin").read_bytes().ljust(1440 * 1024, b"\0")
    return image, 0x7C00 + marker


class Index:
    """Snapshot-Ersatz: genau ein Abbild."""

    def __init__(self, data: bytes, name: str = "build/os.img") -> None:
        self.data, self.name = data, name
        self.files = {name: (len(data), hashlib.sha256(data).hexdigest())}

    def resolve(self, rel: str) -> pathlib.Path:
        path = pathlib.Path(tempfile.mkdtemp()) / "img"
        path.write_bytes(self.data)
        return path


@pytest.fixture()
def sessions_root():
    root = pathlib.Path(tempfile.mkdtemp(prefix="localai-osdebug-", dir="/tmp"))
    os.chmod(root, 0o711)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def config(**extra) -> dict:
    return {"image": "build/os.img", "media": "Floppy", **extra}


# ---------------------------------------------------------------- ohne QEMU
@pytest.mark.parametrize("raw, fehler", [
    ({"image": "../x.img"}, "unzulässiger Pfad"),
    ({"image": "a.img", "media": "Usb"}, "media"),
    ({"image": "a.img", "memoryMB": 99999}, "memoryMB"),
    ({"image": "a.img", "qemuArgs": "-nic user"}, "unbekannte Felder"),
    ({"image": "a.img", "accel": "Whpx"}, "accel"),
])
def test_konfiguration_streng(raw, fehler):
    with pytest.raises(OsDebugError, match=fehler):
        MachineConfig.parse(raw)


def test_befehlszeile_ohne_netz_und_nur_unix_sockets(tmp_path):
    cmd = qemu_command("/usr/bin/qemu-system-x86_64", MachineConfig.parse(config()), tmp_path / "img", tmp_path, kvm_available=False)
    text = " ".join(cmd)
    assert cmd[cmd.index("-nic") + 1] == "none" and cmd[cmd.index("-display") + 1] == "none"
    assert "-sandbox" in cmd and "spawn=deny" in text and "elevateprivileges=deny" in text
    assert "tcp" not in text and "unix:" in text and "-S" in cmd
    assert "shutdown=pause" in text and "accel" not in cmd[cmd.index("-machine") + 1]
    kvm = qemu_command("q", MachineConfig.parse(config(accel="Kvm")), tmp_path / "img", tmp_path, kvm_available=True)
    assert kvm[kvm.index("-accel") + 1] == "kvm"


def test_cpu_modus_aus_registern():
    assert cpu_mode("CR0=00000010 CR2=0") == 16
    assert cpu_mode("CR0=80000011 CR2=0\nCS =0008 00000000 ffffffff 00a09b00 DPL=0 CS64 [-RA]") == 64
    assert cpu_mode("CR0=00000011\nCS =0008 00000000 ffffffff 00cf9b00 DPL=0 CS32 [-RA]") == 32


# ---------------------------------------------------------------- mit QEMU
@needs_qemu
def test_bootsektor_debuggen_bedienen_und_dreifachfehler_erkennen(floppy, sessions_root):
    image, marker = floppy
    manager = OsDebugManager(sessions_root, qemu=QEMU)
    started = manager.start(SESSION, {"config": config(), "maxMinutes": 5, "allowInput": True}, Index(image), "job-1")
    assert "angehalten vor dem ersten Befehl" in started["state"]
    try:
        assert "gesetzt" in manager.command(SESSION, {"command": "break", "address": hex(marker)})["message"]
        hit = manager.command(SESSION, {"command": "continue", "waitMs": 30000})
        assert "Haltepunkt" in hit["state"], hit
        assert "Hallo DaniOS" in hit["serial"]
        registers = manager.command(SESSION, {"command": "registers"})["registers"]
        assert f"EIP={marker:08x}" in registers
        dis = manager.command(SESSION, {"command": "disassemble", "address": "0x7c00", "count": 3})["disassembly"]
        assert "cli" in dis and "xor" in dis
        assert manager.command(SESSION, {"command": "screen-text"})["screenText"].startswith("Hallo DaniOS")
        stepped = manager.command(SESSION, {"command": "step", "count": 1})
        assert f"EIP={marker + 1:08x}" in stepped["registers"]
        memory = manager.command(SESSION, {"command": "memory", "address": "0x7dfe", "length": 2, "physical": True})
        assert "55 aa" in memory["memory"]
        manager.command(SESSION, {"command": "break", "address": hex(marker), "remove": True})
        assert manager.command(SESSION, {"command": "continue", "waitMs": 300})["state"] == "läuft"
        typed = manager.command(SESSION, {"command": "keys", "text": "Ok!", "waitMs": 500})
        assert typed["serial"] == "Ok!"
        shot = manager.command(SESSION, {"command": "screenshot"})
        assert base64.b64decode(shot["screenshot"])[:8] == b"\x89PNG\r\n\x1a\n"
        crashed = manager.command(SESSION, {"command": "keys", "keys": ["x"], "waitMs": 1500})
        assert crashed["state"] == "angehalten nach Reset/Dreifachfehler", crashed
        events = manager.command(SESSION, {"command": "events"})
        assert "Triple fault" in events["log"] and "guest-reset" in events["events"]
        with pytest.raises(OsDebugError, match="command:"):
            manager.command(SESSION, {"command": "human-monitor", "line": "quit"})
    finally:
        assert "beendet" in manager.stop(SESSION)
    assert not any(sessions_root.iterdir())
    with pytest.raises(OsDebugError, match="Keine laufende Debug-Sitzung"):
        manager.command(SESSION, {"command": "registers"})


@needs_qemu
def test_eingaben_nur_wenn_freigegeben_und_sitzung_endet_nach_frist(floppy, sessions_root):
    image, _ = floppy
    manager = OsDebugManager(sessions_root, qemu=QEMU)
    manager.start(SESSION, {"config": config(), "maxMinutes": 1, "haltAtStart": False}, Index(image), "job-2")
    with pytest.raises(OsDebugError, match="allowInput"):
        manager.command(SESSION, {"command": "keys", "text": "a"})
    with pytest.raises(OsDebugError, match="bereits eine Debug-Sitzung"):
        manager.start("22222222-2222-3333-4444-555555555555", {"config": config()}, Index(image), "job-3")
    process = manager.sessions[SESSION].process
    manager.sessions[SESSION].deadline = time.monotonic() - 1  # Frist abgelaufen
    manager.reap()
    assert process.poll() is not None and not manager.sessions


@needs_qemu
@root_only
def test_qemu_laeuft_als_eigener_benutzer_ohne_netz(floppy, sessions_root):
    image, _ = floppy
    manager = OsDebugManager(sessions_root, emu_user=linproc.ToolUser("nobody", 65534, 65534), qemu=QEMU)
    manager.start(SESSION, {"config": config(), "maxMinutes": 2}, Index(image), "job-4")
    try:
        pid = manager.sessions[SESSION].process.pid
        status = pathlib.Path(f"/proc/{pid}/status").read_text()
        assert "\nUid:\t65534" in status and "NoNewPrivs:\t1" in status
        assert os.readlink(f"/proc/{pid}/ns/net") != os.readlink("/proc/self/ns/net")
        assert "Seccomp:\t2" in status
        assert "läuft" in manager.command(SESSION, {"command": "continue", "waitMs": 200})["state"]
    finally:
        manager.stop(SESSION)
    assert linproc._user_pids(65534, pathlib.Path("/proc")) == []


@needs_qemu
def test_als_werkzeug_mit_build_im_snapshot(floppy, tmp_path, sessions_root):
    """OsDebug.Start baut das Abbild mit einer Toolchain aus dem Snapshot und bootet es; Befehle und Stop als Jobs."""
    image, marker = floppy
    builder = b"import pathlib, sys\nsrc = pathlib.Path('src/boot.bin').read_bytes()\n" \
              b"pathlib.Path('build').mkdir(exist_ok=True)\npathlib.Path('build/os.img').write_bytes(src.ljust(1474560, b'\\0'))\n"
    store = SnapshotStore(tmp_path / "work")
    manifest, archive = make_snapshot(tmp_path, {"src/boot.bin": image[:512], "tools/build.py": builder})
    base = store.import_archive(manifest, archive)
    catalog = tmp_path / "toolchains" / "pysys"
    catalog.mkdir(parents=True)
    catalog.joinpath("toolchain.json").write_text(json.dumps({
        "schemaVersion": 1, "name": "pysys", "version": "1", "displayName": "System-Python", "guestPath": "/opt/toolchains/pysys",
        "guest": "Linux", "license": {"proprietary": False}, "binaries": [{"name": "python", "path": "{system}/python3"}],
        "install": {"kind": "None", "expectedMinutes": 1, "check": {"binary": "python", "arguments": ["--version"]}},
        "actions": {"build": {"binary": "python", "arguments": ["tools/build.py"], "timeoutSeconds": 60}},
        "testResultFormat": "ExitCode"}), encoding="utf-8")
    executor = Executor(ToolchainCatalog(tmp_path / "toolchains"), sessions_root / "jobs", low_integrity=False)
    runner = ToolRunner(store, executor, None, OsDebugManager(sessions_root, qemu=QEMU, executor=executor))
    start = proposal("OsDebug.Start", [], base.snapshot_id, config=config(build={"toolchain": "pysys", "action": "build"}),
                     maxMinutes=5, allowInput=False, haltAtStart=True)
    result = runner.run(job(start, 1), None)
    assert result.status == ResultStatus.Succeeded, result.target_results[0].message
    data = json.loads(result.target_results[0].text)
    assert data["session"] == start.proposal_id and any("Build:" in n for n in data["notes"])

    def command(seq: int, **args):
        return runner.run(job(proposal("OsDebug.Command", [], "-", sessionId=start.proposal_id, **args), seq), None)

    command(2, command="break", address=hex(marker))
    hit = command(3, command="continue", waitMs=30000)
    assert "Haltepunkt" in json.loads(hit.target_results[0].text)["state"]
    shot = command(4, command="screenshot")
    assert [t.target_id for t in shot.target_results] == ["debug", "screenshot"]
    denied = command(5, command="keys", text="a")
    assert denied.status == ResultStatus.Failed and "allowInput" in denied.target_results[0].message
    stopped = runner.run(job(proposal("OsDebug.Stop", [], "-", sessionId=start.proposal_id), 6), None)
    assert stopped.status == ResultStatus.Succeeded and "beendet" in stopped.target_results[0].message


def test_ohne_freischaltung_kein_osdebug(tmp_path):
    runner = ToolRunner(SnapshotStore(tmp_path / "work"))
    result = runner.run(job(proposal("OsDebug.Command", [], "-", sessionId=SESSION, command="registers"), 1), None)
    assert result.status == ResultStatus.Failed and "nicht freigeschaltet" in result.target_results[0].message
