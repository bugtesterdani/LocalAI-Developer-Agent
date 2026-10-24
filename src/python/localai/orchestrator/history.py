"""Verlauf eines Agenten über seine Modellschritte.

Zwei Arten von Einträgen:
- **Orchestrator** – was aus einem Schritt wurde (Tool-Ergebnis, Ablehnung, Formatfehler, Rückmeldung zum Patch). Bleibt
  immer wörtlich und vollständig im Prompt.
- **Modell** – was das Modell selbst geschrieben hat (Begründung, ggf. Ende der Denkphase, gewählte Aktion). Die neuesten
  bleiben wörtlich; ältere werden, sobald sie zu viel Platz brauchen, zu einer laufenden Zusammenfassung verdichtet
  (eigenes oder gleiches Modell, siehe ``HistorySettings``). Ohne Zusammenfasser bzw. wenn er scheitert: mechanisch gekürzt.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from localai.orchestrator import protocol
from localai.orchestrator.context import (
    DEFAULT_CHARS_PER_TOKEN,
    HistoryLine,
    estimate_tokens,
    neutralize,
)

MAX_PROSE_CHARS = 1500  # wörtliche Begründung je Schritt
MAX_THINKING_CHARS = 2500  # Ende der Denkphase, nur für die Zusammenfassung
SHOWN_THINKING_CHARS = 600  # Ende der Denkphase im Prompt, wenn das Modell sonst nichts geschrieben hat
SHORT_NOTE_CHARS = 220  # mechanische Kürzung je Schritt

_EDIT_BLOCK = re.compile(r"^[ \t]*<{5,9}[ \t]*SEARCH.*?^[ \t]*>{5,9}[ \t]*(?:REPLACE)?[ \t]*$", re.MULTILINE | re.DOTALL)


@dataclass(frozen=True)
class HistorySettings:
    """``history`` in models.json."""

    mode: str = "summary"  # summary | trim | off
    summary_profiles: tuple[str, ...] = ()  # leer = Modell der Rolle selbst (kein Modellwechsel im Grafikspeicher)
    keep_recent: int = 3  # so viele neueste Modellnotizen bleiben wörtlich
    max_note_tokens: int = 1500  # ab so vielen Token wörtlicher Modellnotizen wird verdichtet
    summary_tokens: int = 600  # höchstens so lang wird die Zusammenfassung


def prose(reasoning: str) -> str:
    """Begründungstext ohne Codeblöcke und SEARCH/REPLACE-Blöcke (die stehen als Patch bzw. Evidenz ohnehin bereit)."""
    text = _EDIT_BLOCK.sub("[edit block]", reasoning)
    for fence in reversed(protocol.fences(text)):
        text = text[:fence.start] + f"[{fence.lang or 'code'} block]\n" + text[fence.end:]
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def describe_action(action: protocol.Action) -> str:
    if action.kind == "list":
        return f"list {action.path} depth {action.depth}"
    if action.kind == "read":
        files = [f.path + (f":{f.start}-{f.end}" if f.start or f.end else "") for f in action.files]
        return "read " + ", ".join(files) + (f" ({action.reason})" if action.reason else "")
    if action.kind in ("search", "symbols"):
        return f"{action.kind} {action.query!r}" + (f" ({action.reason})" if action.reason else "")
    if action.kind == "fetch":
        return f"fetch {action.url}" + (f" ({action.reason})" if action.reason else "")
    if action.kind == "debug":
        return "debug " + str(action.debug.get("command", ""))
    if action.kind == "finish":
        keys = sorted(action.result)
        return "finish (" + ", ".join(keys) + ")" if keys else "finish"
    return action.kind


def _clip(text: str, limit: int, tail: bool = False) -> str:
    if len(text) <= limit:
        return text
    return ("…" + text[-limit:]) if tail else (text[:limit].rstrip() + " …")


@dataclass
class ModelNote:
    step: int
    text: str  # wörtlich im Prompt
    thinking: str = ""  # nur für die Zusammenfassung

    def line(self) -> str:
        return f"Step {self.step} – your note: {self.text}"

    def short(self) -> str:
        return f"Step {self.step} – your note (shortened): {_clip(' '.join(self.text.split()), SHORT_NOTE_CHARS)}"

    def summary_input(self) -> str:
        block = f"Step {self.step}: {self.text}"
        if self.thinking:
            block += f"\n(end of your thinking in step {self.step}: {self.thinking})"
        return block


# (bisherige Zusammenfassung, neue Notizen) → neue Zusammenfassung; wirft bei Fehlern.
Summarizer = Callable[[str, list[str]], str]


@dataclass
class StepHistory:
    settings: HistorySettings = field(default_factory=HistorySettings)
    entries: list[tuple[int, str, ModelNote | None]] = field(default_factory=list)  # (Schritt, Orchestrator-Text, Notiz)
    summary: str = ""
    summarized_through: int = 0  # Modellnotizen bis zu diesem Schritt stecken in ``summary``
    shortened: bool = False  # Zusammenfassung nur mechanisch (Zusammenfasser aus oder gescheitert)

    # ------------------------------------------------------------------ Einträge
    def orchestrator(self, step: int, text: str) -> None:
        self.entries.append((step, text, None))

    def model(self, step: int, action: protocol.Action | None, content: str = "", thinking: str = "") -> None:
        if self.settings.mode == "off":
            return
        text = prose(action.reasoning) if action is not None else prose(content)
        text = _clip(text, MAX_PROSE_CHARS)
        thought = " ".join(thinking.split())
        if not text and thought:
            text = "(thinking) " + _clip(thought, SHOWN_THINKING_CHARS, tail=True)
        if action is not None:
            text = (text + " → " if text else "→ ") + describe_action(action)
        if not text:
            return
        self.entries.append((step, "", ModelNote(step, neutralize(text), neutralize(_clip(thought, MAX_THINKING_CHARS, tail=True)))))

    # ------------------------------------------------------------------ Verdichten
    def _open_notes(self) -> list[ModelNote]:
        return [n for _, _, n in self.entries if n is not None and n.step > self.summarized_through]

    def compact(self, summarize: Summarizer | None, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
                limit_tokens: int | None = None) -> tuple[int, int, str | None] | None:
        """Verdichtet ältere Modellnotizen, wenn sie zu viel Platz brauchen.

        ``limit_tokens``: zusätzliche Grenze aus dem Kontextbudget. Rückgabe: (erster, letzter Schritt, Fehler) der
        verdichteten Notizen oder None, wenn nichts zu tun war."""
        if self.settings.mode == "off":
            return None
        notes = self._open_notes()
        limit = self.settings.max_note_tokens if limit_tokens is None else min(self.settings.max_note_tokens, limit_tokens)
        costs = [estimate_tokens(n.line(), chars_per_token) for n in notes]
        if sum(costs) <= limit:
            return None
        # Neueste wörtlich behalten – bei knappem Platz weniger, aber mindestens die letzte.
        keep = min(self.settings.keep_recent, len(notes))
        while keep > 1 and sum(costs[len(notes) - keep:]) > limit:
            keep -= 1
        fold = notes[:len(notes) - keep]
        if not fold:
            return None
        error: str | None = None
        if self.settings.mode == "summary" and summarize is not None:
            try:
                text = summarize(self.summary, [n.summary_input() for n in fold]).strip()
                if not text:
                    raise ValueError("leere Zusammenfassung")
                max_chars = int(self.settings.summary_tokens * chars_per_token)
                self.summary = neutralize(_clip(text, max_chars))
                self.shortened = False
            except Exception as err:  # noqa: BLE001 – Zusammenfassung ist Komfort; der Agent arbeitet weiter
                error = str(err) or type(err).__name__
                self._shorten(fold, chars_per_token)
        else:
            self._shorten(fold, chars_per_token)
        self.summarized_through = fold[-1].step
        return fold[0].step, fold[-1].step, error

    def _shorten(self, fold: list[ModelNote], chars_per_token: float) -> None:
        lines = ([self.summary] if self.summary else []) + [n.short() for n in fold]
        max_chars = int(self.settings.summary_tokens * chars_per_token)
        text = "\n".join(lines)
        # Älteste Zeilen zuerst opfern; neueste Erkenntnisse sind meist wichtiger.
        self.summary = text if len(text) <= max_chars else "…" + text[-max_chars:]
        self.shortened = True

    # ------------------------------------------------------------------ Prompt
    def lines(self) -> list[HistoryLine]:
        result: list[HistoryLine] = []
        if self.summary:
            how = "shortened" if self.shortened else "summarised"
            result.append(HistoryLine(f"Your notes from steps 1–{self.summarized_through} ({how}):\n{self.summary}", rank=1))
        for step, text, note in self.entries:
            if note is None:
                result.append(HistoryLine(f"Step {step}: {text}", keep=True))
            elif note.step > self.summarized_through:
                result.append(HistoryLine(note.line()))
        return result


def batch_notes(notes: list[str], room_chars: int) -> list[list[str]]:
    """Teilt die Notizen für die Zusammenfassung so auf, dass jeder Aufruf in den Kontext des Zusammenfassers passt
    (älteste zuerst; eine einzelne zu lange Notiz wird gekürzt – Denkphase und Ende zuerst)."""
    room_chars = max(room_chars, 400)
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for note in notes:
        if len(note) > room_chars:
            note = _clip(note, room_chars - 2)
        if current and size + len(note) + 2 > room_chars:
            batches.append(current)
            current, size = [], 0
        current.append(note)
        size += len(note) + 2
    if current:
        batches.append(current)
    return batches
