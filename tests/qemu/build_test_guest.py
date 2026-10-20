"""Baut ein initramfs als Test-Gast für die QEMU-Sandbox (M10) – ohne Debian-Abbild und ohne Netz.

Inhalt: busybox, die Python-Laufzeit der Entwicklungsumgebung (python-build-standalone, wie im echten Gast), die
Worker-Pakete (pydantic …), das Paket ``localai`` und ``guest_init.py``. Der Test-Gast macht im Kleinen, was im echten
Gast cloud-init und ``provision.sh`` tun: Seed-Laufwerk lesen, Verbindungskanal starten, Bericht übers
Transfer-Laufwerk, Worker starten. So laufen Host-Seite (QemuVmHost), Verbindungskanal, Laufwerkstausch und Worker
gegen einen echten QEMU.

Aufruf::

    python tests/qemu/build_test_guest.py --out <initrd.gz>
"""

from __future__ import annotations

import argparse
import gzip
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGES = ["pydantic", "pydantic_core", "annotated_types", "typing_extensions", "typing_inspection"]
SKIP = {"__pycache__", "test", "tests", "idlelib", "tkinter", "turtledemo", "ensurepip", "lib2to3", "pydoc_data", "config-3"}

INIT = r"""#!/bin/busybox sh
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
mkdir -p /dev/virtio-ports /tmp /var/lib/hyperv /mnt/localai-seed
for p in /sys/class/virtio-ports/*; do
    [ -f "$p/name" ] && ln -sf "/dev/$(basename $p)" "/dev/virtio-ports/$(cat $p/name)"
done
[ -f /lib/nls_iso8859-1.ko ] && insmod /lib/nls_iso8859-1.ko
for b in /sys/block/vd*; do
    [ "$(cat $b/serial 2>/dev/null)" = "localai-seed" ] && mount -t vfat -o ro "/dev/$(basename $b)" /mnt/localai-seed
done
echo "LocalAi-Testgast: init"
exec /opt/localai/python/bin/python3 -I /guest_init.py
"""


def libraries(binaries: list[pathlib.Path]) -> set[pathlib.Path]:
    found: set[pathlib.Path] = set()
    for binary in binaries:
        out = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, check=False).stdout
        for line in out.splitlines():
            parts = line.split()
            for part in parts:
                if part.startswith("/") and pathlib.Path(part).exists():
                    found.add(pathlib.Path(part))
    return found


def add_tree(source: pathlib.Path, target: pathlib.Path) -> None:
    for root, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith("config-3")]
        rel = pathlib.Path(root).relative_to(source)
        (target / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            if name.endswith((".pyc", ".a")):
                continue
            src = pathlib.Path(root) / name
            if src.is_symlink():
                os.symlink(os.readlink(src), target / rel / name)
            else:
                shutil.copy2(src, target / rel / name)


def nls_module(kernel: pathlib.Path | None) -> bytes | None:
    """vfat braucht den Zeichensatz iso8859-1; im Ubuntu-Kern ist er ein (zstd-gepacktes) Modul neben dem Kern."""
    if kernel is None:
        return None
    version = kernel.name.removeprefix("vmlinuz-")
    found = list((kernel.parent.parent / "lib" / "modules" / version).rglob("nls_iso8859-1.ko*"))
    if not found:
        return None
    data = found[0].read_bytes()
    if found[0].suffix == ".zst":
        from compression import zstd  # Python 3.14+

        data = zstd.decompress(data)
    return data


def build(out: pathlib.Path, kernel: pathlib.Path | None = None) -> None:
    venv = ROOT / "src" / "python" / ".venv"
    home = next(line.split("=", 1)[1].strip() for line in (venv / "pyvenv.cfg").read_text().splitlines()
                if line.startswith("home"))
    prefix = pathlib.Path(home).resolve().parent
    site = next((venv / "lib").glob("python3*/site-packages"))
    busybox = shutil.which("busybox") or "/bin/busybox"
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "root"
        for d in ("bin", "dev", "proc", "sys", "tmp", "lib64", "opt/localai"):
            (root / d).mkdir(parents=True, exist_ok=True)
        shutil.copy2(busybox, root / "bin" / "busybox")
        python = root / "opt" / "localai" / "python"
        add_tree(prefix, python)
        py_site = next((python / "lib").glob("python3*")) / "site-packages"
        py_site.mkdir(parents=True, exist_ok=True)
        for entry in site.iterdir():
            base = entry.name.split("-")[0].split(".")[0]
            if base in PACKAGES:
                (add_tree(entry, py_site / entry.name) if entry.is_dir() else shutil.copy2(entry, py_site / entry.name))
        localai = ROOT / "src" / "python" / "localai"
        add_tree(localai, py_site / "localai")
        shutil.rmtree(py_site / "localai" / "orchestrator", ignore_errors=True)
        binaries = [pathlib.Path(prefix / "bin").resolve() / p.name for p in (prefix / "bin").glob("python3.*") if not p.name.endswith("-config")]
        binaries += list(python.rglob("*.so"))
        for lib in libraries(binaries):
            if str(lib).startswith(str(prefix)) or str(lib).startswith(str(python)):
                continue
            dest = root / lib.relative_to("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(lib.resolve(), dest)
        if (module := nls_module(kernel)) is not None:
            (root / "lib").mkdir(exist_ok=True)
            (root / "lib" / "nls_iso8859-1.ko").write_bytes(module)
        (root / "init").write_text(INIT, encoding="utf-8")
        os.chmod(root / "init", 0o755)
        shutil.copy2(pathlib.Path(__file__).with_name("guest_init.py"), root / "guest_init.py")
        listing = subprocess.run(["find", "."], cwd=root, capture_output=True, check=True).stdout
        archive = subprocess.run(["cpio", "-o", "-H", "newc", "--quiet"], cwd=root, input=listing, capture_output=True, check=True).stdout
        out.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(out, "wb", compresslevel=1) as f:
            f.write(archive)
    print(f"{out} ({out.stat().st_size // 1024 // 1024} MB)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--kernel", type=pathlib.Path, help="Kern, dessen Module (nls_iso8859-1) mitkommen")
    args = parser.parse_args()
    build(args.out, args.kernel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
