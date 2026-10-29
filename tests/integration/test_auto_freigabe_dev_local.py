"""autoApprove mit echtem Broker und Worker (dev-local-unsafe): erst nach ausdrücklicher Bestätigung im Run automatisch,
nur die Tools der Projektdatei, nie Snapshot/Übernahme; das Audit-Journal bleibt prüfbar."""

from __future__ import annotations

import uuid

import pytest

from localai.contracts import AgentRole, ProposalRequest, ProposalState, ToolTarget
from localai.pipe import BrokerError

from test_orchestrator_broker import setup
from test_sandbox_dev_local import approve_and_wait, req

pytestmark = pytest.mark.integration


def confirm(tools) -> ProposalRequest:
    return ProposalRequest(schema_version=1, run_id="run", task_id="T0", agent=AgentRole.Planner, tool="Run.AutoApprove",
                           tool_version="1", purpose="Automatische Freigabe bestätigen", arguments={"tools": tools}, targets=[],
                           snapshot_id="-", idempotency_key=uuid.uuid4().hex)


def test_auto_freigabe_nur_nach_bestaetigung(tmp_path, broker_factory):
    b = setup(tmp_path, broker_factory, ["-c", "import time; time.sleep(120)"],
              project_extra={"autoApprove": {"File.Read": True, "File.List": True, "Workspace.Snapshot": False}})
    ui, orch = b.ui(), b.orchestrator()
    assert orch.toolchains_available()["autoApprove"] == ["File.List", "File.Read"]
    root = orch.grants()[0].root
    snapshot = orch.submit(req("Workspace.Snapshot", {"grantId": "p", "root": root}))
    assert snapshot.state == ProposalState.AwaitingApproval
    snap = approve_and_wait(ui, orch, snapshot).result.target_results[0].text

    read = req("File.Read", {"grantId": "p"}, ["src/calc.py"], snap)
    assert orch.submit(read).state == ProposalState.AwaitingApproval, "ohne Bestätigung einzeln"

    with pytest.raises(BrokerError):
        orch.submit(confirm(["File.List", "File.Read", "Patch.Apply"]))  # nie mehr als in der Projektdatei
    view = orch.submit(confirm(["File.List", "File.Read"]))
    assert view.state == ProposalState.AwaitingApproval
    assert approve_and_wait(ui, orch, view).state == ProposalState.Succeeded

    auto = orch.submit(req("File.Read", {"grantId": "p"}, ["src/calc.py"], snap))
    assert auto.state != ProposalState.AwaitingApproval
    outcome = orch.wait_final(auto.proposal.proposal_id, timeout=30, poll=0.1)
    assert outcome.state == ProposalState.Succeeded and "return a + b" in outcome.result.target_results[0].text
    # Nicht eingeschaltet bzw. nie automatisch: weiter einzeln.
    again = orch.submit(req("Workspace.Snapshot", {"grantId": "p", "root": root}))
    assert again.state == ProposalState.AwaitingApproval
    assert ui.audit_verify()["ok"] is True

    # Not-Aus beendet die automatische Freigabe.
    ui.emergency_stop()
    ui.release()
    later = orch.submit(req("File.Read", {"grantId": "p"}, [ToolTarget(target_id="t1", path="src/calc.py")], snap))
    assert later.state == ProposalState.AwaitingApproval


def test_nie_erlaubtes_tool_verhindert_den_start(tmp_path, broker_factory):
    with pytest.raises(RuntimeError) as err:
        setup(tmp_path, broker_factory, ["-c", "import time; time.sleep(120)"],
              project_extra={"autoApprove": {"Workspace.ExportPatch": True}})
    assert "Exitcode 4" in str(err.value) and "Workspace.ExportPatch kann nie automatisch genehmigt werden" in str(err.value)
