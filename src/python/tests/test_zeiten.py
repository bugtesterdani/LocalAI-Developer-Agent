"""Zeitaufschlüsselung eines Runs: Ollamas Zeiten je Modell, Freigaben und Sandbox, Hinweise."""

from __future__ import annotations

import json
from types import SimpleNamespace

from localai.orchestrator.timings import RunTimings


def result(model="qwen2.5-coder:7b", load=0.0, prompt=(1000, 2.0), output=(200, 10.0), total=None):
    return SimpleNamespace(model=model, load_seconds=load, prompt_tokens=prompt[0], prompt_seconds=prompt[1],
                           output_tokens=output[0], output_seconds=output[1],
                           total_seconds=total if total is not None else load + prompt[1] + output[1])


def test_aufschluesselung_je_modell_und_kategorie(tmp_path):
    t = RunTimings()
    t.prepare_seconds = 3
    t.model("Coder", result(load=12.0), gpu_wait=1.0)
    t.model("Coder", result())
    t.model("Reviewer", result(model="gemma3:4b", load=8.0, output=(100, 4.0)))
    t.tool("File.Read", 20.0, 0.5)
    t.tool("File.Read", 0.0, 0.3, automatic=True)
    t.tool("Toolchain.Test", 5.0, 40.0)
    text = t.report()
    assert text.startswith("Zeitaufschlüsselung dieses Runs")
    assert "laden 20 s" in text and "Prompt einlesen 6.0 s" in text
    assert "qwen2.5-coder:7b (Coder): 2 Aufrufe, 1× geladen (12 s)" in text
    assert "gemma3:4b (Reviewer): 1 Aufrufe, 1× geladen" in text
    assert "Lesen, Suchen, Patches: 2× – Warten auf Freigabe 20 s, Ausführung 0.8 s, davon 1 automatisch genehmigt" in text
    assert "Sandbox: Build/Test/Ausführen: 1× – Warten auf Freigabe 5.0 s, Ausführung 40 s" in text
    t.save(tmp_path / "zeiten.jsonl", "run-1", "Ziel")
    t.save(tmp_path / "zeiten.jsonl", "run-2", "Ziel 2")
    rows = [json.loads(line) for line in (tmp_path / "zeiten.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["runId"] for r in rows] == ["run-1", "run-2"]
    assert rows[0]["models"]["qwen2.5-coder:7b"]["loads"] == 1 and rows[0]["totals"]["load"] == 20.0


def test_hinweise_modellwechsel_cpu_freigaben():
    t = RunTimings()
    for model in ("a", "b", "a", "b"):
        t.model("Coder", result(model=model, load=10.0, output=(50, 1.0)))
    t.model("Tester", result(model="langsam", output=(400, 100.0)))
    t.started -= 600
    t.tool("File.Read", 300.0, 1.0)
    hints = " ".join(t.hints())
    assert "4× (neu) geladen" in hints
    assert "langsam erzeugt nur 4.0 Token/s" in hints and "ollama ps" in hints
    assert "Warten auf Freigaben" in hints and "autoApprove" in hints


def test_ohne_auffaelligkeiten_keine_hinweise_und_leer():
    t = RunTimings()
    assert t.empty
    t.model("Coder", result(output=(500, 10.0)))
    assert not t.hints() and not t.empty
