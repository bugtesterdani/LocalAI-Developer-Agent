"""M3: echter Broker + echter Orchestrator-Prozess + simuliertes Ollama.

Prüft Chat → Run mit fünf Rollen, Rechte der neuen Methoden, Abbruch pro Agent und SEC-22
(Orchestrator abgestürzt oder hängt: kein Tool ohne Freigabe, Oberfläche bedienbar, Not-Aus wirkt).
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import sys
import time

import pytest

from localai.contracts import ActivityEvent, ActivityKind, ActivityReport, ProposalState
from localai.orchestrator.fake_ollama import FakeOllama
from localai.pipe import BrokerError

pytestmark = pytest.mark.integration
ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = str(ROOT / "src" / "python")


@pytest.fixture
def fake_ollama():
    fake = FakeOllama(delay=0.001).start()
    yield fake
    fake.stop()


def setup(tmp_path, broker_factory, orchestrator_args, heartbeat=30):
    project_dir = tmp_path / "projekt"
    (project_dir / "src").mkdir(parents=True)
    (project_dir / "README.md").write_bytes(b"# Rechner\n")
    (project_dir / "src" / "calc.py").write_bytes(b"def add(a, b):\n    return a + b\n")
    project = {"schemaVersion": 1, "projectId": "m3", "name": "M3",
               "grants": [{"grantId": "p", "root": str(project_dir), "rights": ["List", "Read"], "recursive": True}]}
    (tmp_path / "projekt.json").write_text(json.dumps(project), encoding="utf-8")
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"requireCalibration": False, "profiles": [
        {"name": "standard", "model": "qwen2.5-coder:14b", "contextTokens": 8192, "maxOutputTokens": 2048}]}),
        encoding="utf-8")
    config_dir = tmp_path / "cfg" / "config"
    config_dir.mkdir(parents=True)
    config = {
        "sandbox": {"mode": "dev-local-unsafe",
                    "devWorker": {"command": sys.executable, "arguments": ["-m", "localai.worker"], "workingDirectory": PY}},
        "orchestrator": {"command": sys.executable, "arguments": [a.replace("{models}", str(models)) for a in orchestrator_args],
                         "workingDirectory": PY, "heartbeatTimeoutSeconds": heartbeat},
    }
    (config_dir / "localai.json").write_text(json.dumps(config), encoding="utf-8")
    return broker_factory("--mode", "dev-local-unsafe", "--project", str(tmp_path / "projekt.json"),
                          "--config", str(config_dir / "localai.json"))


def collect(ui, after, until, timeout=60.0, approve=True):
    """Liest Aktivität und genehmigt (als Nutzer) offene Vorschläge, bis ``until(events)`` erfüllt ist."""
    events: list[ActivityEvent] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if approve:
            for view in ui.pending():
                try:
                    ui.decide(view.proposal.proposal_id, view.proposal_sha256, approve=True)
                except BrokerError:
                    pass
        batch = ui.wait_activity(after, 300)
        if batch:
            after = batch[-1].seq
            events.extend(batch)
        if until(events):
            return events, after
    raise AssertionError(f"Zeitüberschreitung; letzte Ereignisse: {[(e.agent, e.kind.value, (e.text or '')[:80]) for e in events[-15:]]}")


def wait_event(ui, type_, timeout=30.0):
    after = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for e in ui.wait_events(after, 500):
            after = e.seq
            if e.type == type_:
                return e
    raise AssertionError(f"Ereignis {type_} kam nicht")


def summary_done(events):
    return any(e.agent == "Orchestrator" and e.kind == ActivityKind.Chat and (e.text or "").startswith("Zusammenfassung")
               for e in events)


def test_chat_startet_run_mit_fuenf_rollen(tmp_path, broker_factory, fake_ollama):
    b = setup(tmp_path, broker_factory, ["-m", "localai.orchestrator", "--models", "{models}", "--ollama", fake_ollama.url])
    ui = b.ui()
    run_id = ui.chat_send("Bitte leere Eingaben in add abfangen")
    assert run_id.startswith("run-")
    events, _ = collect(ui, 0, summary_done, timeout=90)
    assert events[0].agent == "User" and events[0].text == "Bitte leere Eingaben in add abfangen"
    assert [e.seq for e in events] == sorted(e.seq for e in events)
    for role in ("Planner", "Coder", "Tester", "Debugger", "Reviewer"):
        mine = [e for e in events if e.agent == role]
        assert any(e.kind == ActivityKind.Output for e in mine), f"{role}: Live-Ausgabe"
        assert any(e.kind == ActivityKind.Chat for e in mine), f"{role}: Chatnachricht"
    assert all(e.run_id == run_id for e in events)
    # Alle Lesezugriffe liefen über genehmigte Vorschläge des Runs (Testadapter zählt hier nicht: echter Worker).
    status = ui.orchestrator_status()
    assert status["running"] and status["runId"] == run_id and not status["unresponsive"]
    # Zweite Nachricht: gleicher Orchestrator, neues Ziel.
    assert ui.chat_send("Danke") == run_id


def test_rechte_der_neuen_methoden(tmp_path, broker_factory, fake_ollama):
    b = setup(tmp_path, broker_factory, ["-c", "import time; time.sleep(60)"])
    ui, orch = b.ui(), b.orchestrator()
    with pytest.raises(BrokerError) as err:
        orch.chat_send("Ich bin nicht der Nutzer")
    assert err.value.code == 403
    for call in (lambda: orch.wait_activity(0, 0), lambda: orch.agent_cancel("Coder"), lambda: orch.orchestrator_stop()):
        with pytest.raises(BrokerError) as err:
            call()
        assert err.value.code == 403
    report = ActivityReport(run_id="run-x", events=[])
    with pytest.raises(BrokerError) as err:
        ui.report_activity(report)
    assert err.value.code == 403
    with pytest.raises(BrokerError) as err:
        orch.report_activity(report)
    assert err.value.code == 409, "ohne laufenden Run keine Aktivität"
    run_id = ui.chat_send("Start")
    with pytest.raises(BrokerError) as err:
        orch.report_activity(ActivityReport(run_id="run-fremd", events=[]))
    assert err.value.code == 409
    fake_user = ActivityEvent(seq=0, at="2026-01-01T00:00:00Z", run_id=run_id, agent="User", kind=ActivityKind.Chat, text="ok")
    with pytest.raises(BrokerError) as err:
        orch.report_activity(ActivityReport(run_id=run_id, events=[fake_user]))
    assert err.value.code == 422, "nur der Broker schreibt im Namen des Nutzers"
    long = ActivityEvent(seq=99, at="2000-01-01T00:00:00Z", run_id=run_id, agent="Coder", kind=ActivityKind.Output,
                         text="x" * 50_000)
    assert orch.report_activity(ActivityReport(run_id=run_id, events=[long])) == 1
    stored = [e for e in ui.wait_activity(0, 500) if e.agent == "Coder"][-1]
    assert stored.seq != 99 and stored.at != "2000-01-01T00:00:00Z" and len(stored.text) < 17_000
    with pytest.raises(BrokerError) as err:
        ui.agent_cancel("Hacker")
    assert err.value.code == 422
    with pytest.raises(BrokerError) as err:
        ui.chat_send("x" * 20_000)
    assert err.value.code == 422


def test_abbruch_pro_agent_macht_dessen_vorschlaege_ungueltig(tmp_path, broker_factory, fake_ollama):
    b = setup(tmp_path, broker_factory, ["-m", "localai.orchestrator", "--models", "{models}", "--ollama", fake_ollama.url])
    ui = b.ui()
    ui.chat_send("Ziel")
    deadline = time.monotonic() + 30
    while not ui.pending():
        assert time.monotonic() < deadline, "Snapshot-Vorschlag des Planners erwartet"
        time.sleep(0.1)
    view = ui.pending()[0]
    assert view.proposal.agent.value == "Planner"
    assert ui.agent_cancel("Planner") == 1
    assert ui.get(view.proposal.proposal_id).state == ProposalState.Invalidated
    events, _ = collect(ui, 0, lambda ev: any("Ohne Snapshot" in (e.text or "") for e in ev), approve=False)
    assert ui.status()["testAdapterInvocations"] == 0


def test_sec22_absturz_macht_offene_vorschlaege_ungueltig(tmp_path, broker_factory, fake_ollama):
    b = setup(tmp_path, broker_factory, ["-m", "localai.orchestrator", "--models", "{models}", "--ollama", fake_ollama.url])
    ui = b.ui()
    ui.chat_send("Ziel")
    deadline = time.monotonic() + 30
    while not ui.pending():
        assert time.monotonic() < deadline
        time.sleep(0.1)
    view = ui.pending()[0]
    pid = ui.orchestrator_status()["pid"]
    os.kill(pid, signal.SIGTERM if sys.platform != "win32" else signal.SIGTERM)
    exited = wait_event(ui, "orchestrator.exited")
    assert "unerwartet" in exited.message
    assert ui.get(view.proposal.proposal_id).state == ProposalState.Invalidated, "kein Tool ohne lebenden Auftraggeber"
    with pytest.raises(BrokerError):
        ui.decide(view.proposal.proposal_id, view.proposal_sha256, approve=True)
    # Oberfläche bleibt bedienbar, Not-Aus wirkt, neuer Run startet einen neuen Orchestrator.
    assert ui.status()["pending"] == 0
    ui.emergency_stop()
    with pytest.raises(BrokerError) as err:
        ui.chat_send("neu")
    assert err.value.code == 409
    ui.release()
    new_run = ui.chat_send("neu")
    assert ui.orchestrator_status()["running"] and ui.orchestrator_status()["runId"] == new_run


def test_sec22_haengender_orchestrator(tmp_path, broker_factory):
    b = setup(tmp_path, broker_factory, ["-c", "import time; time.sleep(120)"], heartbeat=2)
    ui = b.ui()
    ui.chat_send("Ziel")
    started = time.monotonic()
    event = wait_event(ui, "orchestrator.unresponsive", timeout=20)
    assert "hängt" in event.message
    assert time.monotonic() - started < 15
    assert ui.status()["pending"] == 0 and ui.orchestrator_status()["unresponsive"]
    ui.emergency_stop()
    wait_event(ui, "orchestrator.stopped")
    deadline = time.monotonic() + 10
    while ui.orchestrator_status()["running"]:
        assert time.monotonic() < deadline, "Not-Aus muss den Orchestrator beenden"
        time.sleep(0.1)
    ui.release()
