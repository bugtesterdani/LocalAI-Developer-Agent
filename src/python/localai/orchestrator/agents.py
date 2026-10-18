"""Ein Agent arbeitet eine Aufgabe in Modellschritten ab (höchstens ``max_steps``).

Jeder Schritt: Prompt im Kontextbudget bauen → GPU-Slot → gestreamte Generierung → Aktion auswerten →
ggf. Toolvorschlag beim Broker (wartet auf Nutzerentscheidung) → Evidenz übernehmen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from localai.contracts import ActivityKind, AgentState
from localai.orchestrator import prompts, protocol
from localai.orchestrator.activity import ActivityReporter
from localai.orchestrator.context import DEFAULT_CHARS_PER_TOKEN, Budget, Evidence, assemble
from localai.orchestrator.gateway import (
    ChatRequest,
    GatewayError,
    OllamaGateway,
    OutOfMemory,
    RepetitionLoop,
    StreamCallbacks,
    CancelToken,
)
from localai.orchestrator.models import Limits, Measurement, ModelProfile
from localai.orchestrator.runtime import PRIORITY_NORMAL, FairGate, LimitReached, RunClock
from localai.orchestrator.tools import ToolBridge


class AgentFailed(Exception):
    """Aufgabe endgültig gescheitert (Fehler des Modells/Ollama)."""


@dataclass
class Assignment:
    task_id: str
    title: str
    text: str
    evidence: list[Evidence] = field(default_factory=list)
    priority: int = PRIORITY_NORMAL
    # Nach einem gültigen Ergebnis aufgerufen (z. B. Patch anwenden). Rückgabe None = fertig, sonst eine
    # englische Rückmeldung an das Modell, das dann weiterarbeitet (z. B. Patch passt nicht).
    finisher: Callable[[dict[str, Any]], str | None] | None = None


@dataclass
class AgentResult:
    result: dict[str, Any]
    steps: int
    evidence: list[Evidence]


class Agent:
    def __init__(self, role: str, profile: ModelProfile, measurement: Measurement | None, gateway: OllamaGateway,
                 gpu: FairGate, bridge: ToolBridge, reporter: ActivityReporter, clock: RunClock, limits: Limits,
                 user_notes: Callable[[], list[str]]) -> None:
        self.role = role
        self.profile = profile
        self.chars_per_token = (measurement.chars_per_token * 0.9) if measurement and measurement.chars_per_token > 0 \
            else DEFAULT_CHARS_PER_TOKEN
        self.gateway = gateway
        self.gpu = gpu
        self.bridge = bridge
        self.reporter = reporter
        self.clock = clock
        self.limits = limits
        self.user_notes = user_notes
        self.system, self.prompts = prompts.system_prompt(role)
        self.budget = Budget.split(profile.context_tokens, profile.max_output_tokens)

    @property
    def prompt_tags(self) -> str:
        return ", ".join(p.tag for p in self.prompts)

    def _task_text(self, assignment: Assignment) -> str:
        text = assignment.text
        notes = self.user_notes()
        if notes:
            text += "\n\n## Notes from the user (latest last; they refine the goal)\n" + "\n".join(f"- {n}" for n in notes)
        return text

    def run(self, assignment: Assignment, cancel: CancelToken) -> AgentResult:
        role, task_id = self.role, assignment.task_id
        evidence = list(assignment.evidence)
        history: list[str] = []
        gateway_errors = 0
        denials = 0
        invalid = 0
        max_steps = self.limits.steps_for(role)
        self.reporter.state(role, AgentState.Generating, f"Aufgabe {task_id}: {assignment.title}", task_id)
        for step in range(1, max_steps + 1):
            cancel.raise_if_cancelled()
            if self.clock.exceeded():
                minutes = self.limits.max_active_minutes
                self.reporter.state(role, AgentState.WaitingUser, f"{minutes:g} Minuten aktive Laufzeit erreicht", task_id)
                self.reporter.chat("Orchestrator", f"{role}: {minutes:g} Minuten aktive Laufzeit ohne neue Nachricht von dir "
                                   "erreicht. Schreib eine Nachricht (z. B. „weiter“), um fortzusetzen, oder brich den Agenten ab.")
                self.clock.wait_for_user(cancel)
                self.reporter.state(role, AgentState.Generating, f"Aufgabe {task_id} fortgesetzt", task_id)
            parts = assemble(self.system, self._task_text(assignment), history, evidence, self.budget, self.chars_per_token)
            request = ChatRequest(
                model=self.profile.model,
                messages=[{"role": "system", "content": parts.system}, {"role": "user", "content": parts.user}],
                num_ctx=self.profile.context_tokens, num_predict=self.budget.output, temperature=self.profile.temperature,
                think=self.profile.think, extra_options=dict(self.profile.options))
            label = f"Schritt {step}/{max_steps} · ~{parts.prompt_tokens} Token Eingabe"
            if parts.dropped_evidence:
                label += f" · {len(parts.dropped_evidence)} Evidenzblöcke wegen Budget ausgelassen"
            try:
                with self.gpu.slot(role, self.profile.key, assignment.priority, cancel,
                                   on_wait=lambda: self.reporter.state(role, AgentState.Queued, "wartet auf GPU-Slot", task_id)):
                    self.reporter.state(role, AgentState.Generating, label, task_id)
                    result = self.gateway.chat(request, cancel, self._callbacks(task_id))
            except OutOfMemory as err:
                raise AgentFailed(f"Speichermangel bei {self.profile.label()}: {err}. Profil neu kalibrieren oder kleineres "
                                  "Profil wählen.") from err
            except RepetitionLoop as err:
                self.reporter.event(role, ActivityKind.Warning, str(err), task_id=task_id)
                history.append(f"Step {step}: your answer degenerated into endless repetition and was cut off. "
                               "Answer again, short and concrete.")
                continue
            except GatewayError as err:
                gateway_errors += 1
                self.reporter.event(role, ActivityKind.Error, f"Modellfehler: {err}", task_id=task_id)
                if gateway_errors >= 2:
                    raise AgentFailed(f"Ollama-Fehler: {err}") from err
                history.append(f"Step {step}: model request failed ({err}); answer again.")
                continue
            gateway_errors = 0
            self.reporter.event(role, ActivityKind.Metric,
                                f"{result.output_tokens} Token in {result.total_seconds:.1f} s"
                                + (f", erste Ausgabe nach {result.first_token_seconds:.1f} s" if result.first_token_seconds else ""),
                                task_id=task_id, model=self.profile.model, tokens=result.output_tokens,
                                tokens_per_second=round(result.tokens_per_second, 2),
                                elapsed_ms=int(result.total_seconds * 1000),
                                context_tokens=result.prompt_tokens or parts.prompt_tokens)
            if result.done_reason == "length":
                history.append(f"Step {step}: your answer was cut off at the output limit. Be much shorter.")
            try:
                action = protocol.parse(result.content)
                if action.kind == "finish":
                    final = protocol.validate_result(role, action.result)
                    feedback = assignment.finisher(final) if assignment.finisher else None
                    if feedback is not None:
                        history.append(f"Step {step}: {feedback}")
                        self.reporter.state(role, AgentState.Generating, f"Aufgabe {task_id}: Rückmeldung nach Schritt {step}", task_id)
                        continue
                    self.reporter.state(role, AgentState.Done, f"Aufgabe {task_id} abgeschlossen ({step} Schritte)", task_id)
                    return AgentResult(final, step, evidence)
            except protocol.ProtocolError as err:
                self.reporter.event(role, ActivityKind.Warning, f"Antwort nicht verwertbar: {err}", task_id=task_id)
                history.append(f"Step {step}: invalid answer – {err}")
                invalid += 1
                if self.limits.max_invalid_answers and invalid >= self.limits.max_invalid_answers:
                    raise LimitReached(f"{invalid} unbrauchbare Antworten für Aufgabe {task_id} (limits.maxInvalidAnswersPerTask)")
                continue
            outcome = self.bridge.execute(role, task_id, action, cancel)
            if outcome.denied:
                denials += 1
                if self.limits.max_denials_per_task and denials >= self.limits.max_denials_per_task:
                    raise LimitReached(f"{denials} Vorschläge für Aufgabe {task_id} von dir abgelehnt (limits.maxDenialsPerTask)")
            evidence.extend(outcome.evidence)
            history.append(f"Step {step}: {outcome.summary}")
            self.reporter.state(role, AgentState.Generating, f"Aufgabe {task_id}: weiter nach Schritt {step}", task_id)
        raise LimitReached(f"{max_steps} Modellschritte für Aufgabe {task_id} erreicht")

    def _callbacks(self, task_id: str) -> StreamCallbacks:
        role, reporter, model = self.role, self.reporter, self.profile.model

        def stall(seconds: float) -> None:
            reporter.event(role, ActivityKind.Warning, f"Seit {seconds:.0f} s keine Tokens – hängt? Abbruch ist möglich.",
                           task_id=task_id, stalled=True)

        def resume() -> None:
            reporter.event(role, ActivityKind.Metric, "Tokens kommen wieder", task_id=task_id, stalled=False)

        def progress(tokens: int, elapsed: float) -> None:
            reporter.event(role, ActivityKind.Metric, None, task_id=task_id, model=model, tokens=tokens,
                           tokens_per_second=round(tokens / elapsed, 2) if elapsed > 0 else 0.0,
                           elapsed_ms=int(elapsed * 1000))

        return StreamCallbacks(
            on_content=lambda t: reporter.stream(role, ActivityKind.Output, t, task_id),
            on_thinking=lambda t: reporter.stream(role, ActivityKind.Thinking, t, task_id),
            on_stall=stall, on_resume=resume, on_progress=progress)
