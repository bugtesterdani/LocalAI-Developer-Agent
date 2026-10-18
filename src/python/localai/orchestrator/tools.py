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
from dataclasses import dataclass, field
from typing import Any

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
    TestReport,
    ToolTarget,
)
from localai.orchestrator.activity import ActivityReporter
from localai.orchestrator.context import Evidence
from localai.orchestrator.gateway import Cancelled, CancelToken
from localai.orchestrator.protocol import Action
from localai.orchestrator.runtime import RunClock
from localai.pipe import BrokerError
from localai.worker import patching

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


@dataclass
class PatchOutcome:
    applied: bool
    state: str  # applied | invalid | rejected | not-applicable | denied | expired | …
    snapshot_id: str | None = None
    feedback: str | None = None  # englisch an das Modell: Patch korrigieren und erneut senden
    note_de: str = ""  # für den Chat
    files: list[str] = field(default_factory=list)
    deselected: list[str] = field(default_factory=list)


@dataclass
class ToolchainOutcome:
    ran: bool
    state: str
    report: TestReport | None = None
    log: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        r = self.report
        return self.ran and r is not None and r.exit_code == 0 and not r.timed_out and r.failed == 0


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
        self.snapshot_id: str | None = None  # aktueller Stand (nach angewendeten Patches)
        self.base_snapshot_id: str | None = None  # Stand des Projektordners beim Snapshot
        self._apply_lock = threading.Lock()
        self._toolchains: list[dict[str, Any]] | None = None

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
        self.base_snapshot_id = self.snapshot_id
        return self.snapshot_id

    def rights(self) -> set[FolderRight]:
        return set(self.grant.rights) if self.grant else set()

    # ------------------------------------------------------------------ Repo.Index
    def repo_map(self, agent: str, task_id: str, cancel: CancelToken, max_chars: int = 12_000) -> ToolOutcome:
        assert self.grant is not None and self.snapshot_id is not None
        request = self._request(agent, task_id, "Repo.Index", "Symbolindex und Repo-Map des Projekts als Startkontext",
                                [], {"grantId": self.grant.grant_id, "maxChars": max_chars}, self.snapshot_id)
        outcome = self._submit_and_wait(agent, task_id, request, "Repo-Map erstellen", cancel)
        if isinstance(outcome, str):
            return ToolOutcome([], f"repo map: {outcome}")
        return self._evidence(request, outcome, "repo map")

    # ------------------------------------------------------------------ Toolchains
    def toolchains(self) -> list[dict[str, Any]]:
        """Ausführbare Toolchains der Sandbox (leer außerhalb der VM-Betriebsart)."""
        if self._toolchains is None:
            try:
                info = self.client.toolchains_available()
                self._toolchains = list(info.get("toolchains") or []) if info.get("enabled") else []
            except (BrokerError, AttributeError):
                self._toolchains = []
        return self._toolchains

    def run_toolchain(self, agent: str, task_id: str, toolchain: str, action: str, parameters: dict[str, Any],
                      purpose: str, cancel: CancelToken) -> ToolchainOutcome:
        assert self.grant is not None and self.snapshot_id is not None
        tool = "Toolchain." + action[:1].upper() + action[1:]
        if action == "format":
            tool = "Toolchain.Lint"
        request = self._request(agent, task_id, tool, purpose, [],
                                {"grantId": self.grant.grant_id, "toolchain": toolchain, "parameters": parameters},
                                self.snapshot_id)
        shown = ", ".join(f"{k}={v}" for k, v in parameters.items())
        outcome = self._submit_and_wait(agent, task_id, request, f"{toolchain} {action}" + (f" ({shown})" if shown else ""),
                                        cancel)
        if isinstance(outcome, str):
            return ToolchainOutcome(False, "rejected", message=outcome)
        if outcome.state != ProposalState.Succeeded or outcome.result is None:
            messages = [r.message for r in (outcome.result.target_results if outcome.result else []) if r.message]
            return ToolchainOutcome(False, outcome.state.value.lower(), message="; ".join(messages)[:1000])
        by_id = {r.target_id: r for r in outcome.result.target_results}
        report = None
        if by_id.get("tests") and by_id["tests"].text:
            try:
                report = TestReport.model_validate_json(by_id["tests"].text)
            except ValueError:
                report = None
        log = (by_id["log"].text or "") if "log" in by_id else ""
        message = (by_id["tests"].message or "") if "tests" in by_id else ""
        return ToolchainOutcome(report is not None, "succeeded", report, log, message)

    # ------------------------------------------------------------------ Patch.Apply
    def apply_patch(self, agent: str, task_id: str, patch: str, purpose: str, cancel: CancelToken) -> PatchOutcome:
        """Schlägt Patch.Apply auf dem aktuellen Stand vor; nacheinander, damit kein Patch einen anderen überholt."""
        assert self.grant is not None
        try:
            files = patching.parse(patch)
        except patching.PatchError as err:
            return PatchOutcome(False, "invalid", feedback=f"Your diff could not be read: {err}. Send a corrected unified diff.",
                                note_de=f"Patch unlesbar: {err}")
        targets = [ToolTarget(target_id=f"t{i}", path=f.path) for i, f in enumerate(files, start=1)]
        paths = {t.target_id: t.path for t in targets}
        with self._apply_lock:
            base = self.snapshot_id
            assert base is not None
            request = self._request(agent, task_id, "Patch.Apply", purpose, targets,
                                    {"grantId": self.grant.grant_id, "patch": patch}, base)
            label = f"Patch anwenden ({len(files)} Datei{'en' if len(files) != 1 else ''})"
            outcome = self._submit_and_wait(agent, task_id, request, label, cancel)
            if isinstance(outcome, str):
                code = outcome.split("(", 1)[1].split(")", 1)[0] if "(" in outcome else ""
                if code == "422":
                    return PatchOutcome(False, "rejected", feedback=f"The patch was rejected: {outcome}. Fix it and send it again.",
                                        note_de=outcome, files=list(paths.values()))
                return PatchOutcome(False, "rejected", note_de=outcome, files=list(paths.values()))
            if outcome.state == ProposalState.Succeeded and outcome.result is not None:
                new_id = next((r.text for r in outcome.result.target_results if r.target_id == "snapshot"), None)
                if new_id:
                    self.snapshot_id = new_id
                deselected = [paths.get(t, t) for t in outcome.result.skipped_target_ids or []]
                applied = [p for tid, p in paths.items() if p not in deselected]
                return PatchOutcome(True, "applied", new_id, note_de="angewendet", files=applied, deselected=deselected)
            if outcome.state == ProposalState.Failed and outcome.result is not None:
                problems = [f"{paths.get(r.target_id, r.target_id)}: {r.message}" for r in outcome.result.target_results
                            if r.status == ResultStatus.Failed and r.message and r.target_id != "snapshot"]
                text = "; ".join(problems)[:3000] or "unknown error"
                return PatchOutcome(False, "not-applicable",
                                    feedback=(f"Your patch was NOT applied: {text}. Read the affected files again (they may "
                                              "already contain other changes) and send a corrected patch."),
                                    note_de=f"passt nicht: {text}"[:600], files=list(paths.values()))
            state = outcome.state.value.lower()
            return PatchOutcome(False, state, note_de=STATE_TEXT.get(outcome.state, state), files=list(paths.values()))

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
        elif action.kind == "symbols":
            request = self._request(agent, task_id, "Repo.Index", action.reason or f"Symbole '{action.query}' suchen", [],
                                    {"grantId": grant, "query": action.query}, self.snapshot_id)
            label = f"Symbole '{action.query}'"
            describe = f"symbols {action.query!r}"
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
                if request.tool in ("File.Search", "Repo.Index") and request.arguments.get("query"):
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
