"""Sicherheitsprüffälle M1 gegen den echten Broker-Prozess (Python-Orchestrator ↔ C#-Broker).

SEC-01 keine Ausführung ohne Entscheidung · SEC-02 Hashbindung · SEC-03 genau eine Ausführung
SEC-12 Neustart ohne Wiederholung · SEC-15 Not-Aus · SEC-17 Rollentrennung · SEC-23 Sammelfreigabe
"""

from __future__ import annotations

import concurrent.futures
import subprocess
import time
import uuid

import pytest

from localai.contracts import AgentRole, ProposalRequest, ProposalState, ToolTarget, proposal_sha256
from localai.pipe import BrokerError
from tests_support import wait_until

pytestmark = pytest.mark.integration


def make_request(tool: str = "File.Read", paths: list[str] | None = None, key: str | None = None,
                 purpose: str = "Integrationstest") -> ProposalRequest:
    paths = paths or ["src/Importer.cs"]
    return ProposalRequest(
        schema_version=1, run_id="run-it", task_id="task-it", agent=AgentRole.Coder, tool=tool,
        tool_version="1", purpose=purpose, arguments={},
        targets=[ToolTarget(target_id=f"t{i}", path=p) for i, p in enumerate(paths, start=1)],
        snapshot_id="snap-it", idempotency_key=key or uuid.uuid4().hex,
    )


def invocations(ui) -> int:
    return ui.status()["testAdapterInvocations"]


def test_hash_ist_in_csharp_und_python_identisch(broker):
    orch = broker.orchestrator()
    view = orch.submit(make_request("File.ReadMany", ["src/Größe.cs", "docs/Übernahme ä€😀.md", "a/b/c.txt"],
                                    purpose='Zweck mit "Zitat", Tab\t und Zeilenumbruch\n'))
    assert proposal_sha256(view.proposal) == view.proposal_sha256


def test_sec01_ohne_entscheidung_keine_ausfuehrung(broker_factory):
    b = broker_factory("--ttl-seconds", "2")
    orch, ui = b.orchestrator(), b.ui()
    view = orch.submit(make_request())
    time.sleep(1.0)
    assert orch.outcome(view.proposal.proposal_id).state == ProposalState.AwaitingApproval
    assert invocations(ui) == 0
    wait_until(lambda: orch.outcome(view.proposal.proposal_id).state == ProposalState.Expired, timeout=6)
    with pytest.raises(BrokerError) as err:
        ui.decide(view.proposal.proposal_id, view.proposal_sha256, approve=True)
    assert err.value.code == 410
    assert invocations(ui) == 0


def test_sec01_ablehnung(broker):
    orch, ui = broker.orchestrator(), broker.ui()
    view = orch.submit(make_request())
    ui.decide(view.proposal.proposal_id, view.proposal_sha256, approve=False)
    assert orch.wait_final(view.proposal.proposal_id, timeout=5).state == ProposalState.Denied
    assert invocations(ui) == 0


def test_sec02_falscher_hash_und_parameteraenderung(broker):
    orch, ui = broker.orchestrator(), broker.ui()
    a = orch.submit(make_request(key="fest"))
    b = orch.submit(make_request(paths=["src/Anderes.cs"]))
    with pytest.raises(BrokerError) as err:
        ui.decide(a.proposal.proposal_id, b.proposal_sha256, approve=True)
    assert err.value.code == 409
    with pytest.raises(BrokerError) as err:
        orch.submit(make_request(paths=["src/Geaendert.cs"], key="fest"))
    assert err.value.code == 409
    assert ui.get(a.proposal.proposal_id).state == ProposalState.AwaitingApproval
    assert invocations(ui) == 0


def test_sec03_gleichzeitige_entscheidungen_fuehren_genau_einmal_aus(broker):
    orch = broker.orchestrator()
    view = orch.submit(make_request())
    clients = [broker.ui() for _ in range(8)]

    def decide(client):
        try:
            client.decide(view.proposal.proposal_id, view.proposal_sha256, approve=True)
            return True
        except BrokerError as err:
            assert err.code == 409
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(decide, clients))
    assert results.count(True) == 1
    assert orch.wait_final(view.proposal.proposal_id, timeout=5).state == ProposalState.Succeeded
    assert invocations(broker.ui()) == 1


def test_sec12_neustart_waehrend_ausfuehrung_und_offene_freigaben(broker_factory, tmp_path):
    data = tmp_path / "restart"
    first = broker_factory("--test-adapter-delay-ms", "30000", data_dir=data)
    orch, ui = first.orchestrator(), first.ui()
    running = orch.submit(make_request())
    pending = orch.submit(make_request(paths=["src/Offen.cs"]))
    ui.decide(running.proposal.proposal_id, running.proposal_sha256, approve=True)
    wait_until(lambda: orch.outcome(running.proposal.proposal_id).state == ProposalState.Running, timeout=5)
    first.stop(hard=True)  # Absturz simulieren

    second = broker_factory(data_dir=data)
    orch2, ui2 = second.orchestrator(), second.ui()
    assert orch2.outcome(running.proposal.proposal_id).state == ProposalState.OutcomeUnknown
    assert orch2.outcome(pending.proposal.proposal_id).state == ProposalState.Invalidated
    with pytest.raises(BrokerError) as err:
        ui2.decide(pending.proposal.proposal_id, pending.proposal_sha256, approve=True)
    assert err.value.code == 409
    time.sleep(0.5)
    assert invocations(ui2) == 0  # keine automatische Wiederholung


def test_sec15_notaus_bricht_laufenden_job_ab(broker_factory):
    b = broker_factory("--test-adapter-delay-ms", "30000")
    orch, ui = b.orchestrator(), b.ui()
    running = orch.submit(make_request())
    pending = orch.submit(make_request(paths=["src/Offen.cs"]))
    ui.decide(running.proposal.proposal_id, running.proposal_sha256, approve=True)
    wait_until(lambda: orch.outcome(running.proposal.proposal_id).state == ProposalState.Running, timeout=5)
    ui.emergency_stop()
    assert orch.wait_final(running.proposal.proposal_id, timeout=5).state == ProposalState.Cancelled
    assert orch.outcome(pending.proposal.proposal_id).state == ProposalState.Invalidated
    with pytest.raises(BrokerError) as err:
        orch.submit(make_request())
    assert err.value.code == 409
    ui.release()
    orch.submit(make_request())


def test_sec17_rollentrennung(broker):
    orch, ui = broker.orchestrator(), broker.ui()
    view = orch.submit(make_request())
    for call in (lambda: orch.decide(view.proposal.proposal_id, view.proposal_sha256, approve=True),
                 lambda: orch.narrow(view.proposal.proposal_id, view.proposal_sha256, ["t1"]),
                 lambda: orch.pending(),
                 lambda: orch.emergency_stop(),
                 lambda: orch.pipe.call("dev.startFakeAgent")):
        with pytest.raises(BrokerError) as err:
            call()
        assert err.value.code == 403
    with pytest.raises(BrokerError) as err:
        ui.submit(make_request())  # UI darf keine Agentenvorschläge einschleusen
    assert err.value.code == 403
    from localai.broker_client import BrokerClient
    with pytest.raises(BrokerError) as err:
        BrokerClient(broker.pipe, "falsch").ping()
    assert err.value.code == 403
    with pytest.raises(BrokerError):
        orch.pipe.call("execute", {"command": "calc.exe"})
    assert ui.get(view.proposal.proposal_id).state == ProposalState.AwaitingApproval
    assert invocations(ui) == 0


def test_sec23_sammelfreigabe_mit_abwahl(broker):
    orch, ui = broker.orchestrator(), broker.ui()
    paths = [f"src/Datei{i}.cs" for i in range(1, 7)]
    original = orch.submit(make_request("File.ReadMany", paths))
    with pytest.raises(BrokerError) as err:
        ui.narrow(original.proposal.proposal_id, original.proposal_sha256, ["t1", "t7"])
    assert err.value.code == 422
    narrowed = ui.narrow(original.proposal.proposal_id, original.proposal_sha256, ["t2", "t4", "t6"])
    assert narrowed.proposal.parent_proposal_id == original.proposal.proposal_id
    assert proposal_sha256(narrowed.proposal) == narrowed.proposal_sha256 != original.proposal_sha256
    with pytest.raises(BrokerError) as err:
        ui.decide(original.proposal.proposal_id, original.proposal_sha256, approve=True)
    assert err.value.code == 409
    ui.decide(narrowed.proposal.proposal_id, narrowed.proposal_sha256, approve=True)
    outcome = orch.wait_final(original.proposal.proposal_id, timeout=5)
    assert outcome.final_proposal_id == narrowed.proposal.proposal_id
    assert outcome.state == ProposalState.Succeeded
    assert [r.target_id for r in outcome.result.target_results] == ["t2", "t4", "t6"]
    assert outcome.result.skipped_target_ids == ["t1", "t3", "t5"]
    assert outcome.result.untrusted is True
    assert invocations(ui) == 1


@pytest.mark.parametrize("path", ["../geheim.txt", "C:\\Windows\\win.ini", "\\\\server\\share\\x", "a.txt:stream",
                                  "CON", "/etc/passwd", "src//x.cs"])
def test_unzulaessige_pfade_werden_abgewiesen(broker, path):
    with pytest.raises(BrokerError) as err:
        broker.orchestrator().submit(make_request(paths=[path]))
    assert err.value.code == 403


def test_manipuliertes_journal_verhindert_start(broker_factory, tmp_path):
    data = tmp_path / "tamper"
    first = broker_factory(data_dir=data)
    first.orchestrator().submit(make_request(purpose="Original"))
    first.stop()
    journal = data / "audit.jsonl"
    journal.write_text(journal.read_text(encoding="utf-8").replace("Original", "Gefälscht"), encoding="utf-8")
    from conftest import DEFAULT_DLL
    result = subprocess.run(["dotnet", str(DEFAULT_DLL), "--data", str(data)], input="", capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 3
    assert "manipuliert" in result.stderr
