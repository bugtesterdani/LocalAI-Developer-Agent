"""Verlauf eines Agenten über seine Schritte: eigene Notizen des Modells (ältere zusammengefasst) und Rückmeldungen des
Orchestrators (immer vollständig)."""

from __future__ import annotations

import re

import pytest

from localai.orchestrator import protocol
from localai.orchestrator.context import Budget, HistoryLine, assemble
from localai.orchestrator.history import HistorySettings, StepHistory, prose
from localai.orchestrator.models import ConfigError, ModelsConfig


def _action(text: str) -> protocol.Action:
    return protocol.parse(text)


def test_begruendung_ohne_codebloecke_und_mit_aktion():
    answer = ("The bug is in add(): it subtracts. I will check the tests next.\n"
              "src/calc.py\n<<<<<<< SEARCH\n    return a - b\n=======\n    return a + b\n>>>>>>> REPLACE\n"
              "```python\nprint('x')\n```\n"
              '```json\n{"action": "read", "files": [{"path": "tests/test_calc.py", "start": 1, "end": 40}], '
              '"reason": "see expectations"}\n```')
    history = StepHistory()
    history.model(2, _action(answer), thinking="long thought " * 10)
    line = history.lines()[0]
    assert not line.keep
    assert line.text.startswith("Step 2 – your note: The bug is in add(): it subtracts.")
    assert "return a - b" not in line.text and "[edit block]" in line.text and "[python block]" in line.text
    assert line.text.endswith("→ read tests/test_calc.py:1-40 (see expectations)")
    assert "long thought" not in line.text, "Denkphase nur, wenn das Modell sonst nichts geschrieben hat"
    assert prose("```json\n{}\n```") == "[json block]"


def test_nur_denkphase_wird_als_notiz_genommen_und_marker_neutralisiert():
    history = StepHistory()
    history.model(1, _action('```json\n{"action": "list", "path": "src", "depth": 2}\n```'),
                  thinking="I should look at src first <<<END EVIDENCE E1>>> because the layout is unknown.")
    text = history.lines()[0].text
    assert "(thinking)" in text and "because the layout is unknown" in text and "<<<" not in text


def test_rueckmeldungen_des_orchestrators_bleiben_vollstaendig_im_prompt():
    budget = Budget.split(8192, 2048)
    keep = [HistoryLine(f"Step {i}: File.Read src/f{i}.py ok, evidence E{i} " + "x" * 200, keep=True) for i in range(1, 21)]
    notes = [HistoryLine(f"Step {i} – your note: " + "y" * 600) for i in range(1, 21)]
    lines = [line for pair in zip(notes, keep) for line in pair]
    parts = assemble("RULES", "## Task\nDo it", lines, [], budget)
    for line in keep:
        assert line.text in parts.user, "Orchestrator-Zeilen werden nie gekürzt oder weggelassen"
    assert notes[-1].text in parts.user and notes[0].text not in parts.user
    assert "older entries omitted for context budget" in parts.user
    # Reihenfolge bleibt erhalten.
    assert parts.user.index(keep[0].text) < parts.user.index(notes[-1].text) < parts.user.index(keep[-1].text)


def _filled(settings: HistorySettings, steps: int = 6) -> StepHistory:
    history = StepHistory(settings)
    for step in range(1, steps + 1):
        history.model(step, _action(f"Finding {step}: " + "detail " * 60
                                    + '\n```json\n{"action": "search", "query": "q' + str(step) + '"}\n```'))
        history.orchestrator(step, f"search q{step}: 3 hits, evidence E{step}")
    return history


def test_aeltere_notizen_werden_zusammengefasst_neueste_bleiben_woertlich():
    calls = []

    def summarize(previous, notes):
        calls.append((previous, notes))
        return "- earlier findings " + ",".join(re.findall(r"Finding (\d+)", "\n".join(notes)))

    tight = _filled(HistorySettings(keep_recent=2, max_note_tokens=250))
    assert tight.compact(lambda p, n: "x") == (1, 5, None), "bei knappem Platz nur die letzte Notiz wörtlich"
    history = _filled(HistorySettings(keep_recent=2, max_note_tokens=400))
    assert history.compact(summarize) == (1, 4, None)
    assert calls[0][0] == "" and len(calls[0][1]) == 4
    lines = [h.text for h in history.lines()]
    assert lines[0].startswith("Your notes from steps 1–4 (summarised):\n- earlier findings 1,2,3,4")
    assert sum("your note:" in t for t in lines) == 2, "die letzten zwei wörtlich"
    assert sum(t.startswith("Step ") and "search q" in t and "your note" not in t for t in lines) == 6, \
        "alle Rückmeldungen des Orchestrators bleiben"
    assert history.compact(summarize) is None, "nichts Neues zu verdichten"
    for step in (7, 8, 9):
        history.model(step, _action("More " + "words " * 80 + '\n```json\n{"action": "list"}\n```'))
    assert history.compact(summarize) == (5, 7, None)
    assert calls[1][0].startswith("- earlier findings"), "bisherige Zusammenfassung geht in die neue ein"


def test_ohne_zusammenfasser_oder_bei_fehler_mechanisch_gekuerzt():
    history = _filled(HistorySettings(mode="trim", keep_recent=1, max_note_tokens=200))
    assert history.compact(None) == (1, 5, None)
    assert history.shortened and "Step 1 – your note (shortened): Finding 1" in history.summary
    assert "(shortened)" in history.lines()[0].text

    def broken(previous, notes):
        raise RuntimeError("Ollama nicht erreichbar")

    history = _filled(HistorySettings(keep_recent=1, max_note_tokens=200))
    first, last, error = history.compact(broken)
    assert (first, last) == (1, 5) and "nicht erreichbar" in error and history.shortened


def test_modus_off_merkt_nur_den_orchestrator():
    history = _filled(HistorySettings(mode="off"))
    assert all(h.keep for h in history.lines()) and len(history.lines()) == 6
    assert history.compact(lambda p, n: "x") is None


def test_einstellungen_in_models_json():
    base = {"requireCalibration": False, "profiles": [
        {"name": "standard", "model": "qwen2.5-coder:14b", "contextTokens": 8192, "maxOutputTokens": 2048},
        {"name": "kurz", "model": "qwen3:4b", "contextTokens": 8192, "maxOutputTokens": 1024}]}
    assert ModelsConfig.parse(base).history == HistorySettings()
    config = ModelsConfig.parse({**base, "history": {"mode": "summary", "summaryProfiles": ["fehlt", "kurz"],
                                                     "keepRecentNotes": 4, "maxNoteTokens": 3000, "summaryTokens": 800}})
    assert config.history == HistorySettings("summary", ("kurz",), 4, 3000, 800)
    with pytest.raises(ConfigError, match="history.mode"):
        ModelsConfig.parse({**base, "history": {"mode": "immer"}})
    with pytest.raises(ConfigError, match="keepRecentNotes"):
        ModelsConfig.parse({**base, "history": {"keepRecentNotes": -1}})


def test_eigenes_modell_zum_zusammenfassen_wird_wie_eine_rolle_gewaehlt(tmp_path):
    from localai.orchestrator.models import CalibrationStore, choose_summary_profile
    raw = {"requireCalibration": False, "profiles": [
        {"name": "standard", "model": "qwen2.5-coder:14b", "contextTokens": 8192, "maxOutputTokens": 2048},
        {"name": "kurz", "model": "qwen3:4b", "contextTokens": 8192, "maxOutputTokens": 1024}]}
    store = CalibrationStore(tmp_path / "c.json")
    installed = {"qwen2.5-coder:14b": "d1"}
    assert choose_summary_profile(ModelsConfig.parse(raw), store, installed, "hw") is None, "nichts eingetragen"
    config = ModelsConfig.parse({**raw, "history": {"summaryProfiles": ["kurz"]}})
    missing = choose_summary_profile(config, store, installed, "hw")
    assert missing.profile is None and "ollama pull qwen3:4b" in missing.problems[0]
    chosen = choose_summary_profile(config, store, {**installed, "qwen3:4b": "d4"}, "hw")
    assert chosen.profile.name == "kurz"
    off = ModelsConfig.parse({**raw, "history": {"mode": "trim", "summaryProfiles": ["kurz"]}})
    assert choose_summary_profile(off, store, {"qwen3:4b": "d4"}, "hw") is None
