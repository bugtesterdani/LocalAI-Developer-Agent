"""Erzeugt contracts/testvectors/canonical.json aus der Python-Referenzimplementierung.

Die Vektoren werden eingecheckt; C# und Python testen beide dagegen.
Nach Änderung der Fälle neu erzeugen und die Diffs bewusst prüfen.
"""

import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "python"))

from localai import canonical  # noqa: E402

VALID = [
    ("leeres Objekt", "{}"),
    ("leeres Array", "[]"),
    ("Literale", '[true,false,null]'),
    ("Sortierung", '{"b":1,"a":2,"c":{"z":1,"y":[3,2,1]}}'),
    ("Leerzeichen entfernen", '{ "a" : [ 1 , 2 ] ,\n "b" : "x" }'),
    ("Ganzzahlen", '[0,-1,42,-0,9007199254740991,-9007199254740991]'),
    ("Escapes", '"Zitat \\" Backslash \\\\ Tab \\t NL \\n CR \\r BS \\b FF \\f"'),
    ("Steuerzeichen", '"\\u0001\\u001f\\u0000"'),
    ("Slash nicht escapen", '"a\\/b"'),
    ("Umlaute roh", '"Größe Übernahme ß"'),
    ("Unicode-Escape zu roh", '"\\u00e4\\u20ac"'),
    ("Emoji Surrogatpaar", '"\\ud83d\\ude00"'),
    ("DEL und U+2028 roh", '"\\u007f\\u2028"'),
    ("Sortierung UTF-16", '{"\\ufb01":1,"\\ud83d\\ude00":2,"a":3,"\\u00e4":4,"B":5}'),
    ("Windows-Pfad", '{"path":"C:\\\\Work\\\\repo\\\\src\\\\Datei.cs"}'),
    ("Vorschlag", json.dumps({
        "schemaVersion": 1, "proposalId": "7b0f2a8e-0000-4000-8000-000000000001",
        "runId": "run-1", "taskId": "task-1", "agent": "Coder", "tool": "File.ReadMany",
        "toolVersion": "1", "purpose": "Schnittstelle prüfen",
        "arguments": {"maxBytes": 262144},
        "targets": [
            {"targetId": "t1", "path": "src/Importer.cs", "expectedSha256": "ab" * 32, "lineStart": 1, "lineEnd": 160},
            {"targetId": "t2", "path": "src/Parser.cs"}],
        "snapshotId": "snap-a81f", "policyRevision": 3, "sandboxId": "sbx-1",
        "createdAt": "2026-10-16T17:00:00.000Z", "expiresAt": "2026-10-16T17:05:00.000Z"},
        ensure_ascii=False)),
]

INVALID = [
    ("Gleitkommazahl", "1.5"),
    ("Exponent", "1e3"),
    ("Ganzzahl zu groß", "9007199254740992"),
    ("Doppelter Schlüssel", '{"a":1,"a":2}'),
    ("Einzelnes Surrogat", '"\\ud800"'),
    ("NaN", "NaN"),
]


def main() -> None:
    cases = []
    for name, text in VALID:
        value = canonical.parse(text)
        canon = canonical.canonicalize(value)
        cases.append({
            "name": name,
            "input": text,
            "canonical": canon,
            "sha256": hashlib.sha256(canon.encode("utf-8")).hexdigest(),
        })
    invalid = []
    for name, text in INVALID:
        try:
            canonical.canonicalize(canonical.parse(text))
        except (canonical.CanonicalJsonError, ValueError):
            invalid.append({"name": name, "input": text})
            continue
        raise SystemExit(f"Ungültiger Fall wurde akzeptiert: {name}")
    out = ROOT / "contracts" / "testvectors" / "canonical.json"
    out.write_text(json.dumps({"valid": cases, "invalid": invalid}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(cases)} gültige, {len(invalid)} ungültige Fälle -> {out}")


if __name__ == "__main__":
    main()
