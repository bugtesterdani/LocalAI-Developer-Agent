"""Fake-Agent für M1: reicht Toolvorschläge beim Broker ein und wartet auf Entscheidungen.

Kein Modell, keine echten Dateizugriffe. Der Broker startet ihn über ``dev.startFakeAgent``
mit den Umgebungsvariablen ``LOCALAI_PIPE`` und ``LOCALAI_TOKEN`` (Orchestrator-Token).
"""

from __future__ import annotations

import os
import sys
import time
import traceback
import uuid

from localai.broker_client import BrokerClient
from localai.contracts import AgentRole, ProposalRequest, ToolTarget
from localai.pipe import BrokerError


_START = time.monotonic()


def log(message: str) -> None:
    print(f"[Fake-Agent {time.monotonic() - _START:6.1f} s] {message}", flush=True)


def request(run_id: str, agent: AgentRole, tool: str, purpose: str, targets: list[ToolTarget],
            arguments: dict | None = None) -> ProposalRequest:
    return ProposalRequest(
        schema_version=1,
        run_id=run_id,
        task_id=f"task-{uuid.uuid4().hex[:8]}",
        agent=agent,
        tool=tool,
        tool_version="1",
        purpose=purpose,
        arguments=arguments or {},
        targets=targets,
        snapshot_id="snap-demo",
        idempotency_key=uuid.uuid4().hex,
    )


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

    proposals = []
    single = request(run_id, AgentRole.Coder, "File.Read", "Schnittstelle des CSV-Importers prüfen",
                     [ToolTarget(target_id="t1", path="src/Importer.cs", line_start=1, line_end=160)],
                     {"maxBytes": 65536})
    many = request(run_id, AgentRole.Tester, "File.ReadMany", "Bestehende Tests und Parser für Grenzfälle sichten",
                   [ToolTarget(target_id=f"t{i}", path=p) for i, p in enumerate([
                       "src/Parser.cs", "src/Importer.cs", "src/Csv/Tokenizer.cs", "tests/ParserTests.cs",
                       "tests/ImporterTests.cs", "README.md", "docs/format.md"], start=1)])
    for req in (single, many):
        view = client.submit(req)
        proposals.append(view.proposal.proposal_id)
        log(f"Vorschlag eingereicht: {req.tool} ({len(req.targets)} Ziel(e)), wartet auf Freigabe")

    # Absichtlich unzulässiger Pfad: muss vom Broker abgewiesen werden.
    try:
        client.submit(request(run_id, AgentRole.Coder, "File.Read", "Unzulässiger Pfad (Test)",
                              [ToolTarget(target_id="t1", path="..\\..\\Users\\geheim.txt")]))
        log("FEHLER: unzulässiger Pfad wurde angenommen")
    except BrokerError as err:
        log(f"Unzulässiger Pfad korrekt abgewiesen ({err.code}): {err.message}")

    for proposal_id in proposals:
        outcome = client.wait_final(proposal_id)
        line = f"Ergebnis {proposal_id[:8]}: {outcome.state.value}"
        if outcome.final_proposal_id != proposal_id:
            line += f" (nach Abwahl als {outcome.final_proposal_id[:8]})"
        if outcome.result is not None:
            done = [r.target_id for r in outcome.result.target_results]
            line += f", ausgeführt: {done}"
            if outcome.result.skipped_target_ids:
                line += f", vom Nutzer abgewählt: {outcome.result.skipped_target_ids}"
        log(line)
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
