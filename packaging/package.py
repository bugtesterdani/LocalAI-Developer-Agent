"""Paketierung: Installationsordner zusammenstellen, Konfiguration und ZIP-Archive erzeugen.

Wird von .github/workflows/release.yml aufgerufen; läuft mit jedem Python ab 3.11, ohne Zusatzpakete.

Unterbefehle::

    python packaging/package.py stage --platform windows|linux --app <veröffentlichte Programme> \\
        --runtime <Python-Laufzeit> --version 1.2.3 --out <Installationsordner>
    python packaging/package.py config-zip --version 1.2.3 --out LocalAi-Konfiguration-1.2.3.zip
    python packaging/package.py docs-zip --version 1.2.3 --out LocalAi-Dokumentation-1.2.3.zip
    python packaging/package.py icons --out <Ordner>

Aufbau des Installationsordners (gleich unter Windows und Linux, entspricht dem Repository)::

    app/              Oberfläche + Broker (.NET, eigenständig veröffentlicht)
    config/           localai.json (Standard der Installation), models.json, Beispielprojekte
    contracts/        JSON Schema (Versionsmanifest)
    runtime/python/   Python mit den Laufzeitpaketen; localai.pth zeigt auf src/python
    samples/          Beispielprojekte
    scripts/          Hyper-V-, Gast-, Toolchain- und Linux-Skripte
    src/python/localai
    toolchains/       Vorlagen für Toolchain-Ordner
    tools/prereq/     Offline-Bausteine für den Linux-Gast laden

Eigene Einstellungen gehören nicht in den Installationsordner (schreibgeschützt), sondern in den Benutzerordner:
%LOCALAPPDATA%\\LocalAi\\config bzw. ~/.config/localai (siehe docs/installation.md).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import struct
import sys
import zipfile
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Ordner bzw. Dateien des Repositorys, die zur Laufzeit gebraucht werden
STAGE_TREES = ["config", "contracts/schemas", "samples", "scripts/sandbox", "scripts/toolchains", "src/python/localai",
               "toolchains", "tools/prereq"]
LEGAL_FILES = ["LICENSE", "THIRD-PARTY-NOTICES.md"]

LINUX_SCRIPTS = ["scripts/linux/install-host.sh", "scripts/linux/get-prerequisites.sh"]
SKIP = {"__pycache__", ".pytest_cache", "bin", "obj", ".venv"}

# Python-Module der Prozesse, die der Broker startet (Abschnitt → Pfad in localai.json)
COMMANDS = [("orchestrator",), ("web", "command"), ("fakeAgent",), ("sandbox", "devWorker")]


def python_path(platform: str) -> str:
    return "runtime/python/python.exe" if platform == "windows" else "runtime/python/bin/python3"


def site_packages(runtime: pathlib.Path, platform: str) -> pathlib.Path:
    if platform == "windows":
        return runtime / "Lib" / "site-packages"
    found = sorted((runtime / "lib").glob("python3.*/site-packages"))
    if not found:
        raise SystemExit(f"site-packages nicht gefunden unter {runtime / 'lib'}")
    return found[-1]


def installed_config(platform: str, template: str | None = None) -> dict:
    """localai.json für die installierte Fassung: Prozesse starten mit dem mitgelieferten Python statt mit uv."""
    name = template or ("localai.json" if platform == "windows" else "localai.linux.json")
    config = json.loads((ROOT / "config" / name).read_text(encoding="utf-8-sig"))
    for path in COMMANDS:
        section = config
        for key in path:
            section = section.get(key) if isinstance(section, dict) else None
        if not isinstance(section, dict) or "arguments" not in section:
            continue
        arguments = section["arguments"]
        module = arguments[arguments.index("-m") + 1]
        section.update(command=python_path(platform), arguments=["-m", module], workingDirectory=".")
    return config


def copy_tree(source: pathlib.Path, target: pathlib.Path, skip: set[str] = SKIP) -> None:
    """Kopiert ohne Build- und Cache-Ordner; Programme und Python-Laufzeit mit skip=set() vollständig."""
    shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True,
                    ignore=lambda _d, names: [n for n in names if n in skip or n.endswith(".pyc")])


def stage(args: argparse.Namespace) -> None:
    out: pathlib.Path = args.out
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    copy_tree(args.app, out / "app", skip=set())
    for tree in STAGE_TREES:
        copy_tree(ROOT / tree, out / tree)
    if args.platform == "linux":
        for script in LINUX_SCRIPTS:
            (out / script).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / script, out / script)
    copy_tree(args.runtime, out / "runtime" / "python", skip={"__pycache__"})
    # Paket localai ohne Installation importierbar machen (Pfad in .pth ist relativ zu site-packages).
    site = site_packages(out / "runtime" / "python", args.platform)
    (site / "localai.pth").write_text(pathlib.Path(*[".."] * len(site.relative_to(out).parts), "src", "python").as_posix()
                                      + "\n", encoding="utf-8")
    # Standardkonfiguration der Installation: Entwicklungsmodus mit Demo-Projekt, damit der erste Start gelingt.
    config = installed_config(args.platform)
    config["sandbox"]["mode"] = "dev-local-unsafe"
    config["project"] = "config/demo-project.json"
    (out / "config" / "localai.json").write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "config" / "models.local.json").unlink(missing_ok=True)  # persönliche Datei, gehört in den Benutzerordner
    shutil.copy2(ROOT / "README.md", out / "README.md")
    for name in LEGAL_FILES:
        shutil.copy2(ROOT / name, out / name)
    copy_tree(ROOT / "licenses", out / "licenses")
    shutil.copy2(ROOT / "docs" / "lizenzen.md", out / "licenses" / "RELEASE-CHECKLIST.md")
    (out / "docs").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "docs" / "lizenzen.md", out / "docs" / "lizenzen.md")
    (out / "VERSION").write_text(args.version + "\n", encoding="utf-8")
    print(f"Installationsordner {out} ({args.platform}, {args.version})")


CONFIG_README = """Local AI Developer Agent {version} – Konfiguration
=====================================================

Diese Vorlagen gehören in deinen Benutzerordner (nicht in den Installationsordner):

  Windows:  %LOCALAPPDATA%\\LocalAi\\config\\
  Linux:    ~/.config/localai/

1. Passende Vorlage als localai.json in diesen Ordner kopieren:
     config/localai.windows.json  – Windows mit Hyper-V (MSI oder MSIX)
     config/localai.linux.json    – Linux mit QEMU (.deb)
   Pfade (imagePath, prerequisites, toolchains.directory) anpassen.
2. Optional: models.local.example.json als models.local.json daneben legen (Ollama im LAN, eigene Grenzen).
3. Eigene Projektdatei (Vorlage: config/demo-project.json) mit absoluten Pfaden anlegen und in localai.json unter
   "project" eintragen.
4. Ordner/ zeigt die empfohlene Ablage für Toolchains, Windows-Abbild und Linux-Bausteine.
   toolchains/ enthält die Vorlagen für Toolchain-Ordner (Anleitung: toolchains/README.md).

Ausführlich: Dokumentation, Kapitel „Installation“ und „Konfiguration“.
"""

FOLDERS = {
    "Ordner/Toolchains/LIESMICH.txt": "Je Toolchain ein Unterordner <name>-<version> mit toolchain.json (Vorlagen: toolchains/).\n"
                                      "Pfad in localai.json unter toolchains.directory eintragen.\n",
    "Ordner/WindowsImage/LIESMICH.txt": "Windows-ISO bzw. install.wim für die Sandbox-VM (sandbox.hyperv.imagePath).\n",
    "Ordner/Linux/LIESMICH.txt": "Debian-Abbild und linux-gast/ (sandbox.qemu bzw. sandbox.hypervLinux).\n"
                                 "Befüllen: scripts/linux/get-prerequisites.sh bzw. scripts\\sandbox\\Get-LocalAiLinuxPrerequisites.ps1\n",
    "Ordner/Projekte/LIESMICH.txt": "Eigene Projekte, die in einer Projektdatei unter grants[].root freigegeben werden.\n",
}


def zip_legal(z: zipfile.ZipFile, prefix: str) -> None:
    for name in LEGAL_FILES:
        z.write(ROOT / name, prefix + name)
    for file in sorted((ROOT / "licenses").rglob("*")):
        if file.is_file():
            z.write(file, prefix + file.relative_to(ROOT).as_posix())
    z.write(ROOT / "docs" / "lizenzen.md", prefix + "licenses/RELEASE-CHECKLIST.md")


def config_zip(args: argparse.Namespace) -> None:
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as z:
        prefix = f"LocalAi-Konfiguration-{args.version}/"
        zip_legal(z, prefix)
        z.writestr(prefix + "LIESMICH.txt", CONFIG_README.format(version=args.version))
        for platform, template in (("windows", "localai.json"), ("linux", "localai.linux.json")):
            config = installed_config(platform, template)
            config["sandbox"]["mode"] = "hyperv" if platform == "windows" else "qemu"
            z.writestr(prefix + f"config/localai.{platform}.json", json.dumps(config, indent=2, ensure_ascii=False) + "\n")
        for file in sorted((ROOT / "config").glob("*.json")):
            if file.name not in ("localai.json", "localai.linux.json", "models.local.json"):
                z.write(file, prefix + "config/" + file.name)
        for file in sorted((ROOT / "toolchains").rglob("*")):
            if file.is_file():
                z.write(file, prefix + file.relative_to(ROOT).as_posix())
        for name, text in FOLDERS.items():
            z.writestr(prefix + name, text)
    print(f"Konfiguration: {args.out}")


def docs_zip(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(ROOT / "tools" / "wiki"))
    import build_wiki  # noqa: PLC0415 – gleiche Seitenliste wie das Wiki

    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as z:
        prefix = f"LocalAi-Dokumentation-{args.version}/"
        zip_legal(z, prefix)
        if "docs/lizenzen.md" not in build_wiki.PAGES:
            z.write(ROOT / "docs" / "lizenzen.md", prefix + "docs/lizenzen.md")
        z.write(ROOT / "README.md", prefix + "README.md")
        for source in build_wiki.PAGES:
            if source != "README.md":
                z.write(ROOT / source, prefix + source)
    print(f"Dokumentation: {args.out}")


def png(size: int) -> bytes:
    """Einfaches Programmsymbol (blaues Quadrat mit hellem Rahmen) ohne Bildbibliothek."""
    border = max(1, size // 10)
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            edge = min(x, y, size - 1 - x, size - 1 - y) < border
            row += bytes((0xE8, 0xF0, 0xFE, 0xFF) if edge else (0x1F, 0x4E, 0x9B, 0xFF))
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")


def icons(args: argparse.Namespace) -> None:
    args.out.mkdir(parents=True, exist_ok=True)
    for name, size in {"Square44x44Logo.png": 44, "Square150x150Logo.png": 150, "StoreLogo.png": 50,
                       "localai.png": 256}.items():
        (args.out / name).write_bytes(png(size))
    # ICO mit eingebettetem PNG (ab Windows Vista) für MSI und Verknüpfung
    image = png(256)
    (args.out / "localai.ico").write_bytes(struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(image), 22)
                                           + image)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="package")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("stage")
    p.add_argument("--platform", choices=["windows", "linux"], required=True)
    p.add_argument("--app", type=pathlib.Path, required=True)
    p.add_argument("--runtime", type=pathlib.Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--out", type=pathlib.Path, required=True)
    p.set_defaults(run=stage)
    for name, run in (("config-zip", config_zip), ("docs-zip", docs_zip)):
        p = sub.add_parser(name)
        p.add_argument("--version", required=True)
        p.add_argument("--out", type=pathlib.Path, required=True)
        p.set_defaults(run=run)
    p = sub.add_parser("icons")
    p.add_argument("--out", type=pathlib.Path, required=True)
    p.set_defaults(run=icons)
    args = parser.parse_args(argv)
    args.run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
