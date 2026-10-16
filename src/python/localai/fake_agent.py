"""Fake-Agent für M1/M2a: reicht Toolvorschläge beim Broker ein und wartet auf Entscheidungen.

Kein Modell. Ablauf: Snapshot des freigegebenen Ordners → Auflisten → Einzel- und Sammel-Lesen
→ absichtlich unzulässiger Pfad. Der Broker startet ihn über ``dev.startFakeAgent`` mit
``LOCALAI_PIPE`` und ``LOCALAI_TOKEN`` (Orchestrator-Token).
"""

from __future__ import annotations

import os
import sys
import time
import traceback
import uuid

from localai.broker_client import BrokerClient
from localai.contracts import AgentRole, FolderRight, ProposalOutcome, ProposalRequest, ProposalState, ToolTarget
from localai.pipe import BrokerError

_START = time.monotonic()


def log(message: str) -> None:
    print(f"[Fake-Agent {time.monotonic() - _START:6.1f} s] {message}", flush=True)


def request(run_id: str, agent: AgentRole, tool: str, purpose: str, targets: list[ToolTarget],
            arguments: dict, snapshot: str = "-") -> ProposalRequest:
    return ProposalRequest(
        schema_version=1, run_id=run_id, task_id=f"task-{uuid.uuid4().hex[:8]}", agent=agent, tool=tool,
        tool_version="1", purpose=purpose, arguments=arguments, targets=targets, snapshot_id=snapshot,
        idempotency_key=uuid.uuid4().hex,
    )


def describe(proposal_id: str, outcome: ProposalOutcome) -> str:
    line = f"Ergebnis {proposal_id[:8]}: {outcome.state.value}"
    if outcome.final_proposal_id != proposal_id:
        line += f" (nach Abwahl als {outcome.final_proposal_id[:8]})"
    if outcome.result is not None:
        done = [r.target_id for r in outcome.result.target_results if r.status.value == "Succeeded"]
        line += f", ausgeführt: {done}"
        failed = [f"{r.target_id}: {r.message}" for r in outcome.result.target_results if r.status.value != "Succeeded"]
        if failed:
            line += f", fehlgeschlagen: {failed}"
        if outcome.result.skipped_target_ids:
            line += f", vom Nutzer abgewählt: {outcome.result.skipped_target_ids}"
    return line


def main() -> int:
    pipe = os.environ.get("LOCALAI_PIPE")
    token = os.environ.get("LOCALAI_TOKEN")
    if not pipe or not token:
        log("LOCALAI_PIPE und LOCALAI_TOKEN fehlen – bitte über die Oberfläche starten.")
        return 2

    log(f"Python {sys.version.split()[0]} läuft (PID {os.getpid()}), verbinde mit Pipe {pipe}")
    client = BrokerClient(pipe, token)
    log(f"verbunden: {client.ping()}")
    run_id = f"run-{uuid.uuid4().hex[:8]}"

    grant = next((g for g in client.grants() if {FolderRight.List, FolderRight.Read} <= set(g.rights)), None)
    if grant is None:
        log("Kein freigegebener Ordner mit List- und Read-Recht – Projektkonfiguration prüfen.")
        return 3
    log(f"Ordner-Grant {grant.grant_id}: {grant.root}")

    # 1. Snapshot (Host liest den Ordner erst nach Freigabe)
    snap = client.submit(request(run_id, AgentRole.Planner, "Workspace.Snapshot",
                                 "Projektstand in die Sandbox übernehmen", [],
                                 {"grantId": grant.grant_id, "root": grant.root}))
    log("Snapshot vorgeschlagen, wartet auf Freigabe")
    outcome = client.wait_final(snap.proposal.proposal_id)
    log(describe(snap.proposal.proposal_id, outcome))
    if outcome.state != ProposalState.Succeeded or outcome.result is None:
        log("Ohne Snapshot geht es nicht weiter. fertig")
        return 0
    snapshot_id = outcome.result.target_results[0].text or ""
    log(f"Snapshot {snapshot_id[:12]}…: {outcome.result.target_results[0].message}")

    # 2. Auflisten
    listing = client.submit(request(run_id, AgentRole.Planner, "File.List", "Projektstruktur verstehen",
                                    [ToolTarget(target_id="t1", path=".")],
                                    {"grantId": grant.grant_id, "depth": 5}, snapshot_id))
    log("Auflisten vorgeschlagen, wartet auf Freigabe")
    outcome = client.wait_final(listing.proposal.proposal_id)
    log(describe(listing.proposal.proposal_id, outcome))
    files: list[str] = []
    if outcome.result is not None and outcome.result.target_results and outcome.result.target_results[0].text:
        files = [line.split("  (")[0] for line in outcome.result.target_results[0].text.splitlines() if "  (" in line]
    log(f"{len(files)} Dateien gefunden")

    # 3. Einzel- und Sammel-Lesen
    proposals = []
    if "README.md" in files:
        single = client.submit(request(run_id, AgentRole.Coder, "File.Read", "Projektbeschreibung lesen",
                                       [ToolTarget(target_id="t1", path="README.md", line_start=1, line_end=20)],
                                       {"grantId": grant.grant_id, "maxBytes": 65536}, snapshot_id))
        proposals.append(single.proposal.proposal_id)
    many = client.submit(request(run_id, AgentRole.Tester, "File.ReadMany", "Code und Tests für Grenzfälle sichten",
                                 [ToolTarget(target_id=f"t{i}", path=p) for i, p in enumerate(files[:7], start=1)],
                                 {"grantId": grant.grant_id}, snapshot_id))
    proposals.append(many.proposal.proposal_id)
    log(f"{len(proposals)} Lese-Vorschläge eingereicht, warten auf Freigabe")

    # Absichtlich unzulässiger Pfad: muss vom Broker abgewiesen werden.
    try:
        client.submit(request(run_id, AgentRole.Coder, "File.Read", "Unzulässiger Pfad (Test)",
                              [ToolTarget(target_id="t1", path="..\\..\\Users\\geheim.txt")],
                              {"grantId": grant.grant_id}, snapshot_id))
        log("FEHLER: unzulässiger Pfad wurde angenommen")
    except BrokerError as err:
        log(f"Unzulässiger Pfad korrekt abgewiesen ({err.code}): {err.message}")

    for proposal_id in proposals:
        outcome = client.wait_final(proposal_id)
        log(describe(proposal_id, outcome))
        if outcome.result is not None:
            for r in outcome.result.target_results:
                if r.text:
                    first = r.text.strip().splitlines()[0] if r.text.strip() else ""
                    log(f"  {r.target_id}: {len(r.text)} Zeichen, erste Zeile: {first[:80]}")
    log("fertig")
    client.close()
    return 0


def run() -> int:
    try:
        return main()
    except Exception:  # noqa: BLE001 – vollständigen Fehler ins Protokoll schreiben
        log("FEHLER:\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(run())
