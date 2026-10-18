"""Repo.Index (B12): Symbolindex eines Snapshots und kompakte Repo-Map für die Planung.

Nur Standardbibliothek: Python über ``ast``; C#, C/C++, PowerShell und Batch über Muster; HDevelop (``.hdev``/
``.hdproc``) als XML. Ergebnisse je Dateiinhalt (SHA-256) werden zwischengespeichert, so dass abgeleitete
Snapshots nach einem Patch nur geänderte Dateien neu einlesen. Der Index eines Snapshots liegt in einer SQLite-
Datenbank mit FTS5 (falls verfügbar) und ist an die Snapshot-ID gebunden.

Die Map ordnet Dateien nach Bedeutung: wie oft ihre Symbole in anderen Dateien vorkommen, plus Einstiegspunkte
und Projektdateien. Sie passt in ein Zeichenbudget; der Rest ist über Suche und Lesen erreichbar.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from localai.worker.snapshot import SnapshotIndex

MAX_FILE_BYTES = 512 * 1024
DEFAULT_MAX_CHARS = 12_000
MAX_CHARS = 60_000
MAX_SYMBOLS_PER_FILE = 400

LANGUAGES = {
    ".py": "Python", ".pyi": "Python",
    ".cs": "C#", ".csx": "C#",
    ".c": "C/C++", ".h": "C/C++", ".cpp": "C/C++", ".cc": "C/C++", ".cxx": "C/C++", ".hpp": "C/C++", ".hh": "C/C++",
    ".hxx": "C/C++", ".ipp": "C/C++", ".inl": "C/C++",
    ".ps1": "PowerShell", ".psm1": "PowerShell", ".psd1": "PowerShell",
    ".bat": "Batch", ".cmd": "Batch",
    ".hdev": "HDevelop", ".hdvp": "HDevelop", ".hdpl": "HDevelop",
}
PROJECT_FILES = re.compile(r"(\.(sln|slnx|csproj|vcxproj|props|targets)|(^|/)(pyproject\.toml|setup\.py|setup\.cfg|"
                           r"requirements[^/]*\.txt|CMakeLists\.txt|global\.json|Directory\.Build\.props|pytest\.ini|"
                           r"tox\.ini|conftest\.py))$", re.I)
ENTRY_POINTS = re.compile(r"(^|/)(Program\.cs|Main\.cs|__main__\.py|main\.(py|c|cpp)|app\.py|cli\.py)$", re.I)
TEST_PATH = re.compile(r"(^|/)(tests?|.*\.Tests?)(/|$)|(^|/)test_[^/]*\.py$|[^/]*_test\.py$|Tests?\.cs$", re.I)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CS_TYPE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)*(?:(?:public|internal|private|protected|static|sealed|abstract|partial|"
                      r"readonly|file|unsafe|new)\s+)*(class|interface|struct|record(?:\s+struct|\s+class)?|enum)\s+"
                      r"([A-Za-z_]\w*)", re.M)
_CS_METHOD = re.compile(r"^\s*(?:\[[^\]]*\]\s*)*(?:(?:public|internal|private|protected|static|virtual|override|"
                        r"abstract|async|sealed|extern|unsafe|new|partial)\s+)+[\w<>\[\],.?\s]+?\s+([A-Za-z_]\w*)\s*"
                        r"(?:<[^>()]*>)?\s*\(", re.M)
_CS_NAMESPACE = re.compile(r"^\s*namespace\s+([\w.]+)", re.M)
_CPP_TYPE = re.compile(r"^\s*(?:template\s*<[^>]*>\s*)?(class|struct|enum(?:\s+class)?|namespace)\s+([A-Za-z_]\w*)\s*"
                       r"(?:final\s*)?(?::[^{;]*)?\{", re.M)
_CPP_FUNC = re.compile(r"^(?!\s*(?:if|for|while|switch|return|else|do|case)\b)\s*(?:[\w:<>,*&~\s]+?)\s+\**&?"
                       r"((?:[A-Za-z_]\w*::)*~?[A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:const\s*)?(?:noexcept\s*)?"
                       r"(?:override\s*)?\{", re.M)
_PS_FUNC = re.compile(r"^\s*(function|filter|class|enum|workflow)\s+([A-Za-z_][\w-]*)", re.M | re.I)
_BAT_LABEL = re.compile(r"^\s*:([A-Za-z_][\w.-]*)\s*$", re.M)
_CS_KEYWORDS = {"if", "for", "foreach", "while", "switch", "return", "using", "lock", "catch", "nameof", "typeof",
                "sizeof", "new", "base", "this", "default", "when", "await", "yield", "throw"}


@dataclass
class Symbol:
    name: str
    kind: str
    line: int


@dataclass
class FileInfo:
    path: str
    language: str
    lines: int
    symbols: list[Symbol] = field(default_factory=list)
    identifiers: set[str] = field(default_factory=set)
    error: str | None = None


def language_of(path: str) -> str | None:
    return LANGUAGES.get(pathlib.PurePosixPath(path).suffix.lower())


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def extract(path: str, data: bytes) -> FileInfo:
    language = language_of(path) or "?"
    text = data.decode("utf-8-sig", errors="replace")
    info = FileInfo(path, language, text.count("\n") + (0 if text.endswith("\n") or not text else 1))
    try:
        if language == "Python":
            _python(text, info)
        elif language == "C#":
            for m in _CS_NAMESPACE.finditer(text):
                info.symbols.append(Symbol(m.group(1), "namespace", _line_of(text, m.start())))
            for m in _CS_TYPE.finditer(text):
                info.symbols.append(Symbol(m.group(2), m.group(1).split()[0], _line_of(text, m.start())))
            for m in _CS_METHOD.finditer(text):
                if m.group(1) not in _CS_KEYWORDS:
                    info.symbols.append(Symbol(m.group(1), "method", _line_of(text, m.start())))
        elif language == "C/C++":
            for m in _CPP_TYPE.finditer(text):
                info.symbols.append(Symbol(m.group(2), m.group(1).split()[0], _line_of(text, m.start())))
            for m in _CPP_FUNC.finditer(text):
                info.symbols.append(Symbol(m.group(1), "function", _line_of(text, m.start())))
        elif language == "PowerShell":
            for m in _PS_FUNC.finditer(text):
                info.symbols.append(Symbol(m.group(2), m.group(1).lower(), _line_of(text, m.start())))
        elif language == "Batch":
            for m in _BAT_LABEL.finditer(text):
                if m.group(1).lower() != "eof":
                    info.symbols.append(Symbol(m.group(1), "label", _line_of(text, m.start())))
        elif language == "HDevelop":
            _hdevelop(data, info)
    except (SyntaxError, ValueError, ET.ParseError, RecursionError) as err:
        info.error = f"{type(err).__name__}: {str(err)[:120]}"
    info.symbols.sort(key=lambda s: s.line)
    del info.symbols[MAX_SYMBOLS_PER_FILE:]
    if language != "?":
        info.identifiers = set(_IDENT.findall(text))
    return info


def _python(text: str, info: FileInfo) -> None:
    tree = ast.parse(text)

    def visit(nodes: list[ast.stmt], prefix: str) -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                info.symbols.append(Symbol(prefix + node.name, "class", node.lineno))
                visit(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info.symbols.append(Symbol(prefix + node.name, "method" if prefix else "function", node.lineno))
            elif not prefix and isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        info.symbols.append(Symbol(target.id, "constant", node.lineno))

    visit(tree.body, "")


def _hdevelop(data: bytes, info: FileInfo) -> None:
    root = ET.fromstring(data)
    for proc in root.iter("procedure"):
        name = proc.get("name")
        if name:
            info.symbols.append(Symbol(name, "procedure", 0))


# ------------------------------------------------------------------ Index

class RepoIndexer:
    def __init__(self, work: pathlib.Path) -> None:
        self.base = work / "index"
        self.base.mkdir(parents=True, exist_ok=True)
        self.cache = sqlite3.connect(self.base / "symbols-cache.sqlite", check_same_thread=False)
        self.cache.execute("CREATE TABLE IF NOT EXISTS files (sha TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def _info(self, index: SnapshotIndex, path: str, sha: str, size: int) -> FileInfo | None:
        language = language_of(path)
        if language is None or size > MAX_FILE_BYTES:
            return None
        row = self.cache.execute("SELECT data FROM files WHERE sha = ?", (sha,)).fetchone()
        if row is not None:
            d = json.loads(row[0])
            return FileInfo(path, d["language"] if d["language"] == language else language, d["lines"],
                            [Symbol(*s) for s in d["symbols"]], set(d["identifiers"]), d.get("error"))
        info = extract(path, index.resolve(path).read_bytes())
        self.cache.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", (sha, json.dumps({
            "language": info.language, "lines": info.lines, "error": info.error,
            "symbols": [[s.name, s.kind, s.line] for s in info.symbols],
            "identifiers": sorted(info.identifiers)})))
        return info

    def build(self, index: SnapshotIndex) -> list[FileInfo]:
        infos = []
        for path, (size, sha) in sorted(index.files.items()):
            info = self._info(index, path, sha, size)
            if info is not None:
                infos.append(info)
        self.cache.commit()
        db_path = self.base / f"{index.snapshot_id}.sqlite"
        if not db_path.exists():
            tmp = db_path.with_suffix(".tmp")
            tmp.unlink(missing_ok=True)
            with sqlite3.connect(tmp) as db:
                fts = _has_fts5(db)
                if fts:
                    db.execute("CREATE VIRTUAL TABLE symbols USING fts5(name, kind UNINDEXED, path, line UNINDEXED)")
                else:
                    db.execute("CREATE TABLE symbols (name TEXT, kind TEXT, path TEXT, line INTEGER)")
                db.executemany("INSERT INTO symbols VALUES (?, ?, ?, ?)",
                               [(s.name, s.kind, i.path, s.line) for i in infos for s in i.symbols])
                db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
                db.execute("INSERT INTO meta VALUES ('snapshotId', ?), ('fts5', ?)", (index.snapshot_id, str(fts)))
            tmp.replace(db_path)
        return infos

    def search(self, index: SnapshotIndex, query: str, limit: int = 100) -> list[str]:
        self.build(index)
        with sqlite3.connect(self.base / f"{index.snapshot_id}.sqlite") as db:
            fts = db.execute("SELECT value FROM meta WHERE key = 'fts5'").fetchone()[0] == "True"
            words = _IDENT.findall(query) or [query.strip()]
            rows: list[tuple] = []
            if fts:
                match = " OR ".join(f'"{w}"*' for w in words if w)
                try:
                    rows = db.execute("SELECT name, kind, path, line FROM symbols WHERE symbols MATCH ? "
                                      "ORDER BY rank LIMIT ?", (match, limit)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                like = f"%{words[0]}%"
                rows = db.execute("SELECT name, kind, path, line FROM symbols WHERE name LIKE ? OR path LIKE ? LIMIT ?",
                                  (like, like, limit)).fetchall()
        return [f"{path}:{line} {kind} {name}" for name, kind, path, line in rows]


def _has_fts5(db: sqlite3.Connection) -> bool:
    try:
        db.execute("CREATE VIRTUAL TABLE temp.probe USING fts5(x)")
        db.execute("DROP TABLE temp.probe")
        return True
    except sqlite3.OperationalError:
        return False


# ------------------------------------------------------------------ Map

def rank(infos: list[FileInfo]) -> dict[str, float]:
    """Bedeutung je Datei: Verweise anderer Dateien auf ihre Symbole (gedeckelt), Einstiegspunkte, Tests leicht weniger."""
    defined: dict[str, set[str]] = defaultdict(set)
    for info in infos:
        for s in info.symbols:
            short = s.name.rsplit(".", 1)[-1].rsplit("::", 1)[-1]
            if len(short) >= 3 and s.kind not in ("namespace", "label"):
                defined[short].add(info.path)
    references: Counter[str] = Counter()
    for info in infos:
        for ident in info.identifiers:
            owners = defined.get(ident)
            if owners and len(owners) <= 5:
                for owner in owners:
                    if owner != info.path:
                        references[(owner, ident)] += 1  # type: ignore[index]
    score: dict[str, float] = {i.path: 0.0 for i in infos}
    for (owner, _ident), count in references.items():  # type: ignore[misc]
        score[owner] += min(count, 20)
    for info in infos:
        if ENTRY_POINTS.search(info.path):
            score[info.path] += 25
        if TEST_PATH.search(info.path):
            score[info.path] *= 0.6
        score[info.path] += min(len(info.symbols), 10) * 0.5
    return score


def build_map(index: SnapshotIndex, infos: list[FileInfo], max_chars: int) -> str:
    total = len(index.files)
    languages = Counter(i.language for i in infos)
    lines = [f"Repo map of snapshot {index.snapshot_id[:12]} – {total} files, "
             + (", ".join(f"{lang} {n}" for lang, n in languages.most_common()) or "no source files recognised")]
    projects = sorted(p for p in index.files if PROJECT_FILES.search(p))
    if projects:
        shown = projects[:40]
        lines.append("Project/build files: " + ", ".join(shown) + (f" (+{len(projects) - 40} more)" if len(projects) > 40 else ""))
    dirs: Counter[str] = Counter()
    for p in index.files:
        parts = p.split("/")
        dirs[parts[0] + ("/" if len(parts) > 1 else "")] += 1
        if len(parts) > 2:
            dirs[parts[0] + "/" + parts[1] + "/"] += 1
    top = [f"{d} ({n})" for d, n in sorted(dirs.items()) if d.endswith("/")]
    if top:
        lines.append("Directories (files): " + ", ".join(top[:60]) + (" …" if len(top) > 60 else ""))
    tests = [i.path for i in infos if TEST_PATH.search(i.path)]
    if tests:
        lines.append(f"Test files: {len(tests)}, e.g. " + ", ".join(tests[:8]))
    lines.append("")
    lines.append("Most important files (ranked by references from other files; symbol:line):")
    header = "\n".join(lines)
    budget = max_chars - len(header) - 200
    scores = rank(infos)
    ordered = sorted(infos, key=lambda i: (-scores[i.path], i.path))
    body: list[str] = []
    used = 0
    shown_files = 0
    for info in ordered:
        syms = [s for s in info.symbols if s.kind != "namespace"]
        sym_text = "; ".join(f"{s.kind} {s.name}:{s.line}" if s.line else f"{s.kind} {s.name}" for s in syms[:12])
        if len(syms) > 12:
            sym_text += f"; … {len(syms) - 12} more"
        entry = f"{info.path} [{info.language}, {info.lines} lines]" + (f"\n  {sym_text}" if sym_text else "")
        if info.error:
            entry += f"\n  (not parsed: {info.error})"
        if used + len(entry) + 1 > budget:
            break
        body.append(entry)
        used += len(entry) + 1
        shown_files += 1
    rest = len(infos) - shown_files
    others = total - len(infos)
    tail = []
    if rest > 0:
        tail.append(f"(+{rest} more source files not shown – use search or read)")
    if others > 0:
        tail.append(f"({others} non-source files not listed)")
    return "\n".join([header, *body, *tail])
