"""Collect original licensing evidence from resolved build inputs; no network or package execution."""
from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
LEGAL = re.compile(r"^(licen[cs]e|copying|copyright|notice|third[-_. ]?party[-_. ]?(notices|licenses))($|[._-])", re.I)
TEXT_SUFFIXES = {"", ".txt", ".md", ".rst", ".html", ".htm"}
KNOWN_PERMISSIVE = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "0BSD",
                    "ISC", "PSF-2.0", "OFL-1.1", "CC0-1.0", "Unlicense", "Zlib"}


class LicenseError(ValueError):
    pass


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def child(root: Path, relative: str) -> Path:
    """Metadata paths must never escape their package, including via symlinks."""
    path = (root / relative.replace("\\", "/")).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise LicenseError(f"Missing or unsafe license file: {root.name}/{relative}")
    return path


def legal_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()
                  and LEGAL.match(p.name) and p.suffix.lower() in TEXT_SUFFIXES
                  and p.resolve().is_relative_to(root.resolve()))


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", value):
        raise LicenseError(f"Unsafe package name/version: {value!r}")
    return value.lower()


class Collector:
    def __init__(self, stage: Path, overrides: Path | None = None):
        self.stage = stage
        self.overrides = overrides or ROOT / "licenses" / "overrides"
        self.destination = stage / "licenses" / "collected"
        self.entries: list[dict] = []
        self.seen: set[tuple[str, str, str]] = set()

    def add(self, ecosystem: str, name: str, version: str, declared: str,
            source: Path, documents: list[Path]) -> None:
        name, version = identifier(name), identifier(version)
        key = (ecosystem, name, version)
        if key in self.seen:
            return
        review = None
        if not declared.strip() or not documents:
            override = self.overrides / ecosystem / name / version
            review_path = override / "review.json"
            if review_path.is_file():
                review = read_json(review_path)
                if not all(isinstance(review.get(k), str) and review[k].strip()
                           for k in ("license", "source", "reason")) or not review["source"].startswith("https://"):
                    raise LicenseError(f"Invalid reviewed override: {review_path}")
                declared = review["license"]
                source = override
                documents = legal_files(override)
        if not declared.strip() or not documents:
            raise LicenseError(f"{ecosystem}:{name}@{version}: missing license declaration/original text; "
                               "supply the original package notices or a version-specific reviewed override")
        target = self.destination / ecosystem / name / version
        record = {"ecosystem": ecosystem, "name": name, "version": version,
                  "declaredLicense": declared, "files": []}
        if review is not None:
            record["review"] = review
        if declared not in KNOWN_PERMISSIVE:
            record["reviewRequired"] = "Check full terms, embedded components and compatibility; no automatic legal approval."
        for document in sorted(set(documents)):
            document = child(source, document.relative_to(source).as_posix())
            if not document.stat().st_size:
                raise LicenseError(f"Empty license text: {document}")
            # Preserve original filenames, contents and relative directory structure.
            dest = target / document.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(document, dest)
            record["files"].append({"path": dest.relative_to(self.stage).as_posix(),
                                    "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()})
        self.entries.append(record)
        self.seen.add(key)

    def python_packages(self, site: Path) -> None:
        distributions = sorted(site.glob("*.dist-info"))
        if not distributions:
            raise LicenseError(f"No installed Python distribution metadata in {site}")
        for dist in distributions:
            metadata = Parser().parsestr((dist / "METADATA").read_text(encoding="utf-8"))
            name, version = metadata.get("Name", ""), metadata.get("Version", "")
            declared = metadata.get("License-Expression") or metadata.get("License", "")
            if not declared or declared.strip().upper() in {"UNKNOWN", "UNKNOWN LICENSE"}:
                declared = ""
                for classifier in metadata.get_all("Classifier", []):
                    if classifier.startswith("License ::"):
                        declared = {"MIT License": "MIT", "BSD License": "BSD-3-Clause",
                                    "Apache Software License": "Apache-2.0"}.get(classifier.split(" :: ")[-1], "")
                        if declared:
                            break
            docs = legal_files(dist)
            for filename in metadata.get_all("License-File", []):
                # PEP 639 puts these paths under dist-info/licenses; legacy wheels at dist-info root.
                locations = [dist / "licenses" / filename, dist / filename]
                present = next((p for p in locations if p.is_file()), None)
                if present is None:
                    raise LicenseError(f"{name}: declared License-File missing: {filename}")
                docs.append(child(dist, present.relative_to(dist).as_posix()))
            self.add("python", name, version, declared, dist, docs)

    def nuget_package(self, name: str, version: str, folders: list[Path], relative: str = "") -> None:
        if name.lower().startswith("runtimepack."):
            name = name[len("runtimepack."):]
        candidates = [base / relative for base in folders] if relative else []
        candidates += [base / name.lower() / version.lower() for base in folders]
        package = next((p for p in candidates if p.is_dir() and list(p.glob("*.nuspec"))), None)
        if package is None:
            raise LicenseError(f"NuGet package missing from build cache: {name}/{version}")
        metadata = ET.parse(next(package.glob("*.nuspec"))).getroot()
        license_node = next((node for node in metadata.iter() if node.tag.split("}")[-1] == "license"), None)
        declared = ""
        docs = legal_files(package)
        if license_node is not None:
            if license_node.attrib.get("type") == "file":
                docs.append(child(package, (license_node.text or "").strip()))
                declared = "LicenseRef-PackageFile"
            else:
                declared = (license_node.text or "").strip()
        self.add("nuget", name, version, declared, package, docs)

    def nuget(self, assets: list[Path]) -> None:
        folders: list[Path] = []
        count = 0
        for asset in assets:
            data = read_json(asset)
            roots = [Path(p) for p in data.get("packageFolders", {})]
            folders.extend(p for p in roots if p not in folders)
            for key, library in sorted(data.get("libraries", {}).items()):
                if library.get("type") == "package":
                    name, version = key.rsplit("/", 1)
                    self.nuget_package(name, version, roots, library.get("path", ""))
                    count += 1
        if not count:
            raise LicenseError("No resolved NuGet packages; publish the application first")
        # Self-contained runtime packs are not always present in assets.libraries.
        runtime_packs = 0
        for deps in sorted((self.stage / "app").glob("*.deps.json")):
            for key, library in read_json(deps).get("libraries", {}).items():
                if library.get("type") == "runtimepack":
                    name, version = key.rsplit("/", 1)
                    self.nuget_package(name, version, folders)
                    runtime_packs += 1
                elif library.get("type") == "package":
                    name, version = key.rsplit("/", 1)
                    self.nuget_package(name, version, folders, library.get("path", ""))
        if not runtime_packs:
            raise LicenseError("No self-contained .NET runtime pack found in published deps.json")

    def runtime(self, runtime: Path) -> None:
        # uv's managed Python is python-build-standalone; preserve its actual bundled notices.
        docs = [p for p in legal_files(runtime) if "site-packages" not in p.parts]
        if not docs:
            raise LicenseError("Python runtime has no original licensing evidence; include distribution licenses")
        # Version comes from the installed stdlib, without starting this interpreter.
        versions = sorted((runtime / "lib").glob("python3.*")) if (runtime / "lib").exists() else []
        version = versions[-1].name.removeprefix("python") if versions else runtime.name
        self.add("runtime", "python-distribution", version, "PSF-2.0 and bundled component licenses", runtime, docs)

    def write(self) -> None:
        self.entries.sort(key=lambda e: (e["ecosystem"], e["name"], e["version"]))
        report = {"schemaVersion": 1, "scope": "Resolved build inputs; original notices, not a legal clearance",
                  "components": self.entries}
        (self.stage / "THIRD-PARTY-INVENTORY.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        lines = ["# Third-party inventory", "",
                 "Original notices are retained at the listed paths. This is not a legal clearance.", ""]
        for entry in self.entries:
            lines += [f"## {entry['ecosystem']}: {entry['name']} {entry['version']}", "",
                      f"Declared license: {entry['declaredLicense']}", ""]
            if entry.get("reviewRequired"):
                lines += [entry["reviewRequired"], ""]
            for doc in entry["files"]:
                lines.append(f"- [{doc['path']}]({doc['path']}) (SHA-256: {doc['sha256']})")
            lines.append("")
        (self.stage / "THIRD-PARTY-INVENTORY.md").write_text("\n".join(lines), encoding="utf-8")


def verify(stage: Path) -> None:
    for required in ("LICENSE", "THIRD-PARTY-NOTICES.md", "licenses/Inter-OFL-1.1.txt"):
        child(stage, required)
    report = read_json(stage / "THIRD-PARTY-INVENTORY.json")
    components = report.get("components", [])
    if report.get("schemaVersion") != 1 or not components:
        raise LicenseError("Missing or empty third-party inventory")
    if not {"python", "nuget", "runtime"} <= {c.get("ecosystem") for c in components}:
        raise LicenseError("Inventory does not cover all three runtime ecosystems")
    for component in components:
        if not component.get("files") or not component.get("declaredLicense"):
            raise LicenseError("Incomplete component record")
        for doc in component["files"]:
            path = child(stage, doc["path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != doc["sha256"]:
                raise LicenseError(f"License evidence changed: {doc['path']}")
    child(stage, "THIRD-PARTY-INVENTORY.md")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--assets", type=Path, action="append")
    args = parser.parse_args()
    if args.verify:
        verify(args.stage)
        return
    if args.runtime is None or not args.assets:
        parser.error("--runtime and --assets are required when collecting")
    collector = Collector(args.stage)
    if collector.destination.exists():
        shutil.rmtree(collector.destination)
    # Delete previous reports so a failed collection cannot leave an apparently valid inventory.
    for filename in ("THIRD-PARTY-INVENTORY.json", "THIRD-PARTY-INVENTORY.md"):
        (args.stage / filename).unlink(missing_ok=True)
    site = args.stage / "runtime" / "python" / "Lib" / "site-packages"
    if not site.is_dir():
        matches = sorted((args.stage / "runtime" / "python" / "lib").glob("python3.*/site-packages"))
        if not matches:
            raise LicenseError("Staged Python site-packages missing")
        site = matches[-1]
    collector.python_packages(site)
    collector.nuget(args.assets)
    collector.runtime(args.runtime)
    collector.write()
    verify(args.stage)


if __name__ == "__main__":
    try:
        main()
    except (LicenseError, OSError, ValueError, ET.ParseError) as error:
        raise SystemExit(f"License collection failed: {error}") from error
