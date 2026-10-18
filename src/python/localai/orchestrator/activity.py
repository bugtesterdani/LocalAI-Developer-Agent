"""Live-Aktivität an den Broker melden (B17).

Gestreamte Tokens werden je Agent gesammelt und höchstens alle ``interval`` Sekunden als ein Ereignis
gesendet; Zustände, Aufgaben und Chat gehen sofort in den nächsten Stapel. Ein Herzschlag alle
``heartbeat`` Sekunden zeigt dem Broker, dass der Orchestrator lebt (SEC-22). Meldefehler halten die
Agenten nie auf.
"""

from __future__ import annotations

import datetime as dt
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from localai.contracts import ActivityEvent, ActivityKind, ActivityReport, AgentState

MAX_EVENT_TEXT = 12_000
MAX_BATCH = 400


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ActivityReporter:
    def __init__(self, run_id: str, send: Callable[[ActivityReport], Any], interval: float = 0.1,
                 heartbeat: float = 5.0, echo: bool = False) -> None:
        self.run_id = run_id
        self._send = send
        self._interval = interval
        self._heartbeat = heartbeat
        self._echo = echo
        self._lock = threading.Lock()
        self._queue: list[ActivityEvent] = []
        self._streams: dict[tuple[str, ActivityKind, str | None], list[str]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="activity-reporter", daemon=True)
        self._last_send = time.monotonic()
        self.failures = 0
        self.sent = 0

    def start(self) -> ActivityReporter:
        self._thread.start()
        return self

    # ------------------------------------------------------------------ Meldungen
    def event(self, agent: str, kind: ActivityKind, text: str | None = None, *, task_id: str | None = None,
              state: AgentState | None = None, **metrics: Any) -> None:
        self._flush_stream(agent, task_id)
        e = ActivityEvent(seq=0, at=_now(), run_id=self.run_id, agent=agent, kind=kind, task_id=task_id,
                          text=text[:MAX_EVENT_TEXT] if text else text, state=state, **metrics)
        with self._lock:
            self._queue.append(e)
        if self._echo:
            print(f"[{agent}] {kind.value}{' ' + state.value if state else ''}: {(text or '')[:300]}", file=sys.stderr, flush=True)

    def state(self, agent: str, state: AgentState, text: str | None = None, task_id: str | None = None) -> None:
        self.event(agent, ActivityKind.State, text, task_id=task_id, state=state)

    def chat(self, agent: str, text: str, task_id: str | None = None) -> None:
        self.event(agent, ActivityKind.Chat, text, task_id=task_id)

    def stream(self, agent: str, kind: ActivityKind, text: str, task_id: str | None = None) -> None:
        """Token-Stück (Output oder Thinking); wird gebündelt."""
        with self._lock:
            self._streams.setdefault((agent, kind, task_id), []).append(text)

    def _flush_stream(self, agent: str | None = None, task_id: str | None = None) -> None:
        with self._lock:
            keys = [k for k in self._streams if agent is None or k[0] == agent]
            for key in keys:
                pieces = self._streams.pop(key)
                text = "".join(pieces)
                for start in range(0, len(text), MAX_EVENT_TEXT):
                    self._queue.append(ActivityEvent(seq=0, at=_now(), run_id=self.run_id, agent=key[0], kind=key[1],
                                                     task_id=key[2], text=text[start:start + MAX_EVENT_TEXT]))

    # ------------------------------------------------------------------ Versand
    def flush(self) -> None:
        self._flush_stream()
        while True:
            with self._lock:
                batch, self._queue = self._queue[:MAX_BATCH], self._queue[MAX_BATCH:]
            if not batch:
                return
            try:
                self._send(ActivityReport(run_id=self.run_id, events=batch))
                self.sent += len(batch)
                self._last_send = time.monotonic()
            except Exception as err:  # noqa: BLE001 – Anzeige darf Agenten nie aufhalten
                self.failures += 1
                print(f"Aktivität konnte nicht gemeldet werden: {err}", file=sys.stderr, flush=True)
                return

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            if time.monotonic() - self._last_send >= self._heartbeat:
                with self._lock:
                    idle = not self._queue and not self._streams
                if idle:
                    self.event("Orchestrator", ActivityKind.Heartbeat)
            self.flush()
        self.flush()

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        else:
            self.flush()
