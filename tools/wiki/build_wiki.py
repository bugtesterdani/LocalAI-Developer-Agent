"""Erzeugt die Seiten des GitHub-Wikis aus docs/ (einzige Quelle der Dokumentation).

Jede Datei aus PAGES wird eine Wiki-Seite; Links zwischen ihnen werden auf Wiki-Seiten umgeschrieben, alle anderen
relativen Links auf Dateien im Repository (Branch BRANCH). Dazu kommen _Sidebar.md (Navigation) und _Footer.md.

Aufruf::

    python tools/wiki/build_wiki.py --out <Ordner des geklonten Wikis> [--repo owner/name] [--branch main]

Der Zielordner wird bis auf .git geleert, damit gelöschte Seiten auch im Wiki verschwinden. Veröffentlicht wird
über .github/workflows/wiki.yml bei jedem Push auf main.
"""

from __future__ import annotations

import argparse
import pathlib
import posixpath
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Quelldatei (relativ zur Repository-Wurzel) → Name der Wiki-Seite
PAGES = {
    "docs/README.md": "Home",
    "docs/architektur.md": "Architektur",
    "docs/bedienung.md": "Bedienung",
    "docs/konfiguration.md": "Konfiguration",
    "docs/werkzeuge.md": "Werkzeuge-und-Rechte",
    "docs/betrieb.md": "Betrieb",
    "docs/entwicklung.md": "Entwicklung",
    "docs/glossar.md": "Glossar",
    "docs/installation.md": "Installation",
    "docs/inbetriebnahme-neuer-rechner.md": "Inbetriebnahme-Windows",
    "toolchains/README.md": "Toolchains",
    "docs/Local-AI-Developer-Agent-Projektspezifikation.md": "Projektspezifikation",
    "docs/Umsetzungsplan.md": "Umsetzungsplan",
    "docs/sicherheit/anforderungen-tests.md": "Anforderungen-und-Tests",
    "docs/archiv/Local-AI-Developer-Agent-Projektspezifikation-v1.0.md": "Archiv-Projektspezifikation-v1.0",
    **{f"docs/pruefung-{m}.md": f"Pruefung-{m}"
       for m in ("M1", "M2a", "M2b", "M2c", "M3", "M4", "M5", "M6", "M8", "M9", "M10")},
    "README.md": "Home",  # nur als Linkziel; Home entsteht aus docs/README.md
}

SIDEBAR = """\
**[Start](Home)**

**Handbuch**
- [Architektur](Architektur)
- [Bedienung](Bedienung)
- [Konfiguration](Konfiguration)
- [Werkzeuge und Rechte](Werkzeuge-und-Rechte)
- [Betrieb](Betrieb)
- [Entwicklung](Entwicklung)
- [Glossar](Glossar)

**Einrichtung**
- [Installation (Pakete)](Installation)
- [Inbetriebnahme (Windows)](Inbetriebnahme-Windows)
- [Linux-Host (M10)](Pruefung-M10#0-einrichtung)
- [Toolchains](Toolchains)

**Grundlagen**
- [Projektspezifikation](Projektspezifikation)
- [Umsetzungsplan](Umsetzungsplan)
- [Anforderungen → Tests](Anforderungen-und-Tests)

**Prüfanleitungen**
- [M1](Pruefung-M1) · [M2a](Pruefung-M2a) · [M2b](Pruefung-M2b) · [M2c](Pruefung-M2c)
- [M3](Pruefung-M3) · [M4](Pruefung-M4) · [M5](Pruefung-M5) · [M6](Pruefung-M6)
- [M8](Pruefung-M8) · [M9](Pruefung-M9) · [M10](Pruefung-M10)
"""

FOOTER = ("Automatisch erzeugt aus [`{source}`]({url}) – Änderungen bitte dort vornehmen, nicht im Wiki "
          "(sie werden beim nächsten Push auf `{branch}` überschrieben).\n")

_LINK = re.compile(r"(!?\[[^\]]*\]\()([^)\s]+)(\))")
_FENCE = re.compile(r"^(```|~~~)")


def rewrite(text: str, source: str, repo: str, branch: str) -> str:
    """Schreibt relative Links um – außerhalb von Codeblöcken."""
    base = posixpath.dirname(source)
    out, in_fence = [], False
    for line in text.splitlines(keepends=True):
        if _FENCE.match(line.lstrip()):
            in_fence = not in_fence
        if not in_fence:
            line = _LINK.sub(lambda m: m.group(1) + target(m.group(2), base, repo, branch) + m.group(3), line)
        out.append(line)
    return "".join(out)


def target(link: str, base: str, repo: str, branch: str) -> str:
    if re.match(r"^[a-z][a-z0-9+.-]*:", link, re.I) or link.startswith("#"):
        return link
    path, _, anchor = link.partition("#")
    resolved = posixpath.normpath(posixpath.join(base, path)) if path else ""
    suffix = f"#{anchor}" if anchor else ""
    if resolved in PAGES:
        return PAGES[resolved] + suffix
    kind = "tree" if not posixpath.splitext(resolved)[1] else "blob"
    return f"https://github.com/{repo}/{kind}/{branch}/{resolved}{suffix}"


def build(out: pathlib.Path, repo: str, branch: str) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    for entry in out.iterdir():
        if entry.name != ".git":
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    written = []
    for source, page in PAGES.items():
        if source == "README.md":
            continue
        text = (ROOT / source).read_text(encoding="utf-8")
        text = rewrite(text, source, repo, branch).rstrip() + "\n"
        (out / f"{page}.md").write_text(text, encoding="utf-8")
        written.append(page)
    (out / "_Sidebar.md").write_text(SIDEBAR, encoding="utf-8")
    (out / "_Footer.md").write_text(FOOTER.format(source="docs/", url=f"https://github.com/{repo}/tree/{branch}/docs",
                                                  branch=branch), encoding="utf-8")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_wiki")
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--repo", default="bugtesterdani/LocalAI-Developer-Agent")
    parser.add_argument("--branch", default="main")
    args = parser.parse_args(argv)
    missing = [s for s in PAGES if not (ROOT / s).is_file()]
    if missing:
        print("Fehlende Quelldateien: " + ", ".join(missing), file=sys.stderr)
        return 1
    pages = build(args.out, args.repo, args.branch)
    print(f"{len(pages)} Seiten nach {args.out} geschrieben.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
