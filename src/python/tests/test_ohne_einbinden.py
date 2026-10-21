"""Rechner ohne Einbinden auf dem Host (hostMount: false) und ohne Copy-VMFile (inbound: Disk): Eingangs-Laufwerk im
Worker, Seed aus der ISO im Linux-Gast. Die C#-Seite (VHDX/ISO schreiben): OhneEinbindenTests."""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
import struct
import tarfile

import pytest

from localai.guest import provision
from localai.worker import inbound


def _disk(path: pathlib.Path, sequence: int, files: dict[str, bytes], size: int = 32 * 1024 * 1024, digest: bytes | None = None,
          extra: tarfile.TarInfo | None = None) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if extra is not None:
            tar.addfile(extra)
    data = buf.getvalue()
    header = bytearray(4096)
    header[:8] = inbound.MAGIC
    struct.pack_into("<I", header, 8, 1)
    struct.pack_into("<QQ", header, 16, sequence, len(data))
    header[32:64] = digest or hashlib.sha256(data).digest()
    with open(path, "wb") as f:
        f.truncate(size)
        f.write(bytes(header) + data)


def _disks(folder: pathlib.Path) -> inbound.InboundDisks:
    return inbound.InboundDisks(folder / "state.json", lambda: [str(p) for p in sorted(folder.glob("sd*"))])


def test_eingangs_laufwerk_wird_einmal_uebernommen_ready_zuletzt(tmp_path):
    _disk(tmp_path / "sdb", 3, {"000000000003-a.ready": b"r", "000000000003-a.job.json": b"{}", "000000000003-a.payload.zip": b"z"})
    disks = _disks(tmp_path)
    taken = disks.poll(tmp_path / "inbox")
    assert taken[-1] == "000000000003-a.ready" and len(taken) == 3
    assert (tmp_path / "inbox" / "000000000003-a.payload.zip").read_bytes() == b"z"
    assert not list((tmp_path / "inbox").glob(".*"))
    assert disks.poll(tmp_path / "inbox") == []  # gleiche Sequenz nie zweimal
    # Neustart des Workers: Sequenz bleibt gemerkt.
    assert _disks(tmp_path).poll(tmp_path / "inbox") == []
    _disk(tmp_path / "sdb", 4, {"000000000004-b.job.json": b"{}"})
    assert _disks(tmp_path).poll(tmp_path / "inbox") == ["000000000004-b.job.json"]


def test_andere_laufwerke_falsche_pruefsumme_und_boese_namen_werden_ignoriert(tmp_path):
    (tmp_path / "sda").write_bytes(b"\xeb\x63\x90" + bytes(8189))  # Systemplatte
    _disk(tmp_path / "sdb", 5, {"x.job.json": b"{}"}, digest=b"\0" * 32)
    evil = tarfile.TarInfo("../../etc/passwd")
    _disk(tmp_path / "sdc", 6, {}, extra=evil)
    link = tarfile.TarInfo("link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/shadow"
    _disk(tmp_path / "sdd", 7, {}, extra=link)
    assert _disks(tmp_path).poll(tmp_path / "inbox") == []
    assert not (tmp_path / "inbox").exists() or not any((tmp_path / "inbox").iterdir())
    assert not (tmp_path.parent / "etc").exists()


def test_kopf_wird_streng_gelesen():
    assert inbound.parse_header(bytes(4096)) is None
    header = bytearray(4096)
    header[:8] = inbound.MAGIC
    struct.pack_into("<I", header, 8, 2)
    with pytest.raises(inbound.InboundError, match="Version"):
        inbound.parse_header(bytes(header))
    struct.pack_into("<I", header, 8, 1)
    struct.pack_into("<QQ", header, 16, 1, inbound.MAX_TAR + 1)
    with pytest.raises(inbound.InboundError, match="zu groß"):
        inbound.parse_header(bytes(header))


def test_eingebundene_laufwerke_werden_nie_abgefragt(tmp_path):
    sys_block = tmp_path / "sys"
    for name in ("sda", "sdb", "sdc", "vda"):
        (sys_block / name).mkdir(parents=True)
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/sda1 / ext4 rw 0 0\n/dev/vda /data ext4 rw 0 0\n/dev/sr0 /mnt iso9660 ro 0 0\n")
    assert inbound.linux_disks(sys_block, pathlib.Path("/dev"), mounts) == ["/dev/sdb", "/dev/sdc"]


def test_worker_bekommt_eingangs_laufwerk_nur_mit_inbound_disk():
    paths = provision.Paths()
    assert "--inbound-disk" not in provision.worker_command({"transferDiskBytes": 1}, paths)
    command = provision.worker_command({"transferDiskBytes": 1, "inbound": "Disk"}, paths)
    assert command[command.index("--inbound-disk") + 1] == "auto"


def test_seed_aus_der_iso_wird_nach_der_einrichtung_geloescht(tmp_path):
    seed = tmp_path / "localai-seed"
    (seed / "localai").mkdir(parents=True)
    (seed / "localai" / "session.key").write_text("geheim")
    assert provision.retire_seed(seed, tmp_path / "cloud") is None
    assert not seed.exists()
    assert (tmp_path / "cloud" / "cloud-init.disabled").exists()


def test_gastkonfiguration_traegt_inbound(tmp_path):
    config = tmp_path / "guest-config.json"
    config.write_text(json.dumps({"guest": "Linux", "transferDiskBytes": 1, "inbound": "Disk"}))
    assert provision.load_config(config)["inbound"] == "Disk"
