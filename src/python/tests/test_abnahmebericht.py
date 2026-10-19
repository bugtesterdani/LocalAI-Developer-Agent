"""M7: Abnahmebericht – „nicht ausgeführt“ erscheint nie als „bestanden“ (Spezifikation §16)."""

from __future__ import annotations

import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("bericht", ROOT / "tools" / "abnahme" / "bericht.py")
bericht = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bericht)  # type: ignore[union-attr]


def _k(**kw):
    base = {"id": "X", "szenario": "s", "erwartung": "e", "python": [], "vm": [], "manuell": []}
    return {**base, **kw}


def test_status_regeln(tmp_path):
    (tmp_path / "m4-abnahme.json").write_text(json.dumps({"ok": True, "largeRepoFiles": 1500}), encoding="utf-8")
    (tmp_path / "m6-abnahme.json").write_text(json.dumps({"ok": False, "notaus_status": "x"}), encoding="utf-8")
    junit = [bericht.TestRun("tests.test_a::test_sec01_ok", bericht.PASSED), bericht.TestRun("tests.test_a::test_sec02_x", bericht.FAILED),
             bericht.TestRun("tests.test_a::test_skip_only", "übersprungen")]
    kriterien = [
        _k(id="A", python=["test_sec01_"]),
        _k(id="B", python=["test_sec01_", "test_sec02_"]),
        _k(id="C", python=["test_gibt_es_nicht"]),
        _k(id="D", python=["test_skip_only"]),
        _k(id="E", vm=[{"datei": "m5-abnahme.json", "feld": "ok"}]),
        _k(id="F", vm=[{"datei": "m6-abnahme.json", "feld": "notaus_status"}]),
        _k(id="G", vm=[{"datei": "m4-abnahme.json", "feld": "largeRepoFiles", "min": 2001}]),
        _k(id="H", python=["test_sec01_"], manuell=[{"id": "H-1", "text": "prüfen"}]),
        _k(id="I", python=["test_sec01_"], manuell=[{"id": "I-1", "text": "prüfen"}]),
        _k(id="J"),
    ]
    manuell = {"I-1": {"ok": True, "datum": "2026-10-20"}}
    res = {r.id: r.status for r in bericht.evaluate(kriterien, [], junit, {}, tmp_path, None, manuell)}
    assert res == {"A": "bestanden", "B": "fehlgeschlagen", "C": "unvollständig", "D": "unvollständig", "E": "unvollständig",
                   "F": "fehlgeschlagen", "G": "fehlgeschlagen", "H": "unvollständig", "I": "bestanden", "J": "nicht ausgeführt"}


def test_csharp_traits_und_trx(tmp_path):
    traits = bericht.csharp_traits(ROOT / "src" / "dotnet" / "LocalAi.Tests")
    assert "M7Tests.Zufaellige_Ablaeufe_halten_die_Freigabeinvarianten" in traits["AC03"]
    assert "DesktopTests.Bedienelemente_sind_fuer_Screenreader_benannt_und_Ablehnen_hat_den_Fokus" in traits["AC10"]
    trx = tmp_path / "t.trx"
    trx.write_text('<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><Results>'
                   '<UnitTestResult testName="LocalAi.Tests.M7Tests.Zufaellige_Ablaeufe_halten_die_Freigabeinvarianten(seed: 1)" outcome="Passed"/>'
                   '<UnitTestResult testName="LocalAi.Tests.M7Tests.Audit_Pruefung_findet_Ausfuehrung_ohne_Erlaubnis" outcome="Failed"/>'
                   '</Results></TestRun>', encoding="utf-8")
    runs = bericht.read_trx([trx])
    res = bericht.evaluate([_k(id="AC03")], runs, [], traits, tmp_path, None, {})
    assert res[0].status == "fehlgeschlagen" and "Audit_Pruefung" in res[0].nachweise[0].detail


def test_bericht_und_vorlage_werden_geschrieben(tmp_path):
    code = bericht.main(["--abnahme", str(tmp_path)])
    assert code == 0
    assert (tmp_path / "abnahme-bericht.md").exists() and (tmp_path / "abnahme-manuell.json").exists()
    data = json.loads((tmp_path / "abnahme-bericht.json").read_text(encoding="utf-8"))
    assert len(data["kriterien"]) == 41 and data["zaehlung"]["bestanden"] == 0, "ohne Ergebnisse nichts bestanden"
