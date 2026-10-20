"""M9: Abhängigkeitshülle der Debian-Pakete für die Offline-Linux-VM (tools/prereq/debian_closure.py)."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("debian_closure", ROOT / "tools" / "prereq" / "debian_closure.py")
debian = importlib.util.module_from_spec(spec)
spec.loader.exec_module(debian)

PACKAGES = """Package: qemu-system-x86
Version: 1:10.0.2+ds-1
Priority: optional
Depends: libc6 (>= 2.38), libglib2.0-0t64 (>= 2.75), qemu-system-data (>> 1:10.0~), seabios | qemu-firmware, libslirp0
Filename: pool/main/q/qemu/qemu-system-x86_10.0.2+ds-1_amd64.deb
Size: 100
SHA256: aa

Package: libc6
Version: 2.41-12
Priority: required
Filename: pool/main/g/glibc/libc6_2.41-12_amd64.deb
SHA256: bb

Package: libglib2.0-0t64
Version: 2.84.4-3
Priority: optional
Depends: libc6, libffi8
Filename: pool/main/g/glib2.0/libglib2.0-0t64_2.84.4-3_amd64.deb
SHA256: cc

Package: libffi8
Version: 3.4.8-2
Priority: important
Filename: pool/main/libf/libffi/libffi8_3.4.8-2_amd64.deb
SHA256: dd

Package: qemu-system-data
Version: 1:10.0.2+ds-1
Filename: pool/main/q/qemu/qemu-system-data_10.0.2+ds-1_all.deb
SHA256: ee

Package: seabios
Version: 1.16.3-2
Provides: qemu-firmware
Filename: pool/main/s/seabios/seabios_1.16.3-2_all.deb
SHA256: ff

Package: libslirp0
Version: 4.8.0-1
Depends: libc6, libnotthere
Filename: pool/main/libs/libslirp/libslirp0_4.8.0-1_amd64.deb
SHA256: 11
"""


def test_huelle_ohne_basissystem_mit_alternativen():
    packages = debian.parse_packages(PACKAGES.replace(", libnotthere", ""))
    names = [p.name for p in debian.closure(packages, ["qemu-system-x86"])]
    assert names == ["libglib2.0-0t64", "libslirp0", "qemu-system-data", "qemu-system-x86", "seabios"]
    assert packages["qemu-system-x86"].sha256 == "aa"


def test_virtuelle_pakete_und_fehlende_abhaengigkeiten():
    packages = debian.parse_packages(PACKAGES)
    assert [p.name for p in debian.closure(packages, ["qemu-firmware"])] == ["seabios"]
    with pytest.raises(SystemExit, match="libnotthere"):
        debian.closure(packages, ["qemu-system-x86"])
