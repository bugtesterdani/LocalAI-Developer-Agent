"""Toolchain „testumgebung“: Prüfprogramme der Testumgebung (*.tproj.json) mit Szenarien (*.tsim.json) erstellen und
simulieren. Die Kommandozeile selbst lebt im Testumgebung-Repository (docs/kommandozeile.md)."""

from __future__ import annotations

import json
import pathlib

from localai.orchestrator.run import (
    default_verify,
    is_testumgebung_file,
    repo_facts,
    testumgebung_notes,
    wants_testumgebung,
)
from localai.worker import repoindex

ROOT = pathlib.Path(__file__).resolve().parents[3]
TOOLCHAIN = ROOT / "toolchains" / "testumgebung"


def test_projekt_und_szenariodateien_gelten_als_projektdateien():
    assert repoindex.PROJECT_FILES.search("pruefung/netzteil.tproj.json")
    assert repoindex.PROJECT_FILES.search("pruefung/normal.tsim.json")
    assert not repoindex.PROJECT_FILES.search("pruefung/daten.json")
    assert is_testumgebung_file("A/B.TPROJ.JSON") and not is_testumgebung_file("a.json")


def test_pruefung_mit_der_testumgebung_wird_vorgeschlagen():
    text = ("Repo map of snapshot abc – 3 files, no source files recognised\n"
            "Project/build files: pruefung/netzteil.tproj.json, pruefung/normal.tsim.json\n")
    projects, languages = repo_facts(text)
    manifest = json.loads((TOOLCHAIN / "toolchain.json").read_text(encoding="utf-8"))
    steps = [s.describe() for s in default_verify([manifest], projects, languages)]
    assert steps == ["testumgebung test (path=.)"]
    assert default_verify([manifest], ["src/App.csproj"], {"C#": 3}) == [], "ohne Prüfprogramm kein Lauf"


def test_formathinweise_nur_wenn_pruefprogramme_betroffen_sind():
    with_tc = ["dotnet", "testumgebung"]
    assert wants_testumgebung(["pruefung/netzteil.tproj.json"], [], "irgendwas", [])
    assert wants_testumgebung([], ["p/a.tproj.json"], "Grenzwert anpassen", with_tc)
    assert wants_testumgebung(["neu/x.json"], [], "Erstelle ein Prüfprogramm für das Netzteil", with_tc)
    assert not wants_testumgebung(["src/a.cs"], ["src/App.csproj"], "Fehler beheben", with_tc)
    assert not wants_testumgebung([], ["p/a.tproj.json"], "Prüfprogramm erweitern", ["dotnet"]), "ohne Toolchain nur bei Dateien"


def test_kurzfassung_ist_vollstaendig_und_klein_genug():
    notes = testumgebung_notes()
    assert not notes.startswith("<!--")
    for needed in ('"format":"testumgebung.projekt"', '"format":"testumgebung.szenario"', '"art"', "dutRun",
                   "analog.messen – Messung", "werker – Werkerinteraktion", "- Quelltext (Code, Pflicht",
                   "lint {path}", "run {script"):
        assert needed in notes, needed
    assert len(notes) < 12_000, "Kurzfassung belastet das Kontextfenster kleiner Modelle"


def test_selbsttest_der_vorlage_hat_projekt_und_passendes_szenario():
    scenario = json.loads((TOOLCHAIN / "selftest" / "normal.tsim.json").read_text(encoding="utf-8"))
    assert scenario["format"] == "testumgebung.szenario"
    project = json.loads((TOOLCHAIN / "selftest" / scenario["project"]).read_text(encoding="utf-8"))
    assert project["format"] == "testumgebung.projekt"
    steps = {s["id"] for s in project["loop"]["children"][0]["children"]}
    assert {a["step"] for a in scenario["answers"]} <= steps
    assert {e["step"] for e in scenario["expect"]["steps"]} <= steps
