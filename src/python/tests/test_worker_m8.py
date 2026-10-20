"""M8: Linux-Gast – KVP-Pool, Transfer-Laufwerk unter Linux, isolierte Werkzeugprozesse, Provisionierung."""

from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

import pytest
from localai.contracts import ResultStatus
from localai.guest import provision
from localai.worker import kvp, linproc
from localai.worker.execution import Executor, ToolchainCatalog, binary_path
from localai.worker.snapshot import SnapshotStore
from localai.worker.tools import ToolRunner
from localai.worker.transfer import (
    HEADER_SIZE,
    Signal,
    TransferDisk,
    TransferError,
    TransferKind,
    find_transfer_disk_linux,
    parse_container,
)
from tests.test_worker import job, make_snapshot, proposal

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="nur unter Linux")
root_only = pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="braucht root (Benutzerwechsel, Namespaces)")


# ---------------------------------------------------------------- KVP
def test_kvp_pool_schreibt_datensaetze_fester_laenge(tmp_path):
    pool = tmp_path / ".kvp_pool_1"
    kvp.write_value("LocalAiProgress", "start", pool)
    kvp.write_value("LocalAiSignal", "report:0", pool)
    kvp.write_value("LocalAiProgress", "toolchains", pool)  # ersetzt, hängt nicht an
    data = pool.read_bytes()
    assert len(data) == 2 * kvp.RECORD_SIZE
    assert data[:15] == b"LocalAiProgress" and data[15:kvp.KEY_SIZE] == b"\0" * (kvp.KEY_SIZE - 15)
    assert kvp.read_pool(pool) == {"LocalAiProgress": "toolchains", "LocalAiSignal": "report:0"}


def test_kvp_zu_langer_wert_abgelehnt(tmp_path):
    with pytest.raises(kvp.KvpError):
        kvp.write_value("k", "x" * kvp.VALUE_SIZE, tmp_path / "p")
    with pytest.raises(kvp.KvpError):
        kvp.write_value("a\0b", "x", tmp_path / "p")


@linux_only
def test_signal_im_linux_gast_ueber_kvp(tmp_path):
    pool = tmp_path / "pool"
    Signal(None, pool).send("result:7")
    assert kvp.read_pool(pool)["LocalAiSignal"] == "result:7"


# ---------------------------------------------------------------- Transfer-Laufwerk unter Linux
def _fake_disk(sys_block: pathlib.Path, dev: pathlib.Path, name: str, size: int, header: bytes = b"") -> pathlib.Path:
    (sys_block / name).mkdir(parents=True)
    (sys_block / name / "size").write_text(str(size // 512), encoding="ascii")
    path = dev / name
    with open(path, "wb") as f:
        f.truncate(size)
        f.write(header)
    return path


def test_transfer_laufwerk_unter_linux_an_groesse_und_leerem_kopf(tmp_path):
    size = 64 * 1024 * 1024
    sys_block, dev = tmp_path / "sys", tmp_path / "dev"
    dev.mkdir()
    _fake_disk(sys_block, dev, "sda", 64 * 1024 ** 3, b"\xebsystem")      # Systemplatte
    _fake_disk(sys_block, dev, "sdb", 512 * 1024 * 1024)                   # gelöschtes Seed-Laufwerk (andere Größe)
    used = _fake_disk(sys_block, dev, "sdc", size, b"LAIXFER1")            # benutztes Transfer-Laufwerk
    fresh = _fake_disk(sys_block, dev, "sdd", size)
    assert find_transfer_disk_linux(size, sys_block, dev) == str(fresh)
    used.unlink()
    shutil.rmtree(sys_block / "sdc")
    _fake_disk(sys_block, dev, "sde", size)
    with pytest.raises(TransferError, match="2 Kandidaten"):
        find_transfer_disk_linux(size, sys_block, dev)


def test_transfer_container_auf_datei_wie_im_gast(tmp_path):
    disk = tmp_path / "disk.raw"
    with open(disk, "wb") as f:
        f.truncate(1024 * 1024)
    TransferDisk(str(disk), 1024 * 1024).write(TransferKind.REPORT, 0, b'{"ok": 1}')
    kind, seq, payload = parse_container(disk.read_bytes())
    assert (kind, seq, payload) == (TransferKind.REPORT, 0, b'{"ok": 1}')
    with pytest.raises(TransferError, match="nicht leer"):
        TransferDisk(str(disk), 1024 * 1024).write(TransferKind.RESULT, 1, b"x")
    assert HEADER_SIZE == 4096


# ---------------------------------------------------------------- Werkzeugprozesse im Linux-Gast
LINUX_MANIFEST = {
    "schemaVersion": 1, "name": "pysys", "version": "1", "displayName": "System-Python", "guestPath": "/opt/toolchains/pysys",
    "guest": "Linux", "license": {"proprietary": False}, "binaries": [{"name": "python", "path": "{system}/python3"}],
    "install": {"kind": "None", "expectedMinutes": 1, "check": {"binary": "python", "arguments": ["--version"]}},
    "actions": {"run": {"binary": "python", "arguments": ["{script}"], "parameters": {"script": {"type": "Path"}}, "timeoutSeconds": 20}},
    "testResultFormat": "ExitCode",
}

PROBE = b"""import os, socket, subprocess, pathlib, sys
print("uid", os.getuid(), "gid", os.getgid(), "groups", os.getgroups())
try:
    socket.create_connection(("127.0.0.1", 22), timeout=2); print("loopback offen")
except OSError as err:
    print("loopback zu", type(err).__name__)
print("netz", sorted(line.split(":")[0].strip() for line in open("/proc/net/dev").read().splitlines()[2:]))
try:
    pathlib.Path(sys.argv[0]).resolve().parents[3].joinpath("fremd.txt").write_text("x"); print("schreiben oben moeglich")
except OSError:
    print("schreiben oben verboten")
pathlib.Path("/tmp/localai-m8-spur").write_text("x")
subprocess.Popen(["sleep", "300"], start_new_session=True)
print("NoNewPrivs", [l for l in open("/proc/self/status") if l.startswith("NoNewPrivs")][0].split()[1])
"""


@pytest.fixture()
def linux_exec(tmp_path):
    if shutil.which("python3", path="/usr/bin") is None:
        pytest.skip("/usr/bin/python3 fehlt")
    store = SnapshotStore(tmp_path / "work")
    manifest, archive = make_snapshot(tmp_path, {"tools/probe.py": PROBE})
    base = store.import_archive(manifest, archive)
    catalog_dir = tmp_path / "toolchains" / "pysys"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "toolchain.json").write_text(json.dumps(LINUX_MANIFEST), encoding="utf-8")
    jobs = pathlib.Path(tempfile.mkdtemp(prefix="localai-jobs-", dir="/tmp"))
    os.chmod(jobs, 0o711)
    yield store, base, ToolchainCatalog(tmp_path / "toolchains"), jobs
    shutil.rmtree(jobs, ignore_errors=True)
    pathlib.Path("/tmp/localai-m8-spur").unlink(missing_ok=True)


def test_linux_manifest_installationsort_und_systemprogramme(linux_exec):
    _, _, catalog, _ = linux_exec
    toolchain = catalog.get("pysys")
    assert str(toolchain.install_dir) == "/opt/toolchains/pysys"
    assert str(binary_path(toolchain, "python")) == "/usr/bin/python3"


@linux_only
@root_only
def test_werkzeug_als_eigener_benutzer_ohne_netz_und_ohne_reste(linux_exec):
    store, base, catalog, jobs = linux_exec
    user = linproc.ToolUser("nobody", 65534, 65534)
    runner = ToolRunner(store, Executor(catalog, jobs, low_integrity=False, tools_user=user))
    result = runner.run(job(proposal("Toolchain.Run", [], base.snapshot_id, toolchain="pysys", parameters={"script": "tools/probe.py"}), 1), None)
    assert result.status == ResultStatus.Succeeded, result
    log = next(r.text for r in result.target_results if r.target_id == "log")
    assert "uid 65534 gid 65534 groups []" in log
    assert "loopback zu" in log and "netz ['lo']" in log
    assert "schreiben oben verboten" in log and "NoNewPrivs 1" in log
    # Kein Prozess und keine Datei des Werkzeugbenutzers überlebt den Job; der Arbeitsordner ist weg.
    time.sleep(0.2)
    assert linproc._user_pids(65534, pathlib.Path("/proc")) == []
    assert not pathlib.Path("/tmp/localai-m8-spur").exists()
    assert list(jobs.iterdir()) == []


def test_aufraeumen_nie_fuer_root():
    with pytest.raises(ValueError):
        linproc.kill_user_processes(0)
    with pytest.raises(ValueError):
        linproc.cleanup_temp(0)


# ---------------------------------------------------------------- Provisionierung
def _paths(tmp_path) -> provision.Paths:
    return provision.Paths(root=tmp_path / "var", opt=tmp_path / "opt", toolchains=tmp_path / "opt-toolchains",
                           jobs=tmp_path / "jobs", systemd=tmp_path / "systemd", sys_net=tmp_path / "net")


def _toolchain_src(tmp_path, name: str, archive_name: str, archive: bytes, extra: dict | None = None) -> pathlib.Path:
    import hashlib

    src = tmp_path / "src" / name
    (src / "selftest").mkdir(parents=True)
    (src / archive_name).write_bytes(archive)
    (src / "selftest" / "hallo.sh").write_text("echo 'Hallo aus der Sandbox (Shell)'\n")
    (src / "wheels").mkdir()
    (src / "wheels" / "paket.whl").write_bytes(b"w")
    manifest = {
        "schemaVersion": 1, "name": name, "version": "1.0", "displayName": "Test", "guestPath": f"/opt/toolchains/{name}",
        "guest": "Linux", "license": {"proprietary": False},
        "binaries": [{"name": "tool", "path": "bin/tool"}, {"name": "sh", "path": "{system}/sh"}],
        "install": {"kind": "Archive", "file": archive_name, "expectedMinutes": 1,
                    "check": {"binary": "tool", "arguments": ["--version"], "expectOutput": "tool 1.0"}},
        "selfTest": {"directory": "selftest", "steps": [{"binary": "sh", "arguments": ["{selftest}/hallo.sh"]}],
                     "expectOutput": "Hallo aus der Sandbox (Shell)"},
        "packageSource": {"kind": "Pip", "path": "wheels"},
        "actions": {}, "testResultFormat": "ExitCode",
        **(extra or {}),
    }
    (src / "toolchain.json").write_text(json.dumps(manifest), encoding="utf-8")
    files = {p.relative_to(src).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in src.rglob("*") if p.is_file() and p.name != "toolchain.json"}
    (src / "pinned.json").write_text(json.dumps({"name": name, "version": "1.0", "files": files}), encoding="utf-8")
    return src.parent


TOOL = b"#!/bin/sh\necho tool 1.0\n"


def _tar(members: dict[str, bytes], mode: int = 0o755) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o100755 << 16)
            zf.writestr(info, data)
    return buf.getvalue()


@linux_only
@pytest.mark.parametrize("archive_name, archive", [
    ("tool.tar.gz", _tar({"bin/tool": TOOL})),
    ("tool.zip", _zip({"bin/tool": TOOL})),
], ids=["tar", "zip"])
def test_toolchain_installation_im_linux_gast(tmp_path, archive_name, archive):
    paths = _paths(tmp_path)
    (paths.root / "work").mkdir(parents=True)
    source_root = _toolchain_src(tmp_path, "werkzeug", archive_name, archive)
    entry = provision.install_toolchain("werkzeug", source_root, paths, tmp_path / "tc.log")
    assert entry["error"] is None, entry
    assert entry["installed"] and entry["selfTest"] == "ok" and entry["check"] == "tool 1.0"
    assert (paths.toolchains / "werkzeug" / "bin" / "tool").stat().st_mode & 0o111
    published = paths.published / "werkzeug"
    assert (published / "toolchain.json").exists() and (published / "packages" / "paket.whl").exists()


@linux_only
def test_toolchain_mit_veraenderter_datei_oder_falschem_gast_abgelehnt(tmp_path):
    paths = _paths(tmp_path)
    (paths.root / "work").mkdir(parents=True)
    source_root = _toolchain_src(tmp_path, "werkzeug", "tool.tar.gz", _tar({"bin/tool": TOOL}))
    (source_root / "werkzeug" / "tool.tar.gz").write_bytes(_tar({"bin/tool": b"#!/bin/sh\necho boese\n"}))
    assert "Hash weicht ab" in provision.install_toolchain("werkzeug", source_root, paths, tmp_path / "l")["error"]
    windows = _toolchain_src(tmp_path / "w", "win", "tool.tar.gz", _tar({"bin/tool": TOOL}), {"guest": "Windows"})
    assert "Linux-Gast" in provision.install_toolchain("win", windows, paths, tmp_path / "l")["error"]


def test_archiv_mit_pfad_nach_aussen_abgelehnt(tmp_path):
    with pytest.raises(tarfile.TarError):
        provision.extract_archive(_write(tmp_path / "x.tar.gz", _tar({"../../boese": b"x"})), tmp_path / "ziel")
    with pytest.raises(provision.ToolchainError):
        provision.extract_archive(_write(tmp_path / "x.zip", _zip({"../boese": b"x"})), tmp_path / "ziel2")
    assert not (tmp_path / "boese").exists()


def _write(path: pathlib.Path, data: bytes) -> pathlib.Path:
    path.write_bytes(data)
    return path


def test_worker_einheit_mit_isolierung_und_neustart(tmp_path):
    paths = _paths(tmp_path)
    paths.systemd.mkdir(parents=True)
    unit = provision.write_worker_unit({"guest": "Linux", "transferDiskBytes": 67108864}, paths, reload=False).read_text()
    assert "--tools-user localai-tools" in unit and "--transfer-size 67108864" in unit and "--require-ready" in unit
    assert f"--jobs-root {paths.jobs}" in unit and "Restart=always" in unit
    with pytest.raises(ValueError):
        provision.write_worker_unit({"transferDiskBytes": 1}, paths, reload=False)


def test_netzwerkkarten_zaehlen_ohne_loopback(tmp_path):
    net = tmp_path / "net"
    (net / "lo").mkdir(parents=True)
    assert provision.count_adapters(net) == 0
    (net / "eth0").mkdir()
    assert provision.count_adapters(net) == 1


@linux_only
@root_only
def test_nur_lesbar_fuer_alle_ausser_root(tmp_path):
    folder = tmp_path / "opt"
    (folder / "bin").mkdir(parents=True)
    tool = folder / "bin" / "tool"
    tool.write_text("x")
    os.chmod(tool, 0o777)
    provision.make_read_only(folder)
    assert oct(tool.stat().st_mode & 0o777) == "0o755" and tool.stat().st_uid == 0


def test_provision_sh_liest_dateiliste(tmp_path):
    script = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "sandbox" / "guest-linux" / "provision.sh"
    text = script.read_text(encoding="utf-8")
    assert "\r" not in text and text.startswith("#!/bin/bash")
    func = text[text.index("files_of() {"):text.index("\n}\n", text.index("files_of() {")) + 2]
    seed = tmp_path / "seed"
    (seed / "prereq").mkdir(parents=True)
    (seed / "prereq" / "files.txt").write_text(
        "python cpython-3.13.5+20250612-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz\r\n"
        "wheel wheels/pydantic-2.12.0-py3-none-any.whl\ndeb debs/hyperv-daemons_6.12.48-1_amd64.deb\n"
        "deb ../boese;rm -rf /\n")
    out = subprocess.run(["bash", "-c", f"SEED={seed}\n{func}\nfiles_of python; files_of wheel; files_of deb"],
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["cpython-3.13.5+20250612-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
                   "wheels/pydantic-2.12.0-py3-none-any.whl", "debs/hyperv-daemons_6.12.48-1_amd64.deb"]


# ---------------------------------------------------------------- Worker wie im Linux-Gast (Ende zu Ende ohne VM)
@linux_only
@root_only
def test_worker_wie_im_linux_gast_signal_ueber_kvp_und_isoliertes_werkzeug(linux_exec, tmp_path):
    import hashlib

    from localai.worker.__main__ import main
    from tests.test_worker import KEY, envelope

    _, _, _, jobs = linux_exec
    inbox, work = tmp_path / "inbox", tmp_path / "gast-work"
    inbox.mkdir()
    key_file = tmp_path / "session.key"
    key_file.write_text(KEY.hex(), encoding="ascii")
    disk = tmp_path / "transfer.raw"
    pool = tmp_path / ".kvp_pool_1"
    size = 1024 * 1024
    common = ["--inbox", str(inbox), "--work", str(work), "--key-file", str(key_file), "--transfer-disk", str(disk),
              "--transfer-size", str(size), "--kvp-pool", str(pool), "--require-ready", "--once",
              "--toolchains", str(tmp_path / "toolchains"), "--tools-user", "nobody", "--jobs-root", str(jobs)]

    def deliver(j, payload: pathlib.Path | None = None):
        disk.write_bytes(b"\0" * size)  # frisches Transfer-Laufwerk (Host tauscht es nach jeder Antwort)
        stem = f"{j.sequence:012d}-{j.job_id}"
        if payload is not None:
            shutil.copyfile(payload, inbox / f"{stem}.payload.zip")
        (inbox / f"{stem}.job.json").write_bytes(envelope(j))
        (inbox / f"{stem}.ready").write_text(stem, encoding="ascii")
        assert main(common) == 0
        _kind, seq, data = parse_container(disk.read_bytes())
        assert seq == j.sequence and kvp.read_pool(pool)["LocalAiSignal"] == f"result:{j.sequence}"
        return json.loads(data)["result"]

    (tmp_path / "snap").mkdir()
    manifest, archive = make_snapshot(tmp_path / "snap", {"tools/probe.py": PROBE})
    snap = deliver(job(proposal("Workspace.Snapshot", [], root="X"), 1, snapshot=manifest,
                       payload_sha256=hashlib.sha256(archive.read_bytes()).hexdigest()), archive)
    assert snap["status"] == "Succeeded"
    result = deliver(job(proposal("Toolchain.Run", [], manifest.snapshot_id, toolchain="pysys",
                                  parameters={"script": "tools/probe.py"}), 2))
    log = next(t["text"] for t in result["targetResults"] if t["targetId"] == "log")
    assert "uid 65534" in log and "netz ['lo']" in log
