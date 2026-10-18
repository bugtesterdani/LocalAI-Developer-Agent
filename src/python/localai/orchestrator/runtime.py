"""Scheduler-Bausteine: GPU-Slot mit fairer Reihenfolge, aktive Laufzeit, Abbruchsignale (Spezifikation §5)."""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field

from localai.orchestrator.gateway import Cancelled, CancelToken

PRIORITY_USER = 0      # Antworten auf Nutzerinteraktion
PRIORITY_REVIEW = 1    # abschließende Reviews
PRIORITY_NORMAL = 2


class LimitReached(Exception):
    """Ein Limit (Schritte, Reparaturen, aktive Zeit) wurde erreicht; Nutzerentscheidung nötig."""


@dataclass(order=True)
class _Ticket:
    priority: int
    number: int
    holder: str = field(compare=False)
    group: str = field(compare=False)
    granted: bool = field(default=False, compare=False)


class FairGate:
    """Begrenzt gleichzeitige GPU-Requests (Standard 1).

    Vergabe nach Priorität, dann Reihenfolge (Round-Robin, niemand verhungert). Gleiches Modell/Profil wird
    bevorzugt gruppiert, aber höchstens ``max_group_streak`` Mal hintereinander, wenn andere warten.
    """

    def __init__(self, slots: int = 1, max_group_streak: int = 3) -> None:
        self._slots = slots
        self._cond = threading.Condition()
        self._waiting: list[_Ticket] = []
        self._numbers = itertools.count()
        self._active = 0
        self._last_group: str | None = None
        self._streak = 0
        self._max_streak = max_group_streak
        self.grants: list[str] = []

    def _pick(self) -> _Ticket | None:
        if not self._waiting:
            return None
        best = min(self._waiting)
        same = [t for t in self._waiting if t.group == self._last_group and t.priority == best.priority]
        if same and self._streak < self._max_streak:
            return min(same)
        return best

    @contextmanager
    def slot(self, holder: str, group: str, priority: int = PRIORITY_NORMAL, cancel: CancelToken | None = None,
             on_wait: Callable[[], None] | None = None):
        ticket = _Ticket(priority, next(self._numbers), holder, group)
        with self._cond:
            self._waiting.append(ticket)
            notified = False
            while True:
                if self._active < self._slots and self._pick() is ticket:
                    break
                if cancel is not None and cancel.cancelled:
                    self._waiting.remove(ticket)
                    self._cond.notify_all()
                    raise Cancelled(cancel.reason)
                if not notified and on_wait is not None:
                    notified = True
                    on_wait()
                self._cond.wait(0.1)
            self._waiting.remove(ticket)
            self._active += 1
            self._streak = self._streak + 1 if group == self._last_group else 1
            self._last_group = group
            self.grants.append(holder)
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()


class RunClock:
    """Aktive Laufzeit seit der letzten Nutzerinteraktion; Warten auf Freigaben zählt nicht (§5)."""

    def __init__(self, limit_seconds: float, now: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit_seconds
        self._now = now
        self._lock = threading.Lock()
        self._active = 0.0
        self._since = now()
        self._paused = 0
        self._user = threading.Event()

    def _update(self) -> None:
        now = self._now()
        if self._paused == 0:
            self._active += now - self._since
        self._since = now

    @property
    def active_seconds(self) -> float:
        with self._lock:
            self._update()
            return self._active

    @contextmanager
    def paused(self):
        with self._lock:
            self._update()
            self._paused += 1
        try:
            yield
        finally:
            with self._lock:
                self._update()
                self._paused -= 1

    def user_interaction(self) -> None:
        with self._lock:
            self._active = 0.0
            self._since = self._now()
        self._user.set()

    def exceeded(self) -> bool:
        return self.active_seconds >= self.limit

    def wait_for_user(self, cancel: CancelToken) -> None:
        """Blockiert, bis der Nutzer schreibt (setzt die Uhr zurück) oder abgebrochen wird."""
        self._user.clear()
        with self.paused():
            while not self._user.wait(0.2):
                cancel.raise_if_cancelled()


class AgentControls:
    """Abbruchsignale je Agent, abgeleitet vom Run-Signal."""

    def __init__(self, roles: tuple[str, ...]) -> None:
        self.run = CancelToken()
        self._roles = roles
        self._tokens = {role: CancelToken() for role in roles}

    def token(self, role: str) -> CancelToken:
        return self._tokens[role]

    def cancel_agent(self, role: str, reason: str) -> None:
        self._tokens[role].cancel(reason)

    def reset_agent(self, role: str) -> CancelToken:
        token = CancelToken()
        if self.run.cancelled:
            token.cancel(self.run.reason)
        self._tokens[role] = token
        return token

    def cancel_run(self, reason: str) -> None:
        self.run.cancel(reason)
        for token in self._tokens.values():
            token.cancel(reason)
