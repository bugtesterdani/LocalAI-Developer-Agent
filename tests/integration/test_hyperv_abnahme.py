"""M2b-Abnahme mit echter Hyper-V-VM (nur auf dem Windows-PC, dauert 15–60 Minuten).

Start (PowerShell, im Repository)::

    $env:LOCALAI_HYPERV_ABNAHME = "1"
    uv run --project src/python pytest -s tests/integration/test_hyperv_abnahme.py

Ablauf: Broker im Modus ``hyperv`` starten (UAC-Abfrage bestätigen) → VM-Aufbau abwarten → Selbsttest
→ Snapshot des Demo-Projekts → 50 Lese-Roundtrips (Copy-VMFile hin, Transfer-VHDX zurück) → Ergebnis
mit Zeiten in ``%LOCALAPPDATA%\\LocalAi\\abnahme\\hyperv-abnahme.json``. Danach wird die VM gelöscht.

Mit Toolchains (M2c): im Projekt wählen und in der Oberfläche registrieren (die Registrierung wird
übernommen), dann zusätzlich
``$env:LOCALAI_HYPERV_TOOLCHAINS = "dotnet,python"`` – geprüft wird, dass beide installiert und ihr
Hallo-Welt-Selbsttest bestanden ist.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
import uuid

import pytest

from localai.contracts import AgentRole, ProposalRequest, ProposalState, ResultStatus, ToolTarget

from conftest import ROOT, start_broker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("LOCALAI_HYPERV_ABNAHME") != "1", reason="nur mit LOCALAI_HYPERV_ABNAHME=1 auf dem Hyper-V-PC"),
]

ROUNDTRIPS = int(os.environ.get("LOCALAI_HYPERV_ROUNDTRIPS", "50"))
FILES = ["README.md", "docs/format.md", "src/Importer.cs", "src/Parser.cs", "src/Csv/Tokenizer.cs"]


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def request(tool: str, args: dict, targets: list[ToolTarget], snapshot: str = "-") -> ProposalRequest:
    return ProposalRequest(
        schema_version=1, run_id="abnahme", task_id="m2b", agent=AgentRole.Tester, tool=tool, tool_version="1",
        purpose="M2b-Abnahme", arguments=args, targets=targets, snapshot_id=snapshot, idempotency_key=uuid.uuid4().hex)


def run(ui, orch, req: ProposalRequest, timeout: float = 600):
    view = orch.submit(req)
    ui.decide(view.proposal.proposal_id, view.proposal_sha256, approve=True)
    return orch.wait_final(view.proposal.proposal_id, timeout=timeout, poll=0.2)


def test_hyperv_vm_aufbau_und_roundtrips():
    base = pathlib.Path(os.environ.get("LOCALAPPDATA", str(pathlib.Path.home()))) / "LocalAi" / "abnahme"
    data = base / "broker"
    data.mkdir(parents=True, exist_ok=True)
    # Toolchain-Registrierungen aus der Oberfläche übernehmen (registriert wird nur dort, R12).
    registry = base.parent / "broker" / "toolchains.json"
    if registry.exists():
        (data / "toolchains.json").write_bytes(registry.read_bytes())
        names = [r.get("name") for r in json.loads(registry.read_text(encoding="utf-8"))]
        log(f"Toolchain-Registrierungen aus der Oberfläche übernommen: {', '.join(names) or 'keine'}")
    else:
        (data / "toolchains.json").unlink(missing_ok=True)
        log(f"Keine Toolchain-Registrierungen gefunden ({registry}) – in der Oberfläche unter TOOLCHAINS registrieren")
    started = time.monotonic()
    broker = start_broker(data, "--mode", "hyperv", "--config", str(ROOT / "config" / "localai.json"))
    summary: dict = {"roundtrips": ROUNDTRIPS}
    try:
        ui, orch = broker.ui(), broker.orchestrator()
        last = None
        while True:
            status = ui.status().get("sandbox") or ""
            if status != last:
                log(f"Sandbox: {status}")
                last = status
            if status.startswith("Offline-VM bereit"):
                break
            assert not status.startswith("gesperrt"), status
            assert time.monotonic() - started < 3 * 3600, "VM-Aufbau dauert länger als 3 Stunden"
            time.sleep(5)
        summary["buildMinutes"] = round((time.monotonic() - started) / 60, 1)
        summary["selfTest"] = status
        # M2c: im Projekt gewählte Toolchains, z. B. LOCALAI_HYPERV_TOOLCHAINS=dotnet,python
        for name in filter(None, os.environ.get("LOCALAI_HYPERV_TOOLCHAINS", "").split(",")):
            assert f"{name} " in status and "Hallo-Welt ok" in status, f"Toolchain {name} fehlt im Selbsttest: {status}"
        log(f"VM bereit nach {summary['buildMinutes']} min")

        grant = next(g for g in orch.grants() if "List" in [r.value for r in g.rights])
        t0 = time.monotonic()
        snap = run(ui, orch, request("Workspace.Snapshot", {"grantId": grant.grant_id, "root": grant.root}, []))
        assert snap.state == ProposalState.Succeeded, snap
        snapshot_id = snap.result.target_results[0].text
        summary["snapshotSeconds"] = round(time.monotonic() - t0, 1)
        log(f"Snapshot {snapshot_id[:12]}… in {summary['snapshotSeconds']} s: {snap.result.target_results[0].message}")

        durations = []
        for i in range(ROUNDTRIPS):
            path = FILES[i % len(FILES)]
            t0 = time.monotonic()
            outcome = run(ui, orch, request("File.Read", {"grantId": grant.grant_id}, [ToolTarget(target_id="t1", path=path)], snapshot_id))
            durations.append(time.monotonic() - t0)
            assert outcome.state == ProposalState.Succeeded, outcome
            result = outcome.result.target_results[0]
            assert result.status == ResultStatus.Succeeded and result.text, result
            expected = (pathlib.Path(grant.root) / path).read_text(encoding="utf-8")
            assert result.text.replace("\r\n", "\n") == expected.replace("\r\n", "\n"), path
            if (i + 1) % 10 == 0:
                log(f"{i + 1}/{ROUNDTRIPS} Roundtrips, zuletzt {durations[-1]:.1f} s")
        summary["roundtripSeconds"] = {
            "min": round(min(durations), 1), "max": round(max(durations), 1),
            "mean": round(sum(durations) / len(durations), 1)}
        summary["ok"] = True
        log(f"Fertig: {json.dumps(summary, ensure_ascii=False)}")
    finally:
        (base / "hyperv-abnahme.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        log("Broker wird beendet (VM: gelöscht, gespeichert oder belassen – je nach reuseVm/keepVmOnExit) …")
        broker.stop(timeout=300)
