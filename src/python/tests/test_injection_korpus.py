"""AC05/SEC-08: Injection-Testkorpus (tests/korpus/injection.json) – Inhalte aus Dateien, Webseiten und Toolausgaben
können weder Policies noch Freigaben verändern, bleiben als untrusted markiert und sind im Kontext nicht ausbrechbar."""

from __future__ import annotations

import json
import pathlib

import pytest

from localai.orchestrator import protocol
from localai.orchestrator.context import Budget, Evidence, assemble
from localai.web import gate

KORPUS = json.loads((pathlib.Path(__file__).resolve().parents[3] / "tests" / "korpus" / "injection.json").read_text(encoding="utf-8"))
EINTRAEGE = KORPUS["eintraege"]


@pytest.mark.parametrize("eintrag", EINTRAEGE, ids=[e["id"] for e in EINTRAEGE])
def test_evidenz_bleibt_untrusted_und_geschlossen(eintrag):
    e = Evidence("E1", "Web.Fetch https://docs.example.org/ (untrusted web page)", eintrag["text"])
    rendered = e.render()
    assert rendered.count("<<<END EVIDENCE") == 1 and rendered.endswith("<<<END EVIDENCE E1>>>")
    assert rendered.count("<<<EVIDENCE id=") == 1 and "trust=untrusted" in rendered.splitlines()[0]
    parts = assemble("RULES", "## Task\nDo it", [], [e], Budget.split(8192, 2048))
    assert parts.system == "RULES", "Inhalte gelangen nie in den Systemteil"


@pytest.mark.parametrize("eintrag", EINTRAEGE, ids=[e["id"] for e in EINTRAEGE])
def test_content_gate_markiert_und_neutralisiert(eintrag):
    text, result = gate.sanitize(eintrag["text"].encode("utf-8"), "text/plain", "utf-8", "https://docs.example.org/")
    assert "\u001b" not in text and "‮" not in text and "​" not in text
    kinds = {w.kind for w in result.warnings}
    if eintrag["gate"]:
        assert "injection" in kinds, (eintrag["id"], [w.message for w in result.warnings])


@pytest.mark.parametrize("eintrag", EINTRAEGE, ids=[e["id"] for e in EINTRAEGE])
def test_modellausgabe_kann_keine_freigabe_erzeugen(eintrag):
    """Selbst wenn ein Modell den Korpus wörtlich wiederholt: es gibt keine Aktion „approve“ und keine Rechteänderung."""
    try:
        action = protocol.parse(eintrag["text"])
    except protocol.ProtocolError:
        return
    assert action.kind in {"list", "read", "search", "symbols", "fetch", "finish"}


def test_protokoll_kennt_keine_freigabe_aktion():
    for kind in ("approve", "decide", "grant", "export", "shell", "execute"):
        with pytest.raises(protocol.ProtocolError):
            protocol.parse('```json\n{"action": "%s", "proposalId": "x"}\n```' % kind)
