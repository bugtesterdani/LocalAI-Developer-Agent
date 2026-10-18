"""Brücke Agent → Broker: jede Aktion wird ein unveränderlicher Vorschlag, der auf die Nutzerentscheidung wartet.

Der Orchestrator führt nie selbst etwas aus. Nur Inhalte ausgeführter (und nicht abgewählter) Ziele gelangen
als Evidenz in den Kontext (AC15); abgelehnte oder abgelaufene Vorschläge werden dem Agenten nur als Tatsache
gemeldet.
"""

from __future__ import annotations

import itertools
import threading
import time
import uuid
from dataclasses import dataclass

from localai.contracts import (
    ActivityKind,
    AgentRole,
    AgentState,
    FolderGrant,
    FolderRight,
    ProposalOutcome,
    ProposalRequest,
    ProposalState,
    ResultStatus,
    ToolTarget,
)
from localai.orchestrator.activity import ActivityReporter
from localai.orchestrator.context import Evidence
from localai.orchestrator.gateway import Cancelled, CancelToken
from localai.orchestrator.protocol import Action
from localai.orchestrator.runtime import RunClock
from localai.pipe import BrokerError

TOOL_VERSION = "1"
STATE_TEXT = {
    ProposalState.Succeeded: "ausgeführt",
    ProposalState.Failed: "fehlgeschlagen",
    ProposalState.Denied: "abgelehnt",
    ProposalState.Expired: "abgelaufen",
    ProposalState.Invalidated: "ungültig",
    ProposalState.Cancelled: "abgebrochen",
    ProposalState.OutcomeUnknown: "Ergebnis unklar",
    ProposalState.Rejected: "abgewiesen",
}


@dataclass
class ToolOutcome:
    evidence: list[Evidence]
    summary: str  # englische Kurzfassung für den Verlauf des Agenten


class ToolBridge:
    def __init__(self, client, reporter: ActivityReporter, run_id: str, clock: RunClock, max_jobs: int = 2,
                 poll: float = 0.4) -> None:
        self.client = client
        self.reporter = reporter
        self.run_id = run_id
        self.clock = clock
        self.poll = poll
        self._jobs = threading.BoundedSemaphore(max_jobs)
        self._evidence_ids = itertools.count(1)
        self.grant: FolderGrant | None = None
        self.snapshot_id: str | None = None

    # ------------------------------------------------------------------ Grundlagen
    def find_grant(self) -> FolderGrant | None:
        for grant in self.client.grants():
            if {FolderRight.List, FolderRight.Read} <= set(grant.rights):
                self.grant = grant
                return grant
        return None

    def _request(self, agent: str, task_id: str, tool: str, purpose: str, targets: list[ToolTarget],
                 arguments: dict, snapshot_id: str) -> ProposalRequest:
        return ProposalRequest(schema_version=1, run_id=self.run_id, task_id=task_id, agent=AgentRole(agent), tool=tool,
                               tool_version=TOOL_VERSION, purpose=purpose[:500], arguments=arguments, targets=targets,
                               snapshot_id=snapshot_id, idempotency_key=uuid.uuid4().hex)

    def _submit_and_wait(self, agent: str, task_id: str, request: ProposalRequest, label: str,
                         cancel: CancelToken) -> ProposalOutcome | str:
        """Liefert das Endergebnis oder einen Ablehnungsgrund des Brokers (z. B. Pfad unzulässig)."""
        with self._jobs:
            cancel.raise_if_cancelled()
            try:
                view = self.client.submit(request)
            except BrokerError as err:
                self.reporter.event(agent, ActivityKind.Proposal, f"{label} vom Broker abgewiesen ({err.code}): {err.message}",
                                    task_id=task_id)
                return f"rejected by broker ({err.code}): {err.message}"
            proposal_id = view.proposal.proposal_id
            self.reporter.event(agent, ActivityKind.Proposal, f"{label} – wartet auf Freigabe ({proposal_id[:8]})",
                                task_id=task_id, state=AgentState.WaitingApproval)
            started = time.monotonic()
            # Warten auf die Entscheidung zählt nicht zur aktiven Laufzeit.
            with self.clock.paused():
                while True:
                    if cancel.cancelled:
                        # Broker hat die Vorschläge des Agenten beim Abbruch bereits ungültig gemacht.
                        raise Cancelled(cancel.reason)
                    outcome = self.client.outcome(proposal_id)
                    if outcome.is_final:
                        break
                    cancel.wait(self.poll)
            seconds = time.monotonic() - started
            text = STATE_TEXT.get(outcome.state, outcome.state.value)
            if outcome.final_proposal_id != proposal_id:
                text += " (nach Abwahl)"
            self.reporter.event(agent, ActivityKind.Proposal, f"{label}: {text} nach {seconds:.0f} s", task_id=task_id,
                                state=AgentState.Generating if outcome.state == ProposalState.Succeeded else None)
            return outcome

    # ------------------------------------------------------------------ Snapshot
    def snapshot(self, agent: str, task_id: str, cancel: CancelToken) -> str | None:
        if self.grant is None:
            raise RuntimeError("Kein Grant")
        request = self._request(agent, task_id, "Workspace.Snapshot", "Projektstand für diesen Run in die Sandbox übernehmen",
                                [], {"grantId": self.grant.grant_id, "root": self.grant.root}, "-")
        outcome = self._submit_and_wait(agent, task_id, request, "Snapshot", cancel)
        if isinstance(outcome, str) or outcome.state != ProposalState.Succeeded or outcome.result is None:
            return None
        self.snapshot_id = outcome.result.target_results[0].text or None
        return self.snapshot_id

    # ------------------------------------------------------------------ Agentenaktionen
    def execute(self, agent: str, task_id: str, action: Action, cancel: CancelToken) -> ToolOutcome:
        assert self.grant is not None and self.snapshot_id is not None
        grant = self.grant.grant_id
        if action.kind == "list":
            request = self._request(agent, task_id, "File.List", action.reason or f"Struktur von {action.path} ansehen",
                                    [ToolTarget(target_id="t1", path=action.path)],
                                    {"grantId": grant, "depth": action.depth}, self.snapshot_id)
            label = f"Auflisten {action.path} (Tiefe {action.depth})"
            describe = f"list {action.path} depth {action.depth}"
        elif action.kind == "search":
            request = self._request(agent, task_id, "File.Search", action.reason or f"Nach '{action.query}' suchen",
                                    [ToolTarget(target_id="t1", path=action.path)],
                                    {"grantId": grant, "query": action.query, "maxResults": 100}, self.snapshot_id)
            label = f"Suche '{action.query}' in {action.path}"
            describe = f"search {action.query!r} in {action.path}"
        elif action.kind == "read":
            targets = [ToolTarget(target_id=f"t{i}", path=f.path, line_start=f.start, line_end=f.end)
                       for i, f in enumerate(action.files, start=1)]
            tool = "File.Read" if len(targets) == 1 else "File.ReadMany"
            args = {"grantId": grant}
            request = self._request(agent, task_id, tool, action.reason or "Dateien für die Aufgabe lesen", targets, args,
                                    self.snapshot_id)
            label = (f"Lesen {targets[0].path}" if len(targets) == 1 else f"Lesen von {len(targets)} Dateien")
            describe = "read " + ", ".join(_ref(f.path, f.start, f.end) for f in action.files)
        else:
            raise ValueError(action.kind)
        outcome = self._submit_and_wait(agent, task_id, request, label, cancel)
        if isinstance(outcome, str):
            return ToolOutcome([], f"{describe}: {outcome}")
        return self._evidence(request, outcome, describe)

    def _evidence(self, request: ProposalRequest, outcome: ProposalOutcome, describe: str) -> ToolOutcome:
        if outcome.state != ProposalState.Succeeded or outcome.result is None:
            state = outcome.state.value.lower()
            hint = {"denied": "the user denied this request; do not repeat it unchanged",
                    "expired": "the user did not decide in time; ask again only if still necessary"}.get(state, "")
            if outcome.result is not None:
                messages = [r.message for r in outcome.result.target_results if r.message]
                if messages:
                    hint = "; ".join(messages)[:500]
            return ToolOutcome([], f"{describe}: {state}" + (f" ({hint})" if hint else ""))
        targets = {t.target_id: t for t in request.targets}
        evidence: list[Evidence] = []
        notes: list[str] = []
        for r in outcome.result.target_results:
            target = targets.get(r.target_id)
            path = target.path if target else r.target_id
            if r.status == ResultStatus.Succeeded and r.text is not None:
                source = f"{request.tool} {_ref(path, target.line_start if target else None, target.line_end if target else None)}"
                if request.tool == "File.Search":
                    source += f" query={request.arguments.get('query')!r}"
                evidence.append(Evidence(f"E{next(self._evidence_ids)}", source, r.text or "(empty)"))
            else:
                notes.append(f"{path}: {r.status.value} {r.message or ''}".strip())
        for skipped in outcome.result.skipped_target_ids or []:
            target = targets.get(skipped)
            notes.append(f"{target.path if target else skipped}: deselected by the user, content not provided")
        summary = f"{describe}: ok, evidence {', '.join(e.evidence_id for e in evidence) or 'none'}"
        if outcome.result.truncated:
            summary += " (output truncated by the sandbox)"
        if notes:
            summary += "; " + "; ".join(notes)
        return ToolOutcome(evidence, summary)


def _ref(path: str, start: int | None, end: int | None) -> str:
    if start is None and end is None:
        return path
    return f"{path}:{start or 1}-{end or 'end'}"
