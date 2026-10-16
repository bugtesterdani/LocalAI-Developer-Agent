"""M2a: echter Ablauf Broker → signierter Job → Worker → geprüftes Ergebnis (Entwicklungsmodus ohne VM).

Prüft Ordnerrechte, Snapshot-Bindung, Verknüpfungen und Hash-Abweichungen gegen den echten Broker-Prozess.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time
import uuid

import pytest

from localai.contracts import AgentRole, ProposalRequest, ProposalState, ResultStatus, ToolTarget
from localai.pipe import BrokerError

pytestmark = pytest.mark.integration
ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def sandbox(broker_factory, tmp_path):
    project_dir = tmp_path / "projekt"
    (project_dir / "src").mkdir(parents=True)
    (project_dir / "README.md").write_text("# Testprojekt\nZeile 2\n", encoding="utf-8")
    (project_dir / "src" / "Größe.cs").write_text("class Größe {}\n", encoding="utf-8")
    (project_dir / "bin").mkdir()
    (project_dir / "bin" / "app.dll").write_bytes(b"\x00binaer")
    outside = tmp_path / "ausserhalb"
    outside.mkdir()
    (outside / "geheim.txt").write_text("GEHEIM", encoding="utf-8")
    symlink = True
    try:
        os.symlink(outside / "geheim.txt", project_dir / "link.txt")
    except (OSError, NotImplementedError):
        symlink = False
    other = tmp_path / "anderes"
    other.mkdir()
    (other / "x.txt").write_text("x", encoding="utf-8")
    project = {
        "schemaVersion": 1, "projectId": "it", "name": "Integration",
        "grants": [
            {"grantId": "p", "root": str(project_dir), "rights": ["List", "Read"], "recursive": True},
            {"grantId": "nurliste", "root": str(other), "rights": ["List"], "recursive": True},
        ],
    }
    project_file = tmp_path / "projekt.json"
    project_file.write_text(json.dumps(project), encoding="utf-8")
    b = broker_factory("--mode", "dev-local-unsafe", "--project", str(project_file),
                       "--config", str(ROOT / "config" / "localai.json"))
    return {"broker": b, "root": str(project_dir), "other": str(other), "symlink": symlink}


def req(tool, args, targets=(), snapshot="-") -> ProposalRequest:
    return ProposalRequest(
        schema_version=1, run_id="run", task_id="task", agent=AgentRole.Coder, tool=tool, tool_version="1",
        purpose="Integration", arguments=args,
        targets=[ToolTarget(target_id=f"t{i}", path=p) if isinstance(p, str) else p for i, p in enumerate(targets, start=1)],
        snapshot_id=snapshot, idempotency_key=uuid.uuid4().hex)


def approve_and_wait(ui, orch, view, timeout=30.0):
    ui.decide(view.proposal.proposal_id, view.proposal_sha256, approve=True)
    return orch.wait_final(view.proposal.proposal_id, timeout=timeout, poll=0.1)


def snapshot(sb):
    b = sb["broker"]
    ui, orch = b.ui(), b.orchestrator()
    root = next(g.root for g in orch.grants() if g.grant_id == "p")
    outcome = approve_and_wait(ui, orch, orch.submit(req("Workspace.Snapshot", {"grantId": "p", "root": root})))
    assert outcome.state == ProposalState.Succeeded, outcome
    return ui, orch, outcome.result.target_results[0].text, outcome.result.target_results[0].message


def test_snapshot_und_lesen_ueber_den_worker(sandbox):
    ui, orch, snap, stats = snapshot(sandbox)
    assert stats.startswith("2 Dateien")  # README.md und src/Größe.cs; bin/ ausgeschlossen, Verknüpfung übersprungen
    readme_sha = hashlib.sha256("# Testprojekt\nZeile 2\n".encode()).hexdigest()
    view = orch.submit(req("File.ReadMany", {"grantId": "p"}, [
        ToolTarget(target_id="t1", path="README.md", expected_sha256=readme_sha),
        ToolTarget(target_id="t2", path="src/Größe.cs"),
        ToolTarget(target_id="t3", path="link.txt"),
        ToolTarget(target_id="t4", path="bin/app.dll"),
    ], snap))
    outcome = approve_and_wait(ui, orch, view)
    by_id = {r.target_id: r for r in outcome.result.target_results}
    assert by_id["t1"].text == "# Testprojekt\nZeile 2\n"
    assert by_id["t2"].text == "class Größe {}\n"
    assert by_id["t3"].status == ResultStatus.Failed  # Verknüpfung nie im Snapshot
    assert by_id["t4"].status == ResultStatus.Failed  # bin/ ausgeschlossen
    assert outcome.result.untrusted is True
    assert all("GEHEIM" not in (r.text or "") for r in outcome.result.target_results)


def test_hashabweichung_erzwingt_neuen_vorschlag(sandbox):
    ui, orch, snap, _ = snapshot(sandbox)
    view = orch.submit(req("File.Read", {"grantId": "p"},
                           [ToolTarget(target_id="t1", path="README.md", expected_sha256="0" * 64)], snap))
    outcome = approve_and_wait(ui, orch, view)
    assert outcome.state == ProposalState.Failed
    assert "Hash" in outcome.result.target_results[0].message


def test_ordnerrechte_und_snapshot_bindung(sandbox):
    ui, orch, snap, _ = snapshot(sandbox)
    with pytest.raises(BrokerError) as err:  # Grant ohne Read-Recht
        orch.submit(req("File.Read", {"grantId": "nurliste"}, ["x.txt"], snap))
    assert err.value.code == 403
    with pytest.raises(BrokerError) as err:  # unbekannter Snapshot
        orch.submit(req("File.Read", {"grantId": "p"}, ["README.md"], "f" * 64))
    assert err.value.code == 409
    with pytest.raises(BrokerError) as err:  # Snapshot eines anderen Ordners
        orch.submit(req("Workspace.Snapshot", {"grantId": "p", "root": sandbox["other"]}))
    assert err.value.code == 403
    with pytest.raises(BrokerError) as err:  # Ausbruch per Pfad
        orch.submit(req("File.Read", {"grantId": "p"}, ["../ausserhalb/geheim.txt"], snap))
    assert err.value.code == 403


def test_snapshot_ohne_freigabe_liest_nichts(sandbox):
    b = sandbox["broker"]
    orch = b.orchestrator()
    root = next(g.root for g in orch.grants() if g.grant_id == "p")
    view = orch.submit(req("Workspace.Snapshot", {"grantId": "p", "root": root}))
    time.sleep(1.0)
    work = pathlib.Path(b.data_dir) / "sandbox-dev" / "work" / "snapshots"
    assert not any(work.glob("*.manifest.json")) if work.exists() else True
    assert orch.outcome(view.proposal.proposal_id).state == ProposalState.AwaitingApproval


@pytest.mark.skipif(os.name == "nt", reason="würde unter Windows einen echten VM-Aufbau starten")
def test_hyperv_ohne_vm_bleibt_gesperrt(broker_factory, tmp_path):
    """Ohne verfügbare VM wird nichts ausgeführt; die Oberfläche sieht den Grund."""
    b = broker_factory("--mode", "hyperv", "--config", str(ROOT / "config" / "localai.json"))
    ui, orch = b.ui(), b.orchestrator()
    deadline = time.monotonic() + 15
    while not (ui.status().get("sandbox") or "").startswith("gesperrt") and time.monotonic() < deadline:
        time.sleep(0.1)
    assert (ui.status().get("sandbox") or "").startswith("gesperrt")
    grant = orch.grants()[0]
    outcome = approve_and_wait(ui, orch, orch.submit(req("Workspace.Snapshot", {"grantId": grant.grant_id, "root": grant.root})))
    assert outcome.state == ProposalState.Failed
    assert "nicht bereit" in outcome.result.target_results[0].message
    events = ui.wait_events(0, timeout_ms=100)
    assert any(e.type == "sandbox.status" for e in events)
