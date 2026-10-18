"""M4-Abnahme mit echter Hyper-V-VM (nur auf dem Windows-PC; VM-Aufbau 15–60 Minuten, danach ca. 10 Minuten).

Voraussetzungen (docs/pruefung-M4.md): dotnet- und python-Toolchain mit Offline-Paketen registriert, Windows-ISO
wie in M2b. Start (PowerShell, im Repository)::

    $env:LOCALAI_M4_ABNAHME = "1"
    uv run --project src/python pytest -s tests/integration/test_m4_abnahme.py

Geprüft wird ohne Modell, direkt über den Broker (jede Aktion ein genehmigter Vorschlag):

1. C# Ende zu Ende: Snapshot → Patch.Apply (neue Methode + Test) → Toolchain.Test (dotnet test, Trx) → Patch mit
   absichtlich falschem Test → Testbericht mit genau diesem Fehler.
2. Python Ende zu Ende: dasselbe mit pytest (JUnit).
3. SEC-20: nicht deklarierte Aktion, freies Kommando, eingeschmuggelte Option → abgewiesen, nichts ausgeführt.
4. SEC-13/-21/-07: Build eines bösartigen Projekts (samples/m4-sec13) – Schreiben in Worker/Toolchain/Windows/HKLM,
   Netz und SSH-Schlüssel scheitern, nur die eigene Arbeitskopie ist beschreibbar; danach arbeitet der Worker normal.
5. Großes Repository (> 2.000 Dateien): Snapshot und Repo-Map im Zeichenbudget, zentrale Datei oben.

Ergebnis mit Zeiten: ``%LOCALAPPDATA%\\LocalAi\\abnahme\\m4-abnahme.json``, die vollständigen Logs der Toolchain-Läufe
daneben (``m4-*.log``). Die VM liegt im selben Ordner wie die der Oberfläche (``%LOCALAPPDATA%\\LocalAi\\vm``): mit
``reuseVm`` wird sie gespeichert und beim nächsten Lauf (oder von der Oberfläche) fortgesetzt, sofern das Projekt der
Oberfläche dieselben Toolchains wählt (``dotnet``, ``python``); mit ``keepVmOnExit`` bleibt sie stehen.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time
import uuid

import pytest

from localai.contracts import AgentRole, ProposalRequest, ProposalState, ToolTarget
from localai.contracts import TestReport as Report
from localai.pipe import BrokerError

from conftest import ROOT, start_broker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("LOCALAI_M4_ABNAHME") != "1", reason="nur mit LOCALAI_M4_ABNAHME=1 auf dem Hyper-V-PC"),
]

CS_PATCH = """--- a/src/Rechner/Rechner.cs
+++ b/src/Rechner/Rechner.cs
@@ -8,3 +8,6 @@
     public static int Multiplizieren(int a, int b) => a * b;
+
+    public static int Teilen(int a, int b) =>
+        b == 0 ? throw new ArgumentException("Teilen durch 0 ist nicht erlaubt", nameof(b)) : a / b;
 }
--- /dev/null
+++ b/tests/Rechner.Tests/TeilenTests.cs
@@ -0,0 +1,10 @@
+namespace Rechner.Tests;
+
+public sealed class TeilenTests
+{
+    [Fact]
+    public void Teilen() => Assert.Equal(2, Rechner.Teilen(6, 3));
+
+    [Fact]
+    public void Durch_null() => Assert.Throws<ArgumentException>(() => Rechner.Teilen(1, 0));
+}
"""
CS_FAILING = """--- /dev/null
+++ b/tests/Rechner.Tests/FalschTests.cs
@@ -0,0 +1,7 @@
+namespace Rechner.Tests;
+
+public sealed class FalschTests
+{
+    [Fact]
+    public void Absichtlich_falsch() => Assert.Equal(7, Rechner.Teilen(6, 3));
+}
"""
PY_PATCH = """--- a/rechner/core.py
+++ b/rechner/core.py
@@ -8,2 +8,8 @@
 def multiplizieren(a: int, b: int) -> int:
     return a * b
+
+
+def teilen(a: int, b: int) -> float:
+    if b == 0:
+        raise ValueError("Teilen durch 0 ist nicht erlaubt")
+    return a / b
--- /dev/null
+++ b/tests/test_teilen.py
@@ -0,0 +1,12 @@
+import pytest
+
+from rechner.core import teilen
+
+
+def test_teilen():
+    assert teilen(6, 3) == 2
+
+
+def test_durch_null():
+    with pytest.raises(ValueError):
+        teilen(1, 0)
"""
PY_FAILING = """--- /dev/null
+++ b/tests/test_falsch.py
@@ -0,0 +1,5 @@
+from rechner.core import teilen
+
+
+def test_absichtlich_falsch():
+    assert teilen(6, 3) == 7
"""


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def request(tool: str, args: dict, targets: list[str], snapshot: str = "-") -> ProposalRequest:
    return ProposalRequest(
        schema_version=1, run_id="abnahme-m4", task_id="m4", agent=AgentRole.Tester, tool=tool, tool_version="1",
        purpose="M4-Abnahme", arguments=args, targets=[ToolTarget(target_id=f"t{i}", path=p) for i, p in enumerate(targets, 1)],
        snapshot_id=snapshot, idempotency_key=uuid.uuid4().hex)


def run(ui, orch, req: ProposalRequest, timeout: float = 1800):
    view = orch.submit(req)
    ui.decide(view.proposal.proposal_id, view.proposal_sha256, approve=True)
    return orch.wait_final(view.proposal.proposal_id, timeout=timeout, poll=0.5)


def vm_exit_text() -> str:
    try:
        hyperv = json.loads((ROOT / "config" / "localai.json").read_text(encoding="utf-8-sig"))["sandbox"]["hyperv"]
    except (OSError, KeyError, ValueError):
        hyperv = {}
    if hyperv.get("reuseVm"):
        return "VM wird gespeichert (reuseVm) …"
    if hyperv.get("keepVmOnExit"):
        return "VM bleibt stehen (keepVmOnExit) …"
    return "VM wird gelöscht …"


def report_of(outcome) -> tuple[Report, str]:
    by_id = {r.target_id: r for r in outcome.result.target_results}
    return Report.model_validate_json(by_id["tests"].text), by_id["log"].text or ""


def test_m4_abnahme():
    base = pathlib.Path(os.environ.get("LOCALAPPDATA", str(pathlib.Path.home()))) / "LocalAi" / "abnahme"
    base.mkdir(parents=True, exist_ok=True)
    # Datenordner neben dem der Oberfläche → derselbe VM-Ordner (…\LocalAi\vm), damit reuseVm greift.
    data = base.parent / "broker-m4"
    data.mkdir(parents=True, exist_ok=True)
    registry = base.parent / "broker" / "toolchains.json"
    assert registry.exists(), f"Keine Toolchain-Registrierungen ({registry}) – in der Oberfläche unter TOOLCHAINS registrieren"
    (data / "toolchains.json").write_bytes(registry.read_bytes())
    large = data / "gross"
    if not large.exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "samples" / "make_large_repo.py"), str(large)], check=True)
    rights = ["List", "Read", "Modify", "Create", "Execute"]
    project = {
        "schemaVersion": 1, "projectId": "m4-abnahme", "name": "M4-Abnahme",
        "grants": [
            {"grantId": "cs", "root": str(ROOT / "samples" / "m4-csharp"), "rights": rights, "recursive": True},
            {"grantId": "py", "root": str(ROOT / "samples" / "m4-python"), "rights": rights, "recursive": True},
            {"grantId": "boese", "root": str(ROOT / "samples" / "m4-sec13"), "rights": rights, "recursive": True},
            {"grantId": "gross", "root": str(large), "rights": ["List", "Read"], "recursive": True},
        ],
        "toolchains": ["dotnet", "python"],
    }
    project_file = data / "m4-projekt.json"
    project_file.write_text(json.dumps(project), encoding="utf-8")
    started = time.monotonic()
    broker = start_broker(data, "--mode", "hyperv", "--config", str(ROOT / "config" / "localai.json"),
                          "--project", str(project_file))
    summary: dict = {}
    try:
        ui, orch = broker.ui(), broker.orchestrator()
        last = None
        while True:
            status = ui.status().get("sandbox") or ""
            if status != last:
                log(f"Sandbox: {status}")
                last = status
            if status.startswith("Offline-VM bereit"):
                break
            assert not status.startswith("gesperrt"), status
            assert time.monotonic() - started < 3 * 3600, "VM-Aufbau dauert länger als 3 Stunden"
            time.sleep(5)
        summary["buildMinutes"] = round((time.monotonic() - started) / 60, 1)
        available = orch.toolchains_available()
        assert available["enabled"] and {t["name"] for t in available["toolchains"]} >= {"dotnet", "python"}, available

        def snapshot(grant_id: str) -> str:
            root = next(g.root for g in orch.grants() if g.grant_id == grant_id)
            t0 = time.monotonic()
            out = run(ui, orch, request("Workspace.Snapshot", {"grantId": grant_id, "root": root}, []))
            assert out.state == ProposalState.Succeeded, out
            summary[f"snapshot_{grant_id}_s"] = round(time.monotonic() - t0, 1)
            return out.result.target_results[0].text

        def apply(grant_id: str, snap: str, patch: str) -> str:
            paths = re.findall(r"^\+\+\+ b/(.+)$", patch, re.M)
            out = run(ui, orch, request("Patch.Apply", {"grantId": grant_id, "patch": patch}, paths, snap))
            assert out.state == ProposalState.Succeeded, out
            return next(r.text for r in out.result.target_results if r.target_id == "snapshot")

        def toolchain(tool: str, grant_id: str, snap: str, name: str, parameters: dict, key: str):
            t0 = time.monotonic()
            out = run(ui, orch, request(tool, {"grantId": grant_id, "toolchain": name, "parameters": parameters}, [], snap))
            summary[f"{key}_s"] = round(time.monotonic() - t0, 1)
            if out.result is not None:
                text = "\n\n".join(f"== {r.target_id}: {r.status.value} {r.message or ''}\n{r.text or ''}" for r in out.result.target_results)
                (base / f"m4-{key}.log").write_text(text, encoding="utf-8")
            assert out.state == ProposalState.Succeeded, out
            return out

        # 1. C#
        log("C#: Snapshot, Patch, dotnet test …")
        snap = apply("cs", snapshot("cs"), CS_PATCH)
        params = {"project": "tests/Rechner.Tests/Rechner.Tests.csproj", "configuration": "Debug"}
        report, text = report_of(toolchain("Toolchain.Test", "cs", snap, "dotnet", params, "dotnet_test"))
        assert report.exit_code == 0 and report.total == 5 and report.failed == 0, (report, text[-3000:])
        bad = apply("cs", snap, CS_FAILING)
        report, text = report_of(toolchain("Toolchain.Test", "cs", bad, "dotnet", params, "dotnet_test_fehler"))
        assert report.exit_code != 0 and report.failed == 1 and "Absichtlich_falsch" in report.failures[0].name, report
        log(f"C# ok: {summary['dotnet_test_s']} s / {summary['dotnet_test_fehler_s']} s")

        # 2. Python
        log("Python: Snapshot, Patch, pytest …")
        snap = apply("py", snapshot("py"), PY_PATCH)
        report, text = report_of(toolchain("Toolchain.Test", "py", snap, "python", {"path": "."}, "pytest"))
        assert report.exit_code == 0 and report.total == 5 and report.failed == 0, (report, text[-3000:])
        bad = apply("py", snap, PY_FAILING)
        report, _ = report_of(toolchain("Toolchain.Test", "py", bad, "python", {"path": "."}, "pytest_fehler"))
        assert report.failed == 1 and "test_absichtlich_falsch" in report.failures[0].name, report
        log("Python ok")

        # 3. SEC-20
        before = ui.status()["pending"]
        for tool, args in (
            ("Toolchain.Lint", {"toolchain": "python", "parameters": {}}),
            ("Toolchain.Run", {"toolchain": "python", "parameters": {"script": "rechner/core.py; del *"}}),
            ("Toolchain.Run", {"toolchain": "python", "parameters": {"script": "-c"}}),
            ("Toolchain.Test", {"toolchain": "python", "parameters": {"path": ".", "extra": "-x"}}),
            ("Toolchain.Test", {"toolchain": "python", "command": "powershell -c Remove-Item C:\\", "parameters": {"path": "."}}),
            ("Shell.Execute", {"command": "cmd /c whoami"}),
        ):
            with pytest.raises(BrokerError) as err:
                orch.submit(request(tool, {"grantId": "py", **args}, [], snap))
            assert err.value.code in (403, 422), (tool, err.value)
        assert ui.status()["pending"] == before
        summary["sec20"] = "ok"
        log("SEC-20 ok")

        # 4. SEC-13 / SEC-21 / SEC-07
        log("SEC-13: Build des bösartigen Projekts …")
        out = toolchain("Toolchain.Build", "boese", snapshot("boese"), "dotnet", {"project": "Boese.csproj", "configuration": "Debug"},
                        "sec13_build")
        _, text = report_of(out)
        line = re.search(r"SEC13 (.+)", text)
        assert line, text[-3000:]
        codes = dict(part.split("=", 1) for part in line.group(1).split())
        summary["sec13"] = codes
        for key in ("worker", "toolchain", "windows"):
            assert codes[key] == "verweigert", f"SEC-13/-21: Schreiben ({key}) hätte scheitern müssen: {codes}"
        for key in ("registry", "netz"):
            assert codes[key] not in ("", "0"), f"SEC-13/-06: {key} hätte scheitern müssen: {codes}"
        assert codes["ssh"] == "fehlt", codes
        assert codes["arbeitskopie"] == "geschrieben", codes
        # Worker und Toolchain unverändert: erneuter Test läuft wie vorher
        report, _ = report_of(toolchain("Toolchain.Test", "py", snap, "python", {"path": "."}, "pytest_danach"))
        assert report.failed == 0 and report.total == 5
        log(f"SEC-13/-21/-07 ok: {codes}")

        # 5. großes Repository
        log("Großes Repository: Snapshot und Repo-Map …")
        snap = snapshot("gross")
        t0 = time.monotonic()
        out = run(ui, orch, request("Repo.Index", {"grantId": "gross", "maxChars": 12000}, [], snap))
        summary["repo_index_s"] = round(time.monotonic() - t0, 1)
        assert out.state == ProposalState.Succeeded, out
        text = out.result.target_results[0].text
        files = int(re.search(r"– (\d+) files", text).group(1))
        assert files > 2000 and len(text) <= 12000
        first = text.split("Most important files")[1].split("\n")[1]
        assert first.startswith("src/Core/CsvParser.cs"), first
        summary["largeRepoFiles"] = files
        summary["ok"] = True
        log(f"Fertig: {json.dumps(summary, ensure_ascii=False)}")
    finally:
        (base / "m4-abnahme.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        log("Broker wird beendet, " + vm_exit_text())
        broker.stop(timeout=300)
