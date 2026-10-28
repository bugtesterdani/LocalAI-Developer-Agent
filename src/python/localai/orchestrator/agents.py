"""Ein Agent arbeitet eine Aufgabe in Modellschritten ab (höchstens ``max_steps``).

Jeder Schritt: Prompt im Kontextbudget bauen → GPU-Slot → gestreamte Generierung → Aktion auswerten →
ggf. Toolvorschlag beim Broker (wartet auf Nutzerentscheidung) → Evidenz übernehmen.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from localai.contracts import ActivityKind, AgentState
from localai.orchestrator import prompts, protocol
from localai.orchestrator.activity import ActivityReporter
from localai.orchestrator.context import (
    DEFAULT_CHARS_PER_TOKEN,
    Budget,
    Evidence,
    assemble,
    estimate_tokens,
    task_budget,
    task_text,
)
from localai.orchestrator.gateway import (
    CancelToken,
    ChatRequest,
    GatewayError,
    OllamaGateway,
    OutOfMemory,
    RepetitionLoop,
    StreamCallbacks,
)
from localai.orchestrator.history import HistorySettings, StepHistory, batch_notes
from localai.orchestrator.models import Limits, Measurement, ModelProfile
from localai.orchestrator.runtime import (
    PRIORITY_NORMAL,
    FairGate,
    LimitReached,
    RunClock,
)
from localai.orchestrator.tools import ToolBridge

# Zeichen je Token, die eine Kalibrierung realistisch liefern kann (Code/Englisch ≈ 2,5–4,5).
PLAUSIBLE_CHARS_PER_TOKEN = (1.5, 6.0)
# Quelle einer Auflistung (tools.ToolBridge._evidence): "File.List <Ordner> depth=<n>"
_LISTING = re.compile(r"File\.List (.+) depth=(\d+)")
# Denkphase, die manche Modelle trotz think=false in den Text schreiben
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def _merge_reads(actions: list[protocol.Action]) -> list[protocol.Action]:
    """Aufeinanderfolgende Leseaktionen einer Antwort → eine Sammelfreigabe (jede Datei einzeln abwählbar)."""
    merged: list[protocol.Action] = []
    for action in actions:
        last = merged[-1] if merged else None
        if (action.kind == "read" and last is not None and last.kind == "read"
                and len(last.files) + len(action.files) <= protocol.MAX_READ_FILES):
            paths = {(f.path, f.start, f.end) for f in last.files}
            last.files += [f for f in action.files if (f.path, f.start, f.end) not in paths]
            last.reason = "; ".join(r for r in (last.reason, action.reason) if r)
            continue
        merged.append(action)
    return merged


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
    # Dateien, die die Aufgabe ändern darf (Pfad für SEARCH/REPLACE-Blöcke bzw. Diffs ohne Kopfzeilen).
    files: list[str] = field(default_factory=list)
    # Mehr Modellschritte (z. B. Debug-Sitzung: jeder Befehl ist ein Schritt); None = limits der Rolle.
    max_steps: int | None = None


@dataclass
class AgentResult:
    result: dict[str, Any]
    steps: int
    evidence: list[Evidence]


class Agent:
    def __init__(self, role: str, profile: ModelProfile, measurement: Measurement | None, gateway: OllamaGateway,
                 gpu: FairGate, bridge: ToolBridge, reporter: ActivityReporter, clock: RunClock, limits: Limits,
                 user_notes: Callable[[], list[str]], history: HistorySettings | None = None,
                 summary_profile: ModelProfile | None = None) -> None:
        self.role = role
        self.profile = profile
        measured = measurement.chars_per_token if measurement else 0.0
        self.chars_per_token = measured * 0.9 if PLAUSIBLE_CHARS_PER_TOKEN[0] <= measured <= PLAUSIBLE_CHARS_PER_TOKEN[1] \
            else DEFAULT_CHARS_PER_TOKEN
        if measured and self.chars_per_token == DEFAULT_CHARS_PER_TOKEN:
            # Unplausible Messung (z. B. Ollama meldet zu wenige Prompt-Token): sonst würde der Prompt um ein Vielfaches
            # unterschätzt und Ollama schneidet still den Anfang (die Regeln) ab.
            reporter.event(role, ActivityKind.Warning, f"Kalibrierung meldet {measured:g} Zeichen je Token für "
                           f"{profile.model} – unplausibel, gerechnet wird mit {DEFAULT_CHARS_PER_TOKEN:g}. Profil neu "
                           "kalibrieren.")
        self.gateway = gateway
        self.gpu = gpu
        self.bridge = bridge
        self.reporter = reporter
        self.clock = clock
        self.limits = limits
        self.user_notes = user_notes
        self.system, self.prompts = prompts.system_prompt(role)
        self.budget = Budget.split(profile.context_tokens, profile.max_output_tokens)
        self.history_settings = history or HistorySettings()
        # Eigenes Modell zum Zusammenfassen; None = Modell der Rolle (bleibt geladen, kein Wechsel im Grafikspeicher).
        self.summary_profile = summary_profile
        self.summary_prompt = prompts.load("summarizer")

    @property
    def prompt_tags(self) -> str:
        used = self.prompts + ([self.summary_prompt] if self.history_settings.mode == "summary" else [])
        return ", ".join(p.tag for p in used)

    def _task_text(self, assignment: Assignment) -> str:
        text = assignment.text
        notes = self.user_notes()
        if notes:
            text += "\n\n## Notes from the user (latest last; they refine the goal)\n" + "\n".join(f"- {n}" for n in notes)
        return text

    def run(self, assignment: Assignment, cancel: CancelToken) -> AgentResult:
        role, task_id = self.role, assignment.task_id
        evidence = list(assignment.evidence)
        max_steps = assignment.max_steps or self.limits.steps_for(role)
        try:
            return self._run(assignment, cancel, evidence, max_steps)
        finally:
            # M9: eine offene Debug-Sitzung dieser Aufgabe endet mit ihr (QEMU wird beendet).
            end = getattr(self.bridge, "end_debug", None)
            if end is not None:
                end(role, task_id)

    def _run(self, assignment: Assignment, cancel: CancelToken, evidence: list[Evidence], max_steps: int) -> AgentResult:
        role, task_id = self.role, assignment.task_id
        history = StepHistory(self.history_settings)

        def summarize(previous: str, notes: list[str]) -> str:
            return self._summarize(assignment, previous, notes, cancel)

        gateway_errors = 0
        denials = 0
        invalid = 0
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
            task = self._task_text(assignment)
            self._compact(history, summarize, task, task_id)
            parts = assemble(self.system, task, history.lines(), evidence, self.budget, self.chars_per_token)
            # Nicht (vollständig) sichtbare Evidenz: weggelassen oder gekürzt – darauf nie verweisen statt erneut anzufragen.
            dropped = {d.split(" ", 1)[0] for d in parts.dropped_evidence} | set(parts.truncated_evidence)
            limit = self._output_limit(parts.prompt_tokens)
            request = ChatRequest(
                model=self.profile.model,
                messages=[{"role": "system", "content": parts.system}, {"role": "user", "content": parts.user}],
                num_ctx=self.profile.context_tokens, num_predict=limit, temperature=self.profile.temperature,
                think=self.profile.think, extra_options=dict(self.profile.options))
            label = f"Schritt {step}/{max_steps} · ~{parts.prompt_tokens} Token Eingabe"
            if limit != self.budget.output:
                label += f" · bis zu {limit} Token Ausgabe"
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
                history.orchestrator(step, "your answer degenerated into endless repetition and was cut off. "
                                     "Answer again, short and concrete.")
                continue
            except GatewayError as err:
                gateway_errors += 1
                self.reporter.event(role, ActivityKind.Error, f"Modellfehler: {err}", task_id=task_id)
                if gateway_errors >= 2:
                    raise AgentFailed(f"Ollama-Fehler: {err}") from err
                history.orchestrator(step, f"model request failed ({err}); answer again.")
                continue
            gateway_errors = 0
            self.reporter.event(role, ActivityKind.Metric,
                                f"{result.output_tokens} Token in {result.total_seconds:.1f} s"
                                + (f", erste Ausgabe nach {result.first_token_seconds:.1f} s" if result.first_token_seconds else ""),
                                task_id=task_id, model=self.profile.model, tokens=result.output_tokens,
                                tokens_per_second=round(result.tokens_per_second, 2),
                                elapsed_ms=int(result.total_seconds * 1000),
                                context_tokens=result.prompt_tokens or parts.prompt_tokens)
            content = result.content
            try:
                if result.done_reason == "length":
                    # Abgeschnittene Antwort: das letzte JSON darin ist meist ein inneres Objekt (z. B. aus einer Datei) –
                    # „Unknown action“ oder „No change found“ würden das Modell auf eine falsche Fährte setzen.
                    self.reporter.event(role, ActivityKind.Warning, f"Antwort an der Ausgabegrenze abgeschnitten "
                                        f"({limit} Token, Denkphase eingeschlossen; Profil {self.profile.name}: maxOutputTokens bzw. "
                                        "outputUpToContext)",
                                        task_id=task_id)
                    raise protocol.ProtocolError(
                        f"Your answer was cut off at the output limit ({limit} tokens, thinking included) and was not used. "
                        "Answer again much shorter: reasoning in a few sentences, findings only in \"notes\", and for a large "
                        "new file nothing but its edit block before the JSON (no copy of it anywhere else).")
                called = protocol.tool_call_action(result.tool_calls) if result.tool_calls else None
                if called:
                    # Funktionsaufruf statt Text (gpt-oss): als Aktion übernehmen; die Antwort zählt wie geschrieben.
                    content = content.rstrip() + "\n" + called
                    self.reporter.stream(role, ActivityKind.Output,
                                         f"\n[Funktionsaufruf übernommen: {called.strip('`').strip()}]\n", task_id)
                if not content.strip():
                    raise protocol.ProtocolError(self._empty_answer_hint(result))
                action = protocol.parse(content)
                history.model(step, action, thinking=result.thinking)
                if action.kind == "finish":
                    protocol.resolve_patch(role, action, assignment.files, lambda p: self._file_lines(evidence, p))
                    final = protocol.validate_result(role, action.result, analysis_allowed=not assignment.files)
                    feedback = assignment.finisher(final) if assignment.finisher else None
                    if feedback is not None:
                        history.orchestrator(step, feedback)
                        self.reporter.state(role, AgentState.Generating, f"Aufgabe {task_id}: Rückmeldung nach Schritt {step}", task_id)
                        continue
                    self.reporter.state(role, AgentState.Done, f"Aufgabe {task_id} abgeschlossen ({step} Schritte)", task_id)
                    return AgentResult(final, step, evidence)
            except protocol.ProtocolError as err:
                self.reporter.event(role, ActivityKind.Warning, f"Antwort nicht verwertbar: {err}", task_id=task_id)
                # Für die Fehlersuche im Orchestrator-Log (bleibt lokal): was das Modell tatsächlich geschrieben hat.
                print(f"[{role}] {task_id} Schritt {step} – unbrauchbare Antwort ({len(result.content)} Zeichen, "
                      f"Denkphase {len(result.thinking)} Zeichen, done_reason={result.done_reason or '-'}, "
                      f"Funktionsaufrufe {result.tool_calls or '-'}):\n{result.content[:2500]}\n"
                      f"[Ende der Denkphase: {result.thinking[-600:]}]\n[Ende der Antwort]", file=sys.stderr, flush=True)
                history.orchestrator(step, f"invalid answer – {err}")
                invalid += 1
                if self.limits.max_invalid_answers and invalid >= self.limits.max_invalid_answers:
                    raise LimitReached(f"{invalid} unbrauchbare Antworten für Aufgabe {task_id} (limits.maxInvalidAnswersPerTask)")
                continue
            if action.ignored_finish:
                history.orchestrator(step, "your finish was not used because the same answer requested other actions; "
                                     "finish again after you have seen their results.")
            for act in _merge_reads([action, *action.more]):
                done = self._perform(step, act, evidence, dropped, history, cancel, task_id)
                if done == "denied":
                    denials += 1
                    if self.limits.max_denials_per_task and denials >= self.limits.max_denials_per_task:
                        raise LimitReached(f"{denials} Vorschläge für Aufgabe {task_id} von dir abgelehnt "
                                           "(limits.maxDenialsPerTask)")
                if done == "expired":
                    break  # der Nutzer war weg: übrige Aktionen nicht mehr vorschlagen, das Modell entscheidet neu
            self.reporter.state(role, AgentState.Generating, f"Aufgabe {task_id}: weiter nach Schritt {step}", task_id)
        raise LimitReached(f"{max_steps} Modellschritte für Aufgabe {task_id} erreicht")

    def _compact(self, history: StepHistory, summarize: Callable[[str, list[str]], str], task: str, task_id: str) -> None:
        # Platz für eigene Notizen: was vom Anteil für Aufgabe und Verlauf nach Aufgabentext, Rückmeldungen des
        # Orchestrators und bisheriger Zusammenfassung bleibt.
        cpt = self.chars_per_token
        room = task_budget(self.system, self.budget, cpt)
        used = estimate_tokens(task_text(task, room, cpt), cpt)
        used += sum(estimate_tokens(h.text, cpt) for h in history.lines() if h.keep or h.rank)
        done = history.compact(summarize if self.history_settings.mode == "summary" else None, cpt,
                               max(0, room - used))
        if done is None:
            return
        first, last, error = done
        span = f"Schritte {first}–{last}" if first != last else f"Schritt {first}"
        size = estimate_tokens(history.summary, self.chars_per_token)
        if error:
            self.reporter.event(self.role, ActivityKind.Warning, f"Verlauf ({span}) nicht zusammengefasst ({error}) – "
                                f"gekürzt übernommen, ~{size} Token", task_id=task_id)
        else:
            how = "gekürzt" if history.shortened else "zusammengefasst"
            self.reporter.event(self.role, ActivityKind.Metric, f"Eigene Notizen {span} {how}, ~{size} Token:\n"
                                f"{history.summary}", task_id=task_id)

    def _summarize(self, assignment: Assignment, previous: str, notes: list[str], cancel: CancelToken) -> str:
        """Fasst ältere Notizen des Modells zusammen (Rückmeldungen des Orchestrators bleiben ohnehin wörtlich).

        Passen die Notizen (inkl. Denkphase) nicht in einen Aufruf, wird in mehreren Aufrufen nacheinander
        fortgeschrieben – Grenze ist der Kontext des Zusammenfassers, nicht der der Rolle."""
        profile = self.summary_profile or self.profile
        settings = self.history_settings
        cpt = self.chars_per_token if self.summary_profile is None else DEFAULT_CHARS_PER_TOKEN
        system = self.summary_prompt.text.replace("{words}", str(settings.summary_tokens // 2))
        header = f"## Task of the agent\n{assignment.title}\n\n## Previous summary\n\n## New notes (oldest first)\n"
        output = min(settings.summary_tokens + 200, profile.max_output_tokens)
        # Kontext minus Ausgabe, Reserve, Anweisungen, Kopf und (höchstens so lange) bisherige Zusammenfassung.
        room = (profile.context_tokens - output - profile.context_tokens // 16 - estimate_tokens(system, cpt)
                - estimate_tokens(header, cpt) - settings.summary_tokens)
        for batch in batch_notes(notes, int(room * cpt)):
            user = f"## Task of the agent\n{assignment.title}\n\n"
            if previous:
                user += f"## Previous summary\n{previous}\n\n"
            user += "## New notes (oldest first)\n" + "\n\n".join(batch)
            # Gleiches num_ctx wie das Profil: Ollama lädt das Modell sonst neu.
            previous = self._summary_call(profile, system, user, output, assignment, cancel)
            # Für den nächsten Teil auf die zulässige Länge begrenzen.
            previous = previous[:int(settings.summary_tokens * cpt)]
        return previous

    def _summary_call(self, profile: ModelProfile, system: str, user: str, output: int, assignment: Assignment,
                      cancel: CancelToken) -> str:
        """Ohne Denkphase (think=false): Modelle, die von sich aus denken (z. B. qwen3.6), verbrauchten sonst die ganze
        Ausgabe fürs Denken und lieferten eine leere Zusammenfassung. Lehnt das Modell think=false ab oder bleibt die
        Antwort leer: einmal mit der Einstellung des Profils und mehr Ausgabe."""
        role, task_id = self.role, assignment.task_id
        attempts = [(False, output), (profile.think, min(profile.max_output_tokens, max(output * 4, 2048)))]
        last_error: Exception | None = None
        for think, num_predict in attempts:
            request = ChatRequest(
                model=profile.model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                num_ctx=profile.context_tokens, num_predict=num_predict, temperature=0.1, think=think,
                extra_options=dict(profile.options))
            with self.gpu.slot(role, profile.key, assignment.priority, cancel,
                               on_wait=lambda: self.reporter.state(role, AgentState.Queued, "wartet auf GPU-Slot", task_id)):
                self.reporter.state(role, AgentState.Generating, f"Eigene Notizen zusammenfassen ({profile.model})", task_id)
                try:
                    result = self.gateway.chat(request, cancel)
                except OutOfMemory:
                    raise
                except GatewayError as err:
                    last_error = err
                    continue
            text = _THINK.sub("", result.content).strip()
            if text:
                return text
            last_error = ValueError("leere Zusammenfassung" + (" (nur Denkphase)" if result.thinking.strip() else ""))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _empty_answer_hint(result) -> str:
        if result.tool_calls:
            names = ", ".join(sorted({c.get("name") or "?" for c in result.tool_calls}))
            return (f"You called the function(s) {names}, but no functions are available here. Write the action as one "
                    "```json block in your visible answer instead (see Actions), e.g. "
                    '```json\n{"action": "read", "files": [{"path": "..."}], "reason": "..."}\n```')
        if result.thinking.strip():
            return ("Your visible answer was empty – you only thought. After thinking, always write the visible answer: "
                    "a few sentences of reasoning and exactly one ```json block with the action (see Actions).")
        return "Your answer was empty. Write a few sentences of reasoning and exactly one ```json block with the action."

    def _output_limit(self, prompt_tokens: int) -> int:
        """Höchstzahl erzeugter Token (Ollama zählt die Denkphase mit). Standard: maxOutputTokens. Mit outputUpToContext:
        alles, was nach dem Prompt im Kontext frei ist (abzüglich Reserve, da der Prompt nur geschätzt ist)."""
        if not self.profile.output_up_to_context:
            return self.budget.output
        return max(self.budget.output, self.profile.context_tokens - prompt_tokens - self.budget.reserve)

    def _perform(self, step: int, action: protocol.Action, evidence: list[Evidence], dropped: set[str],
                 history: StepHistory, cancel: CancelToken, task_id: str) -> str:
        """Eine Aktion (Lesen, Auflisten, Suchen, …) mit eigener Freigabe. Ergebnis: done | skipped | denied | expired."""
        role = self.role
        if action.kind == "read":
            known = self._known_reads(action, evidence, dropped)
            if known:
                action.files = [f for f in action.files if f.path not in known]
                note = (f"read {', '.join(sorted(known))}: not read again – the current content (same snapshot) is "
                        f"already in the evidence ({', '.join(known[p] for p in sorted(known))}). Use it.")
                if not action.files:
                    self.reporter.state(role, AgentState.Generating, f"{', '.join(sorted(known))} liegt schon vor – "
                                        "nicht erneut gelesen", task_id)
                    history.orchestrator(step, f"{note} Continue with your task now.")
                    return "skipped"
                history.orchestrator(step, note)
        if action.kind == "list" and (known_list := self._known_listing(action, evidence, dropped)):
            self.reporter.state(role, AgentState.Generating, f"Auflistung {action.path} liegt schon vor – nicht erneut "
                                "angefragt", task_id)
            history.orchestrator(step, f"list {action.path} depth {action.depth}: not requested again – the same listing "
                                 f"(same snapshot) is already in the evidence ({known_list}). Use it and continue with your task.")
            return "skipped"
        outcome = self.bridge.execute(role, task_id, action, cancel)
        if outcome.expired:
            # Niemand entscheidet: nicht weiter vorschlagen (jeder Vorschlag wartet sonst wieder bis zum Ablauf),
            # sondern warten, bis der Nutzer zurück ist.
            self.reporter.state(role, AgentState.WaitingUser, "Vorschlag abgelaufen – wartet auf dich", task_id)
            self.reporter.chat("Orchestrator", f"{role}: Ein Vorschlag ist abgelaufen, ohne dass du entschieden hast. Ich "
                               "warte, bis du wieder da bist – schreib eine Nachricht (z. B. „weiter“), dann geht es weiter.")
            self.clock.wait_for_user(cancel)
            self.reporter.state(role, AgentState.Generating, f"Aufgabe {task_id} fortgesetzt", task_id)
            history.orchestrator(step, f"{outcome.summary}. The user was away and is back now; propose it again only if "
                                 "you still need it.")
            return "expired"
        evidence.extend(outcome.evidence)
        history.orchestrator(step, outcome.summary)
        return "denied" if outcome.denied else "done"

    def _current_snapshot(self) -> str:
        return getattr(self.bridge, "snapshot_id", None) or ""

    def _file_lines(self, evidence: list[Evidence], path: str) -> list[str] | None:
        """Ganzer Dateiinhalt auf dem aktuellen Stand, falls im Kontext (neueste Evidenz zuerst)."""
        snapshot = self._current_snapshot()
        if not snapshot:
            return None
        for item in reversed(evidence):
            if item.path == path and item.snapshot_id == snapshot:
                return [] if item.text == "(empty)" else item.text.splitlines()
        return None

    def _known_reads(self, action: protocol.Action, evidence: list[Evidence], dropped: set[str]) -> dict[str, str]:
        """Pfade der Leseanfrage, deren ganzer Inhalt auf dem aktuellen Stand schon im Prompt steht → Evidenz-ID."""
        snapshot = self._current_snapshot()
        known: dict[str, str] = {}
        if not snapshot:
            return known
        for ref in action.files:
            for item in reversed(evidence):
                if item.path == ref.path and item.snapshot_id == snapshot and item.evidence_id not in dropped:
                    known[ref.path] = item.evidence_id
                    break
        return known

    def _known_listing(self, action: protocol.Action, evidence: list[Evidence], dropped: set[str]) -> str | None:
        """Evidenz-ID einer vollständigen Auflistung desselben Ordners auf dem aktuellen Stand mit mindestens dieser Tiefe,
        die ungekürzt im Prompt steht (abgeschnittene Auflistungen tragen „truncated“ und passen nicht auf _LISTING)."""
        snapshot = self._current_snapshot()
        if not snapshot:
            return None
        for item in reversed(evidence):
            match = _LISTING.fullmatch(item.source)
            if (match and item.snapshot_id == snapshot and item.evidence_id not in dropped
                    and match.group(1) == action.path and int(match.group(2)) >= action.depth):
                return item.evidence_id
        return None

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
