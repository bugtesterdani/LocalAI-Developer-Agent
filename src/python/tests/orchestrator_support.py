"""Hilfen für Orchestrator-Tests: Broker im Speicher (entscheidet per Regel) und Ereignissammlung."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable

import json

from localai.contracts import (
    ActivityEvent,
    ActivityReport,
    FolderGrant,
    FolderRight,
    OrchestratorCommand,
    OrchestratorCommandKind,
    ProposalOutcome,
    ProposalRequest,
    ProposalState,
    ResultStatus,
    TargetResult,
    TestReport,
    TestResultFormat,
    ToolResult,
)
from localai.orchestrator.models import ModelsConfig

FILES = {
    "README.md": "# Demo\nA small calculator.\n",
    "src/calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": "from src.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
}


def models_config(**limits) -> ModelsConfig:
    raw = {
        "requireCalibration": False,
        "ollama": {"stallWarningSeconds": 5, "stallAbortSeconds": 60},
        "profiles": [{"name": "standard", "model": "qwen2.5-coder:14b", "contextTokens": 8192, "maxOutputTokens": 2048}],
        "limits": {"maxStepsPerTask": 20, **limits},
    }
    return ModelsConfig.parse(raw)


@dataclass
class FakeBroker:
    """Antwortet wie der Broker; ``decide(request) -> ProposalState | None`` (None = bleibt offen)."""

    decide: Callable[[ProposalRequest], ProposalState | None] = lambda _r: ProposalState.Succeeded
    files: dict[str, str] = field(default_factory=lambda: dict(FILES))
    decision_delay: float = 0.0
    proposals: dict[str, ProposalRequest] = field(default_factory=dict)
    submitted_at: dict[str, float] = field(default_factory=dict)
    events: list[ActivityEvent] = field(default_factory=list)
    commands: list[OrchestratorCommand] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    rights: tuple[FolderRight, ...] = (FolderRight.List, FolderRight.Read, FolderRight.Modify, FolderRight.Create,
                                       FolderRight.Execute)
    toolchains: list[dict] = field(default_factory=lambda: [{
        "name": "python", "actions": {"test": {"parameters": {"path": {"type": "Path"}}, "timeoutSeconds": 600}}}])
    # Ergebnis je Testlauf nacheinander: (Exitcode, fehlgeschlagene Tests); danach immer grün.
    test_runs: list[tuple[int, int]] = field(default_factory=lambda: [(1, 1)])
    patch_failures: int = 0  # so viele Patch.Apply-Versuche passen „nicht“ (Hunk) – danach angewendet
    snapshots: int = 1
    _results: dict = field(default_factory=dict)

    # -------------------------------------------------- Orchestrator-Methoden
    def ping(self):
        return {"role": "Orchestrator"}

    def grants(self):
        return [FolderGrant(grant_id="p", root="/projekt", rights=list(self.rights), recursive=True)]

    def toolchains_available(self):
        return {"enabled": bool(self.toolchains), "toolchains": self.toolchains}

    def submit(self, request: ProposalRequest):
        with self.lock:
            pid = f"prop-{len(self.proposals) + 1:04d}"
            self.proposals[pid] = request
            self.submitted_at[pid] = time.monotonic()
        return SimpleNamespace(proposal=SimpleNamespace(proposal_id=pid), proposal_sha256="x")

    def outcome(self, pid: str) -> ProposalOutcome:
        request = self.proposals[pid]
        if time.monotonic() - self.submitted_at[pid] < self.decision_delay:
            return ProposalOutcome(requested_proposal_id=pid, final_proposal_id=pid, state=ProposalState.AwaitingApproval,
                                   is_final=False)
        state = self.decide(request)
        if state is None:
            return ProposalOutcome(requested_proposal_id=pid, final_proposal_id=pid, state=ProposalState.AwaitingApproval,
                                   is_final=False)
        result = self._execute(pid, request) if state == ProposalState.Succeeded else None
        if result is not None and result.status == ResultStatus.Failed and request.tool == "Patch.Apply":
            state = ProposalState.Failed
        return ProposalOutcome(requested_proposal_id=pid, final_proposal_id=pid, state=state, is_final=True, result=result)

    def _special(self, pid: str, request: ProposalRequest) -> ToolResult | None:
        def done(results, status=ResultStatus.Succeeded, exit_code=0):
            return ToolResult(invocation_id="inv", proposal_id=pid, status=status, snapshot_id=request.snapshot_id,
                              target_results=results, output_sha256="x", truncated=False, untrusted=True, exit_code=exit_code)

        with self.lock:
            cached = self._results.get(pid)
            if cached is not None:
                return cached
            if request.tool == "Repo.Index":
                if request.arguments.get("query"):
                    text = "\n".join(f"{p}:1 function x" for p in self.files)
                else:
                    text = ("Repo map of snapshot snap – 3 files, Python 2\nProject/build files: tests/test_calc.py\n\n"
                            "Most important files (ranked by references from other files; symbol:line):\n"
                            + "\n".join(f"{p} [Python, {c.count(chr(10))} lines]\n  function add:1" for p, c in sorted(self.files.items())
                                        if p.endswith(".py")))
                result = done([TargetResult(target_id="map", status=ResultStatus.Succeeded, text=text)])
            elif request.tool == "Patch.Apply":
                if self.patch_failures > 0:
                    self.patch_failures -= 1
                    result = done([TargetResult(target_id="t1", status=ResultStatus.Failed, message="Hunk 1 passt nicht"),
                                   TargetResult(target_id="snapshot", status=ResultStatus.Failed, message="Nichts angewendet")],
                                  ResultStatus.Failed, 1)
                else:
                    self.snapshots += 1
                    result = done([TargetResult(target_id=t.target_id, status=ResultStatus.Succeeded, message="1 Hunk(s)")
                                   for t in request.targets]
                                  + [TargetResult(target_id="snapshot", status=ResultStatus.Succeeded, text=f"snap-{self.snapshots}")])
            elif request.tool.startswith("Toolchain."):
                exit_code, failed = self.test_runs.pop(0) if self.test_runs else (0, 0)
                report = TestReport(format=TestResultFormat.JUnit, exit_code=exit_code, timed_out=False, total=3,
                                    passed=3 - failed, failed=failed, skipped=0,
                                    failures=[{"name": "tests.test_calc.test_empty", "message": "TypeError: unsupported operand"}][:failed])
                log = "FAILED tests/test_calc.py::test_empty - TypeError" if failed else "3 passed"
                result = done([TargetResult(target_id="log", status=ResultStatus.Succeeded, text=log),
                               TargetResult(target_id="tests", status=ResultStatus.Succeeded,
                                            text=json.dumps(report.to_json_obj()), message=f"python test: Exitcode {exit_code}")],
                              exit_code=exit_code)
            else:
                return None
            self._results[pid] = result
            return result

    def _execute(self, pid: str, request: ProposalRequest) -> ToolResult:
        special = self._special(pid, request)
        if special is not None:
            return special
        results = []
        if request.tool == "Workspace.Snapshot":
            results.append(TargetResult(target_id="snapshot", status=ResultStatus.Succeeded, text="snap-1"))
        for t in request.targets:
            if request.tool == "File.List":
                text = "\n".join(f"{p}  ({len(c)} Bytes)" for p, c in sorted(self.files.items()))
            elif request.tool == "File.Search":
                q = request.arguments["query"]
                text = "\n".join(f"{p}:1: {c.splitlines()[0]}" for p, c in self.files.items() if q in c)
            elif t.path in self.files:
                text = self.files[t.path]
            else:
                results.append(TargetResult(target_id=t.target_id, status=ResultStatus.Failed, message="Datei nicht im Snapshot"))
                continue
            results.append(TargetResult(target_id=t.target_id, status=ResultStatus.Succeeded, text=text))
        status = ResultStatus.Succeeded if all(r.status == ResultStatus.Succeeded for r in results) else ResultStatus.Failed
        return ToolResult(invocation_id="inv", proposal_id=pid, status=status, snapshot_id=request.snapshot_id,
                          target_results=results, output_sha256="x", truncated=False, untrusted=True)

    def report_activity(self, report: ActivityReport) -> int:
        with self.lock:
            self.events.extend(report.events)
        return len(report.events)

    def wait_commands(self, after: int, timeout_ms: int):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            with self.lock:
                found = [c for c in self.commands if c.seq > after]
            if found:
                return found
            time.sleep(0.02)
        return []

    # -------------------------------------------------- Testsicht
    def send(self, kind: OrchestratorCommandKind, text: str | None = None, agent: str | None = None) -> None:
        with self.lock:
            self.commands.append(OrchestratorCommand(seq=len(self.commands) + 1, at="2026-10-18T00:00:00Z", kind=kind,
                                                     text=text, agent=agent))

    def chats(self, agent: str | None = None) -> list[str]:
        with self.lock:
            return [e.text or "" for e in self.events if e.kind.value == "Chat" and (agent is None or e.agent == agent)]

    def of(self, agent: str) -> list[ActivityEvent]:
        with self.lock:
            return [e for e in self.events if e.agent == agent]


def wait_for(condition, timeout: float = 20.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("Bedingung nicht rechtzeitig erfüllt")
        time.sleep(interval)
