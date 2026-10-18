"""Unified Diffs lesen und tolerant auf Snapshot-Dateien anwenden (Patch.Apply, B08).

Modelle liefern Diffs mit falschen Zeilennummern oder Zeilenzahlen. Deshalb gilt:

* Kopfzeilen ``--- a/<pfad>`` bzw. ``--- /dev/null`` und ``+++ b/<pfad>`` bzw. ``+++ /dev/null`` sind Pflicht.
* Die Zählungen im ``@@``-Kopf werden ignoriert; ein Hunk endet an der nächsten Kopfzeile.
* Ein Hunk wird an der Stelle angewendet, an der seine Kontext- und Entfernt-Zeilen exakt (sonst ohne
  Leerzeichen am Zeilenende) stehen – bei mehreren Treffern an der, die der angegebenen Zeile am nächsten liegt.
  Passt nichts, werden bis zu zwei Kontextzeilen am Rand weggelassen. Passt dann noch nichts, scheitert der Hunk.
* Zeilenenden (CRLF/LF), BOM und ein fehlender Zeilenumbruch am Dateiende bleiben erhalten.

Umbenennungen und Binärpatches werden abgelehnt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_PATCH_CHARS = 1024 * 1024
MAX_FUZZ = 2

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_LOOSE_HUNK = re.compile(r"^@@.*@@")


class PatchError(Exception):
    """Diff ist unlesbar oder lässt sich nicht anwenden (Meldung geht an den Agenten)."""


@dataclass
class Hunk:
    old_start: int
    lines: list[str] = field(default_factory=list)  # mit Präfix ' ', '-', '+'
    no_newline_old: bool = False
    no_newline_new: bool = False

    @property
    def old(self) -> list[str]:
        return [ln[1:] for ln in self.lines if ln[0] in " -"]

    @property
    def new(self) -> list[str]:
        return [ln[1:] for ln in self.lines if ln[0] in " +"]


@dataclass
class FilePatch:
    old_path: str | None  # None = neue Datei
    new_path: str | None  # None = Datei löschen
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def kind(self) -> str:
        if self.old_path is None:
            return "create"
        if self.new_path is None:
            return "delete"
        return "modify"


def _header_path(raw: str, prefix: str) -> str | None:
    value = raw.split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if value.startswith(prefix):
        value = value[len(prefix):]
    value = value.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    if not value:
        raise PatchError("Leerer Pfad in einer Kopfzeile")
    return value


def parse(text: str) -> list[FilePatch]:
    """Zerlegt einen Unified Diff in Dateien und Hunks."""
    if len(text) > MAX_PATCH_CHARS:
        raise PatchError(f"Patch zu groß (höchstens {MAX_PATCH_CHARS} Zeichen)")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    files: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(("rename from", "rename to", "copy from", "copy to")):
            raise PatchError("Umbenennen/Kopieren wird nicht unterstützt – als Löschen + Neuanlegen schreiben")
        if line.startswith(("GIT binary patch", "Binary files")):
            raise PatchError("Binärpatches werden nicht unterstützt")
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            current = FilePatch(_header_path(line[4:], "a/"), _header_path(lines[i + 1][4:], "b/"))
            if current.old_path is None and current.new_path is None:
                raise PatchError("Kopfzeilen --- /dev/null und +++ /dev/null zugleich")
            files.append(current)
            hunk = None
            i += 2
            continue
        if line.startswith("@@"):
            if current is None:
                raise PatchError("Hunk ohne Kopfzeilen '--- a/<pfad>' und '+++ b/<pfad>'")
            match = _HUNK.match(line)
            if match is None and not _LOOSE_HUNK.match(line):
                raise PatchError(f"Ungültiger Hunk-Kopf: {line[:80]}")
            hunk = Hunk(int(match.group(1)) if match else 0)
            current.hunks.append(hunk)
            i += 1
            continue
        if hunk is not None:
            if line.startswith("\\"):
                # "\ No newline at end of file" gilt für die Zeile davor.
                if hunk.lines:
                    last = hunk.lines[-1][0]
                    if last in " -":
                        hunk.no_newline_old = True
                    if last in " +":
                        hunk.no_newline_new = True
            elif line[:1] in (" ", "-", "+"):
                hunk.lines.append(line)
            elif line == "":
                # Leere Kontextzeile ohne Leerzeichen (häufig bei Modellen) – nur, wenn der Hunk weitergeht.
                j = i + 1
                while j < len(lines) and lines[j] == "":
                    j += 1
                nxt = lines[j] if j < len(lines) else ""
                header = nxt.startswith("--- ") and j + 1 < len(lines) and lines[j + 1].startswith("+++ ")
                if nxt[:1] in (" ", "-", "+") and not header:
                    hunk.lines.append(" ")
                else:
                    hunk = None
            else:
                hunk = None
        i += 1
    if not files:
        raise PatchError("Kein Unified Diff gefunden (Kopfzeilen '--- a/<pfad>' und '+++ b/<pfad>' fehlen)")
    seen: set[str] = set()
    for f in files:
        if f.old_path is not None and f.new_path is not None and f.old_path != f.new_path:
            raise PatchError(f"Unterschiedliche Pfade in den Kopfzeilen ({f.old_path} → {f.new_path}) – Umbenennen wird nicht unterstützt")
        if f.path in seen:
            raise PatchError(f"Datei kommt mehrfach im Patch vor: {f.path} – Hunks einer Datei zusammenfassen")
        seen.add(f.path)
        if not f.hunks and f.kind != "delete":
            raise PatchError(f"Keine Hunks für {f.path}")
        for h in f.hunks:
            if not h.lines:
                raise PatchError(f"Leerer Hunk in {f.path}")
    return files


# ------------------------------------------------------------------ Anwenden

@dataclass
class TextFile:
    lines: list[str]
    eol: str
    bom: bool
    final_newline: bool

    @classmethod
    def decode(cls, data: bytes, path: str) -> TextFile:
        bom = data.startswith(b"\xef\xbb\xbf")
        if bom:
            data = data[3:]
        if b"\x00" in data[:8192]:
            raise PatchError(f"{path}: Binärdatei – kann nicht gepatcht werden")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as err:
            raise PatchError(f"{path}: kein UTF-8 ({err.reason}) – kann nicht gepatcht werden") from err
        eol = "\r\n" if text.count("\r\n") * 2 > text.count("\n") else "\n"
        normalized = text.replace("\r\n", "\n")
        final = normalized.endswith("\n") or normalized == ""
        lines = normalized.split("\n")
        if final:
            lines = lines[:-1] if normalized else []
        return cls(lines, eol, bom, final)

    def encode(self) -> bytes:
        text = self.eol.join(self.lines)
        if self.final_newline and self.lines:
            text += self.eol
        data = text.encode("utf-8")
        return (b"\xef\xbb\xbf" + data) if self.bom else data


def _find(lines: list[str], needle: list[str], hint: int, loose: bool) -> int | None:
    if not needle:
        return min(max(hint, 0), len(lines))
    norm = (lambda s: s.rstrip()) if loose else (lambda s: s)
    want = [norm(n) for n in needle]
    first = want[0]
    best: int | None = None
    for i in range(0, len(lines) - len(want) + 1):
        if norm(lines[i]) != first:
            continue
        if all(norm(lines[i + k]) == want[k] for k in range(1, len(want))):
            if best is None or abs(i - hint) < abs(best - hint):
                best = i
    return best


@dataclass
class ApplyReport:
    hunks: int = 0
    shifted: int = 0
    fuzzy: int = 0

    def describe(self) -> str:
        text = f"{self.hunks} Hunk(s) angewendet"
        extra = []
        if self.shifted:
            extra.append(f"{self.shifted} mit Versatz")
        if self.fuzzy:
            extra.append(f"{self.fuzzy} mit gekürztem Kontext")
        return text + (f" ({', '.join(extra)})" if extra else "")


def apply_hunks(file: TextFile, hunks: list[Hunk], path: str) -> ApplyReport:
    """Wendet die Hunks der Reihe nach an (Positionen verschieben sich mit). Wirft PatchError."""
    report = ApplyReport()
    offset = 0
    lines = file.lines
    for number, hunk in enumerate(hunks, start=1):
        old, new = hunk.old, hunk.new
        hint = max(hunk.old_start - 1, 0) + offset
        pos = None
        trim_front = trim_back = 0
        for fuzz in range(0, MAX_FUZZ + 1):
            # Nur Kontextzeilen am Rand dürfen wegfallen.
            front = _leading_context(hunk, fuzz)
            back = _trailing_context(hunk, fuzz)
            if fuzz and front == trim_front and back == trim_back:
                continue
            trim_front, trim_back = front, back
            needle = old[front:len(old) - back]
            for loose in (False, True):
                pos = _find(lines, needle, hint + front, loose)
                if pos is not None:
                    break
            if pos is not None:
                if fuzz:
                    report.fuzzy += 1
                break
        if pos is None:
            preview = "\n".join(f"  {ln}" for ln in old[:4]) or "  (leer)"
            raise PatchError(f"{path}: Hunk {number} passt nicht zum aktuellen Stand der Datei. Erwartet wurde:\n{preview}")
        replacement = new[trim_front:len(new) - trim_back]
        start = pos
        end = pos + len(old) - trim_front - trim_back
        if start != hint + trim_front:
            report.shifted += 1
        lines[start:end] = replacement
        offset += len(replacement) - (end - start)
        report.hunks += 1
        at_end = start + len(replacement) == len(lines)
        if hunk.no_newline_new and at_end:
            file.final_newline = False
        elif hunk.no_newline_old and at_end:
            file.final_newline = True
    return report


def _leading_context(hunk: Hunk, limit: int) -> int:
    n = 0
    for ln in hunk.lines:
        if ln[0] != " " or n >= limit:
            break
        n += 1
    return n


def _trailing_context(hunk: Hunk, limit: int) -> int:
    n = 0
    for ln in reversed(hunk.lines):
        if ln[0] != " " or n >= limit:
            break
        n += 1
    return n


def new_file(patch: FilePatch) -> TextFile:
    lines: list[str] = []
    for hunk in patch.hunks:
        for ln in hunk.lines:
            if ln[0] == "-":
                raise PatchError(f"{patch.path}: neue Datei enthält entfernte Zeilen")
            lines.append(ln[1:])
    final = not (patch.hunks and patch.hunks[-1].no_newline_new)
    return TextFile(lines, "\n", False, final)


def file_section(text: str, path: str) -> str:
    """Abschnitt einer Datei aus einem Patch (für die Anzeige pro Ziel)."""
    out: list[str] = []
    take = False
    lines = text.replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old, new = _header_path(line[4:], "a/"), _header_path(lines[i + 1][4:], "b/")
            take = (new or old) == path
        elif line.startswith("diff --git"):
            take = False
        if take:
            out.append(line)
    return "\n".join(out)
