"""Typisierter Client für die Broker-Methoden.

Der Orchestrator nutzt nur ``submit``, ``outcome``, ``grants``, ``report_activity`` und ``wait_commands``.
Die UI-Methoden existieren hier ausschließlich für Integrationstests; der Broker
lehnt sie mit dem Orchestrator-Token ab (SEC-17).
"""

from __future__ import annotations

import time
from typing import Any

from localai.contracts import (
    ActivityEvent,
    ActivityReport,
    BrokerEvent,
    OrchestratorCommand,
    FolderGrant,
    DecisionKind,
    LimitAnswer,
    LimitQuestion,
    NarrowRequest,
    ProposalOutcome,
    ProposalRequest,
    ProposalView,
    ToolchainInfo,
    UserDecision,
)
from localai.pipe import PipeClient


class BrokerClient:
    def __init__(self, pipe_name: str, token: str, timeout: float | None = 60.0) -> None:
        self.pipe = PipeClient(pipe_name, token, timeout)

    # ---------------------------------------------------------------- Orchestrator
    def ping(self) -> dict[str, Any]:
        return self.pipe.call("ping")

    def submit(self, request: ProposalRequest) -> ProposalView:
        return ProposalView.model_validate(self.pipe.call("proposals.submit", request.to_json_obj()))

    def outcome(self, proposal_id: str) -> ProposalOutcome:
        return ProposalOutcome.model_validate(self.pipe.call("proposals.outcome", {"proposalId": proposal_id}))

    def grants(self) -> list[FolderGrant]:
        return [FolderGrant.model_validate(g) for g in self.pipe.call("project.grants")]

    def wait_events(self, after_seq: int, timeout_ms: int = 10_000) -> list[BrokerEvent]:
        raw = self.pipe.call("events.wait", {"afterSeq": after_seq, "timeoutMs": timeout_ms})
        return [BrokerEvent.model_validate(e) for e in raw]

    def wait_final(self, proposal_id: str, timeout: float = 600.0, poll: float = 0.5) -> ProposalOutcome:
        """Wartet, bis der Vorschlag (bzw. sein Nachfolger nach Abwahl) abgeschlossen ist."""
        deadline = time.monotonic() + timeout
        while True:
            outcome = self.outcome(proposal_id)
            if outcome.is_final or time.monotonic() > deadline:
                return outcome
            time.sleep(poll)

    def report_activity(self, report: ActivityReport) -> int:
        return int(self.pipe.call("activity.report", report.to_json_obj())["accepted"])

    def wait_commands(self, after_seq: int, timeout_ms: int = 10_000) -> list[OrchestratorCommand]:
        raw = self.pipe.call("orchestrator.wait", {"afterSeq": after_seq, "timeoutMs": timeout_ms})
        return [OrchestratorCommand.model_validate(c) for c in raw]

    def limit_ask(self, question: LimitQuestion) -> str:
        """Schrittlimit erreicht: Eingabe des nächsten Schritts der Oberfläche vorlegen."""
        return str(self.pipe.call("limit.ask", question.to_json_obj())["questionId"])

    def limit_withdraw(self, question_id: str) -> bool:
        return bool(self.pipe.call("limit.withdraw", {"questionId": question_id})["withdrawn"])

    # ---------------------------------------------------------------- nur UI / Tests
    def limit_pending(self) -> list[LimitQuestion]:
        return [LimitQuestion.model_validate(q) for q in self.pipe.call("limit.pending", {})]

    def limit_answer(self, answer: LimitAnswer) -> list[str]:
        """Liefert die Warnungen der Prüfung (Fehler weist der Broker mit 422 ab, die Frage bleibt offen)."""
        return [str(w) for w in self.pipe.call("limit.answer", answer.to_json_obj()).get("warnings") or []]

    def chat_send(self, text: str) -> str:
        return str(self.pipe.call("chat.send", {"text": text})["runId"])

    def agent_cancel(self, agent: str) -> int:
        return int(self.pipe.call("agent.cancel", {"agent": agent})["cancelledProposals"])

    def run_cancel(self) -> int:
        return int(self.pipe.call("run.cancel")["cancelledProposals"])

    def wait_activity(self, after_seq: int, timeout_ms: int = 10_000) -> list[ActivityEvent]:
        raw = self.pipe.call("activity.wait", {"afterSeq": after_seq, "timeoutMs": timeout_ms})
        return [ActivityEvent.model_validate(e) for e in raw]

    def orchestrator_status(self) -> dict[str, Any]:
        return self.pipe.call("orchestrator.status")

    def orchestrator_stop(self) -> bool:
        return bool(self.pipe.call("orchestrator.stop")["stopped"])

    def status(self) -> dict[str, Any]:
        return self.pipe.call("status")

    def pending(self) -> list[ProposalView]:
        return [ProposalView.model_validate(v) for v in self.pipe.call("proposals.pending")]

    def get(self, proposal_id: str) -> ProposalView:
        return ProposalView.model_validate(self.pipe.call("proposals.get", {"proposalId": proposal_id}))

    def decide(self, proposal_id: str, sha256: str, approve: bool) -> ProposalView:
        decision = UserDecision(
            proposal_id=proposal_id,
            proposal_sha256=sha256,
            decision=DecisionKind.Approve if approve else DecisionKind.Deny,
        )
        return ProposalView.model_validate(self.pipe.call("approvals.decide", decision.to_json_obj()))

    def narrow(self, proposal_id: str, sha256: str, keep: list[str]) -> ProposalView:
        request = NarrowRequest(proposal_id=proposal_id, proposal_sha256=sha256, keep_target_ids=keep)
        return ProposalView.model_validate(self.pipe.call("approvals.narrow", request.to_json_obj()))

    def toolchains_scan(self) -> list[ToolchainInfo]:
        return [ToolchainInfo.model_validate(t) for t in self.pipe.call("toolchains.scan")]

    def toolchains_register(self, directory: str, digest: str) -> dict[str, Any]:
        return self.pipe.call("toolchains.register", {"directory": directory, "digest": digest})

    def toolchains_unregister(self, name: str) -> dict[str, Any]:
        return self.pipe.call("toolchains.unregister", {"name": name})

    def toolchains_available(self) -> dict[str, Any]:
        """In der Sandbox ausführbare Toolchains mit Aktionen und Parametern (Orchestrator und UI)."""
        return self.pipe.call("toolchains.available", {})

    def emergency_stop(self) -> None:
        self.pipe.call("run.emergencyStop")

    def release(self) -> None:
        self.pipe.call("run.release")

    # ---------------------------------------------------------------- System (M7, nur Oberfläche)
    def system_manifest(self) -> dict[str, Any]:
        return self.pipe.call("system.manifest", {})

    def audit_verify(self) -> dict[str, Any]:
        return self.pipe.call("audit.verify", {})

    def diagnostics_export(self, include_logs: bool = False) -> dict[str, Any]:
        return self.pipe.call("diagnostics.export", {"includeLogs": include_logs})

    def backup_create(self) -> dict[str, Any]:
        return self.pipe.call("backup.create", {})

    def close(self) -> None:
        self.pipe.close()
