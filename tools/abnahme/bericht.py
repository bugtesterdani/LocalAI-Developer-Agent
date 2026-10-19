"""Abnahmebericht M7: SEC-01–24 und AC01–17 aus Testergebnissen, VM-Abnahmen und manuellen Bestätigungen.

Aufruf (scripts/abnahme.ps1 erledigt das)::

    python tools/abnahme/bericht.py --trx <csharp.trx> --junit <python.xml> --abnahme <Ordner> [--daten <Datenordner>]

Status je Kriterium: „bestanden“ nur, wenn jede Nachweisart vorhanden und erfolgreich ist; fehlt etwas, heißt es
„unvollständig“ (nie „bestanden“, Spezifikation §16: „Nicht ausgeführt“ darf nie als „bestanden“ erscheinen); ein
Fehlschlag macht das Kriterium „fehlgeschlagen“. Ergebnis: abnahme-bericht.md und abnahme-bericht.json im
Abnahme-Ordner; fehlt abnahme-manuell.json, wird eine Vorlage zum Ausfüllen angelegt.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parents[2]
PASSED, FAILED, MISSING = "bestanden", "fehlgeschlagen", "nicht ausgeführt"
_TRAIT = re.compile(r'Trait\("Pruefung",\s*"([A-Z0-9-]+)"\)')
_CLASS = re.compile(r"^(?:public|internal)\s+(?:static\s+)?(?:sealed\s+)?(?:partial\s+)?class\s+(\w+)")
_METHOD = re.compile(r"\bpublic\s+(?:static\s+)?(?:async\s+)?[\w<>\[\],? ]+\s+(\w+)\s*\(")


@dataclass
class TestRun:
    name: str
    outcome: str  # bestanden | fehlgeschlagen | übersprungen


@dataclass
class Nachweis:
    art: str
    status: str
    detail: str


@dataclass
class Ergebnis:
    id: str
    szenario: str
    erwartung: str
    status: str
    nachweise: list[Nachweis] = field(default_factory=list)


def csharp_traits(test_dir: pathlib.Path) -> dict[str, set[str]]:
    """Kriterium → {Klasse.Methode} aus [Trait("Pruefung", "...")] im Testcode."""
    result: dict[str, set[str]] = {}
    for file in sorted(test_dir.glob("*.cs")):
        current_class = ""
        pending: list[str] = []
        for line in file.read_text(encoding="utf-8").splitlines():
            if m := _CLASS.search(line):
                current_class = m.group(1)
            pending += _TRAIT.findall(line)
            if pending and (m := _METHOD.search(line)) and "Trait(" not in line:
                for crit in pending:
                    result.setdefault(crit, set()).add(f"{current_class}.{m.group(1)}")
                pending = []
    return result


def read_trx(paths: list[pathlib.Path]) -> list[TestRun]:
    runs = []
    for path in paths:
        if not path.exists():
            continue
        for node in ET.parse(path).getroot().iter():
            if node.tag.endswith("UnitTestResult"):
                outcome = node.get("outcome", "")
                runs.append(TestRun(node.get("testName", ""), PASSED if outcome == "Passed" else
                                    FAILED if outcome in ("Failed", "Error", "Timeout", "Aborted") else "übersprungen"))
    return runs


def read_junit(paths: list[pathlib.Path]) -> list[TestRun]:
    runs = []
    for path in paths:
        if not path.exists():
            continue
        for case in ET.parse(path).getroot().iter("testcase"):
            children = {c.tag for c in case}
            outcome = FAILED if children & {"failure", "error"} else "übersprungen" if "skipped" in children else PASSED
            runs.append(TestRun(f"{case.get('classname', '')}::{case.get('name', '')}", outcome))
    return runs


def _summary(runs: list[TestRun], art: str) -> Nachweis:
    if not runs:
        return Nachweis(art, MISSING, "keine passenden Tests ausgeführt")
    failed = [r.name for r in runs if r.outcome == FAILED]
    passed = sum(r.outcome == PASSED for r in runs)
    if failed:
        return Nachweis(art, FAILED, f"{len(failed)} fehlgeschlagen: {', '.join(failed[:5])}")
    if passed == 0:
        return Nachweis(art, MISSING, f"{len(runs)} übersprungen")
    skipped = len(runs) - passed
    return Nachweis(art, PASSED, f"{passed} bestanden" + (f", {skipped} übersprungen" if skipped else ""))


def vm_evidence(spec: dict, abnahme: pathlib.Path, daten: pathlib.Path | None) -> Nachweis:
    if "kalibrierung" in spec:
        file = (daten or pathlib.Path()) / "calibration.json"
        if daten is None or not file.exists():
            return Nachweis("Kalibrierung", MISSING, f"{file} fehlt (Kalibrierung auf dem Ollama-Rechner ausführen)")
        measurements = json.loads(file.read_text(encoding="utf-8")).get("measurements", {})
        if spec["kalibrierung"] == "ueber8k":
            above = [m for m in measurements.values() if m.get("context_tokens", 0) > 8192]
            return (Nachweis("Kalibrierung", PASSED, f"{len(above)} Messung(en) über 8K") if above
                    else Nachweis("Kalibrierung", MISSING, "keine Messung über 8K Kontext"))
        stable = [k for k, m in measurements.items() if m.get("stable") and not m.get("oom")]
        return (Nachweis("Kalibrierung", PASSED, f"stabil: {', '.join(stable[:3])}") if stable
                else Nachweis("Kalibrierung", FAILED if measurements else MISSING, "keine stabile Messung ohne OOM"))
    file = abnahme / spec["datei"]
    art = f"VM {spec['datei']}"
    if not file.exists():
        return Nachweis(art, MISSING, "Abnahme-Skript nicht ausgeführt")
    data = json.loads(file.read_text(encoding="utf-8"))
    if data.get("ok") is not True:
        return Nachweis(art, FAILED, "Abnahme-Skript nicht erfolgreich beendet")
    value = data.get(spec["feld"])
    if spec["feld"] != "ok" and value in (None, "", False, [], {}):
        return Nachweis(art, MISSING, f"Feld {spec['feld']} fehlt")
    if isinstance(value, dict) and value.get("ok") is False:
        return Nachweis(art, FAILED, f"{spec['feld']}: {json.dumps(value, ensure_ascii=False)[:200]}")
    if "min" in spec and not (isinstance(value, (int, float)) and value >= spec["min"]):
        return Nachweis(art, FAILED, f"{spec['feld']} = {value} (mindestens {spec['min']})")
    shown = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return Nachweis(art, PASSED, f"{spec['feld']}: {shown[:160]}")


def evaluate(kriterien: list[dict], trx: list[TestRun], junit: list[TestRun], traits: dict[str, set[str]],
             abnahme: pathlib.Path, daten: pathlib.Path | None, manuell: dict) -> list[Ergebnis]:
    results = []
    for k in kriterien:
        nachweise: list[Nachweis] = []
        methods = traits.get(k["id"], set())
        if methods:
            matched = [r for r in trx if any(re.search(rf"\.{re.escape(m)}(\(|$)", r.name) for m in methods)]
            nachweise.append(_summary(matched, f"C# ({len(methods)} Tests)"))
        if k["python"]:
            matched = [r for r in junit
                       if any(r.name.split("::")[-1].startswith(p) or f".{p}" in r.name.split("::")[0] or r.name.split("::")[0].endswith(p)
                              for p in k["python"])]
            nachweise.append(_summary(matched, "Python"))
        for spec in k["vm"]:
            nachweise.append(vm_evidence(spec, abnahme, daten))
        for item in k["manuell"]:
            entry = manuell.get(item["id"]) or {}
            status = PASSED if entry.get("ok") is True else FAILED if entry.get("ok") is False else "offen"
            detail = item["text"] + (f" – {entry.get('datum', '')} {entry.get('notiz', '')}".rstrip() if entry else "")
            nachweise.append(Nachweis("manuell", status, detail))
        if not nachweise:
            status = MISSING
        elif any(n.status == FAILED for n in nachweise):
            status = FAILED
        elif all(n.status == PASSED for n in nachweise):
            status = PASSED
        else:
            status = "unvollständig"
        results.append(Ergebnis(k["id"], k["szenario"], k["erwartung"], status, nachweise))
    return results


def write_report(results: list[Ergebnis], out: pathlib.Path, sources: dict[str, str]) -> tuple[pathlib.Path, pathlib.Path]:
    out.mkdir(parents=True, exist_ok=True)
    counts = {s: sum(r.status == s for r in results) for s in (PASSED, "unvollständig", FAILED, MISSING)}
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Abnahmebericht M7 ({now})", "",
             f"**{counts[PASSED]} bestanden · {counts['unvollständig']} unvollständig · {counts[FAILED]} fehlgeschlagen · "
             f"{counts[MISSING]} nicht ausgeführt** (von {len(results)})", "",
             "„Bestanden“ nur, wenn alle Nachweise vorhanden und erfolgreich sind; fehlende Nachweise erscheinen nie als bestanden.",
             "", "Quellen: " + ", ".join(f"{k}: {v}" for k, v in sources.items()), "",
             "| Kriterium | Status | Szenario | Nachweise |", "|---|---|---|---|"]
    icon = {PASSED: "✅", FAILED: "❌", "unvollständig": "⚠️", MISSING: "⬜", "offen": "⬜"}
    for r in results:
        evidence = "<br>".join(f"{icon.get(n.status, '')} {n.art}: {n.detail}".replace("|", "\\|") for n in r.nachweise)
        lines.append(f"| {r.id} | {icon.get(r.status, '')} {r.status} | {r.szenario} → {r.erwartung} | {evidence} |")
    md = out / "abnahme-bericht.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    js = out / "abnahme-bericht.json"
    js.write_text(json.dumps({"erstellt": now, "zaehlung": counts, "quellen": sources,
                              "kriterien": [{"id": r.id, "status": r.status,
                                             "nachweise": [vars(n) for n in r.nachweise]} for r in results]},
                             ensure_ascii=False, indent=1), encoding="utf-8")
    return md, js


def manual_template(kriterien: list[dict], path: pathlib.Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8-sig"))
    template = {"_hinweis": "Je Punkt nach eigener Prüfung ok auf true (oder false) setzen, Datum und Notiz eintragen."}
    for k in kriterien:
        for item in k["manuell"]:
            template[item["id"]] = {"kriterium": k["id"], "pruefung": item["text"], "ok": None, "datum": "", "notiz": ""}
    path.write_text(json.dumps(template, ensure_ascii=False, indent=1), encoding="utf-8")
    return template


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Abnahmebericht M7")
    parser.add_argument("--trx", action="append", default=[], type=pathlib.Path)
    parser.add_argument("--junit", action="append", default=[], type=pathlib.Path)
    parser.add_argument("--abnahme", required=True, type=pathlib.Path, help="Ordner mit *-abnahme.json (und Ausgabe)")
    parser.add_argument("--daten", type=pathlib.Path, help="Datenordner des Brokers (calibration.json)")
    parser.add_argument("--kriterien", type=pathlib.Path, default=pathlib.Path(__file__).with_name("kriterien.json"))
    args = parser.parse_args(argv)
    kriterien = json.loads(args.kriterien.read_text(encoding="utf-8"))["kriterien"]
    manuell = manual_template(kriterien, args.abnahme / "abnahme-manuell.json")
    trx, junit = read_trx(args.trx), read_junit(args.junit)
    results = evaluate(kriterien, trx, junit, csharp_traits(ROOT / "src" / "dotnet" / "LocalAi.Tests"), args.abnahme, args.daten, manuell)
    md, _ = write_report(results, args.abnahme, {"C#": f"{len(trx)} Tests", "Python": f"{len(junit)} Tests",
                                                  "VM": str(args.abnahme), "manuell": "abnahme-manuell.json"})
    counts = {s: sum(r.status == s for r in results) for s in (PASSED, "unvollständig", FAILED, MISSING)}
    print(f"Abnahmebericht: {md}")
    print("  " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    return 1 if counts[FAILED] else 0


if __name__ == "__main__":
    sys.exit(main())
