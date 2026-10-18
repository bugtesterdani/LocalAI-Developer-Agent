"""Ablauf eines Runs: Snapshot → Planner (DAG) → Coder/Tester parallel → Reviewer ↔ Debugger → Zusammenfassung.

Alle fünf Rollen sind ab Runstart sichtbar (AC02). Mehrere Aufgaben laufen gleichzeitig, sofern Rollen und
Schreibmengen es erlauben; GPU-Requests werden über einen fairen Slot vergeben, Toolvorschläge warten
unabhängig davon auf die Nutzerentscheidung. Chatantworten an den Nutzer sind deutsch.

M3-Hinweis: Build/Test-Tools und Patch.Apply folgen in M4. Patches bleiben hier Vorschläge, Tests werden als
nicht ausgeführt gekennzeichnet; der Debugger arbeitet auf Review-Befunden (ab M4 auch auf Testfehlern).
"""

from __future__ import annotations

import concurrent.futures as cf
import pathlib
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from localai.contracts import ActivityKind, AgentState, OrchestratorCommand, OrchestratorCommandKind
from localai.orchestrator import dag, hardware
from localai.orchestrator.activity import ActivityReporter
from localai.orchestrator.agents import Agent, AgentFailed, Assignment
from localai.orchestrator.context import Evidence
from localai.orchestrator.gateway import Cancelled, GatewayError, OllamaGateway
from localai.orchestrator.models import ROLES, CalibrationStore, ConfigError, ModelsConfig, ProfileChoice, choose_profiles
from localai.orchestrator.protocol import Action
from localai.orchestrator.runtime import PRIORITY_REVIEW, PRIORITY_USER, AgentControls, FairGate, LimitReached, RunClock
from localai.orchestrator.tools import ToolBridge

SEVERITY_DE = {"high": "hoch", "medium": "mittel", "low": "niedrig"}
STATE_DE = {dag.TaskState.DONE: "erledigt", dag.TaskState.FAILED: "gescheitert", dag.TaskState.CANCELLED: "abgebrochen",
            dag.TaskState.SKIPPED: "übersprungen", dag.TaskState.PENDING: "offen", dag.TaskState.RUNNING: "läuft"}
MAX_CHAT = 15_000


@dataclass
class Preparation:
    choices: dict[str, ProfileChoice]
    problems: list[str]
    hardware: hardware.Hardware | None = None
    ollama_version: str = ""


@dataclass
class GoalState:
    goal: str
    controls: AgentControls
    clock: RunClock
    notes: list[str] = field(default_factory=list)


class Orchestrator:
    def __init__(self, client, inbox_client, run_id: str, config: ModelsConfig, data_dir: pathlib.Path,
                 gateway: OllamaGateway | None = None, detect=hardware.detect, inbox_after: int = 0,
                 echo: bool = False, reporter: ActivityReporter | None = None) -> None:
        self.client = client
        self.inbox_client = inbox_client
        self.run_id = run_id
        self.config = config
        self.data_dir = data_dir
        self.gateway = gateway or OllamaGateway(config.ollama_url, config.stall_warning_seconds, config.stall_abort_seconds)
        self.detect = detect
        self.inbox_after = inbox_after
        self.reporter = reporter or ActivityReporter(run_id, client.report_activity, echo=echo)
        self.goals: queue.Queue[str | None] = queue.Queue()
        self.current: GoalState | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.previous_summary = ""
        self.bridge: ToolBridge | None = None
        self.gpu = FairGate(config.limits.max_gpu_requests)
        self.listing: list[Evidence] = []

    # ------------------------------------------------------------------ Aufträge der Oberfläche
    def handle(self, command: OrchestratorCommand) -> None:
        with self._lock:
            current = self.current
        if command.kind == OrchestratorCommandKind.Chat:
            text = (command.text or "").strip()
            if not text:
                return
            if current is None:
                self.goals.put(text)
            else:
                current.notes.append(text)
                current.clock.user_interaction()
                self.reporter.chat("Orchestrator", "Hinweis aufgenommen – fließt in die nächsten Schritte der Agenten ein.")
        elif command.kind == OrchestratorCommandKind.CancelAgent and current is not None and command.agent in ROLES:
            current.controls.cancel_agent(command.agent, f"{command.agent} durch Nutzer abgebrochen")
            self.reporter.state(command.agent, AgentState.Cancelled, "Abbruch durch Nutzer")
        elif command.kind == OrchestratorCommandKind.CancelRun and current is not None:
            current.controls.cancel_run("Run durch Nutzer abgebrochen")

    def inbox_loop(self) -> None:
        after = self.inbox_after
        while not self._stop.is_set():
            try:
                commands = self.inbox_client.wait_commands(after, 10_000)
            except Exception as err:  # noqa: BLE001 – Broker weg: Orchestrator beendet sich
                self.reporter.event("Orchestrator", ActivityKind.Error, f"Verbindung zum Broker verloren: {err}")
                self._stop.set()
                self.goals.put(None)
                return
            for command in commands:
                after = max(after, command.seq)
                self.handle(command)

    # ------------------------------------------------------------------ Vorbereitung
    def prepare(self) -> Preparation:
        problems: list[str] = []
        try:
            version = self.gateway.version()
            installed = {m["name"]: m.get("digest", "") for m in self.gateway.list_models()}
        except GatewayError as err:
            return Preparation({}, [f"Ollama ist nicht erreichbar ({err}). Läuft der Dienst auf {self.config.ollama_url}?"])
        hw = self.detect()
        try:
            store = CalibrationStore(self.data_dir / "calibration.json")
        except ConfigError as err:
            return Preparation({}, [str(err)], hw, version)
        choices = choose_profiles(self.config, store, installed, hw.fingerprint())
        for role, choice in choices.items():
            if choice.profile is None:
                problems.append(f"{role}: kein nutzbares Profil – " + "; ".join(choice.problems))
        return Preparation(choices, problems, hw, version)

    # ------------------------------------------------------------------ Hauptschleife
    def serve(self) -> None:
        self.reporter.start()
        threading.Thread(target=self.inbox_loop, name="inbox", daemon=True).start()
        self.reporter.state("Orchestrator", AgentState.Ready, f"Orchestrator bereit (Run {self.run_id})")
        for role in ROLES:
            self.reporter.state(role, AgentState.Ready, "bereit")
        try:
            while not self._stop.is_set():
                goal = self.goals.get()
                if goal is None:
                    break
                self.run_goal(goal)
        finally:
            self.reporter.state("Orchestrator", AgentState.Done, "Orchestrator beendet")
            self.reporter.close()

    def stop(self) -> None:
        self._stop.set()
        self.goals.put(None)

    def run_goal(self, goal: str) -> None:
        state = GoalState(goal, AgentControls(ROLES), RunClock(self.config.limits.max_active_minutes * 60))
        with self._lock:
            self.current = state
        try:
            self._run(state)
        except Cancelled as err:
            self.reporter.chat("Orchestrator", f"Run abgebrochen: {err.reason}")
        except Exception as err:  # noqa: BLE001 – Fehler melden, Orchestrator bleibt bedienbar
            self.reporter.event("Orchestrator", ActivityKind.Error, traceback.format_exc()[-4000:])
            self.reporter.chat("Orchestrator", f"Interner Fehler im Orchestrator: {err}. Details im Aktivitätsfeed.")
        finally:
            with self._lock:
                self.current = None
            for role in ROLES:
                self.reporter.state(role, AgentState.Ready, "bereit")

    def _agents(self, prep: Preparation, state: GoalState, bridge: ToolBridge) -> dict[str, Agent]:
        agents = {}
        for role in ROLES:
            choice = prep.choices[role]
            assert choice.profile is not None
            agents[role] = Agent(role, choice.profile, choice.measurement, self.gateway, self.gpu, bridge, self.reporter,
                                 state.clock, self.config.limits, lambda: list(state.notes))
        return agents

    def _run(self, state: GoalState) -> None:
        reporter = self.reporter
        reporter.state("Orchestrator", AgentState.Generating, "Run startet: Modelle und Hardware prüfen")
        prep = self.prepare()
        if prep.hardware is not None:
            reporter.event("Orchestrator", ActivityKind.Metric, f"Hardware: {prep.hardware.describe()} · Ollama {prep.ollama_version}")
        if prep.problems:
            reporter.chat("Orchestrator", "Der Run kann nicht starten:\n- " + "\n- ".join(prep.problems)
                          + "\n\nKalibrierung: `uv run --project src/python python -m localai.orchestrator.calibrate` "
                          "(Anleitung: docs/pruefung-M3.md).")
            reporter.state("Orchestrator", AgentState.Failed, "Modelle nicht bereit")
            return
        if self.bridge is None:
            self.bridge = ToolBridge(self.client, reporter, self.run_id, state.clock, self.config.limits.max_sandbox_jobs)
        bridge = self.bridge
        bridge.clock = state.clock
        agents = self._agents(prep, state, bridge)
        for role, agent in agents.items():
            reporter.state(role, AgentState.Ready, f"bereit · {prep.choices[role].describe()} · Prompts {agent.prompt_tags}")
        if bridge.grant is None and bridge.find_grant() is None:
            reporter.chat("Orchestrator", "Kein freigegebener Projektordner mit List- und Read-Recht. Bitte in "
                          "config/demo-project.json (bzw. deiner Projektdatei) einen Ordner freigeben.")
            return
        planner_cancel = state.controls.token("Planner")
        if bridge.snapshot_id is None:
            reporter.state("Planner", AgentState.WaitingApproval, "Projektstand übernehmen (Snapshot)", "T0")
            try:
                snapshot = bridge.snapshot("Planner", "T0", planner_cancel)
            except Cancelled:
                state.controls.run.raise_if_cancelled()
                snapshot = None
            if snapshot is None:
                reporter.chat("Orchestrator", "Ohne Snapshot kann ich nicht arbeiten. Schreib mir erneut, wenn du den "
                              "Snapshot freigeben möchtest.")
                return
        if not self.listing:
            try:
                listing = bridge.execute("Planner", "T0", Action("list", "", path=".", depth=4,
                                                                   reason="Dateiliste des Projekts als Startkontext"),
                                         planner_cancel)
                self.listing = [Evidence(e.evidence_id, e.source, e.text, pinned=True) for e in listing.evidence]
            except Cancelled:
                state.controls.run.raise_if_cancelled()
        plan_tasks, acceptance = self._plan(state, agents["Planner"])
        if plan_tasks is None:
            return
        self._execute(state, agents, plan_tasks, acceptance)

    # ------------------------------------------------------------------ Planner
    def _plan(self, state: GoalState, planner: Agent) -> tuple[list[dag.Task] | None, list[str]]:
        text = f"## User goal (German)\n{state.goal}"
        if self.previous_summary:
            text += f"\n\n## Summary of the previous run\n{self.previous_summary}"
        for attempt in (1, 2):
            try:
                result = planner.run(Assignment("P1", "Plan erstellen", text, list(self.listing), PRIORITY_USER),
                                     state.controls.token("Planner"))
            except Cancelled:
                if state.controls.run.cancelled:
                    raise
                self.reporter.chat("Orchestrator", "Planner abgebrochen – ohne Plan geht es nicht weiter.")
                return None, []
            except (LimitReached, AgentFailed) as err:
                self.reporter.chat("Planner", f"Kein Plan: {err}")
                return None, []
            try:
                tasks = dag.build_plan(result.result["tasks"], self.bridge.snapshot_id or "", self.config.limits.max_tasks)
            except dag.PlanError as err:
                self.reporter.event("Planner", ActivityKind.Warning, f"Plan ungültig: {err}")
                text += f"\n\n## Your previous plan was rejected\n{err} Fix the plan."
                continue
            acceptance = result.result.get("acceptance", [])
            lines = [result.result["message_de"], "", "Plan:"]
            for t in tasks:
                deps = f" (nach {', '.join(t.depends_on)})" if t.depends_on else ""
                writes = ", ".join(t.writes) if t.writes else "unbekannt → exklusiv"
                lines.append(f"{t.task_id} · {t.role} · {t.title}{deps} – ändert: {writes}")
            if acceptance:
                lines += ["", "Akzeptanzkriterien:"] + [f"- {a}" for a in acceptance]
            self.reporter.chat("Planner", "\n".join(lines))
            for t in tasks:
                self.reporter.event(t.role, ActivityKind.Task, f"{t.task_id}: {t.title}", task_id=t.task_id,
                                    state=AgentState.Queued)
            return tasks, acceptance
        self.reporter.chat("Planner", "Ich konnte keinen gültigen Plan erstellen (zweimal ungültig). Bitte formuliere das Ziel genauer.")
        return None, []

    # ------------------------------------------------------------------ Coder/Tester-Aufgaben
    def _task_text(self, state: GoalState, task: dag.Task, tasks: list[dag.Task], acceptance: list[str]) -> str:
        lines = [f"## User goal (German)\n{state.goal}", f"## Your task {task.task_id}: {task.title}\n{task.description}",
                 f"Files to read: {', '.join(task.reads) or 'decide yourself'}",
                 f"Files you may change: {', '.join(task.writes) or 'unknown – keep changes minimal and name them'}"]
        if acceptance:
            lines.append("## Acceptance criteria\n" + "\n".join(f"- {a}" for a in acceptance))
        others = [f"- {t.task_id} ({t.role}): {t.title}" for t in tasks if t is not task]
        if others:
            lines.append("## Other tasks in this plan (done by other agents)\n" + "\n".join(others))
        return "\n\n".join(lines)

    def _dependency_evidence(self, task: dag.Task, tasks: list[dag.Task]) -> list[Evidence]:
        by_id = {t.task_id: t for t in tasks}
        evidence = []
        for dep in task.depends_on:
            result = by_id[dep].result or {}
            patch = result.get("patch") or result.get("test_patch")
            if patch:
                evidence.append(Evidence(f"D{dep}", f"proposed patch of task {dep} (not applied)", patch, pinned=True))
        return evidence

    def _run_task(self, state: GoalState, agent: Agent, task: dag.Task, tasks: list[dag.Task],
                  acceptance: list[str]) -> None:
        cancel = state.controls.token(task.role)
        try:
            result = agent.run(Assignment(task.task_id, task.title, self._task_text(state, task, tasks, acceptance),
                                          self.listing + self._dependency_evidence(task, tasks)), cancel)
            task.result, task.steps, task.state = result.result, result.steps, dag.TaskState.DONE
            self._post_result(task)
        except Cancelled as err:
            task.state, task.error = dag.TaskState.CANCELLED, err.reason
            self.reporter.state(task.role, AgentState.Cancelled, f"{task.task_id} abgebrochen: {err.reason}", task.task_id)
        except LimitReached as err:
            task.state, task.error = dag.TaskState.FAILED, str(err)
            self.reporter.state(task.role, AgentState.Failed, str(err), task.task_id)
            self.reporter.chat(task.role, f"{task.task_id}: {err}. Bitte entscheide, wie es weitergeht (z. B. Ziel "
                               "eingrenzen und neu senden).")
        except AgentFailed as err:
            task.state, task.error = dag.TaskState.FAILED, str(err)
            self.reporter.state(task.role, AgentState.Failed, str(err), task.task_id)
            self.reporter.chat(task.role, f"{task.task_id} gescheitert: {err}")

    def _post_result(self, task: dag.Task) -> None:
        r = task.result or {}
        if task.role == "Coder":
            text = f"{task.task_id} · {task.title}\n{r['message_de']}\n\nPatch-Vorschlag (nicht angewendet):\n```diff\n{r['patch']}\n```"
        else:
            cases = "\n".join(f"- {c.get('name', '?')}: {c.get('purpose', '')}" for c in r.get("test_plan", [])
                              if isinstance(c, dict))
            text = (f"{task.task_id} · {task.title}\n{r['message_de']}\n\nTestfälle (NICHT ausgeführt):\n{cases}")
            if r.get("test_patch"):
                text += f"\n\nTest-Patch (nicht angewendet):\n```diff\n{r['test_patch']}\n```"
        self.reporter.chat(task.role, text[:MAX_CHAT])

    def _execute(self, state: GoalState, agents: dict[str, Agent], tasks: list[dag.Task], acceptance: list[str]) -> None:
        locks = dag.WriteLocks()
        busy: dict[str, dag.Task] = {}
        futures: dict[cf.Future, dag.Task] = {}
        with cf.ThreadPoolExecutor(max_workers=len(ROLES), thread_name_prefix="agent") as pool:
            while True:
                state.controls.run.raise_if_cancelled()
                for t in tasks:
                    if t.state == dag.TaskState.PENDING and state.controls.token(t.role).cancelled:
                        t.state, t.error = dag.TaskState.CANCELLED, "Agent abgebrochen"
                for t in dag.skip_blocked(tasks):
                    self.reporter.event(t.role, ActivityKind.Task, f"{t.task_id} übersprungen: {t.error}", task_id=t.task_id)
                for t in dag.ready_tasks(tasks):
                    if t.role in busy:
                        continue
                    if not locks.try_acquire(t):
                        continue
                    t.state = dag.TaskState.RUNNING
                    busy[t.role] = t
                    futures[pool.submit(self._run_task, state, agents[t.role], t, tasks, acceptance)] = t
                if not futures:
                    if all(t.state.final for t in tasks):
                        break
                    # Nichts läuft und nichts ist startbar: kann nur durch Locks bei leerer Belegung nicht passieren.
                    time.sleep(0.1)
                    continue
                done, _ = cf.wait(list(futures), timeout=0.2, return_when=cf.FIRST_COMPLETED)
                for f in done:
                    t = futures.pop(f)
                    f.result()
                    locks.release(t)
                    busy.pop(t.role, None)
        self._review(state, agents, tasks, acceptance)

    # ------------------------------------------------------------------ Review ↔ Debugger
    def _patches(self, tasks: list[dag.Task], fixes: list[dict[str, Any]]) -> list[Evidence]:
        evidence = []
        for t in tasks:
            r = t.result or {}
            if t.state == dag.TaskState.DONE and r.get("patch"):
                evidence.append(Evidence(f"P{t.task_id}", f"patch of {t.task_id} by Coder (not applied)", r["patch"], pinned=True))
            if t.state == dag.TaskState.DONE and r.get("test_patch"):
                evidence.append(Evidence(f"P{t.task_id}", f"test patch of {t.task_id} by Tester (not applied, not executed)",
                                         r["test_patch"], pinned=True))
        for n, fix in enumerate(fixes, start=1):
            evidence.append(Evidence(f"F{n}", f"Debugger fix attempt {n} (replaces affected hunks; not applied)",
                                     fix["fix_patch"], pinned=True))
        return evidence

    def _test_report(self, tasks: list[dag.Task]) -> str:
        lines = []
        for t in tasks:
            if t.role == "Tester" and t.state == dag.TaskState.DONE:
                for c in (t.result or {}).get("test_plan", []):
                    if isinstance(c, dict):
                        lines.append(f"- {c.get('name')}: NOT EXECUTED ({c.get('purpose', '')})")
        return "\n".join(lines) or "- no tests were written"

    def _review(self, state: GoalState, agents: dict[str, Agent], tasks: list[dag.Task], acceptance: list[str]) -> None:
        fixes: list[dict[str, Any]] = []
        verdict = "nicht durchgeführt"
        if not any(t.state == dag.TaskState.DONE and t.role == "Coder" for t in tasks):
            self.reporter.state("Reviewer", AgentState.Ready, "kein Patch zu prüfen")
            self.reporter.state("Debugger", AgentState.Ready, "nicht benötigt")
        else:
            attempt = 0
            while True:
                state.controls.run.raise_if_cancelled()
                text = (f"## User goal (German)\n{state.goal}\n\n## Acceptance criteria\n"
                        + ("\n".join(f"- {a}" for a in acceptance) or "- none given")
                        + f"\n\n## Test report\n{self._test_report(tasks)}"
                        + (f"\n\n## Repair attempt {attempt} of {self.config.limits.max_repair_attempts}: check whether the "
                           "earlier findings are resolved by the Debugger fixes." if attempt else ""))
                try:
                    review = agents["Reviewer"].run(Assignment(f"R{attempt + 1}", "Review", text,
                                                               self._patches(tasks, fixes), PRIORITY_REVIEW),
                                                    state.controls.token("Reviewer"))
                except Cancelled:
                    state.controls.run.raise_if_cancelled()
                    verdict = "abgebrochen"
                    break
                except (LimitReached, AgentFailed) as err:
                    self.reporter.chat("Reviewer", f"Review nicht abgeschlossen: {err}")
                    verdict = "gescheitert"
                    break
                findings = review.result.get("findings", [])
                lines = [f"Review {attempt + 1} (keine Freigabe – die Entscheidung liegt bei dir): {review.result['message_de']}"]
                for f in findings:
                    where = f.get("file") or "?"
                    if f.get("line"):
                        where += f":{f['line']}"
                    lines.append(f"- [{SEVERITY_DE.get(f['severity'], f['severity'])}] {where}: {f['finding']}")
                self.reporter.chat("Reviewer", "\n".join(lines)[:MAX_CHAT])
                if review.result["verdict"] == "ready_for_user_review":
                    verdict = "ohne wesentliche Befunde"
                    break
                if attempt >= self.config.limits.max_repair_attempts:
                    verdict = f"nach {attempt} Reparaturversuchen weiterhin Befunde"
                    self.reporter.chat("Orchestrator", f"Nach {attempt} Reparaturversuchen gibt es weiterhin Befunde. "
                                       "Ich höre hier auf – bitte entscheide, wie es weitergeht.")
                    break
                attempt += 1
                debug_text = (f"## User goal (German)\n{state.goal}\n\n## Review findings to fix (attempt {attempt})\n"
                              + "\n".join(f"- [{f['severity']}] {f.get('file', '?')}:{f.get('line', '?')}: {f['finding']}"
                                          f" Evidence: {f.get('evidence', '')}" for f in findings))
                try:
                    fix = agents["Debugger"].run(Assignment(f"D{attempt}", f"Reparatur {attempt}", debug_text,
                                                            self._patches(tasks, fixes)), state.controls.token("Debugger"))
                except Cancelled:
                    state.controls.run.raise_if_cancelled()
                    verdict = "Reparatur abgebrochen"
                    break
                except (LimitReached, AgentFailed) as err:
                    self.reporter.chat("Debugger", f"Reparatur nicht abgeschlossen: {err}")
                    verdict = "Reparatur gescheitert"
                    break
                fixes.append(fix.result)
                self.reporter.chat("Debugger", (f"Reparatur {attempt}: {fix.result['message_de']}\nHypothese: "
                                                f"{fix.result['hypothesis']}\n\nKorrektur (nicht angewendet):\n```diff\n"
                                                f"{fix.result['fix_patch']}\n```")[:MAX_CHAT])
        self._summary(state, tasks, verdict, len(fixes))

    def _summary(self, state: GoalState, tasks: list[dag.Task], verdict: str, fixes: int) -> None:
        lines = ["Zusammenfassung:"]
        for t in tasks:
            extra = f" – {t.error}" if t.error and t.state != dag.TaskState.DONE else ""
            lines.append(f"- {t.task_id} ({t.role}) {t.title}: {STATE_DE[t.state]}{extra}")
        lines.append(f"- Review: {verdict}" + (f", {fixes} Korrektur(en) vom Debugger" if fixes else ""))
        lines.append("Patches sind Vorschläge und wurden nicht angewendet; Tests wurden nicht ausgeführt "
                     "(Build/Test und Übernahme folgen mit M4).")
        self.reporter.chat("Orchestrator", "\n".join(lines))
        self.previous_summary = "\n".join(lines)[:3000]
        self.reporter.state("Orchestrator", AgentState.Done, "Run abgeschlossen – wartet auf deine nächste Nachricht")
