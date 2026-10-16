"""Typisierter Client für die Broker-Methoden.

Der Orchestrator nutzt nur ``submit``, ``outcome`` und ``wait_events``.
Die UI-Methoden existieren hier ausschließlich für Integrationstests; der Broker
lehnt sie mit dem Orchestrator-Token ab (SEC-17).
"""

from __future__ import annotations

import time
from typing import Any

from localai.contracts import (
    BrokerEvent,
    DecisionKind,
    NarrowRequest,
    ProposalOutcome,
    ProposalRequest,
    ProposalView,
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

    # ---------------------------------------------------------------- nur UI / Tests
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

    def emergency_stop(self) -> None:
        self.pipe.call("run.emergencyStop")

    def release(self) -> None:
        self.pipe.call("run.release")

    def close(self) -> None:
        self.pipe.close()
