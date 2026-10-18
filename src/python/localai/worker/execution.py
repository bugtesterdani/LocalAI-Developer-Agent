"""Toolchain-Aktionen in der Sandbox (Toolchain.Restore/Build/Test/Run/Lint, B08).

Sicherheitsmodell (Spezifikation §8, SEC-13/-20/-21):

* Es gibt kein freies Kommando. Ausgeführt wird nur eine im ``toolchain.json`` deklarierte Aktion mit festem
  Binary; Parameter sind typisiert (Path/Choice/Text/Integer) und werden hier – nach dem Broker – erneut geprüft.
* Jeder Job bekommt ein frisches Arbeitsverzeichnis mit einer Kopie des Snapshots; der Snapshot selbst bleibt
  unverändert. Nach dem Job wird das Verzeichnis gelöscht.
* Unter Windows läuft der Prozess mit einem eingeschränkten Token (alle Privilegien entfernt) auf niedriger
  Integritätsstufe in einem Job Object: er kann Worker, Toolchains und Systemdateien nicht verändern (keine
  Schreibrechte „nach oben“), und am Ende werden alle Prozesse des Baums beendet.
* Umgebung minimal und fest; Zeitgrenze aus dem Manifest; Ausgabe begrenzt.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from localai.contracts import TestCaseFailure, TestReport, TestResultFormat, ToolchainAction, ToolchainManifest
from localai.pathrules import ROOT, PathRuleError, is_within, normalize_relative
from localai.worker.snapshot import SnapshotIndex

ACTIONS = {"Toolchain.Restore": ["restore"], "Toolchain.Build": ["build"], "Toolchain.Test": ["test"],
           "Toolchain.Run": ["run"], "Toolchain.Lint": ["lint", "format"]}
GUEST_ROOT = "C:\\Toolchains\\"
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 3600
MAX_LOG_BYTES = 10 * 1024 * 1024
MAX_RESULT_FILE = 20 * 1024 * 1024
MAX_FAILURES = 50
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_TEXT = re.compile(r"^[A-Za-z0-9 ._:=~,/\\+#@()!&|*-]{1,200}$")
_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]*)\}")
# Vom Worker fest gesetzt, nie aus dem Manifest: keine Hintergrunddienste, die den Job überleben.
WORKER_ENV = {
    "MSBUILDDISABLENODEREUSE": "1",
    "DOTNET_CLI_USE_MSBUILD_SERVER": "0",
    "UseSharedCompilation": "false",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}
BLOCKED_ENV = {"PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "PSMODULEPATH"}


class ExecError(Exception):
    """Aktion abgelehnt oder nicht ausführbar (Meldung geht an Agent und Nutzer)."""


@dataclass
class Toolchain:
    manifest: ToolchainManifest
    directory: pathlib.Path  # <toolchains>/<name>: toolchain.json, packages/
    install_dir: pathlib.Path  # C:\Toolchains\<name> (Tests: anderer Ort)

    @property
    def packages(self) -> pathlib.Path:
        return self.directory / "packages"


class ToolchainCatalog:
    """Manifeste der beim Aufbau installierten Toolchains (``C:\\LocalAi\\toolchains\\<name>\\toolchain.json``)."""

    def __init__(self, root: pathlib.Path, install_root: pathlib.Path | None = None) -> None:
        self.root = root
        self.install_root = install_root or pathlib.Path(GUEST_ROOT)

    def get(self, name: Any) -> Toolchain:
        if not isinstance(name, str) or not _NAME.match(name):
            raise ExecError("toolchain: ungültiger Name")
        path = self.root / name / "toolchain.json"
        if not path.is_file():
            raise ExecError(f"Toolchain '{name}' ist in dieser Sandbox nicht installiert")
        manifest = ToolchainManifest.model_validate_json(path.read_text(encoding="utf-8-sig"))
        if manifest.name != name or manifest.guest_path != GUEST_ROOT + name:
            raise ExecError(f"Toolchain '{name}': Manifest passt nicht zur Installation")
        return Toolchain(manifest, self.root / name, self.install_root / name)


def resolve_action(toolchain: Toolchain, tool: str) -> tuple[str, ToolchainAction]:
    for name in ACTIONS.get(tool, []):
        action = toolchain.manifest.actions.get(name)
        if action is not None:
            return name, action
    raise ExecError(f"Toolchain '{toolchain.manifest.name}' hat keine Aktion für {tool} "
                    f"(vorhanden: {', '.join(sorted(toolchain.manifest.actions)) or 'keine'})")


def validate_parameters(action: ToolchainAction, values: Any, index: SnapshotIndex) -> dict[str, tuple[str, str]]:
    """Prüft die Parameter gegen die Deklaration. Ergebnis: Name → (Typ, Wert). Wirft ExecError (SEC-20)."""
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise ExecError("parameters muss ein Objekt sein")
    declared = action.parameters or {}
    unknown = sorted(set(values) - set(declared))
    if unknown:
        raise ExecError(f"Nicht deklarierte Parameter: {', '.join(unknown)}")
    missing = sorted(set(declared) - set(values))
    if missing:
        raise ExecError(f"Parameter fehlen: {', '.join(missing)}")
    result: dict[str, tuple[str, str]] = {}
    for name, spec in declared.items():
        value = values[name]
        kind = spec.type.value
        if kind == "Integer":
            if not isinstance(value, int) or isinstance(value, bool) or not -(2**31) <= value < 2**31:
                raise ExecError(f"{name}: ganze Zahl erwartet")
            text = str(value)
        elif not isinstance(value, str):
            raise ExecError(f"{name}: Text erwartet")
        elif kind == "Choice":
            if value not in (spec.values or []):
                raise ExecError(f"{name}: erlaubt sind {', '.join(spec.values or [])}")
            text = value
        elif kind == "Path":
            try:
                text = normalize_relative(value.replace("\\", "/"), allow_root=True)
            except PathRuleError as err:
                raise ExecError(f"{name}: unzulässiger Pfad: {err}") from err
            if text.startswith("-"):
                raise ExecError(f"{name}: Pfad darf nicht mit '-' beginnen")
            if text != ROOT and text not in index.files and not any(is_within(p, text) for p in index.files):
                raise ExecError(f"{name}: '{text}' gibt es im Snapshot nicht")
        else:  # Text
            if spec.pattern:
                if not re.fullmatch(spec.pattern, value):
                    raise ExecError(f"{name}: entspricht nicht dem Muster {spec.pattern}")
            elif not _TEXT.match(value):
                raise ExecError(f"{name}: unzulässige Zeichen (erlaubt: Buchstaben, Ziffern und ._:=~,/\\+#@()!&|*- )")
            if value.startswith("-"):
                raise ExecError(f"{name}: Wert darf nicht mit '-' beginnen (keine zusätzlichen Optionen)")
            text = value
        if kind != "Path" and pattern_violation(spec.pattern, text):
            raise ExecError(f"{name}: entspricht nicht dem Muster {spec.pattern}")
        result[name] = (kind, text)
    return result


def pattern_violation(pattern: str | None, value: str) -> bool:
    return bool(pattern) and re.fullmatch(pattern, value) is None  # type: ignore[arg-type]


@dataclass
class JobDirs:
    root: pathlib.Path
    repo: pathlib.Path
    results: pathlib.Path
    home: pathlib.Path

    @classmethod
    def create(cls, base: pathlib.Path, job_id: str) -> JobDirs:
        root = base / job_id
        if root.exists():
            remove_tree(root)
        root.mkdir(parents=True)
        dirs = cls(root, root / "repo", root / "results", root / "home")
        return dirs


def expand(action: ToolchainAction, params: dict[str, tuple[str, str]], fixed: dict[str, str],
           repo: pathlib.Path) -> list[str]:
    args: list[str] = []
    for raw in action.arguments:
        whole = _PLACEHOLDER.fullmatch(raw)
        if whole and whole.group(1) in params:
            kind, value = params[whole.group(1)]
            if kind == "Path":
                value = str(repo if value == ROOT else repo.joinpath(*value.split("/")))
            args.append(value)
            continue

        def sub(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in fixed:
                raise ExecError(f"Platzhalter {{{key}}} ist in Aktionen nicht erlaubt")
            return fixed[key]

        args.append(_PLACEHOLDER.sub(sub, raw))
    return args


def environment(toolchain: Toolchain, dirs: JobDirs, fixed: dict[str, str]) -> dict[str, str]:
    home = dirs.home
    roaming, local = home / "AppData" / "Roaming", home / "AppData" / "Local"
    temp = local / "Temp"
    for d in (roaming, local, temp):
        d.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = {}
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        env.update({
            "SystemRoot": system_root, "windir": system_root, "SystemDrive": system_root[:2],
            "ComSpec": os.path.join(system_root, "System32", "cmd.exe"),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "PATH": ";".join([str(toolchain.install_dir), os.path.join(system_root, "System32"), system_root]),
            "NUMBER_OF_PROCESSORS": os.environ.get("NUMBER_OF_PROCESSORS", "2"),
            "PROCESSOR_ARCHITECTURE": os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64"),
            "ProgramData": str(home / "ProgramData"), "ALLUSERSPROFILE": str(home / "ProgramData"),
        })
        (home / "ProgramData").mkdir(exist_ok=True)
    else:
        env["PATH"] = os.pathsep.join([str(toolchain.install_dir), "/usr/bin", "/bin"])
    env.update({
        "USERPROFILE": str(home), "HOME": str(home), "APPDATA": str(roaming), "LOCALAPPDATA": str(local),
        "TEMP": str(temp), "TMP": str(temp), "DOTNET_CLI_HOME": str(home),
    })
    for key, value in (toolchain.manifest.environment or {}).items():
        if key.upper() in BLOCKED_ENV:
            continue
        env[key] = _PLACEHOLDER.sub(lambda m: fixed.get(m.group(1), m.group(0)), value)
    env.update(WORKER_ENV)
    return env


@dataclass
class ProcessOutcome:
    exit_code: int
    timed_out: bool
    seconds: float
    log: bytes
    log_truncated: bool


def run_process(exe: pathlib.Path, args: list[str], cwd: pathlib.Path, env: dict[str, str], timeout: int,
                log_path: pathlib.Path, low_integrity: bool, memory_mb: int = 0) -> ProcessOutcome:
    started = time.monotonic()
    if os.name == "nt":
        from localai.worker import winproc
        exit_code, timed_out = winproc.run(str(exe), args, str(cwd), env, timeout, str(log_path), low_integrity, memory_mb)
    else:
        exit_code, timed_out = _run_posix(str(exe), args, cwd, env, timeout, log_path)
    size = log_path.stat().st_size if log_path.exists() else 0
    with open(log_path, "rb") if log_path.exists() else _Empty() as f:
        if size > MAX_LOG_BYTES:
            head = f.read(MAX_LOG_BYTES // 4)
            f.seek(size - MAX_LOG_BYTES * 3 // 4)
            data = head + "\n[... Ausgabe gekürzt ...]\n".encode() + f.read()
        else:
            data = f.read()
    return ProcessOutcome(exit_code, timed_out, time.monotonic() - started, data, size > MAX_LOG_BYTES)


class _Empty:
    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, *_: object) -> bytes:
        return b""

    def seek(self, *_: object) -> None:
        return None


def _run_posix(exe: str, args: list[str], cwd: pathlib.Path, env: dict[str, str], timeout: int,
               log_path: pathlib.Path) -> tuple[int, bool]:
    import signal

    with open(log_path, "wb") as log:
        process = subprocess.Popen([exe, *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()
    return (process.returncode if not timed_out else -1), timed_out


# ------------------------------------------------------------------ Testergebnisse

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str:
    for el in element.iter():
        if _local(el.tag) == name and el.text:
            return el.text.strip()
    return ""


@dataclass
class _Counts:
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    failures: list[TestCaseFailure] = field(default_factory=list)
    result_files: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def parse_results(fmt: TestResultFormat, results: pathlib.Path, exit_code: int, timed_out: bool) -> TestReport:
    report = _Counts()
    if fmt != TestResultFormat.ExitCode:
        _collect(fmt, results, report)
    return TestReport(format=fmt, exit_code=exit_code, timed_out=timed_out, total=report.total, passed=report.passed,
                      failed=report.failed, skipped=report.skipped, failures=report.failures,
                      result_files=report.result_files, problems=report.problems)


def _collect(fmt: TestResultFormat, results: pathlib.Path, report: _Counts) -> None:
    pattern = {"Trx": "*.trx", "JUnit": "*.xml", "NUnit": "*.xml"}[fmt.value]
    files = sorted(p for p in results.rglob(pattern) if p.is_file())
    for path in files:
        rel = path.relative_to(results).as_posix()
        report.result_files.append(rel)
        if path.stat().st_size > MAX_RESULT_FILE:
            report.problems.append(f"{rel}: zu groß, nicht ausgewertet")
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as err:
            report.problems.append(f"{rel}: kein gültiges XML ({err})")
            continue
        if fmt == TestResultFormat.Trx:
            _parse_trx(root, report)
        elif fmt == TestResultFormat.NUnit:
            _parse_nunit(root, report)
        else:
            _parse_junit(root, report)
    if not files:
        report.problems.append("keine Ergebnisdatei gefunden (Build fehlgeschlagen oder keine Tests?)")


def _add_failure(report: _Counts, name: str, message: str, trace: str) -> None:
    report.failed += 1
    if len(report.failures) < MAX_FAILURES:
        report.failures.append(TestCaseFailure(name=name[:300], message=message[:2000] or None,
                                               stack_trace=trace[:3000] or None))


def _parse_trx(root: ET.Element, report: _Counts) -> None:
    for el in root.iter():
        if _local(el.tag) != "UnitTestResult":
            continue
        report.total += 1
        outcome = el.get("outcome", "")
        name = el.get("testName", "?")
        if outcome == "Passed":
            report.passed += 1
        elif outcome in ("Failed", "Error", "Timeout", "Aborted"):
            _add_failure(report, name, _child_text(el, "Message"), _child_text(el, "StackTrace"))
        else:
            report.skipped += 1


def _parse_junit(root: ET.Element, report: _Counts) -> None:
    for case in root.iter("testcase"):
        report.total += 1
        name = ".".join(x for x in (case.get("classname"), case.get("name")) if x) or "?"
        problem = next((c for c in case if c.tag in ("failure", "error")), None)
        if problem is not None:
            _add_failure(report, name, problem.get("message", ""), (problem.text or "").strip())
        elif any(c.tag == "skipped" for c in case):
            report.skipped += 1
        else:
            report.passed += 1


def _parse_nunit(root: ET.Element, report: _Counts) -> None:
    for case in root.iter("test-case"):
        report.total += 1
        result = case.get("result", "")
        name = case.get("fullname") or case.get("name") or "?"
        if result == "Passed":
            report.passed += 1
        elif result == "Failed":
            _add_failure(report, name, _child_text(case, "message"), _child_text(case, "stack-trace"))
        else:
            report.skipped += 1


# ------------------------------------------------------------------ Logauszug für Agenten

_ERRORISH = re.compile(r"error|fail|exception|traceback|assert|warning CS|fehler", re.I)


def log_excerpt(data: bytes, limit: int = 24_000) -> str:
    """Fehlerzeilen zuerst, dann das Ende der Ausgabe – innerhalb von ``limit`` Zeichen."""
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    if len(text) <= limit:
        return "\n".join(lines).strip()
    errors: list[str] = []
    seen: set[str] = set()
    for ln in lines:
        if _ERRORISH.search(ln) and ln not in seen:
            seen.add(ln)
            errors.append(ln[:400])
    head = "\n".join(errors)[: limit // 2]
    tail_budget = limit - len(head) - 80
    tail: list[str] = []
    used = 0
    for ln in reversed(lines):
        if used + len(ln) + 1 > tail_budget:
            break
        tail.append(ln)
        used += len(ln) + 1
    return ("[Fehlerzeilen]\n" + head + "\n\n[Ende der Ausgabe]\n" + "\n".join(reversed(tail))).strip()


# ------------------------------------------------------------------ Dateisystem

def remove_tree(path: pathlib.Path) -> None:
    def onexc(func, p, _exc):  # noqa: ANN001
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onexc=onexc)


def copy_snapshot(index: SnapshotIndex, target: pathlib.Path) -> None:
    """Kopie mit normalen (schreibbaren) Dateien – der Prozess darf seine Arbeitskopie verändern."""
    target.mkdir(parents=True, exist_ok=True)
    for rel in index.files:
        dest = target.joinpath(*rel.split("/"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(index.resolve(rel), dest)


def label_low_integrity(directory: pathlib.Path) -> None:
    """Ordner (und alles, was darin entsteht) für Prozesse niedriger Integrität beschreibbar machen."""
    icacls = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"
    run = subprocess.run([str(icacls), str(directory), "/setintegritylevel", "(OI)(CI)low"], capture_output=True,
                         timeout=60, check=False)
    if run.returncode != 0:
        raise ExecError(f"Integritätsstufe für den Arbeitsordner nicht setzbar (icacls {run.returncode})")


@dataclass
class ExecResult:
    report: TestReport
    log: str
    message: str
    seconds: float
    command: list[str] = field(default_factory=list)


class Executor:
    def __init__(self, catalog: ToolchainCatalog, jobs_root: pathlib.Path, low_integrity: bool = True) -> None:
        self.catalog = catalog
        self.jobs_root = jobs_root
        self.low_integrity = low_integrity and os.name == "nt"

    def run(self, tool: str, job_id: str, arguments: dict[str, Any], index: SnapshotIndex) -> ExecResult:
        toolchain = self.catalog.get(arguments.get("toolchain"))
        action_name, action = resolve_action(toolchain, tool)
        params = validate_parameters(action, arguments.get("parameters"), index)
        binary = next((b for b in toolchain.manifest.binaries if b.name == action.binary), None)
        if binary is None:
            raise ExecError(f"Binary {action.binary} nicht deklariert")
        exe = toolchain.install_dir.joinpath(*binary.path.replace("\\", "/").split("/"))
        if not exe.is_file():
            raise ExecError(f"Programm fehlt in der Sandbox: {exe}")
        if binary.sha256:
            import hashlib
            if hashlib.sha256(exe.read_bytes()).hexdigest() != binary.sha256:
                raise ExecError(f"Hash von {exe.name} weicht vom Manifest ab")
        timeout = min(action.timeout_seconds or DEFAULT_TIMEOUT, MAX_TIMEOUT)

        dirs = JobDirs.create(self.jobs_root, job_id)
        try:
            if self.low_integrity:
                label_low_integrity(dirs.root)
            for d in (dirs.repo, dirs.results, dirs.home):
                d.mkdir(parents=True, exist_ok=True)
            copy_snapshot(index, dirs.repo)
            packages = toolchain.packages
            fixed = {"guest": str(toolchain.install_dir), "work": str(dirs.root), "results": str(dirs.results),
                     "packages": str(packages if packages.is_dir() else dirs.root / "no-packages")}
            (dirs.root / "no-packages").mkdir(exist_ok=True)
            args = expand(action, params, fixed, dirs.repo)
            env = environment(toolchain, dirs, fixed)
            outcome = run_process(exe, args, dirs.repo, env, timeout, dirs.root / "output.log", self.low_integrity)
            report = parse_results(toolchain.manifest.test_result_format if action_name == "test" else TestResultFormat.ExitCode,
                                   dirs.results, outcome.exit_code, outcome.timed_out)
        finally:
            remove_tree(dirs.root)
        state = "Zeitüberschreitung" if outcome.timed_out else f"Exitcode {outcome.exit_code}"
        message = f"{toolchain.manifest.name} {action_name}: {state} nach {outcome.seconds:.0f} s"
        if action_name == "test" and report.total:
            message += f"; Tests: {report.total}, bestanden {report.passed}, fehlgeschlagen {report.failed}, übersprungen {report.skipped}"
        if outcome.log_truncated:
            message += "; Ausgabe gekürzt"
        shown = [exe.name, *[a.replace(str(dirs.root), "{work}") for a in args]]
        return ExecResult(report, log_excerpt(outcome.log), message, outcome.seconds, shown)


if __name__ == "__main__":  # pragma: no cover
    print("Nur als Modul nutzbar", file=sys.stderr)
