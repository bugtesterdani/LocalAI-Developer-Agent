"""Ablauf eines Runs (M4–M6):

Snapshot → Repo-Map → Planner (DAG, Prüfläufe) → Coder/Tester parallel, Patches nach Freigabe in der Sandbox
angewendet → Build/Test in der Sandbox → Debugger auf echten Fehlern (höchstens N Reparaturen, jeweils erneut
geprüft) → Reviewer mit echtem Testbericht ↔ Debugger → Zusammenfassung → Übernahme in den Projektordner (M6:
Workspace.Diff, dann Workspace.ExportPatch – zwei Freigaben, Diff je Datei abwählbar).

Alle fünf Rollen sind ab Runstart sichtbar (AC02). Mehrere Aufgaben laufen gleichzeitig, sofern Rollen und
Schreibmengen es erlauben; GPU-Requests werden über einen fairen Slot vergeben, Toolvorschläge warten
unabhängig davon auf die Nutzerentscheidung. Patches werden nacheinander angewendet (jeder auf den Stand des
vorigen). Chatantworten an den Nutzer sind deutsch. Der Projektordner auf dem Host ändert sich nur durch die
freigegebene Übernahme.

Chatbefehle ohne laufenden Run: „übernehmen“ (Übernahme erneut anbieten) und „fortsetzen“ (unterbrochenen Run aus dem
Runjournal mit neuen Freigaben fortsetzen).
"""

from __future__ import annotations

import concurrent.futures as cf
import pathlib
import queue
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from localai.contracts import (
    ActivityKind,
    AgentState,
    FolderRight,
    OrchestratorCommand,
    OrchestratorCommandKind,
)
from localai.orchestrator import dag, hardware
from localai.orchestrator.activity import ActivityReporter
from localai.orchestrator.agents import Agent, AgentFailed, Assignment
from localai.orchestrator.context import Evidence
from localai.orchestrator.gateway import Cancelled, GatewayError, OllamaGateway
from localai.orchestrator.journal import RunJournal
from localai.orchestrator.models import (
    ROLES,
    CalibrationStore,
    ConfigError,
    ModelsConfig,
    ProfileChoice,
    choose_profiles,
)
from localai.orchestrator.protocol import Action
from localai.orchestrator.runtime import (
    PRIORITY_REVIEW,
    PRIORITY_USER,
    AgentControls,
    FairGate,
    LimitReached,
    RunClock,
)
from localai.orchestrator.tools import PatchOutcome, ToolBridge, ToolchainOutcome

SEVERITY_DE = {"high": "hoch", "medium": "mittel", "low": "niedrig"}
STATE_DE = {dag.TaskState.DONE: "erledigt", dag.TaskState.FAILED: "gescheitert", dag.TaskState.CANCELLED: "abgebrochen",
            dag.TaskState.SKIPPED: "übersprungen", dag.TaskState.PENDING: "offen", dag.TaskState.RUNNING: "läuft"}
MAX_CHAT = 15_000
NOT_EXECUTED = re.compile(r"not\s+executed|nicht\s+ausgef(ü|ue)hrt|not\s+been\s+(run|executed)", re.I)
_MAP_PROJECTS = re.compile(r"^Project/build files: (.*?)(?: \(\+\d+ more\))?$", re.M)
_MAP_HEADER = re.compile(r"^Repo map of snapshot \S+ – \d+ files, (.*)$", re.M)
EXPORT_COMMANDS = {"übernehmen", "uebernehmen", "übernahme"}
RESUME_COMMANDS = {"fortsetzen", "weiter machen", "weitermachen"}
DISCARD_WEB_COMMANDS = {"webinhalt verwerfen", "webinhalte verwerfen", "web verwerfen", "webinhalt verdächtig"}
NEW_COMMANDS = {"neu", "neu starten", "neu beginnen"}


def chat_command(text: str) -> str | None:
    word = text.strip().strip("/!.").strip().lower()
    if word in EXPORT_COMMANDS:
        return "export"
    if word in RESUME_COMMANDS:
        return "resume"
    if word in NEW_COMMANDS:
        return "new"
    if word in DISCARD_WEB_COMMANDS:
        return "discard_web"
    return None


@dataclass
class VerifyStep:
    toolchain: str
    action: str
    parameters: dict[str, Any]

    def describe(self) -> str:
        shown = ", ".join(f"{k}={v}" for k, v in self.parameters.items())
        return f"{self.toolchain} {self.action}" + (f" ({shown})" if shown else "")


@dataclass
class Verification:
    ran: bool
    ok: bool
    text_en: str  # Evidenz für Debugger/Reviewer
    summary_de: str  # Chat
    snapshot_id: str | None = None
    procurement: list[str] = field(default_factory=list)  # fehlende Offlinepakete (Runbook: Job stoppen, Nutzer beschafft)


@dataclass
class AppliedPatch:
    label: str
    patch: str
    files: list[str]
    task_id: str = ""


def repo_facts(map_text: str) -> tuple[list[str], dict[str, int]]:
    """Projektdateien und Sprachen aus der Repo-Map (Format aus localai.worker.repoindex.build_map)."""
    projects: list[str] = []
    match = _MAP_PROJECTS.search(map_text)
    if match:
        projects = [p.strip() for p in match.group(1).split(",") if p.strip()]
    languages: dict[str, int] = {}
    header = _MAP_HEADER.search(map_text)
    if header:
        for part in header.group(1).split(","):
            name, _, count = part.strip().rpartition(" ")
            if name and count.isdigit():
                languages[name] = int(count)
    return projects, languages


def _depth(path: str) -> int:
    return path.count("/")


def default_verify(toolchains: list[dict[str, Any]], projects: list[str], languages: dict[str, int]) -> list[VerifyStep]:
    """Standard-Prüflauf je passender Toolchain: test (sonst build); Parameter aus Projektdateien und Auswahlwerten."""
    steps: list[VerifyStep] = []
    for tc in toolchains:
        name = tc.get("name", "")
        actions = tc.get("actions") or {}
        action = "test" if "test" in actions else "build" if "build" in actions else None
        if action is None:
            continue
        guess: str | None = None
        if name == "dotnet":
            solutions = sorted((p for p in projects if p.lower().endswith((".sln", ".slnx"))), key=lambda p: (_depth(p), p))
            csproj = sorted((p for p in projects if p.lower().endswith(".csproj")), key=lambda p: ("test" not in p.lower(), _depth(p), p))
            guess = (solutions or csproj or [None])[0]
        elif name.startswith("python") and languages.get("Python"):
            guess = "."
        elif name.startswith("cpp"):
            cmake = sorted((p for p in projects if p.rsplit("/", 1)[-1] == "CMakeLists.txt"), key=lambda p: (_depth(p), p))
            if cmake:
                guess = cmake[0].rsplit("/", 1)[0] if "/" in cmake[0] else "."
        elif name.startswith("powershell") and languages.get("PowerShell"):
            guess = "."
        elif name == "batch" and languages.get("Batch"):
            guess = "."
        elif name == "hdevelop" and languages.get("HDevelop"):
            guess = "."
        if guess is None:
            continue
        params: dict[str, Any] = {}
        usable = True
        for pname, spec in (actions[action].get("parameters") or {}).items():
            kind = spec.get("type")
            if kind == "Choice" and spec.get("values"):
                params[pname] = spec["values"][0]
            elif kind == "Path":
                params[pname] = guess
            else:
                usable = False
        if usable:
            steps.append(VerifyStep(name, action, params))
    return steps


def check_verify(entries: list[dict[str, Any]], toolchains: list[dict[str, Any]]) -> tuple[list[VerifyStep], list[str]]:
    """Prüfläufe aus dem Plan gegen die verfügbaren Toolchains (Broker und Sandbox prüfen erneut)."""
    by_name = {tc.get("name"): tc for tc in toolchains}
    steps: list[VerifyStep] = []
    problems: list[str] = []
    for entry in entries:
        tc = by_name.get(entry.get("toolchain"))
        action = str(entry.get("action", "")).lower()
        params = entry.get("parameters") or {}
        if tc is None or action not in (tc.get("actions") or {}):
            problems.append(f"{entry.get('toolchain')} {action}: nicht verfügbar")
            continue
        declared = (tc["actions"][action].get("parameters") or {})
        params = dict(params)
        for key, spec in declared.items():
            # Fehlende Auswahlwerte (z. B. configuration) mit dem ersten erlaubten Wert ergänzen statt zu verwerfen.
            if key not in params and spec.get("type") == "Choice" and spec.get("values"):
                params[key] = spec["values"][0]
        if set(params) != set(declared):
            problems.append(f"{tc['name']} {action}: Parameter {sorted(params)} statt {sorted(declared)}")
            continue
        bad = [k for k, spec in declared.items() if spec.get("type") == "Choice" and params[k] not in (spec.get("values") or [])]
        if bad:
            problems.append(f"{tc['name']} {action}: ungültiger Wert für {', '.join(bad)}")
            continue
        steps.append(VerifyStep(tc["name"], action, dict(params)))
    return steps[:4], problems


_LANGUAGE_HINTS = (
    ((".hdev", ".hdvp", ".hdpl"), "HDevelop files are XML: every code line is <l>…</l> (comments <c>…</c>) inside "
     "<procedure><body>; write &amp; &lt; &gt; for & < > inside lines. Tests: local procedures test_* in a *_test.hdev "
     "program; fail with throw ('message')."),
    ((".bat", ".cmd"), "Batch tests are files *.test.cmd (or test_*.cmd): exit /b 0 passes, any other exit code fails; "
     "an optional <name>.expected.txt must match the output."),
    ((".ps1", ".psm1"), "PowerShell tests are Pester 5 files *.Tests.ps1 (Describe/It/Should)."),
    (("cmakelists.txt", ".cpp", ".hpp", ".h", ".cc"), "C++ tests are registered in CMake with enable_testing() and "
     "add_test(); a test executable returning non-zero fails."),
)


_LANGUAGE_SAMPLE = {"HDevelop": "x.hdev", "Batch": "x.cmd", "PowerShell": "x.ps1", "C/C++": "x.cpp"}


def language_hints(paths: list[str]) -> list[str]:
    """Kurze Hinweise für Sprachen, deren Dateiformat oder Testkonvention kleine Modelle oft nicht kennen."""
    lowered = [p.lower() for p in paths]
    return [hint for suffixes, hint in _LANGUAGE_HINTS if any(p.endswith(suffixes) for p in lowered)]


_WANTS_TESTS = re.compile(
    r"\b(mit|inkl\.?|inklusive|samt|plus|und|with|and)\s+(passenden\s+|neuen\s+)?(unit-?)?tests?\b"
    r"|\btests?\s+(schreiben|hinzufügen|ergänzen|anlegen|erstellen)|\btestf[aä]ll|\bunit-?tests?\b"
    r"|\b(add|write)\s+(unit\s+)?tests\b", re.I)


def ensure_tester_task(goal: str, tasks: list[dict[str, Any]], acceptance: list[str]) -> str | None:
    """Verlangt das Ziel Tests, der Plan hat aber keine Tester-Aufgabe (kleine Modelle vergessen sie gern),
    wird eine unabhängige Tester-Aufgabe ergänzt. Liefert ihre ID oder None."""
    if not _WANTS_TESTS.search(goal) or any(t.get("role") == "Tester" for t in tasks) \
            or not any(t.get("role") == "Coder" for t in tasks):
        return None
    ids = {str(t.get("id")) for t in tasks}
    n = len(tasks) + 1
    while f"T{n}" in ids:
        n += 1
    criteria = "; ".join(acceptance) or "the behaviour described in the user goal"
    tasks.append({"id": f"T{n}", "role": "Tester", "title": "Tests für die Änderung",
                  "description": ("Write tests for the requested behaviour in the existing test project or test folder "
                                  f"(acceptance criteria: {criteria})."),
                  "reads": [], "writes": [], "depends_on": []})
    return f"T{n}"


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
        self.gateway = gateway or config.gateway()
        self.detect = detect if detect is not hardware.detect else (lambda: hardware.detect(config.ollama_url))
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
        self.plan_evidence: list[Evidence] = []
        self.listing_snapshot: str | None = None
        self.projects: list[str] = []
        self.languages: dict[str, int] = {}
        self.verify_steps: list[VerifyStep] = []
        self.applied: list[AppliedPatch] = []
        self._applied_lock = threading.Lock()
        self.journal = RunJournal(data_dir)
        self._plan_raw: list[dict[str, Any]] = []
        self._first_goal = True
        self._held_goal: str | None = None  # erstes Ziel, solange ein unterbrochener Run auf „fortsetzen“/„neu“ wartet
        self._discard_requested = False  # „webinhalt verwerfen“ während eines Runs
        self._discarded_urls: list[str] = []
        self._live: tuple[list[dag.Task], list[str]] | None = None  # laufender Plan (für Journal beim Abbruch)

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
            elif chat_command(text) == "discard_web":
                # Runbook „Inhalt verdächtig“: Run anhalten, betroffene Aufgaben und Patches verwerfen.
                self._discard_requested = True
                current.controls.cancel_run("Webinhalt als verdächtig verworfen")
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
        command = chat_command(goal)
        self._live = None
        first, self._first_goal = self._first_goal, False
        if command is None and first and RunJournal.resumable(self.journal.load()):
            # Unterbrochener Run: nicht stillschweigend ersetzen – erst fragen.
            self._held_goal = goal
            self._resume_hint()
            return
        if command == "new":
            if self._held_goal is None:
                self.reporter.chat("Orchestrator", "Schreib mir einfach dein Ziel.")
                return
            goal, command = self._held_goal, None
        if command != "export":
            self._held_goal = None
        state = GoalState(goal, AgentControls(ROLES), RunClock(self.config.limits.max_active_minutes * 60))
        with self._lock:
            self.current = state
        try:
            if command == "export":
                self._export_command(state)
            elif command == "resume":
                self._resume(state)
            elif command == "discard_web":
                self._discard_web_content()
            else:
                self._run(state)
        except Cancelled as err:
            if self._live is not None:
                self._save_journal(state, self._live[0], self._live[1], "cancelled")
            self._journal_status("cancelled")
            if self._discard_requested:
                self._discard_requested = False
                self._discard_web_content()
            else:
                self.reporter.chat("Orchestrator", f"Run abgebrochen: {err.reason}" + self._resume_suffix())
        except Exception as err:  # noqa: BLE001 – Fehler melden, Orchestrator bleibt bedienbar
            self._journal_status("failed")
            self.reporter.event("Orchestrator", ActivityKind.Error, traceback.format_exc()[-4000:])
            self.reporter.chat("Orchestrator", f"Interner Fehler im Orchestrator: {err}. Details im Aktivitätsfeed."
                               + self._resume_suffix())
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
        agents = self._prepare_run(state)
        if agents is None:
            return
        self.applied = []
        bridge = self.bridge
        assert bridge is not None
        if not self.listing or self.listing_snapshot != bridge.snapshot_id:
            self._start_evidence(state, bridge, agents["Planner"])
        self._announce_capabilities(bridge)
        plan_tasks, acceptance = self._plan(state, agents["Planner"])
        if plan_tasks is None:
            return
        self._save_journal(state, plan_tasks, acceptance, "running")
        self._execute(state, agents, plan_tasks, acceptance)

    def _prepare_run(self, state: GoalState, fresh_snapshot: bool = False) -> dict[str, Agent] | None:
        """Modelle, Agenten, Grant und Snapshot; None, wenn der Run nicht starten kann (Grund steht im Chat)."""
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
            return None
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
            return None
        planner_cancel = state.controls.token("Planner")
        if fresh_snapshot:
            bridge.snapshot_id = bridge.base_snapshot_id = None
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
                return None
        return agents

    def _start_evidence(self, state: GoalState, bridge: ToolBridge, planner: Agent) -> None:
        """Repo-Map als angeheftete Startevidenz; ohne Index (abgelehnt/fehlgeschlagen) die Dateiliste."""
        cancel = state.controls.token("Planner")
        max_chars = max(3000, min(30_000, int(planner.budget.evidence * planner.chars_per_token * 0.45)))
        try:
            outcome = bridge.repo_map("Planner", "T0", cancel, max_chars)
            if not outcome.evidence:
                outcome = bridge.execute("Planner", "T0", Action("list", "", path=".", depth=4,
                                                                 reason="Dateiliste des Projekts als Startkontext"), cancel)
            else:
                self.projects, self.languages = repo_facts(outcome.evidence[0].text)
        except Cancelled:
            state.controls.run.raise_if_cancelled()
            return
        self.listing = [Evidence(e.evidence_id, e.source, e.text, pinned=True) for e in outcome.evidence]
        self.listing_snapshot = bridge.snapshot_id

    def _announce_capabilities(self, bridge: ToolBridge) -> None:
        notes = []
        rights = bridge.rights()
        missing = [r.value for r in (FolderRight.Modify, FolderRight.Create) if r not in rights]
        if missing:
            notes.append(f"Patches können nicht angewendet werden: der Projektfreigabe fehlen die Rechte {', '.join(missing)} "
                         "(in der Projektdatei unter rights ergänzen). Die Agenten liefern dann nur Vorschläge.")
        if not bridge.toolchains():
            notes.append("Builds und Tests laufen nur in der Offline-VM (Betriebsart hyperv) mit registrierten Toolchains und "
                         "dem Recht Execute – in dieser Sitzung werden Tests nicht ausgeführt.")
        elif FolderRight.Execute not in rights:
            notes.append("Builds und Tests sind gesperrt: der Projektfreigabe fehlt das Recht Execute.")
        if notes:
            self.reporter.chat("Orchestrator", "\n".join(notes))

    def _toolchain_text(self, bridge: ToolBridge) -> str:
        lines = []
        for tc in bridge.toolchains():
            for action, spec in (tc.get("actions") or {}).items():
                params = []
                for pname, p in (spec.get("parameters") or {}).items():
                    kind = p.get("type")
                    params.append(f"{pname}: one of {p.get('values')}" if kind == "Choice" else f"{pname}: {kind}")
                where = " [runs on Linux]" if tc.get("guest") == "Linux" else ""
                lines.append(f"- {tc['name']} {action}(" + ", ".join(params) + ")" + where)
        return "\n".join(lines) if lines else "- none (builds and tests cannot run in this session)"

    # ------------------------------------------------------------------ Planner
    def _plan(self, state: GoalState, planner: Agent) -> tuple[list[dag.Task] | None, list[str]]:
        text = f"## User goal (German)\n{state.goal}"
        if self.previous_summary:
            text += f"\n\n## Summary of the previous run\n{self.previous_summary}"
        if self.bridge is not None:
            text += f"\n\n## Available toolchains (for verify)\n{self._toolchain_text(self.bridge)}"
        attempts = self.config.limits.max_plan_attempts
        for attempt in range(1, attempts + 1):
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
            added_tester = ensure_tester_task(state.goal, result.result["tasks"], result.result.get("acceptance") or [])
            self._plan_raw = [dict(t) for t in result.result["tasks"] if isinstance(t, dict)]
            try:
                tasks = dag.build_plan(result.result["tasks"], self.bridge.snapshot_id or "", self.config.limits.max_tasks)
            except dag.PlanError as err:
                self.reporter.event("Planner", ActivityKind.Warning, f"Plan ungültig: {err}")
                text += f"\n\n## Your previous plan was rejected\n{err} Fix the plan."
                continue
            acceptance = result.result.get("acceptance", [])
            # Ganze Dateien, die der Planner gelesen hat: bekommen die Aufgaben mit (solange der Stand gilt).
            self.plan_evidence = [e for e in result.evidence if e.path]
            self._choose_verify(result.result.get("verify") or [])
            lines = [result.result["message_de"], "", "Plan:"]
            for t in tasks:
                deps = f" (nach {', '.join(t.depends_on)})" if t.depends_on else ""
                writes = ", ".join(t.writes) if t.writes else "unbekannt → exklusiv"
                lines.append(f"{t.task_id} · {t.role} · {t.title}{deps} – ändert: {writes}")
            if added_tester:
                lines.append(f"(ergänzt: {added_tester} · Tester, weil du Tests möchtest)")
            if acceptance:
                lines += ["", "Akzeptanzkriterien:"] + [f"- {a}" for a in acceptance]
            if self.verify_steps:
                lines += ["", "Prüfung in der Sandbox:"] + [f"- {v.describe()}" for v in self.verify_steps]
            self.reporter.chat("Planner", "\n".join(lines))
            for t in tasks:
                self.reporter.event(t.role, ActivityKind.Task, f"{t.task_id}: {t.title}", task_id=t.task_id,
                                    state=AgentState.Queued)
            return tasks, acceptance
        self.reporter.chat("Planner", f"Ich konnte keinen gültigen Plan erstellen ({attempts}× ungültig). Bitte formuliere das Ziel genauer.")
        return None, []

    def _choose_verify(self, entries: list[dict[str, Any]]) -> None:
        toolchains = self.bridge.toolchains() if self.bridge else []
        steps, problems = check_verify(entries, toolchains)
        if problems:
            self.reporter.event("Planner", ActivityKind.Warning, "Prüfläufe aus dem Plan verworfen: " + "; ".join(problems))
        self.verify_steps = steps or default_verify(toolchains, self.projects, self.languages)

    # ------------------------------------------------------------------ Coder/Tester-Aufgaben
    def _task_text(self, state: GoalState, task: dag.Task, tasks: list[dag.Task], acceptance: list[str]) -> str:
        lines = [f"## User goal (German)\n{state.goal}", f"## Your task {task.task_id}: {task.title}\n{task.description}",
                 f"Files to read: {', '.join(task.reads) or 'decide yourself'}",
                 f"Files you may change: {', '.join(task.writes) or 'unknown – keep changes minimal and name them'}"]
        if acceptance:
            lines.append("## Acceptance criteria\n" + "\n".join(f"- {a}" for a in acceptance))
        paths = [*task.reads, *task.writes] or [_LANGUAGE_SAMPLE[lang] for lang in self.languages if lang in _LANGUAGE_SAMPLE]
        hints = language_hints(paths)
        if hints:
            lines.append("## Language notes\n" + "\n".join(f"- {h}" for h in hints))
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
                applied = isinstance(result.get("apply"), PatchOutcome) and result["apply"].applied
                evidence.append(Evidence(f"D{dep}", f"patch of task {dep} ({'applied – files already contain it' if applied else 'NOT applied'})",
                                         patch, pinned=True))
        return evidence

    def _known_files(self, task: dag.Task) -> list[Evidence]:
        """Vom Planner gelesene Dateien der Aufgabe, wenn sie auf dem aktuellen Stand noch gelten (spart Leserunden)."""
        current = self.bridge.snapshot_id if self.bridge is not None else None
        wanted = set(task.reads) | set(task.writes)
        latest: dict[str, Evidence] = {}
        for e in self.plan_evidence:
            if e.path in wanted and e.snapshot_id == current:
                latest[e.path] = e
        return list(latest.values())

    def _run_task(self, state: GoalState, agent: Agent, task: dag.Task, tasks: list[dag.Task],
                  acceptance: list[str]) -> None:
        cancel = state.controls.token(task.role)
        field_name = "patch" if task.role == "Coder" else "test_patch"
        finisher = self._finisher(task.role, task.task_id, f"{task.task_id}: {task.title}", field_name, cancel)
        try:
            result = agent.run(Assignment(task.task_id, task.title, self._task_text(state, task, tasks, acceptance),
                                          self.listing + self._dependency_evidence(task, tasks) + self._known_files(task),
                                          finisher=finisher, files=list(task.writes)), cancel)
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

    def _finisher(self, role: str, task_id: str, label: str, field_name: str, cancel):  # noqa: ANN001, ANN202
        """Wendet den Patch des Ergebnisses an; passt er nicht, bekommt der Agent den Grund und korrigiert (≤ 3×)."""
        retries = {"n": 0}

        def finish(result: dict[str, Any]) -> str | None:
            patch = (result.get(field_name) or "").strip()
            if not patch or self.bridge is None:
                result["apply"] = None
                return None
            if FolderRight.Modify not in self.bridge.rights() and FolderRight.Create not in self.bridge.rights():
                result["apply"] = PatchOutcome(False, "no-rights", note_de="nicht angewendet (Recht Modify/Create fehlt)")
                return None
            outcome = self.bridge.apply_patch(role, task_id, patch + "\n", label, cancel)
            result["apply"] = outcome
            if outcome.applied:
                with self._applied_lock:
                    self.applied.append(AppliedPatch(label, patch, outcome.files, task_id))
                return None
            max_retries = self.config.limits.max_patch_retries
            if outcome.feedback and retries["n"] < max_retries:
                retries["n"] += 1
                self.reporter.event(role, ActivityKind.Warning, f"{task_id}: Patch nicht angewendet ({outcome.note_de}) – "
                                    f"Agent korrigiert (Versuch {retries['n']}/{max_retries})", task_id=task_id)
                return outcome.feedback
            return None

        return finish

    @staticmethod
    def _apply_text(r: dict[str, Any]) -> str:
        outcome = r.get("apply")
        if not isinstance(outcome, PatchOutcome):
            return "nicht angewendet"
        if outcome.applied:
            text = f"angewendet → Stand {(outcome.snapshot_id or '?')[:12]}"
            if outcome.deselected:
                text += f"; abgewählt (unverändert): {', '.join(outcome.deselected)}"
            return text
        return f"nicht angewendet ({outcome.note_de or outcome.state})"

    def _post_result(self, task: dag.Task) -> None:
        r = task.result or {}
        if task.role == "Coder":
            text = (f"{task.task_id} · {task.title}\n{r['message_de']}\n\nPatch – {self._apply_text(r)}:\n"
                    f"```diff\n{r['patch']}\n```")
        else:
            cases = "\n".join(f"- {c.get('name', '?')}: {c.get('purpose', '')}" for c in r.get("test_plan", [])
                              if isinstance(c, dict))
            text = f"{task.task_id} · {task.title}\n{r['message_de']}\n\nTestfälle:\n{cases}"
            if r.get("test_patch"):
                text += f"\n\nTest-Patch – {self._apply_text(r)}:\n```diff\n{r['test_patch']}\n```"
        self.reporter.chat(task.role, text[:MAX_CHAT])

    def _execute(self, state: GoalState, agents: dict[str, Agent], tasks: list[dag.Task], acceptance: list[str]) -> None:
        self._live = (tasks, acceptance)
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
                    self._save_journal(state, tasks, acceptance, "running")
        self._save_journal(state, tasks, acceptance, "reviewing")
        self._review(state, agents, tasks, acceptance)

    # ------------------------------------------------------------------ Build/Test in der Sandbox
    def _verify(self, state: GoalState, role: str, task_id: str) -> Verification:
        bridge = self.bridge
        assert bridge is not None
        head = bridge.snapshot_id
        if not self.verify_steps:
            reason = ("no toolchain available in this session" if not bridge.toolchains()
                      else "no build/test run could be derived for this repository")
            return Verification(False, False, f"Build/test: NOT EXECUTED ({reason}).",
                                "Tests nicht ausgeführt: " + ("keine Toolchain in dieser Sitzung (nur Offline-VM)."
                                                              if not bridge.toolchains() else "kein passender Prüflauf gefunden."), head)
        if FolderRight.Execute not in bridge.rights():
            return Verification(False, False, "Build/test: NOT EXECUTED (the project grant lacks the Execute right).",
                                "Tests nicht ausgeführt: der Projektfreigabe fehlt das Recht Execute.", head)
        cancel = state.controls.token(role)
        results: list[tuple[VerifyStep, ToolchainOutcome]] = []
        for step in self.verify_steps:
            self.reporter.state(role, AgentState.WaitingApproval, f"Prüflauf {step.describe()}", task_id)
            try:
                outcome = bridge.run_toolchain(role, task_id, step.toolchain, step.action, step.parameters,
                                               f"Prüflauf {step.describe()} auf Stand {(head or '?')[:12]}", cancel)
            except Cancelled:
                state.controls.run.raise_if_cancelled()
                outcome = ToolchainOutcome(False, "cancelled")
            results.append((step, outcome))
            if not outcome.ran:
                break
        ran = any(o.ran for _, o in results)
        ok = bool(results) and all(o.ok for _, o in results)
        procurement: list[str] = []
        en = [f"## Sandbox build/test results (snapshot {(head or '?')[:12]})"]
        de = [f"Prüfung in der Sandbox (Stand {(head or '?')[:12]}):"]
        for step, o in results:
            r = o.report
            if not o.ran or r is None:
                en.append(f"- {step.describe()}: NOT EXECUTED ({o.state}{': ' + o.message if o.message else ''})")
                de.append(f"- {step.describe()}: nicht ausgeführt ({STATE_RUN_DE.get(o.state, o.state)})")
                continue
            status = "TIMEOUT" if r.timed_out else f"exit code {r.exit_code}"
            en.append(f"- {step.describe()}: {status}, tests {r.total}, passed {r.passed}, failed {r.failed}, skipped {r.skipped}")
            line = f"- {step.describe()}: " + ("Zeitüberschreitung" if r.timed_out else
                                               "ok" if o.ok else f"fehlgeschlagen (Exitcode {r.exit_code})")
            if r.total:
                line += f" · {r.total} Tests, {r.passed} bestanden, {r.failed} fehlgeschlagen"
            de.append(line)
            for f in r.failures[:8]:
                en.append(f"  FAILED {f.name}: {(f.message or '').strip()[:500]}")
                if f.stack_trace:
                    en.append("    " + "\n    ".join(f.stack_trace.strip().splitlines()[:6]))
                de.append(f"  - {f.name}: {(f.message or '').strip().splitlines()[0][:200] if f.message else ''}")
            for problem in r.problems or []:
                if problem.startswith("Beschaffungsvorschlag"):
                    procurement.append(problem)
                    de.append(f"  - {problem}")
                    en.append(f"  MISSING OFFLINE PACKAGE (not fixable by code changes; the user must provide it): {problem}")
            if not o.ok:
                en.append("  Log excerpt:\n" + "\n".join("  " + ln for ln in o.log[-6000:].splitlines()))
        return Verification(ran, ok, "\n".join(en), "\n".join(de), head, procurement)

    # ------------------------------------------------------------------ Review ↔ Debugger
    def _change_evidence(self) -> list[Evidence]:
        with self._applied_lock:
            applied = list(self.applied)
        return [Evidence(f"A{n}", f"applied patch {n}: {a.label} (already part of the current files)", a.patch, pinned=True)
                for n, a in enumerate(applied, start=1)]

    def _repair(self, state: GoalState, agents: dict[str, Agent], attempt: int, problem_en: str,
                evidence: list[Evidence]) -> tuple[bool, str]:
        """Ein Debugger-Durchlauf; True, wenn ein Fix angewendet wurde."""
        task_id = f"D{attempt}"
        cancel = state.controls.token("Debugger")
        text = f"## User goal (German)\n{state.goal}\n\n## Problem to fix (repair attempt {attempt})\n{problem_en}"
        finisher = self._finisher("Debugger", task_id, f"Reparatur {attempt}", "fix_patch", cancel)
        try:
            fix = agents["Debugger"].run(Assignment(task_id, f"Reparatur {attempt}", text, evidence, finisher=finisher), cancel)
        except Cancelled:
            state.controls.run.raise_if_cancelled()
            return False, "Reparatur abgebrochen"
        except (LimitReached, AgentFailed) as err:
            self.reporter.chat("Debugger", f"Reparatur nicht abgeschlossen: {err}")
            return False, "Reparatur gescheitert"
        if not fix.result.get("fix_patch", "").strip():
            self.reporter.chat("Debugger", f"Reparatur {attempt}: keine Codeänderung – {fix.result['message_de']}")
            return False, "keine Codekorrektur möglich – bitte selbst prüfen"
        self.reporter.chat("Debugger", (f"Reparatur {attempt}: {fix.result['message_de']}\nHypothese: {fix.result['hypothesis']}"
                                        f"\n\nKorrektur – {self._apply_text(fix.result)}:\n```diff\n{fix.result['fix_patch']}\n```")[:MAX_CHAT])
        outcome = fix.result.get("apply")
        if not (isinstance(outcome, PatchOutcome) and outcome.applied):
            return False, "Korrektur nicht angewendet"
        return True, ""

    def _review(self, state: GoalState, agents: dict[str, Agent], tasks: list[dag.Task], acceptance: list[str]) -> None:
        attempt = 0
        verdict = "nicht durchgeführt"
        verification: Verification | None = None
        max_attempts = self.config.limits.max_repair_attempts
        if not any(t.state == dag.TaskState.DONE and t.role in ("Coder", "Tester") for t in tasks):
            self.reporter.state("Reviewer", AgentState.Ready, "keine Änderung zu prüfen")
            self.reporter.state("Debugger", AgentState.Ready, "nicht benötigt")
            self._summary(state, tasks, verdict, None, 0)
            return
        verification = self._run_verification(state, "Tester", "V1")
        while True:
            state.controls.run.raise_if_cancelled()
            # 1. Echte Build-/Testfehler zuerst reparieren.
            if verification.ran and not verification.ok and verification.procurement:
                verdict = "angehalten: Offlinepakete fehlen"
                self.reporter.chat("Orchestrator", "Build/Tests scheitern an fehlenden Offlinepaketen – das kann kein Agent durch "
                                   "Codeänderungen beheben. Bitte beschaffen und die Toolchain neu registrieren, dann „fortsetzen“ "
                                   "oder das Ziel erneut senden:\n- " + "\n- ".join(verification.procurement))
                break
            if verification.ran and not verification.ok:
                if attempt >= max_attempts:
                    verdict = f"Tests nach {attempt} Reparaturversuchen weiterhin fehlerhaft"
                    self.reporter.chat("Orchestrator", f"Nach {attempt} Reparaturversuchen schlagen Build/Tests weiterhin fehl. "
                                       "Ich höre hier auf – bitte entscheide, wie es weitergeht.")
                    break
                attempt += 1
                fixed, why = self._repair(state, agents, attempt, verification.text_en,
                                          self._change_evidence() + [self._verification_evidence(verification)])
                if not fixed:
                    verdict = why
                    break
                verification = self._run_verification(state, "Debugger", f"V{attempt + 1}")
                continue
            # 2. Review mit echtem Testbericht.
            text = (f"## User goal (German)\n{state.goal}\n\n## Acceptance criteria\n"
                    + ("\n".join(f"- {a}" for a in acceptance) or "- none given")
                    + f"\n\n## Test report\n{verification.text_en if verification.ran else self._planned_tests(tasks)}"
                    + (f"\n\n## Repair attempt {attempt} of {max_attempts}: check whether earlier findings are resolved."
                       if attempt else ""))
            try:
                review = agents["Reviewer"].run(Assignment(f"R{attempt + 1}", "Review", text,
                                                           self._change_evidence() + [self._verification_evidence(verification)],
                                                           PRIORITY_REVIEW), state.controls.token("Reviewer"))
            except Cancelled:
                state.controls.run.raise_if_cancelled()
                verdict = "abgebrochen"
                break
            except (LimitReached, AgentFailed) as err:
                self.reporter.chat("Reviewer", f"Review nicht abgeschlossen: {err}")
                verdict = "gescheitert"
                break
            findings = review.result.get("findings", [])
            if not verification.ran:
                # Ohne Testlauf ist „nicht ausgeführt“ kein Befund für den Debugger.
                findings = [f for f in findings if not NOT_EXECUTED.search(f"{f.get('finding', '')} {f.get('evidence', '')}")]
            if review.result["verdict"] == "changes_needed" and not any(f["severity"] in ("high", "medium") for f in findings):
                review.result["verdict"] = "ready_for_user_review"
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
            if attempt >= max_attempts:
                verdict = f"nach {attempt} Reparaturversuchen weiterhin Befunde"
                self.reporter.chat("Orchestrator", f"Nach {attempt} Reparaturversuchen gibt es weiterhin Befunde. "
                                   "Ich höre hier auf – bitte entscheide, wie es weitergeht.")
                break
            attempt += 1
            problem = "Review findings:\n" + "\n".join(
                f"- [{f['severity']}] {f.get('file', '?')}:{f.get('line', '?')}: {f['finding']} Evidence: {f.get('evidence', '')}"
                for f in findings)
            fixed, why = self._repair(state, agents, attempt, problem,
                                      self._change_evidence() + [self._verification_evidence(verification)])
            if not fixed:
                verdict = why
                break
            verification = self._run_verification(state, "Debugger", f"V{attempt + 1}")
        self._summary(state, tasks, verdict, verification, attempt)

    def _run_verification(self, state: GoalState, role: str, task_id: str) -> Verification:
        bridge = self.bridge
        if bridge is not None and self.verify_steps and bridge.snapshot_id == bridge.base_snapshot_id and not self.applied:
            # Nichts angewendet: ein Testlauf zeigte nur den Ausgangsstand.
            return Verification(False, False, "Build/test: NOT EXECUTED (no change was applied).",
                                "Tests nicht ausgeführt: keine Änderung angewendet.", bridge.snapshot_id)
        verification = self._verify(state, role, task_id)
        self.reporter.chat("Orchestrator", verification.summary_de[:MAX_CHAT])
        return verification

    @staticmethod
    def _verification_evidence(v: Verification) -> Evidence:
        return Evidence("V", "build/test run in the sandbox (tool output, untrusted)", v.text_en, pinned=True)

    @staticmethod
    def _planned_tests(tasks: list[dag.Task]) -> str:
        lines = []
        for t in tasks:
            if t.role == "Tester" and t.state == dag.TaskState.DONE:
                for c in (t.result or {}).get("test_plan", []):
                    if isinstance(c, dict):
                        lines.append(f"- {c.get('name')}: NOT EXECUTED ({c.get('purpose', '')})")
        return "\n".join(lines) or "- no tests were written"

    def _summary(self, state: GoalState, tasks: list[dag.Task], verdict: str, verification: Verification | None,
                 repairs: int) -> None:
        if self._live is not None:
            self._save_journal(state, self._live[0], self._live[1], "reviewing")
        self._live = None
        lines = ["Zusammenfassung:"]
        for t in tasks:
            extra = f" – {t.error}" if t.error and t.state != dag.TaskState.DONE else ""
            lines.append(f"- {t.task_id} ({t.role}) {t.title}: {STATE_DE[t.state]}{extra}")
        tests = ("nicht ausgeführt" if verification is None or not verification.ran
                 else "bestanden" if verification.ok else "fehlgeschlagen")
        lines.append(f"- Build/Tests in der Sandbox: {tests}")
        lines.append(f"- Review: {verdict}" + (f", {repairs} Reparaturversuch(e)" if repairs else ""))
        bridge = self.bridge
        with self._applied_lock:
            applied = list(self.applied)
        if applied and bridge is not None:
            files = sorted({f for a in applied for f in a.files})
            lines.append(f"- {len(applied)} Patch(es) angewendet ({len(files)} Datei(en): {', '.join(files[:12])}"
                         f"{' …' if len(files) > 12 else ''}) → Stand {(bridge.snapshot_id or '?')[:12]} in der Sandbox")
            lines.append("Dein Projektordner ist noch unverändert. Gleich folgt die Übernahme in zwei Freigaben: erst die "
                         "Vorbereitung (nur lesen), dann der Diff je Datei – Dateien kannst du abwählen.")
        else:
            lines.append("Es wurde keine Änderung angewendet; dein Projektordner ist unverändert.")
        self.reporter.chat("Orchestrator", "\n".join(lines))
        self.previous_summary = "\n".join(lines)[:3000]
        blocked = verdict.startswith("angehalten: Offlinepakete")
        self.journal.update(status="blocked" if blocked else "done")
        if applied and bridge is not None and not blocked:
            text = self._export(state)
            self.previous_summary = (self.previous_summary + "\nÜbernahme: " + text)[:3000]
        self.reporter.state("Orchestrator", AgentState.Done, "Run abgeschlossen – wartet auf deine nächste Nachricht")

    # ------------------------------------------------------------------ Übernahme in den Projektordner (M6)
    def _export(self, state: GoalState) -> str:
        """Workspace.Diff (Vorbereitung) → Workspace.ExportPatch (Diff je Datei). Ergebnis als Chat und Rückgabetext."""
        bridge = self.bridge
        assert bridge is not None
        text = self._export_inner(state, bridge)
        self.reporter.chat("Orchestrator", text[:MAX_CHAT])
        return text

    def _export_inner(self, state: GoalState, bridge: ToolBridge) -> str:
        rights = bridge.rights()
        if FolderRight.Modify not in rights and FolderRight.Create not in rights:
            return ("Übernahme nicht möglich: der Projektfreigabe fehlen die Rechte Modify/Create. Die Änderungen bleiben in "
                    "der Sandbox; die Patches stehen oben im Chat.")
        if bridge.snapshot_id is None or bridge.snapshot_id == bridge.base_snapshot_id:
            return "Keine Änderungen in der Sandbox, die übernommen werden könnten."
        cancel = state.controls.token("Reviewer")
        self.reporter.state("Reviewer", AgentState.WaitingApproval, "Übernahme vorbereiten", "X1")
        try:
            preview, why = bridge.prepare_export("Reviewer", "X1", "Geänderte Dateien aus der Sandbox zur Übernahme vorbereiten "
                                                 "(nur lesen; der Diff gegen deinen Projektordner folgt als eigene Freigabe)",
                                                 cancel)
            if preview is None:
                return f"Übernahme nicht vorbereitet ({why}). Mit „übernehmen“ kannst du sie erneut anstoßen."
            if not preview.files:
                bridge.base_snapshot_id = bridge.snapshot_id
                self.journal.update(exported=True)
                return "Keine Unterschiede zu deinem Projektordner – nichts zu übernehmen."
            purpose = (f"{len(preview.files)} Datei(en) in deinen Projektordner übernehmen – Ziel: {state.goal[:200]}")
            outcome = bridge.export("Reviewer", "X2", preview, purpose, cancel)
        except Cancelled:
            state.controls.run.raise_if_cancelled()
            return "Übernahme abgebrochen. Mit „übernehmen“ kannst du sie erneut anstoßen."
        finally:
            self.reporter.state("Reviewer", AgentState.Ready, "bereit")
        if not outcome.applied:
            return (f"Übernahme nicht durchgeführt ({outcome.note_de or outcome.state}); dein Projektordner ist unverändert. "
                    "Mit „übernehmen“ kannst du sie erneut anstoßen.")
        # Neue Basis: weitere Übernahmen zeigen nur noch spätere Änderungen (abgewählte Dateien bleiben in der Sandbox).
        bridge.base_snapshot_id = bridge.snapshot_id
        self.journal.update(exported=True)
        written = [f for f in outcome.files if f not in outcome.deselected]
        text = f"Übernommen: {len(written)} Datei(en) – {', '.join(written[:12])}{' …' if len(written) > 12 else ''}."
        if outcome.deselected:
            text += f" Abgewählt (nicht übernommen, bleiben nur in der Sandbox): {', '.join(outcome.deselected)}."
        if outcome.note_de:
            text += f"\n{outcome.note_de}"
        return text

    def _export_command(self, state: GoalState) -> None:
        bridge = self.bridge
        if bridge is None or bridge.grant is None or bridge.snapshot_id is None:
            data = self.journal.load()
            hint = (" Mit „fortsetzen“ stelle ich den Stand des letzten Runs in der Sandbox wieder her."
                    if RunJournal.resumable(data) else "")
            self.reporter.chat("Orchestrator", "In dieser Sitzung gibt es keine Änderungen in der Sandbox." + hint)
            return
        self._export(state)
        self.reporter.state("Orchestrator", AgentState.Done, "wartet auf deine nächste Nachricht")

    # ------------------------------------------------------------------ Runjournal und Fortsetzen (M6)
    def _save_journal(self, state: GoalState, tasks: list[dag.Task], acceptance: list[str], status: str) -> None:
        with self._applied_lock:
            applied = [{"label": a.label, "patch": a.patch, "files": a.files, "taskId": a.task_id} for a in self.applied]
        self.journal.save({
            "runId": self.run_id, "goal": state.goal, "status": status, "exported": False,
            "plan": self._plan_raw, "acceptance": acceptance,
            "verify": [{"toolchain": v.toolchain, "action": v.action, "parameters": v.parameters} for v in self.verify_steps],
            "tasks": {t.task_id: {"state": t.state.value, "result": t.result if t.state == dag.TaskState.DONE else None,
                                  "error": t.error, "steps": t.steps} for t in tasks},
            "applied": applied,
            "captures": list(self.bridge.released) if self.bridge else [],
            "discardedUrls": list(self._discarded_urls),
        })

    def _discard_web_content(self) -> None:
        """Runbook „Inhalt verdächtig“: Capture-IDs protokollieren, alle Aufgaben, die den Webtext gesehen haben (und ihre
        Abhängigen), sowie jeden Patch ab dem ersten betroffenen verwerfen; „fortsetzen“ baut aus sauberer Evidenz neu auf."""
        data = self.journal.load()
        captures = [c for c in (data or {}).get("captures") or [] if isinstance(c, dict)]
        if data is None or not captures:
            self.reporter.chat("Orchestrator", "Im aktuellen bzw. letzten Run wurde kein Webinhalt übergeben – nichts zu verwerfen.")
            return
        plan = [t for t in data.get("plan") or [] if isinstance(t, dict)]
        tainted = {str(c.get("taskId", "")) for c in captures}
        changed = True
        while changed:  # Abhängige Aufgaben haben Ergebnisse der betroffenen gesehen
            changed = False
            for t in plan:
                if str(t.get("id")) not in tainted and any(str(d) in tainted for d in t.get("depends_on") or []):
                    tainted.add(str(t.get("id")))
                    changed = True
        applied = [a for a in data.get("applied") or [] if isinstance(a, dict)]
        first = next((i for i, a in enumerate(applied) if str(a.get("taskId", "")) in tainted), None)
        kept = applied if first is None else applied[:first]
        dropped = [] if first is None else applied[first:]
        # Spätere Patches bauen auf dem betroffenen Stand auf: deren Aufgaben ebenfalls neu.
        tainted |= {str(a.get("taskId", "")) for a in dropped if a.get("taskId")}
        tasks = {k: v for k, v in (data.get("tasks") or {}).items() if k not in tainted}
        urls = list(dict.fromkeys([*(data.get("discardedUrls") or []), *(str(c.get("url", "")) for c in captures)]))
        self._discarded_urls = urls
        ids = ", ".join(str(c.get("captureId", ""))[:12] for c in captures)
        self.reporter.event("Orchestrator", ActivityKind.Warning, f"Webinhalt verworfen – Capture-IDs: {ids}")
        self.journal.update(status="cancelled", exported=False, applied=kept, tasks=tasks, captures=[], discardedUrls=urls)
        if self.bridge is not None:
            self.bridge.released = []
        redo = sorted(t for t in tainted if t)
        text = (f"Webinhalt verworfen (Capture-IDs {ids}). Betroffen und neu zu bearbeiten: {', '.join(redo) or '–'}; "
                f"{len(dropped)} Patch(es) verworfen, {len(kept)} bleiben. Mit „fortsetzen“ baue ich den Stand ohne diese "
                "Inhalte neu auf (neuer Snapshot, neue Freigaben); die Seiten werden den Agenten als gesperrt mitgeteilt.")
        if data.get("exported"):
            text += (" Achtung: Die Änderungen waren bereits in deinen Projektordner übernommen – bitte dort prüfen (Sicherung "
                     "der Originale unter %LOCALAPPDATA%\\LocalAi\\broker\\export).")
        self.reporter.chat("Orchestrator", text)

    def _journal_status(self, status: str) -> None:
        data = self.journal.load()
        if data is not None and data.get("status") not in ("done", None):
            self.journal.update(status=status)

    def _resume_suffix(self) -> str:
        data = self.journal.load()
        if data is not None and data.get("status") != "done":
            return " Mit „fortsetzen“ kannst du den Run später fortsetzen (mit neuen Freigaben)."
        return ""

    def _resume_hint(self) -> None:
        data = self.journal.load()
        if not RunJournal.resumable(data):
            return
        assert data is not None
        tasks = data.get("tasks") or {}
        done = sum(1 for t in tasks.values() if isinstance(t, dict) and t.get("state") == dag.TaskState.DONE.value)
        patches = len(data.get("applied") or [])
        if data.get("status") == "done":
            text = (f"Der letzte Run („{data['goal'][:120]}“) ist fertig, seine Änderungen ({patches} Patch(es)) wurden aber "
                    "nicht in deinen Projektordner übernommen.")
        else:
            text = (f"Ein früherer Run wurde nicht abgeschlossen („{data['goal'][:120]}“, {done}/{len(tasks)} Aufgaben "
                    f"erledigt, {patches} Patch(es) angewendet).")
        if self._held_goal is not None:
            text += f" Dein neues Ziel („{self._held_goal[:120]}“) habe ich noch nicht begonnen."
        self.reporter.chat("Orchestrator", text + " Schreib „fortsetzen“, um ihn fortzusetzen (Snapshot und Patches werden dir "
                           "erneut zur Freigabe vorgelegt – frühere Freigaben gelten nicht mehr), oder „neu“, um ihn zu "
                           "verwerfen und mit deinem Ziel zu beginnen.")

    def _resume(self, state: GoalState) -> None:
        data = self.journal.load()
        if not RunJournal.resumable(data):
            self.reporter.chat("Orchestrator", "Es gibt keinen unterbrochenen Run zum Fortsetzen. Schreib mir einfach ein neues Ziel.")
            return
        assert data is not None
        state.goal = data["goal"]
        self._discarded_urls = [str(u) for u in data.get("discardedUrls") or []]
        if self._discarded_urls:
            state.notes.append("Do not fetch these web pages again; the user rejected their content as suspicious: "
                               + ", ".join(self._discarded_urls[:10]))
        if self.bridge is not None:
            self.bridge.released = []
        self.reporter.chat("Orchestrator", f"Ich setze den Run „{state.goal[:200]}“ fort. Zuerst ein neuer Snapshot deines "
                           "Projektordners, dann lege ich die bereits angewendeten Patches erneut zur Freigabe vor.")
        agents = self._prepare_run(state, fresh_snapshot=True)
        if agents is None:
            return
        bridge = self.bridge
        assert bridge is not None
        self.applied = []
        records = [a for a in data.get("applied") or [] if isinstance(a, dict) and a.get("patch")]
        for n, record in enumerate(records, start=1):
            label = f"Wiederherstellen {n}/{len(records)}: {record.get('label', '')}"[:200]
            outcome = bridge.apply_patch("Coder", f"W{n}", str(record["patch"]).rstrip("\n") + "\n", label,
                                         state.controls.token("Coder"))
            if not outcome.applied:
                self.reporter.chat("Orchestrator", f"{label}: nicht angewendet ({outcome.note_de or outcome.state}). Ich breche "
                                   "das Fortsetzen hier ab; das Journal bleibt erhalten. Schreib „fortsetzen“ für einen neuen "
                                   "Versuch oder ein neues Ziel.")
                return
            if outcome.deselected:
                self.reporter.chat("Orchestrator", f"{label}: abgewählt – {', '.join(outcome.deselected)} fehlen im "
                                   "wiederhergestellten Stand.")
            with self._applied_lock:
                self.applied.append(AppliedPatch(str(record.get("label", "")), str(record["patch"]),
                                                 [f for f in outcome.files], str(record.get("taskId", ""))))
        self.listing = []
        self._start_evidence(state, bridge, agents["Planner"])
        if data.get("status") == "done":
            self.journal.update(applied=[{"label": a.label, "patch": a.patch, "files": a.files, "taskId": a.task_id}
                                         for a in self.applied])
            self.previous_summary = f"Resumed run: {state.goal[:500]}"
            self._export(state)
            self.reporter.state("Orchestrator", AgentState.Done, "Run abgeschlossen – wartet auf deine nächste Nachricht")
            return
        self._plan_raw = [t for t in data.get("plan") or [] if isinstance(t, dict)]
        try:
            tasks = dag.build_plan(self._plan_raw, bridge.snapshot_id or "", max(len(self._plan_raw), 1))
        except (dag.PlanError, KeyError, TypeError, ValueError) as err:
            self.reporter.chat("Orchestrator", f"Der gespeicherte Plan ist nicht lesbar ({err}). Bitte das Ziel neu senden.")
            return
        saved = data.get("tasks") or {}
        for t in tasks:
            entry = saved.get(t.task_id) if isinstance(saved, dict) else None
            if isinstance(entry, dict) and entry.get("state") == dag.TaskState.DONE.value and isinstance(entry.get("result"), dict):
                t.state, t.result, t.steps = dag.TaskState.DONE, entry["result"], int(entry.get("steps") or 0)
        acceptance = [str(a) for a in data.get("acceptance") or []]
        steps, problems = check_verify(data.get("verify") or [], bridge.toolchains())
        self.verify_steps = steps or default_verify(bridge.toolchains(), self.projects, self.languages)
        if problems:
            self.reporter.event("Orchestrator", ActivityKind.Warning, "Prüfläufe aus dem Journal verworfen: " + "; ".join(problems))
        open_tasks = [t for t in tasks if t.state != dag.TaskState.DONE]
        self.reporter.chat("Orchestrator", f"Stand wiederhergestellt ({len(self.applied)} Patch(es)). Erledigt: "
                           f"{', '.join(t.task_id for t in tasks if t.state == dag.TaskState.DONE) or 'keine'}; "
                           f"jetzt: {', '.join(t.task_id for t in open_tasks) or 'Prüfung und Review'}.")
        for t in open_tasks:
            self.reporter.event(t.role, ActivityKind.Task, f"{t.task_id}: {t.title}", task_id=t.task_id, state=AgentState.Queued)
        self._save_journal(state, tasks, acceptance, "running")
        self._execute(state, agents, tasks, acceptance)


STATE_RUN_DE = {"denied": "abgelehnt", "expired": "abgelaufen", "rejected": "vom Broker abgewiesen", "cancelled": "abgebrochen",
                "failed": "fehlgeschlagen", "invalidated": "ungültig", "outcomeunknown": "Ergebnis unklar"}
