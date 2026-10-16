import json
import os
import pathlib
import subprocess
import sys

import pytest
from pydantic import ValidationError

from localai import canonical
from localai.contracts import ToolProposal, UserDecision, proposal_sha256

ROOT = pathlib.Path(__file__).resolve().parents[3]
VECTORS = json.loads((ROOT / "contracts" / "testvectors" / "canonical.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("vector", VECTORS["valid"], ids=lambda v: v["name"])
def test_kanonisierung_entspricht_testvektoren(vector):
    canon = canonical.canonicalize(canonical.parse(vector["input"]))
    assert canon == vector["canonical"]
    assert canonical.sha256_hex(canonical.parse(vector["input"])) == vector["sha256"]


@pytest.mark.parametrize("vector", VECTORS["invalid"], ids=lambda v: v["name"])
def test_ungueltige_eingaben_werden_abgelehnt(vector):
    with pytest.raises((canonical.CanonicalJsonError, ValueError)):
        canonical.canonicalize(canonical.parse(vector["input"]))


def test_vorschlag_ueber_pydantic_modell_ergibt_identischen_hash():
    vector = next(v for v in VECTORS["valid"] if v["name"] == "Vorschlag")
    proposal = ToolProposal.model_validate_json(vector["input"])
    assert proposal_sha256(proposal) == vector["sha256"]


def test_unbekannte_felder_und_fehlende_pflichtfelder_werden_abgelehnt():
    with pytest.raises(ValidationError):
        UserDecision.model_validate({"proposalId": "a", "proposalSha256": "b", "decision": "Approve", "extra": 1})
    with pytest.raises(ValidationError):
        UserDecision.model_validate({"proposalId": "a", "decision": "Approve"})


def test_generierte_dateien_sind_aktuell():
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "codegen" / "generate.py"), "--check"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            env={**os.environ, "PYTHONUTF8": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
