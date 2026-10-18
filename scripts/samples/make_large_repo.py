"""Erzeugt ein großes Beispiel-Repository (> 2.000 Dateien) für die M4-Prüfung von Repo.Index und Repo-Map.

Aufruf (Zielordner darf nicht existieren)::

    uv run --project src/python python scripts/samples/make_large_repo.py D:\\Agent\\Projekte\\gross --files 2200

Inhalt: C#-Module mit je einer Klasse, die einen zentralen ``CsvParser`` benutzen (er soll in der Repo-Map oben
stehen), ein paar Python-Hilfsskripte, Tests und eine README. Nur zum Lesen/Indizieren gedacht, nicht zum Bauen.
"""

from __future__ import annotations

import argparse
import pathlib
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", type=pathlib.Path)
    parser.add_argument("--files", type=int, default=2200)
    args = parser.parse_args(argv)
    if args.target.exists():
        print(f"Zielordner existiert bereits: {args.target}", file=sys.stderr)
        return 2
    root = args.target
    (root / "src" / "Core").mkdir(parents=True)
    (root / "src" / "Core" / "CsvParser.cs").write_text(
        "namespace Gross.Core;\n\npublic sealed class CsvParser\n{\n"
        "    public string[] ParseLine(string line) => line.Split(';');\n\n"
        "    public int CountFields(string line) => ParseLine(line).Length;\n}\n", encoding="utf-8")
    (root / "README.md").write_text("# Großes Beispielprojekt\n\nGeneriert für die Prüfung von Repo.Index (M4).\n",
                                    encoding="utf-8")
    modules = max(1, args.files // 50)
    for i in range(args.files):
        module = root / "src" / f"Modul{i % modules:02d}"
        module.mkdir(exist_ok=True)
        (module / f"Funktion{i:04d}.cs").write_text(
            f"namespace Gross.Modul{i % modules:02d};\n\nusing Gross.Core;\n\n"
            f"public sealed class Funktion{i:04d}\n{{\n"
            f"    private readonly CsvParser _parser = new();\n\n"
            f"    public int Ausfuehren(string zeile) => _parser.CountFields(zeile) + {i};\n}}\n", encoding="utf-8")
    (root / "tools").mkdir()
    for i in range(10):
        (root / "tools" / f"skript_{i}.py").write_text(f"def schritt_{i}(wert):\n    return wert * {i}\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "CsvParserTests.cs").write_text(
        "namespace Gross.Tests;\n\npublic sealed class CsvParserTests\n{\n    // Zeilen mit Leerwerten\n}\n", encoding="utf-8")
    total = sum(1 for p in root.rglob("*") if p.is_file())
    print(f"{total} Dateien in {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
