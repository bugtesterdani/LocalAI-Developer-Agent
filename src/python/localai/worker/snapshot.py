"""Import und Zugriff auf Snapshots in der Sandbox.

Ein Snapshot ist ein ZIP mit regulären Dateien plus ein Manifest (Pfad, Größe, SHA-256).
Der Worker liefert ausschließlich Dateien aus, die im geprüften Manifest stehen; er folgt
keinen Verknüpfungen und durchsucht nie das Dateisystem außerhalb des Snapshots.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import stat
import zipfile
from dataclasses import dataclass

from localai import canonical
import datetime as dt

from localai.contracts import SnapshotFile, SnapshotManifest
from localai.pathrules import PathRuleError, normalize_relative

MAX_FILES = 50_000
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


class SnapshotError(Exception):
    """Snapshot ist ungültig oder unbekannt."""


def snapshot_id(grant_id: str, files: list[dict]) -> str:
    """SHA-256 über das kanonische JSON von {grantId, files} – identisch zu SnapshotBuilder (C#)."""
    return canonical.sha256_hex({"grantId": grant_id, "files": files})


@dataclass(frozen=True)
class SnapshotIndex:
    snapshot_id: str
    grant_id: str
    root: pathlib.Path
    files: dict[str, tuple[int, str]]  # Pfad -> (Größe, SHA-256)

    def resolve(self, rel_path: str) -> pathlib.Path:
        if rel_path not in self.files:
            raise SnapshotError(f"Datei nicht im Snapshot: {rel_path}")
        return self.root.joinpath(*rel_path.split("/"))


class SnapshotStore:
    """Verwaltet importierte Snapshots unter ``<work>/snapshots``."""

    def __init__(self, work: pathlib.Path) -> None:
        self.base = work / "snapshots"
        self.base.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, SnapshotIndex] = {}

    def get(self, snap_id: str, grant_id: str) -> SnapshotIndex:
        index = self._cache.get(snap_id)
        if index is None:
            manifest_path = self.base / f"{snap_id}.manifest.json"
            if not manifest_path.exists():
                raise SnapshotError(f"Snapshot unbekannt: {snap_id}")
            index = self._load(SnapshotManifest.model_validate_json(manifest_path.read_text(encoding="utf-8")))
        if index.grant_id != grant_id:
            raise SnapshotError("Snapshot gehört zu einem anderen Ordner-Grant")
        return index

    def _load(self, manifest: SnapshotManifest) -> SnapshotIndex:
        index = SnapshotIndex(
            manifest.snapshot_id, manifest.grant_id, self.base / manifest.snapshot_id,
            {f.path: (f.size, f.sha256) for f in manifest.files},
        )
        self._cache[manifest.snapshot_id] = index
        return index

    def import_archive(self, manifest: SnapshotManifest, archive: pathlib.Path) -> SnapshotIndex:
        """Prüft Manifest und Archiv vollständig und legt den Snapshot schreibgeschützt ab."""
        files = [f.to_json_obj() for f in manifest.files]
        if snapshot_id(manifest.grant_id, files) != manifest.snapshot_id:
            raise SnapshotError("snapshotId passt nicht zum Manifest")
        if len(files) > MAX_FILES:
            raise SnapshotError("Zu viele Dateien im Snapshot")
        expected: dict[str, tuple[int, str]] = {}
        total = 0
        for f in manifest.files:
            try:
                norm = normalize_relative(f.path)
            except PathRuleError as err:
                raise SnapshotError(f"Unzulässiger Pfad im Manifest: {f.path}: {err}") from err
            if norm != f.path or norm in expected:
                raise SnapshotError(f"Pfad nicht normalisiert oder doppelt: {f.path}")
            if f.size < 0:
                raise SnapshotError("Negative Dateigröße")
            total += f.size
            expected[norm] = (f.size, f.sha256)
        if total > MAX_TOTAL_BYTES:
            raise SnapshotError("Snapshot zu groß")

        final = self.base / manifest.snapshot_id
        if final.exists():
            return self.get(manifest.snapshot_id, manifest.grant_id)
        partial = self.base / f"{manifest.snapshot_id}.partial"
        if partial.exists():
            _remove_tree(partial)
        partial.mkdir()
        try:
            self._extract(archive, partial, expected)
            for path in partial.rglob("*"):
                if path.is_file():
                    path.chmod(stat.S_IREAD)
            partial.replace(final)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        (self.base / f"{manifest.snapshot_id}.manifest.json").write_text(
            json.dumps(manifest.to_json_obj(), ensure_ascii=False), encoding="utf-8")
        return self._load(manifest)

    def derive(self, base: SnapshotIndex, changes: dict[str, bytes | None]) -> SnapshotIndex:
        """Abgeleiteter, ebenfalls unveränderlicher Snapshot: ``base`` plus geänderte/neue Dateien, ``None`` = gelöscht.

        Unveränderte Dateien werden – wenn möglich – als harte Verknüpfung übernommen (gleicher Inhalt, kein Platz).
        """
        files = dict(base.files)
        for path, data in changes.items():
            if data is None:
                files.pop(path, None)
            else:
                files[path] = (len(data), hashlib.sha256(data).hexdigest())
        if len(files) > MAX_FILES:
            raise SnapshotError("Zu viele Dateien im Snapshot")
        entries = [{"path": p, "size": size, "sha256": sha} for p, (size, sha) in sorted(files.items())]
        new_id = snapshot_id(base.grant_id, entries)
        if (self.base / new_id).exists() and (self.base / f"{new_id}.manifest.json").exists():
            return self.get(new_id, base.grant_id)
        partial = self.base / f"{new_id}.partial"
        if partial.exists():
            _remove_tree(partial)
        partial.mkdir()
        try:
            for path in files:
                dest = partial.joinpath(*path.split("/"))
                dest.parent.mkdir(parents=True, exist_ok=True)
                if path in changes:
                    dest.write_bytes(changes[path])  # type: ignore[arg-type]
                else:
                    src = base.resolve(path)
                    try:
                        os.link(src, dest)
                    except OSError:
                        shutil.copyfile(src, dest)
                if not dest.is_symlink():
                    dest.chmod(stat.S_IREAD)
            partial.replace(self.base / new_id)
        except BaseException:
            _remove_tree(partial)
            raise
        manifest = SnapshotManifest(
            schema_version=1, snapshot_id=new_id, grant_id=base.grant_id,
            created_at=dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            files=[SnapshotFile(path=e["path"], size=e["size"], sha256=e["sha256"]) for e in entries])
        (self.base / f"{new_id}.manifest.json").write_text(
            json.dumps(manifest.to_json_obj(), ensure_ascii=False), encoding="utf-8")
        return self._load(manifest)

    @staticmethod
    def _extract(archive: pathlib.Path, target: pathlib.Path, expected: dict[str, tuple[int, str]]) -> None:
        seen: set[str] = set()
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    raise SnapshotError(f"Verzeichniseintrag im Archiv nicht erlaubt: {info.filename}")
                mode = (info.external_attr >> 16) & 0o170000
                if mode not in (0, stat.S_IFREG):
                    raise SnapshotError(f"Nur reguläre Dateien erlaubt (Verknüpfung?): {info.filename}")
                try:
                    name = normalize_relative(info.filename)
                except PathRuleError as err:
                    raise SnapshotError(f"Unzulässiger Name im Archiv: {info.filename}: {err}") from err
                if name not in expected or name in seen:
                    raise SnapshotError(f"Archiv enthält Datei außerhalb des Manifests: {name}")
                seen.add(name)
                size, sha = expected[name]
                dest = target.joinpath(*name.split("/"))
                dest.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                written = 0
                with zf.open(info) as src, open(dest, "xb") as out:
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        written += len(chunk)
                        if written > size:
                            raise SnapshotError(f"Datei größer als im Manifest: {name}")
                        digest.update(chunk)
                        out.write(chunk)
                if written != size or digest.hexdigest() != sha:
                    raise SnapshotError(f"Inhalt stimmt nicht mit dem Manifest überein: {name}")
        missing = set(expected) - seen
        if missing:
            raise SnapshotError(f"Archiv unvollständig, es fehlen {len(missing)} Datei(en), z. B. {sorted(missing)[0]}")
        if os.name != "nt" and any(p.is_symlink() for p in target.rglob("*")):
            raise SnapshotError("Verknüpfung im entpackten Snapshot gefunden")


def _remove_tree(path: pathlib.Path) -> None:
    """Löscht einen Ordner mit schreibgeschützten Dateien (Windows)."""
    def onerror(func, p, _exc):  # noqa: ANN001
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onexc=onerror)
