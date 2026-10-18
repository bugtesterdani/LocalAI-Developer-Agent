"""M5 im Worker: mehrstufige Aktionen (then), Windows-Programme ({system}), HDevelop-Prüfung nach Patches,
Toolchain-Vorlagen für C++, PowerShell, Batch und HDevelop."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import sys

import pytest
from localai.contracts import ResultStatus, ToolchainManifest, ToolTarget
from localai.contracts import TestReport as Report
from localai.worker import hdevelop
from localai.worker.execution import ExecError, Executor, ToolchainCatalog, binary_path
from localai.worker.snapshot import SnapshotStore
from localai.worker.tools import ToolRunner
from tests.test_worker import job, make_snapshot, proposal

REPO = pathlib.Path(__file__).resolve().parents[3]
TEMPLATES = REPO / "toolchains"

STEP = b"""import pathlib, sys
step, mode = sys.argv[1], sys.argv[2]
print("schritt", step)
if step == "1":
    pathlib.Path("build.marker").write_text("x")
elif step == "2":
    assert pathlib.Path("build.marker").exists(), "Schritt 1 lief nicht vorher"
    if mode == "fail":
        print("error C2065: nicht deklariert"); sys.exit(2)
else:
    pathlib.Path(sys.argv[3], "r.xml").write_text('<testsuite><testcase classname="c" name="a"/></testsuite>')
"""


@pytest.fixture()
def store(tmp_path):
    return SnapshotStore(tmp_path / "work")


@pytest.fixture()
def runner(tmp_path, store):
    install = tmp_path / "install" / "steps"
    install.mkdir(parents=True)
    exe = install / "python.exe"
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    catalog = tmp_path / "toolchains" / "steps"
    catalog.mkdir(parents=True)
    step = ["s.py"]
    manifest = {
        "schemaVersion": 1, "name": "steps", "version": "1", "displayName": "Steps", "guestPath": "C:\\Toolchains\\steps",
        "license": {"proprietary": False}, "binaries": [{"name": "python", "path": "python.exe"}],
        "install": {"kind": "None", "expectedMinutes": 1, "check": {"binary": "python", "arguments": ["--version"]}},
        "actions": {"test": {"binary": "python", "arguments": [*step, "1", "{mode}"],
                             "then": [{"binary": "python", "arguments": [*step, "2", "{mode}"]},
                                      {"binary": "python", "arguments": [*step, "3", "{mode}", "{results}"]}],
                             "parameters": {"mode": {"type": "Choice", "values": ["ok", "fail"]}}, "timeoutSeconds": 30}},
        "testResultFormat": "JUnit",
    }
    (catalog / "toolchain.json").write_text(json.dumps(manifest), encoding="utf-8")
    manifest_, archive = make_snapshot(tmp_path, {"s.py": STEP})
    base = store.import_archive(manifest_, archive)
    return ToolRunner(store, Executor(ToolchainCatalog(tmp_path / "toolchains", tmp_path / "install"),
                                      tmp_path / "work" / "jobs", low_integrity=False)), base


def _report(result) -> Report:
    return Report.model_validate_json(next(r.text for r in result.target_results if r.target_id == "tests"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX-Ersatz für das Binary")
def test_mehrstufige_aktion_laeuft_nacheinander_im_selben_arbeitsordner(runner):
    run, base = runner
    result = run.run(job(proposal("Toolchain.Test", [], base.snapshot_id, toolchain="steps", parameters={"mode": "ok"}), 1), None)
    report = _report(result)
    assert result.exit_code == 0 and (report.total, report.passed) == (1, 1)
    log = next(r for r in result.target_results if r.target_id == "log")
    assert "Schritt 1/3" in log.text and "Schritt 3/3" in log.text and "schritt 2" in log.text


@pytest.mark.skipif(os.name == "nt", reason="POSIX-Ersatz für das Binary")
def test_mehrstufige_aktion_bricht_beim_ersten_fehler_ab(runner):
    run, base = runner
    result = run.run(job(proposal("Toolchain.Test", [], base.snapshot_id, toolchain="steps", parameters={"mode": "fail"}), 1), None)
    report = _report(result)
    assert result.exit_code == 2 and report.total == 0
    assert any("Schritt 2 von 3" in p for p in report.problems)
    log = next(r for r in result.target_results if r.target_id == "log")
    assert "error C2065" in log.text and "(Schritt 2/3)" in log.message and "schritt 3" not in log.text


def _manifest(binaries: list[dict]) -> ToolchainManifest:
    return ToolchainManifest.model_validate({
        "schemaVersion": 1, "name": "batch", "version": "1", "displayName": "B", "guestPath": "C:\\Toolchains\\batch",
        "license": {"proprietary": False}, "binaries": binaries,
        "install": {"kind": "None", "expectedMinutes": 1, "check": {"binary": "cmd", "arguments": []}},
        "actions": {}, "testResultFormat": "ExitCode"})


def test_windows_programme_nur_aus_der_erlaubten_liste(tmp_path):
    from localai.worker.execution import Toolchain
    system = tmp_path / "System32"
    tc = Toolchain(_manifest([{"name": "cmd", "path": "{system}\\cmd.exe"},
                              {"name": "ps", "path": "{SYSTEM}\\WindowsPowerShell\\v1.0\\powershell.exe"},
                              {"name": "evil", "path": "{system}\\..\\explorer.exe"},
                              {"name": "up", "path": "..\\..\\Windows\\notepad.exe"},
                              {"name": "hash", "path": "{system}\\cmd.exe", "sha256": "0" * 64}]), tmp_path, tmp_path / "inst")
    assert binary_path(tc, "cmd", system) == system / "cmd.exe"
    assert binary_path(tc, "ps", system) == system / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    for name in ("evil", "up", "hash"):
        with pytest.raises(ExecError):
            binary_path(tc, name, system)


@pytest.mark.parametrize("value", ["a&calc.cmd", "x%PATH%.cmd", "a^b", "a!b!.cmd"])
def test_pfadparameter_ohne_cmd_sonderzeichen(tmp_path, store, value):
    from localai.contracts import ToolchainAction
    from localai.worker.execution import validate_parameters
    manifest_, archive = make_snapshot(tmp_path, {value: b"x", "ok.cmd": b"x"})
    index = store.import_archive(manifest_, archive)
    action = ToolchainAction.model_validate({"binary": "cmd", "arguments": ["{script}"], "parameters": {"script": {"type": "Path"}}})
    assert validate_parameters(action, {"script": "ok.cmd"}, index)["script"] == ("Path", "ok.cmd")
    with pytest.raises(ExecError, match="Zeichen"):
        validate_parameters(action, {"script": value}, index)


# ---------------------------------------------------------------- HDevelop (B20)
HDEV = (b'<?xml version="1.0" encoding="UTF-8"?>\n<hdevelop file_version="1.2" halcon_version="24.11.0.0">\n'
        b'<procedure name="main">\n<interface/>\n<body>\n<l>A := 1</l>\n<l>B := A + 1</l>\n</body>\n'
        b'<docu id="main">\n<parameters/>\n</docu>\n</procedure>\n</hdevelop>\n')


@pytest.mark.parametrize("data,fehler", [
    (HDEV.replace(b"A := 1", b"if (A < 1)"), "kein gültiges XML"),
    (HDEV.replace(b'name="main"', b'name="x"'), "ohne Prozedur main"),
    (HDEV.replace(b"<hdevelop", b'<!DOCTYPE x [<!ENTITY e "x">]><hdevelop'), "DOCTYPE"),
    (b"<root/>", "Wurzelelement"),
    (HDEV.replace(b"<body>\n<l>A := 1</l>\n<l>B := A + 1</l>\n</body>\n", b""), "keinen <body>"),
])
def test_hdevelop_pruefung_erkennt_kaputte_dateien(data, fehler):
    assert hdevelop.check("p/main.hdev", HDEV) is None
    assert fehler in (hdevelop.check("p/main.hdev", data) or "")
    assert hdevelop.check("p/readme.txt", b"<kaputt") is None


def test_patch_der_hdev_xml_zerstoert_wird_nicht_angewendet(tmp_path, store):
    manifest_, archive = make_snapshot(tmp_path, {"vision/main.hdev": HDEV})
    base = store.import_archive(manifest_, archive)
    run = ToolRunner(store)
    bad = "--- a/vision/main.hdev\n+++ b/vision/main.hdev\n@@ -7 +7 @@\n-<l>B := A + 1</l>\n+<l>if (A < 2)</l>\n"
    result = run.run(job(proposal("Patch.Apply", [ToolTarget(target_id="t1", path="vision/main.hdev")], base.snapshot_id,
                                  patch=bad), 1), None)
    assert result.status == ResultStatus.Failed
    message = next(r.message for r in result.target_results if r.target_id == "t1")
    assert "HDevelop-Datei nach dem Patch ungültig" in message and "&lt;" in message
    good = bad.replace("+<l>if (A < 2)</l>", "+<l>B := A + 2</l>")
    ok = run.run(job(proposal("Patch.Apply", [ToolTarget(target_id="t1", path="vision/main.hdev")], base.snapshot_id,
                              patch=good), 2), None)
    assert ok.status == ResultStatus.Succeeded


# ---------------------------------------------------------------- Vorlagen
@pytest.mark.parametrize("name", ["dotnet", "python", "cpp", "cpp-clang", "powershell", "powershell5", "batch", "hdevelop"])
def test_vorlagen_sind_gueltig_und_vollstaendig(name):
    directory = TEMPLATES / name
    manifest = ToolchainManifest.model_validate_json((directory / "toolchain.json").read_text(encoding="utf-8"))
    assert manifest.name == name and manifest.guest_path == f"C:\\Toolchains\\{name}"
    assert "test" in manifest.actions, "jede Sprache braucht einen Testlauf (M5-Abnahme)"
    assert (directory / manifest.self_test.directory).is_dir()
    binaries = {b.name for b in manifest.binaries}
    for action in manifest.actions.values():
        assert action.binary in binaries and all(c.binary in binaries for c in action.then or [])
    for entry in manifest.deploy or []:
        if entry.source in ("runner", "harness"):
            assert (directory / entry.source).is_dir(), f"{name}: {entry.source} fehlt in der Vorlage"


def test_powershell_runner_in_beiden_vorlagen_gleich():
    for script in (TEMPLATES / "powershell" / "runner").iterdir():
        assert (TEMPLATES / "powershell5" / "runner" / script.name).read_bytes() == script.read_bytes()


def test_powershell_skripte_mit_bom_fuer_windows_powershell():
    # Windows PowerShell 5.1 liest .ps1 ohne BOM als ANSI – Umlaute in Meldungen gingen kaputt.
    for script in TEMPLATES.rglob("*.ps1"):
        assert script.read_bytes().startswith(b"\xef\xbb\xbf"), script
