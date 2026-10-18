"""Aufgaben-DAG mit Lese-/Schreibmengen (Spezifikation §5).

Schreibaufgaben auf denselben Pfad werden serialisiert; Aufgaben ohne bekannte Schreibmenge erhalten einen
exklusiven Workspace-Lock. Lesen konfliktiert nie.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PlanError(Exception):
    """Plan des Planners ist kein gültiger DAG; wird an den Planner zurückgemeldet."""


class TaskState(str, Enum):
    PENDING = "Pending"
    RUNNING = "Running"
    DONE = "Done"
    FAILED = "Failed"
    CANCELLED = "Cancelled"
    SKIPPED = "Skipped"

    @property
    def final(self) -> bool:
        return self in (TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED, TaskState.SKIPPED)


@dataclass
class Task:
    task_id: str
    role: str
    title: str
    description: str
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    snapshot_id: str = ""
    state: TaskState = TaskState.PENDING
    result: dict[str, Any] | None = None
    error: str = ""
    steps: int = 0

    @property
    def exclusive(self) -> bool:
        """Unbekannte Schreibmenge → exklusiver Workspace-Lock."""
        return not self.writes

    def write_keys(self) -> set[str]:
        return {normalize(p) for p in self.writes}


def normalize(path: str) -> str:
    """Windows-Pfade sind nicht case-sensitiv: Vergleich in Kleinbuchstaben mit Schrägstrichen."""
    p = path.strip().replace("\\", "/").lower()
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def build_plan(tasks: list[dict[str, Any]], snapshot_id: str, max_tasks: int) -> list[Task]:
    if len(tasks) > max_tasks:
        raise PlanError(f"At most {max_tasks} tasks allowed, got {len(tasks)}.")
    result: list[Task] = []
    ids: set[str] = set()
    for raw in tasks:
        task_id = str(raw["id"]).strip()
        if not task_id or task_id in ids or len(task_id) > 40:
            raise PlanError(f"Task ids must be unique and short (problem: {task_id!r}).")
        ids.add(task_id)
        result.append(Task(task_id=task_id, role=str(raw["role"]), title=str(raw["title"])[:200],
                           description=str(raw["description"])[:4000], reads=list(raw.get("reads", [])),
                           writes=list(raw.get("writes", [])), depends_on=list(raw.get("depends_on", [])),
                           snapshot_id=snapshot_id))
    for task in result:
        unknown = [d for d in task.depends_on if d not in ids]
        if unknown:
            raise PlanError(f"Task {task.task_id} depends on unknown tasks {unknown}.")
        if task.task_id in task.depends_on:
            raise PlanError(f"Task {task.task_id} depends on itself.")
    _check_acyclic(result)
    return result


def _check_acyclic(tasks: list[Task]) -> None:
    graph = {t.task_id: t.depends_on for t in tasks}
    state: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            raise PlanError(f"Dependency cycle: {' -> '.join(path + [node])}.")
        state[node] = 1
        for dep in graph[node]:
            visit(dep, path + [node])
        state[node] = 2

    for task_id in graph:
        visit(task_id, [])


def ready_tasks(tasks: list[Task]) -> list[Task]:
    by_id = {t.task_id: t for t in tasks}
    return [t for t in tasks if t.state == TaskState.PENDING
            and all(by_id[d].state == TaskState.DONE for d in t.depends_on)]


def skip_blocked(tasks: list[Task]) -> list[Task]:
    """Aufgaben, deren Abhängigkeit endgültig nicht erfolgreich ist, werden übersprungen."""
    by_id = {t.task_id: t for t in tasks}
    skipped = []
    changed = True
    while changed:
        changed = False
        for t in tasks:
            if t.state == TaskState.PENDING and any(
                    by_id[d].state in (TaskState.FAILED, TaskState.CANCELLED, TaskState.SKIPPED) for d in t.depends_on):
                t.state = TaskState.SKIPPED
                t.error = "Abhängigkeit nicht erfolgreich"
                skipped.append(t)
                changed = True
    return skipped


class WriteLocks:
    """Serialisiert Aufgaben mit überlappender Schreibmenge; exklusive Aufgaben laufen allein."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._held: dict[str, Task] = {}

    def _conflicts(self, task: Task) -> bool:
        for other in self._held.values():
            if task.exclusive or other.exclusive or (task.write_keys() & other.write_keys()):
                return True
        return False

    def try_acquire(self, task: Task) -> bool:
        with self._cond:
            if self._conflicts(task):
                return False
            self._held[task.task_id] = task
            return True

    def acquire(self, task: Task, cancelled=lambda: False, poll: float = 0.2) -> bool:
        with self._cond:
            while self._conflicts(task):
                if cancelled():
                    return False
                self._cond.wait(poll)
            self._held[task.task_id] = task
            return True

    def release(self, task: Task) -> None:
        with self._cond:
            self._held.pop(task.task_id, None)
            self._cond.notify_all()

    def holders(self) -> list[str]:
        with self._cond:
            return list(self._held)
