"""M5-Abnahme mit echter Hyper-V-VM: je ein Beispiel pro Sprache gebaut und getestet (C++, PowerShell, Batch,
HDevelop), Installationszeiten der Toolchains im VM-Aufbau. Nur auf dem Windows-PC.

Voraussetzungen (docs/pruefung-M5.md): Toolchains cpp, powershell, batch und hdevelop registriert. Ohne nutzbare
HALCON-Lizenz in der VM: ``$env:LOCALAI_M5_HDEVELOP = "0"`` – dann entfällt die Toolchain hdevelop, die XML-Prüfung
von .hdev-Patches wird trotzdem geprüft (sie braucht kein HALCON). Start (PowerShell, im Repository)::

    $env:LOCALAI_M5_ABNAHME = "1"
    uv run --project src/python pytest -s tests/integration/test_m5_abnahme.py

Geprüft wird ohne Modell, direkt über den Broker (jede Aktion ein genehmigter Vorschlag), je Sprache:
Snapshot → Patch (neue Funktion + Tests) → Toolchain.Test grün → Patch mit absichtlich falschem Test → genau dieser
Fehler im Bericht; dazu Toolchain.Lint (PSScriptAnalyzer, Batch-Prüfung, HDevelop-Lade-Test) und ein Patch, der eine
.hdev-Datei als XML zerstören würde (wird nicht angewendet).

Ergebnis: ``%LOCALAPPDATA%\\LocalAi\\abnahme\\m5-abnahme.json`` (mit Installationszeiten), Logs ``m5-*.log``.
"""

from __future__ import annotations

import difflib
import json
import os
import pathlib
import re
import time

import pytest
from conftest import ROOT, start_broker
from localai.contracts import ProposalState
from test_m4_abnahme import log, report_of, request, run, vm_exit_text

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("LOCALAI_M5_ABNAHME") != "1", reason="nur mit LOCALAI_M5_ABNAHME=1 auf dem Hyper-V-PC"),
]

WITH_HDEVELOP = os.environ.get("LOCALAI_M5_HDEVELOP", "1") != "0"
TOOLCHAINS = ["cpp", "powershell", "batch"] + (["hdevelop"] if WITH_HDEVELOP else [])


class Repo:
    """Aktueller Stand eines Beispiels (für Patches, die aufeinander aufbauen)."""

    def __init__(self, root: pathlib.Path) -> None:
        self.files = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
                      for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts}

    def patch(self, changes: dict[str, str]) -> str:
        out: list[str] = []
        for path, new in changes.items():
            new = new if new.endswith("\n") else new + "\n"
            old = self.files.get(path)
            if old is None:
                lines = new.splitlines()
                out += ["--- /dev/null", f"+++ b/{path}", f"@@ -0,0 +1,{len(lines)} @@"] + ["+" + ln for ln in lines]
            else:
                out += [ln.rstrip("\n") for ln in difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                                        f"a/{path}", f"b/{path}", n=3)]
            self.files[path] = new
        return "\n".join(out) + "\n"

    def edit(self, path: str, old: str, new: str) -> str:
        text = self.files[path]
        assert old in text, (path, old)
        return text.replace(old, new, 1)


def cpp_changes(repo: Repo) -> dict[str, str]:
    return {
        "include/rechner.hpp": repo.edit("include/rechner.hpp", "int multiplizieren(int a, int b);",
                                         "int multiplizieren(int a, int b);\n\n/// Quotient; wirft std::invalid_argument bei b == 0.\n"
                                         "int teilen(int a, int b);"),
        "src/rechner.cpp": repo.edit("src/rechner.cpp", '#include "rechner.hpp"\n', '#include "rechner.hpp"\n\n#include <stdexcept>\n')
        .replace("    return a * b;\n}\n", "    return a * b;\n}\n\nint teilen(int a, int b)\n{\n    if (b == 0)\n    {\n"
                 "        throw std::invalid_argument(\"Teilen durch 0 ist nicht erlaubt\");\n    }\n    return a / b;\n}\n"),
        "tests/rechner_tests.cpp": repo.edit("tests/rechner_tests.cpp", "struct Fall\n",
                                             "void teilen()\n{\n    PRUEFE(rechner::teilen(6, 3) == 2);\n    bool geworfen = false;\n"
                                             "    try\n    {\n        rechner::teilen(1, 0);\n    }\n"
                                             "    catch (const std::invalid_argument&)\n    {\n        geworfen = true;\n    }\n"
                                             "    PRUEFE(geworfen);\n}\n\nstruct Fall\n")
        .replace('#include <cstring>\n', '#include <cstring>\n#include <stdexcept>\n')
        .replace('    {"multiplizieren", multiplizieren},\n', '    {"multiplizieren", multiplizieren},\n    {"teilen", teilen},\n'),
        "CMakeLists.txt": repo.edit("CMakeLists.txt", "foreach(fall addieren multiplizieren)", "foreach(fall addieren multiplizieren teilen)"),
    }


def cpp_failing(repo: Repo) -> dict[str, str]:
    return {
        "tests/rechner_tests.cpp": repo.edit("tests/rechner_tests.cpp", "struct Fall\n",
                                             "void falsch()\n{\n    PRUEFE(rechner::teilen(6, 3) == 7);\n}\n\nstruct Fall\n")
        .replace('    {"teilen", teilen},\n', '    {"teilen", teilen},\n    {"falsch", falsch},\n'),
        "CMakeLists.txt": repo.edit("CMakeLists.txt", "multiplizieren teilen)", "multiplizieren teilen falsch)"),
    }


PS_QUOTIENT = """
function Get-Quotient {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][int]$A,
        [Parameter(Mandatory = $true)][int]$B
    )
    if ($B -eq 0) { throw 'Teilen durch 0 ist nicht erlaubt' }
    return [math]::Truncate($A / $B)
}
"""
PS_TESTS = """BeforeAll {
    Import-Module (Join-Path $PSScriptRoot '..\\src\\Rechner.psm1') -Force
}

Describe 'Get-Quotient' {
    It 'teilt' {
        Get-Quotient -A 6 -B 3 | Should -Be 2
    }
    It 'lehnt 0 ab' {
        { Get-Quotient -A 1 -B 0 } | Should -Throw '*nicht erlaubt*'
    }
}
"""
PS_FAILING = """BeforeAll {
    Import-Module (Join-Path $PSScriptRoot '..\\src\\Rechner.psm1') -Force
}

Describe 'Absichtlich' {
    It 'falsch' {
        Get-Quotient -A 6 -B 3 | Should -Be 7
    }
}
"""

HDEV_HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n<hdevelop file_version="1.2" halcon_version="24.11.0.0">\n'
HDVP_TEILEN = HDEV_HEAD + """<procedure name="teilen">
<interface>
<ic>
<par name="A" base_type="ctrl" dimension="0"/>
<par name="B" base_type="ctrl" dimension="0"/>
</ic>
<oc>
<par name="Quotient" base_type="ctrl" dimension="0"/>
</oc>
</interface>
<body>
<l>if (B == 0)</l>
<l>    throw ('Teilen durch 0 ist nicht erlaubt')</l>
<l>endif</l>
<l>Quotient := A / B</l>
<l>return ()</l>
</body>
<docu id="teilen">
<parameters>
<parameter id="A"/>
<parameter id="B"/>
<parameter id="Quotient"/>
</parameters>
</docu>
</procedure>
</hdevelop>
"""


def hdev_test(name: str, lines: list[str]) -> str:
    body = "".join(f"<l>{ln}</l>\n" for ln in lines)
    return (f'<procedure name="{name}">\n<interface/>\n<body>\n{body}</body>\n<docu id="{name}">\n<parameters/>\n</docu>\n'
            "</procedure>\n")


def test_m5_abnahme():
    base = pathlib.Path(os.environ.get("LOCALAPPDATA", str(pathlib.Path.home()))) / "LocalAi" / "abnahme"
    base.mkdir(parents=True, exist_ok=True)
    data = base.parent / "broker-m5"  # neben dem Datenordner der Oberfläche → derselbe VM-Ordner (reuseVm)
    data.mkdir(parents=True, exist_ok=True)
    registry = base.parent / "broker" / "toolchains.json"
    assert registry.exists(), f"Keine Toolchain-Registrierungen ({registry}) – in der Oberfläche unter TOOLCHAINS registrieren"
    (data / "toolchains.json").write_bytes(registry.read_bytes())
    rights = ["List", "Read", "Modify", "Create", "Execute"]
    samples = {"cpp": "m5-cpp", "ps": "m5-powershell", "bat": "m5-batch", "hdev": "m5-hdevelop"}
    project = {
        "schemaVersion": 1, "projectId": "m5-abnahme", "name": "M5-Abnahme",
        "grants": [{"grantId": g, "root": str(ROOT / "samples" / s), "rights": rights, "recursive": True} for g, s in samples.items()],
        "toolchains": TOOLCHAINS,
    }
    project_file = data / "m5-projekt.json"
    project_file.write_text(json.dumps(project), encoding="utf-8")
    started = time.monotonic()
    broker = start_broker(data, "--mode", "hyperv", "--config", str(ROOT / "config" / "localai.json"),
                          "--project", str(project_file))
    summary: dict = {"toolchains": TOOLCHAINS}
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
            assert time.monotonic() - started < 4 * 3600, "VM-Aufbau dauert länger als 4 Stunden"
            time.sleep(5)
        summary["buildMinutes"] = round((time.monotonic() - started) / 60, 1)
        # Installationszeiten aus der Statuszeile („cpp 17.14 (Hallo-Welt ok) 25 min“); bei wiederverwendeter VM die
        # Zeiten des ursprünglichen Aufbaus, sofern im Status enthalten.
        summary["status"] = last
        summary["installTimes"] = {m.group(1): m.group(2) for m in re.finditer(r"([a-z0-9-]+) [^,;()]+(?:\([^)]*\))? (\d+ (?:s|min))", last)}
        available = orch.toolchains_available()
        assert available["enabled"] and {t["name"] for t in available["toolchains"]} >= set(TOOLCHAINS), available

        def snapshot(grant_id: str) -> str:
            root = next(g.root for g in orch.grants() if g.grant_id == grant_id)
            t0 = time.monotonic()
            out = run(ui, orch, request("Workspace.Snapshot", {"grantId": grant_id, "root": root}, []))
            assert out.state == ProposalState.Succeeded, out
            summary[f"snapshot_{grant_id}_s"] = round(time.monotonic() - t0, 1)
            return out.result.target_results[0].text

        def apply(grant_id: str, snap: str, patch: str, expect_ok: bool = True):
            paths = re.findall(r"^\+\+\+ b/(.+)$", patch, re.M)
            out = run(ui, orch, request("Patch.Apply", {"grantId": grant_id, "patch": patch}, paths, snap))
            if not expect_ok:
                return out
            assert out.state == ProposalState.Succeeded, out
            return next(r.text for r in out.result.target_results if r.target_id == "snapshot")

        def toolchain(tool: str, grant_id: str, snap: str, name: str, parameters: dict, key: str):
            t0 = time.monotonic()
            out = run(ui, orch, request(tool, {"grantId": grant_id, "toolchain": name, "parameters": parameters}, [], snap),
                      timeout=3600)
            summary[f"{key}_s"] = round(time.monotonic() - t0, 1)
            if out.result is not None:
                text = "\n\n".join(f"== {r.target_id}: {r.status.value} {r.message or ''}\n{r.text or ''}" for r in out.result.target_results)
                (base / f"m5-{key}.log").write_text(text, encoding="utf-8")
            assert out.state == ProposalState.Succeeded, out
            return report_of(out)

        def language(grant: str, sample: str, name: str, params: dict, good: dict, failing, totals: tuple[int, int], fail_name: str):
            repo = Repo(ROOT / "samples" / sample)
            snap = snapshot(grant)
            report, text = toolchain("Toolchain.Test", grant, snap, name, params, f"{name}_vorher")
            assert report.exit_code == 0 and report.total == totals[0] and report.failed == 0, (report, text[-3000:])
            changes = good(repo) if callable(good) else good
            snap = apply(grant, snap, repo.patch(changes))
            report, text = toolchain("Toolchain.Test", grant, snap, name, params, f"{name}_test")
            assert report.exit_code == 0 and report.total == totals[1] and report.failed == 0, (report, text[-3000:])
            bad = apply(grant, snap, repo.patch(failing(repo)))
            report, text = toolchain("Toolchain.Test", grant, bad, name, params, f"{name}_fehler")
            assert report.exit_code != 0 and report.failed == 1 and fail_name in report.failures[0].name, (report, text[-3000:])
            return snap, repo

        # 1. C++ (MSVC, CMake, CTest)
        log("C++: Snapshot, Tests vorher, Patch teilen, Tests, absichtlicher Fehler …")
        language("cpp", "m5-cpp", "cpp", {"project": ".", "configuration": "Debug"}, cpp_changes, cpp_failing,
                 (2, 3), "falsch")
        log("C++ ok")

        # 2. PowerShell (Pester, PSScriptAnalyzer)
        log("PowerShell: Pester und PSScriptAnalyzer …")
        snap, _ = language(
            "ps", "m5-powershell", "powershell", {"path": "."},
            lambda r: {"src/Rechner.psm1": r.edit("src/Rechner.psm1", "Export-ModuleMember -Function Get-Summe, Get-Produkt",
                                                  PS_QUOTIENT.lstrip() + "\nExport-ModuleMember -Function Get-Summe, Get-Produkt, Get-Quotient"),
                       "tests/Quotient.Tests.ps1": PS_TESTS},
            lambda r: {"tests/Falsch.Tests.ps1": PS_FAILING}, (3, 5), "falsch")
        report, text = toolchain("Toolchain.Lint", "ps", snap, "powershell", {"path": "."}, "powershell_lint")
        assert report.exit_code == 0 and "PSScriptAnalyzer" in text, text[-3000:]
        log("PowerShell ok")

        # 3. Batch (Harness)
        log("Batch: Harness und statische Prüfung …")
        sub_label = ":mul\nset /a ERGEBNIS=%~2 * %~3\necho %ERGEBNIS%\nexit /b 0\n"
        snap, repo = language(
            "bat", "m5-batch", "batch", {"path": "."},
            lambda r: {"scripts/rechne.cmd": r.edit("scripts/rechne.cmd", 'if /i "%~1"=="mul" goto :mul\n',
                                                    'if /i "%~1"=="mul" goto :mul\nif /i "%~1"=="sub" goto :sub\n')
                       .replace(sub_label, sub_label + "\n:sub\nset /a ERGEBNIS=%~2 - %~3\necho %ERGEBNIS%\nexit /b 0\n"),
                       "tests/sub.test.cmd": "@echo off\ncall scripts\\rechne.cmd sub 5 3\n",
                       "tests/sub.test.expected.txt": "2\n"},
            lambda r: {"tests/falsch.test.cmd": "@echo off\ncall scripts\\rechne.cmd sub 5 3\n",
                       "tests/falsch.test.expected.txt": "3\n"}, (3, 4), "falsch")
        report, text = toolchain("Toolchain.Lint", "bat", snap, "batch", {"path": "."}, "batch_lint")
        assert report.exit_code == 0, text[-3000:]
        broken = apply("bat", snap, Repo(ROOT / "samples" / "m5-batch").patch(
            {"scripts/kaputt.cmd": "@echo off\ngoto :nirgends\nif 1==1 (\necho offen\n"}))
        report, text = toolchain("Toolchain.Lint", "bat", broken, "batch", {"path": "."}, "batch_lint_fehler")
        assert report.exit_code == 1 and ":nirgends" in text and "nicht geschlossen" in text, text[-3000:]
        log("Batch ok")

        # 4. HDevelop (XML-Prüfung nach jedem Patch; mit HALCON: Lade-Test und Tests über HDevEngine)
        log("HDevelop: XML-Prüfung nach Patches …")
        repo = Repo(ROOT / "samples" / "m5-hdevelop")
        snap = snapshot("hdev")
        out = apply("hdev", snap, repo.patch({"tests/rechner_test.hdev": repo.edit(
            "tests/rechner_test.hdev", "<l>if (Summe != 5)</l>", "<l>if (Summe < 5)</l>")}), expect_ok=False)
        assert out.state == ProposalState.Failed, out
        message = next(r.message for r in out.result.target_results if r.target_id != "snapshot")
        assert "HDevelop-Datei nach dem Patch ungültig" in message, message
        summary["hdevelop_xml"] = "ok"
        if WITH_HDEVELOP:
            log("HDevelop: Lade-Test und Tests …")
            repo = Repo(ROOT / "samples" / "m5-hdevelop")
            test_file = "tests/rechner_test.hdev"
            snap, _ = language(
                "hdev", "m5-hdevelop", "hdevelop", {"path": "."},
                lambda r: {"procedures/teilen.hdvp": HDVP_TEILEN,
                           test_file: r.edit(test_file, "</hdevelop>", hdev_test("test_teilen", [
                               "teilen (6, 3, Quotient)", "if (Quotient != 2)", "    throw ('teilen(6, 3) ergab ' + Quotient)",
                               "endif", "return ()"]) + "</hdevelop>")},
                lambda r: {test_file: r.edit(test_file, "</hdevelop>", hdev_test("test_falsch", [
                    "teilen (6, 3, Quotient)", "if (Quotient != 7)", "    throw ('absichtlich falsch: ' + Quotient)",
                    "endif", "return ()"]) + "</hdevelop>")}, (2, 3), "test_falsch")
            report, text = toolchain("Toolchain.Lint", "hdev", snap, "hdevelop", {"path": "."}, "hdevelop_lint")
            assert report.exit_code == 0 and "Lade-Test" in text, text[-3000:]
            log("HDevelop ok")
        else:
            summary["hdevelop"] = "übersprungen (LOCALAI_M5_HDEVELOP=0)"
        summary["ok"] = True
        log(f"Fertig: {json.dumps(summary, ensure_ascii=False)}")
    finally:
        (base / "m5-abnahme.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        log("Broker wird beendet, " + vm_exit_text())
        broker.stop(timeout=300)
