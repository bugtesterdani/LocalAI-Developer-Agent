"""Lädt Debian-Pakete samt Abhängigkeiten für die Offline-Linux-VM (M8/M9) – auf dem Host mit Internet.

Aufruf (von Get-LocalAiLinuxPrerequisites.ps1)::

    uv run --no-sync --project src/python python tools/prereq/debian_closure.py --dest <linux-gast>\\debs \
        hyperv-daemons qemu-system-x86 qemu-utils ovmf binutils

* Liest ``Packages.xz`` von trixie (main, amd64) und löst ``Depends``/``Pre-Depends`` auf (erste erfüllbare
  Alternative, virtuelle Pakete über ``Provides``).
* Pakete mit Priorität ``required``/``important`` bzw. ``Essential: yes`` gehören zum Debian-Cloud-Abbild und werden
  nicht geladen.
* Jede Datei wird gegen den SHA-256 aus ``Packages`` geprüft; ``Packages.xz`` selbst gegen den Hash in ``Release``.
  (Die Signatur von ``Release`` prüft dieses Skript nicht – Transport über HTTPS.)
"""

from __future__ import annotations

import argparse
import hashlib
import lzma
import pathlib
import re
import sys
import urllib.request
from dataclasses import dataclass, field

MIRROR = "https://deb.debian.org/debian"
SUITE = "trixie"
BASE_PRIORITIES = {"required", "important"}


@dataclass
class Package:
    name: str
    version: str
    filename: str
    sha256: str
    size: int
    priority: str = "optional"
    essential: bool = False
    depends: list[list[str]] = field(default_factory=list)  # Und-Liste von Oder-Listen (nur Namen)
    provides: list[str] = field(default_factory=list)


def _names(field_value: str) -> list[list[str]]:
    groups = []
    for part in field_value.split(","):
        alternatives = []
        for alt in part.split("|"):
            name = re.split(r"[\s(\[<:]", alt.strip(), maxsplit=1)[0]
            if name:
                alternatives.append(name)
        if alternatives:
            groups.append(alternatives)
    return groups


def parse_packages(text: str) -> dict[str, Package]:
    packages: dict[str, Package] = {}
    for stanza in text.split("\n\n"):
        fields: dict[str, str] = {}
        key = None
        for line in stanza.splitlines():
            if line.startswith((" ", "\t")) and key:
                fields[key] += "\n" + line.strip()
            elif ":" in line:
                key, value = line.split(":", 1)
                fields[key] = value.strip()
        if "Package" not in fields or "Filename" not in fields:
            continue
        package = Package(fields["Package"], fields.get("Version", ""), fields["Filename"], fields.get("SHA256", ""),
                          int(fields.get("Size", "0") or 0), fields.get("Priority", "optional"), fields.get("Essential") == "yes",
                          _names(fields.get("Pre-Depends", "")) + _names(fields.get("Depends", "")),
                          [g[0] for g in _names(fields.get("Provides", ""))])
        packages.setdefault(package.name, package)
    return packages


def closure(packages: dict[str, Package], wanted: list[str]) -> list[Package]:
    """Abhängigkeitshülle ohne Pakete des Basissystems; Reihenfolge stabil (Namen sortiert)."""
    providers: dict[str, list[str]] = {}
    for p in packages.values():
        for virtual in p.provides:
            providers.setdefault(virtual, []).append(p.name)

    def in_base(name: str) -> bool:
        p = packages.get(name)
        return p is not None and (p.priority in BASE_PRIORITIES or p.essential)

    def resolve(name: str) -> str | None:
        if name in packages:
            return name
        candidates = sorted(providers.get(name, []))
        return candidates[0] if candidates else None

    result: dict[str, Package] = {}
    missing: list[str] = []
    queue = list(wanted)
    while queue:
        name = queue.pop()
        real = resolve(name)
        if real is None:
            missing.append(name)
            continue
        if real in result or (in_base(real) and real not in wanted):
            continue
        package = packages[real]
        result[real] = package
        for group in package.depends:
            options = [resolve(n) for n in group]
            if any(o is not None and (o in result or in_base(o)) for o in options):
                continue
            chosen = next((o for o in options if o is not None), None)
            if chosen is None:
                missing.append(" | ".join(group))
            else:
                queue.append(chosen)
    if missing:
        raise SystemExit(f"Nicht auflösbar: {', '.join(sorted(set(missing)))}")
    return [result[n] for n in sorted(result)]


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Debian-Pakete mit Abhängigkeiten offline laden")
    parser.add_argument("--dest", type=pathlib.Path, required=True)
    parser.add_argument("--mirror", default=MIRROR)
    parser.add_argument("--suite", default=SUITE)
    parser.add_argument("packages", nargs="+")
    args = parser.parse_args(argv)
    release = fetch(f"{args.mirror}/dists/{args.suite}/Release").decode("utf-8")
    expected = re.search(r"^ ([0-9a-f]{64})\s+\d+\s+main/binary-amd64/Packages\.xz$", release, re.MULTILINE)
    raw = fetch(f"{args.mirror}/dists/{args.suite}/main/binary-amd64/Packages.xz")
    if expected is None or hashlib.sha256(raw).hexdigest() != expected.group(1):
        raise SystemExit("Packages.xz passt nicht zum Hash in Release")
    packages = parse_packages(lzma.decompress(raw).decode("utf-8"))
    selected = closure(packages, args.packages)
    args.dest.mkdir(parents=True, exist_ok=True)
    total = 0
    for p in selected:
        target = args.dest / pathlib.Path(p.filename).name
        if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != p.sha256:
            data = fetch(f"{args.mirror}/{p.filename}")
            if hashlib.sha256(data).hexdigest() != p.sha256:
                raise SystemExit(f"{p.name}: SHA-256 stimmt nicht")
            target.write_bytes(data)
        total += p.size
        print(f"{p.name} {p.version}")
    print(f"{len(selected)} Pakete, {total / 1024 / 1024:.1f} MB", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
