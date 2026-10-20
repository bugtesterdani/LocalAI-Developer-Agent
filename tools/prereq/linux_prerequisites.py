"""Lädt alles, was die Linux-Sandbox-VM offline braucht, und schreibt prerequisites.json (M10, Linux-Host).

Gegenstück zu ``scripts/sandbox/Get-LocalAiLinuxPrerequisites.ps1`` (Windows-Host). Läuft auf dem Host mit Internet::

    uv run --no-sync --project src/python python tools/prereq/linux_prerequisites.py --target ~/LocalAi/linux [--with-qemu]

1. Debian 13 Cloud-Abbild (generic, amd64, Rohformat), geprüft gegen SHA512SUMS
2. Python für Linux (python-build-standalone, install_only_stripped), geprüft gegen SHA256SUMS des Releases
3. Wheels des Workers für Linux (pydantic samt Abhängigkeiten, Versionen wie in src/python/.venv)
4. Debian-Pakete samt Abhängigkeiten (tools/prereq/debian_closure.py): unter QEMU keine; ``--hyperv-daemons`` für eine
   Hyper-V-VM; ``--with-qemu`` (M9, Betriebssystem-Debugging in der VM): QEMU, OVMF und binutils

Ergebnis::

    <Ziel>/debian-13-generic-amd64.raw          → sandbox.qemu.imagePath
    <Ziel>/linux-gast/prerequisites.json …      → sandbox.qemu.prerequisites (Standard: neben dem Abbild)
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
import sysconfig
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEBIAN_IMAGE = "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.raw"
RELEASES = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"


def sha(path: pathlib.Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, target: pathlib.Path) -> None:
    print(f"Lade {url}", flush=True)
    tmp = target.with_name(target.name + ".part")
    with urllib.request.urlopen(url, timeout=120) as response, open(tmp, "wb") as out:
        shutil.copyfileobj(response, out, 1 << 20)
    tmp.replace(target)


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf-8")


def debian_image(target: pathlib.Path, url: str) -> pathlib.Path:
    image = target / url.rsplit("/", 1)[1]
    sums = fetch_text(url.rsplit("/", 1)[0] + "/SHA512SUMS")
    line = next((ln for ln in sums.splitlines() if ln.endswith(" " + image.name)), None)
    if line is None:
        raise SystemExit(f"{image.name} fehlt in SHA512SUMS")
    expected = line.split()[0].lower()
    if not image.exists() or sha(image, "sha512") != expected:
        download(url, image)
        if sha(image, "sha512") != expected:
            raise SystemExit("Debian-Abbild: SHA512 stimmt nicht")
    print(f"Debian-Abbild geprüft: {image}")
    return image


def python_runtime(prereq: pathlib.Path, minor: str) -> tuple[str, str]:
    release = json.loads(fetch_text(RELEASES))
    pattern = re.compile(r"^cpython-" + re.escape(minor) + r"\.[0-9]+\+[0-9]+-x86_64-unknown-linux-gnu-install_only_stripped\.tar\.gz$")
    asset = next((a for a in release["assets"] if pattern.match(a["name"])), None)
    if asset is None:
        raise SystemExit(f"Kein python-build-standalone für Python {minor} im Release {release.get('tag_name')}")
    target = prereq / asset["name"]
    if not target.exists():
        download(asset["browser_download_url"], target)
    sums = next((a for a in release["assets"] if a["name"] == "SHA256SUMS"), None)
    if sums is not None:
        line = next((ln for ln in fetch_text(sums["browser_download_url"]).splitlines() if ln.endswith(asset["name"])), None)
        if line is not None and line.split()[0].lower() != sha(target):
            raise SystemExit("Python-Archiv: SHA256 stimmt nicht")
    version = re.match(r"^cpython-([0-9.]+)\+", asset["name"]).group(1)  # type: ignore[union-attr]
    print(f"Python für Linux: {asset['name']}")
    return asset["name"], version


def wheels(prereq: pathlib.Path, minor: str) -> None:
    venv = ROOT / "src" / "python" / ".venv" / "lib"
    # Installierte Fassung: kein .venv, pydantic liegt in der mitgelieferten Laufzeit (die dieses Skript ausführt).
    site = next(venv.glob("python3*/site-packages"), None) if venv.is_dir() else pathlib.Path(sysconfig.get_paths()["purelib"])
    if site is None:
        raise SystemExit("src/python/.venv fehlt – zuerst 'uv sync --project src/python' ausführen")
    dist = next(site.glob("pydantic-*.dist-info"), None)
    if dist is None:
        raise SystemExit(f"pydantic ist in {site} nicht installiert")
    version = dist.name.removeprefix("pydantic-").removesuffix(".dist-info")
    directory = prereq / "wheels"
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("*.whl"):
        old.unlink()
    pip = ["uvx", "--from", "pip", "pip"] if shutil.which("uvx") else [sys.executable, "-m", "pip"]
    subprocess.run([*pip, "download", f"pydantic=={version}", "--dest", str(directory),
                    "--only-binary=:all:", "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_17_x86_64",
                    "--python-version", minor, "--implementation", "cp"], check=True)


def debs(prereq: pathlib.Path, packages: list[str]) -> None:
    directory = prereq / "debs"
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("*.deb"):
        old.unlink()
    if packages:
        subprocess.run([sys.executable, str(ROOT / "tools" / "prereq" / "debian_closure.py"), "--dest", str(directory), *packages],
                       check=True)


def manifest(prereq: pathlib.Path, python: str, python_version: str) -> pathlib.Path:
    wheel_files = sorted("wheels/" + p.name for p in (prereq / "wheels").glob("*.whl"))
    deb_files = sorted("debs/" + p.name for p in (prereq / "debs").glob("*.deb"))
    data = {
        "schemaVersion": 1,
        "python": python,
        "pythonVersion": python_version,
        "wheels": wheel_files,
        "debs": deb_files,
        "sha256": {f: sha(prereq / f) for f in [python, *wheel_files, *deb_files]},
        "created": datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat(),
    }
    path = prereq / "prerequisites.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline-Bausteine für die Linux-Sandbox-VM laden")
    parser.add_argument("--target", type=pathlib.Path, required=True)
    parser.add_argument("--python-minor", default="3.13")
    parser.add_argument("--debian-image-url", default=DEBIAN_IMAGE)
    parser.add_argument("--with-qemu", action="store_true", help="QEMU, OVMF, binutils für das Betriebssystem-Debugging (M9)")
    parser.add_argument("--hyperv-daemons", action="store_true", help="hyperv-daemons (nur für eine Hyper-V-VM nötig)")
    parser.add_argument("--skip-image", action="store_true")
    args = parser.parse_args(argv)
    target = args.target.expanduser().resolve()
    prereq = target / "linux-gast"
    prereq.mkdir(parents=True, exist_ok=True)
    image = None if args.skip_image else debian_image(target, args.debian_image_url)
    python, version = python_runtime(prereq, args.python_minor)
    wheels(prereq, args.python_minor)
    packages = (["hyperv-daemons"] if args.hyperv_daemons else []) + (["qemu-system-x86", "ovmf", "binutils"] if args.with_qemu else [])
    debs(prereq, packages)
    path = manifest(prereq, python, version)
    print()
    print("Fertig. In config/localai.json unter sandbox.qemu eintragen:")
    print(f'  "imagePath": "{image or target / "debian-13-generic-amd64.raw"}"')
    print(f'  "prerequisites": "{path.parent}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
