"""M4 im Worker: Patch.Apply (abgeleitete Snapshots), Toolchain-Ausführung (SEC-20/-21), Repo.Index (B12)."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import sys

import pytest

from localai.contracts import ResultStatus, ToolTarget
from localai.contracts import TestReport as Report
from localai.contracts import TestResultFormat as ResultFormat
from localai.worker import patching
from localai.worker.execution import Executor, ToolchainCatalog, log_excerpt, parse_results
from localai.worker.repoindex import RepoIndexer, build_map, extract
from localai.worker.snapshot import SnapshotStore
from localai.worker.tools import ToolRunner

from tests.test_worker import job, make_snapshot, proposal

BASE = {
    "src/calc.py": b"def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
    "src/win.cs": b"class A\r\n{\r\n    int X() => 1;\r\n}\r\n",
    "README.md": b"# Demo\n",
}


@pytest.fixture()
def store(tmp_path):
    return SnapshotStore(tmp_path / "work")


def imported(tmp_path, store, files=None):
    manifest, archive = make_snapshot(tmp_path, files or BASE)
    return store.import_archive(manifest, archive)


def patch_job(snap: str, patch: str, paths: list[str], seq: int = 1):
    targets = [ToolTarget(target_id=f"t{i}", path=p) for i, p in enumerate(paths, start=1)]
    return job(proposal("Patch.Apply", targets, snap, patch=patch), seq)


# ---------------------------------------------------------------- Diff lesen/anwenden
PATCH = """--- a/src/calc.py
+++ b/src/calc.py
@@ -1,2 +1,4 @@
 def add(a, b):
+    if a is None:
+        raise ValueError("a")
     return a + b
--- /dev/null
+++ b/tests/test_calc.py
@@ -0,0 +1,3 @@
+from src.calc import add
+def test_add():
+    assert add(1, 2) == 3
"""


def test_parse_erkennt_dateien_und_arten():
    files = patching.parse(PATCH)
    assert [(f.path, f.kind) for f in files] == [("src/calc.py", "modify"), ("tests/test_calc.py", "create")]


@pytest.mark.parametrize("text, fehler", [
    ("@@ -1 +1 @@\n-a\n+b\n", "ohne Kopfzeilen"),
    ("nur text", "Kein Unified Diff"),
    ("--- a/x\n+++ b/y\n@@ -1 +1 @@\n-a\n+b\n", "Unterschiedliche Pfade"),
    ("diff --git a/x b/y\nrename from x\nrename to y\n", "Umbenennen"),
    ("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n--- a/x\n+++ b/x\n@@ -3 +3 @@\n-c\n+d\n", "mehrfach"),
])
def test_parse_lehnt_ab(text, fehler):
    with pytest.raises(patching.PatchError, match=fehler):
        patching.parse(text)


def test_hunk_mit_falschen_zeilennummern_und_crlf():
    file = patching.TextFile.decode(BASE["src/win.cs"], "src/win.cs")
    patch = patching.parse("--- a/src/win.cs\n+++ b/src/win.cs\n@@ -40,3 +40,4 @@\n {\n     int X() => 1;\n+    int Y() => 2;\n }\n")
    report = patching.apply_hunks(file, patch[0].hunks, "src/win.cs")
    assert file.encode() == b"class A\r\n{\r\n    int X() => 1;\r\n    int Y() => 2;\r\n}\r\n"
    assert report.shifted == 1


def test_hunk_kontext_mit_abweichendem_rand_wird_gekuerzt():
    file = patching.TextFile.decode(b"a\nb\nc\nd\n", "x")
    patch = patching.parse("--- a/x\n+++ b/x\n@@ -1,4 +1,4 @@\n FALSCH\n b\n-c\n+C\n d\n")
    report = patching.apply_hunks(file, patch[0].hunks, "x")
    assert file.encode() == b"a\nb\nC\nd\n" and report.fuzzy == 1


def test_hunk_passt_nicht():
    file = patching.TextFile.decode(b"a\nb\n", "x")
    patch = patching.parse("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-zzz\n+y\n")
    with pytest.raises(patching.PatchError, match="Hunk 1 passt nicht"):
        patching.apply_hunks(file, patch[0].hunks, "x")


def test_bom_und_fehlender_zeilenumbruch_bleiben():
    file = patching.TextFile.decode(b"\xef\xbb\xbfeins\nzwei", "x")
    patch = patching.parse("--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n-eins\n+EINS\n zwei\n\\ No newline at end of file\n")
    patching.apply_hunks(file, patch[0].hunks, "x")
    assert file.encode() == b"\xef\xbb\xbfEINS\nzwei"


# ---------------------------------------------------------------- Patch.Apply im Worker
def test_patch_apply_leitet_unveraenderlichen_snapshot_ab(tmp_path, store):
    base = imported(tmp_path, store)
    runner = ToolRunner(store)
    result = runner.run(patch_job(base.snapshot_id, PATCH, ["src/calc.py", "tests/test_calc.py"]), None)
    assert result.status == ResultStatus.Succeeded, result
    new_id = next(r.text for r in result.target_results if r.target_id == "snapshot")
    derived = store.get(new_id, "g1")
    assert new_id != base.snapshot_id
    assert b"raise ValueError" in derived.resolve("src/calc.py").read_bytes()
    assert "tests/test_calc.py" in derived.files and "tests/test_calc.py" not in base.files
    # Basis bleibt unverändert, abgeleitete Dateien sind schreibgeschützt
    assert base.resolve("src/calc.py").read_bytes() == BASE["src/calc.py"]
    assert not os.access(derived.resolve("src/calc.py"), os.W_OK) or os.geteuid() == 0
    # Nach einem Neustart des Workers bekannt (Manifest auf der Platte)
    assert SnapshotStore(tmp_path / "work").get(new_id, "g1").files == derived.files


def test_patch_apply_nur_ausgewaehlte_dateien(tmp_path, store):
    base = imported(tmp_path, store)
    result = ToolRunner(store).run(patch_job(base.snapshot_id, PATCH, ["tests/test_calc.py"]), None)
    derived = store.get(next(r.text for r in result.target_results if r.target_id == "snapshot"), "g1")
    assert derived.resolve("src/calc.py").read_bytes() == BASE["src/calc.py"]
    assert "tests/test_calc.py" in derived.files


def test_patch_apply_alles_oder_nichts(tmp_path, store):
    base = imported(tmp_path, store)
    bad = PATCH + "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-# Anders\n+# Neu\n"
    result = ToolRunner(store).run(patch_job(base.snapshot_id, bad, ["src/calc.py", "tests/test_calc.py", "README.md"]), None)
    assert result.status == ResultStatus.Failed
    by_id = {r.target_id: r for r in result.target_results}
    assert by_id["t1"].status == ResultStatus.Succeeded and by_id["t3"].status == ResultStatus.Failed
    assert "passt nicht" in by_id["t3"].message
    assert len(list((tmp_path / "work" / "snapshots").glob("*.manifest.json"))) == 1


def test_patch_apply_neue_datei_existiert_schon(tmp_path, store):
    base = imported(tmp_path, store)
    text = "--- /dev/null\n+++ b/README.md\n@@ -0,0 +1 @@\n+x\n"
    result = ToolRunner(store).run(patch_job(base.snapshot_id, text, ["README.md"]), None)
    assert result.status == ResultStatus.Failed and "existiert bereits" in result.target_results[0].message


def test_patch_apply_loeschen(tmp_path, store):
    base = imported(tmp_path, store)
    text = "--- a/README.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-# Demo\n"
    result = ToolRunner(store).run(patch_job(base.snapshot_id, text, ["README.md"]), None)
    derived = store.get(next(r.text for r in result.target_results if r.target_id == "snapshot"), "g1")
    assert "README.md" not in derived.files


# ---------------------------------------------------------------- Toolchains (SEC-20/-21)
@pytest.fixture()
def fake_toolchain(tmp_path):
    """Toolchain 'pyfake' mit dem Test-Python als Binary (POSIX: Skript namens python.exe)."""
    install = tmp_path / "install" / "pyfake"
    install.mkdir(parents=True)
    exe = install / "python.exe"
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    catalog = tmp_path / "toolchains" / "pyfake"
    (catalog / "packages").mkdir(parents=True)
    manifest = {
        "schemaVersion": 1, "name": "pyfake", "version": "1", "displayName": "Fake", "guestPath": "C:\\Toolchains\\pyfake",
        "license": {"proprietary": False}, "binaries": [{"name": "python", "path": "python.exe"}],
        "install": {"kind": "None", "expectedMinutes": 1, "check": {"binary": "python", "arguments": ["--version"]}},
        "environment": {"FAKE_PACKAGES": "{packages}", "PATH": "boese"},
        "actions": {
            "run": {"binary": "python", "arguments": ["{script}", "{mode}"],
                    "parameters": {"script": {"type": "Path"}, "mode": {"type": "Choice", "values": ["ok", "fail", "write", "sleep", "env"]}},
                    "timeoutSeconds": 3},
            "test": {"binary": "python", "arguments": ["-c", "import sys; sys.path.insert(0, '.'); import junit; junit.main(r'{results}')"],
                     "timeoutSeconds": 30},
        },
        "testResultFormat": "JUnit",
    }
    (catalog / "toolchain.json").write_text(json.dumps(manifest), encoding="utf-8")
    return ToolchainCatalog(tmp_path / "toolchains", tmp_path / "install")


SCRIPT = b"""import os, sys, time, pathlib
mode = sys.argv[1]
if mode == "fail":
    print("error CS1002: ; expected"); sys.exit(3)
if mode == "write":
    pathlib.Path(os.environ["OUTSIDE"]).write_text("manipuliert")
if mode == "sleep":
    time.sleep(30)
if mode == "env":
    print("PATH=" + os.environ.get("PATH", "")); print("PKG=" + os.environ.get("FAKE_PACKAGES", ""))
    print("NODEREUSE=" + os.environ.get("MSBUILDDISABLENODEREUSE", "")); print("SECRET=" + os.environ.get("LOCALAI_SECRET", "-"))
print("fertig", mode)
"""
JUNIT = b"""import pathlib
def main(results):
    pathlib.Path(results, 'r.xml').write_text('''<testsuites><testsuite name="s">
<testcase classname="t" name="ok"/><testcase classname="t" name="kaputt"><failure message="assert 1 == 2">Traceback...</failure></testcase>
<testcase classname="t" name="weg"><skipped/></testcase></testsuite></testsuites>''')
"""


def tool_job(tool: str, snap: str, seq: int, **params):
    return job(proposal(tool, [], snap, toolchain="pyfake", parameters=params), seq)


@pytest.fixture()
def exec_runner(tmp_path, store, fake_toolchain):
    base = imported(tmp_path, store, {"tools/s.py": SCRIPT, "junit.py": JUNIT, "README.md": b"x"})
    runner = ToolRunner(store, Executor(fake_toolchain, tmp_path / "work" / "jobs", low_integrity=False))
    return runner, base


@pytest.mark.skipif(os.name == "nt", reason="POSIX-Ersatz für das Binary")
def test_toolchain_run_mit_fester_umgebung(exec_runner, monkeypatch):
    runner, base = exec_runner
    monkeypatch.setenv("LOCALAI_SECRET", "geheim")
    result = runner.run(tool_job("Toolchain.Run", base.snapshot_id, 1, script="tools/s.py", mode="env"), None)
    assert result.status == ResultStatus.Succeeded and result.exit_code == 0
    log = next(r.text for r in result.target_results if r.target_id == "log")
    assert "SECRET=-" in log and "NODEREUSE=1" in log and "boese" not in log
    assert "PKG=" in log and "packages" in log


@pytest.mark.skipif(os.name == "nt", reason="POSIX-Ersatz für das Binary")
def test_toolchain_exitcode_und_logauszug(exec_runner):
    runner, base = exec_runner
    result = runner.run(tool_job("Toolchain.Run", base.snapshot_id, 1, script="tools/s.py", mode="fail"), None)
    assert result.status == ResultStatus.Succeeded and result.exit_code == 3
    log = next(r for r in result.target_results if r.target_id == "log")
    assert "error CS1002" in log.text and "Exitcode 3" in log.message


@pytest.mark.skipif(os.name == "nt", reason="POSIX-Ersatz für das Binary")
def test_toolchain_zeitgrenze_beendet_prozess(exec_runner):
    runner, base = exec_runner
    result = runner.run(tool_job("Toolchain.Run", base.snapshot_id, 1, script="tools/s.py", mode="sleep"), None)
    report = Report.model_validate_json(next(r.text for r in result.target_results if r.target_id == "tests"))
    assert report.timed_out and result.exit_code == -1


@pytest.mark.skipif(os.name == "nt", reason="POSIX-Ersatz für das Binary")
def test_toolchain_test_liefert_einheitliches_ergebnis(exec_runner):
    runner, base = exec_runner
    result = runner.run(tool_job("Toolchain.Test", base.snapshot_id, 1), None)
    report = Report.model_validate_json(next(r.text for r in result.target_results if r.target_id == "tests"))
    assert (report.total, report.passed, report.failed, report.skipped) == (3, 1, 1, 1)
    assert report.failures[0].name == "t.kaputt" and "assert 1 == 2" in report.failures[0].message


@pytest.mark.skipif(os.name == "nt", reason="POSIX-Ersatz für das Binary")
def test_job_arbeitet_auf_frischer_kopie(exec_runner, tmp_path, store):
    runner, base = exec_runner
    runner.run(tool_job("Toolchain.Run", base.snapshot_id, 1, script="tools/s.py", mode="ok"), None)
    assert list((tmp_path / "work" / "jobs").iterdir()) == []  # Arbeitsordner gelöscht
    assert base.resolve("tools/s.py").read_bytes() == SCRIPT  # Snapshot unverändert


@pytest.mark.parametrize("tool, params, fehler", [
    ("Toolchain.Run", {"script": "tools/s.py", "mode": "rm -rf"}, "erlaubt sind"),
    ("Toolchain.Run", {"script": "../etc/passwd", "mode": "ok"}, "unzulässiger Pfad"),
    ("Toolchain.Run", {"script": "fehlt.py", "mode": "ok"}, "gibt es im Snapshot nicht"),
    ("Toolchain.Run", {"script": "tools/s.py", "mode": "ok", "extra": "x"}, "Nicht deklarierte Parameter"),
    ("Toolchain.Run", {"script": "tools/s.py"}, "Parameter fehlen"),
    ("Toolchain.Build", {}, "keine Aktion für Toolchain.Build"),
])
def test_sec20_nicht_deklarierte_aktion_oder_freies_kommando(exec_runner, tool, params, fehler):
    runner, base = exec_runner
    result = runner.run(tool_job(tool, base.snapshot_id, 1, **params), None)
    assert result.status == ResultStatus.Failed and fehler in result.target_results[0].message


def test_toolchain_ohne_freischaltung_abgelehnt(tmp_path, store):
    base = imported(tmp_path, store)
    result = ToolRunner(store).run(tool_job("Toolchain.Test", base.snapshot_id, 1), None)
    assert result.status == ResultStatus.Failed and "nie auf dem Host" in result.target_results[0].message


def test_unbekannte_toolchain(exec_runner):
    runner, base = exec_runner
    result = runner.run(job(proposal("Toolchain.Test", [], base.snapshot_id, toolchain="dotnet", parameters={}), 1), None)
    assert "nicht installiert" in result.target_results[0].message


def test_trx_auswertung(tmp_path):
    (tmp_path / "a.trx").write_text("""<?xml version="1.0"?><TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">
<Results><UnitTestResult testName="Csv.Parse_ok" outcome="Passed"/>
<UnitTestResult testName="Csv.Parse_leer" outcome="Failed"><Output><ErrorInfo><Message>Expected 1</Message><StackTrace>at X</StackTrace></ErrorInfo></Output></UnitTestResult>
<UnitTestResult testName="Csv.Ignoriert" outcome="NotExecuted"/></Results></TestRun>""")
    report = parse_results(ResultFormat.Trx, tmp_path, 1, False)
    assert (report.total, report.passed, report.failed, report.skipped) == (3, 1, 1, 1)
    assert report.failures[0].message == "Expected 1" and report.failures[0].stack_trace == "at X"


def test_logauszug_bevorzugt_fehler():
    data = ("\n".join(f"Zeile {i}" for i in range(5000)) + "\nerror CS0103: Name fehlt\n" + "\n".join("x" * 50 for _ in range(2000))).encode()
    text = log_excerpt(data, limit=4000)
    assert "error CS0103" in text and len(text) <= 4100


# ---------------------------------------------------------------- Repo.Index (B12)
def test_symbole_je_sprache():
    py = extract("a.py", b"class K:\n    def m(self): pass\nMAX = 3\ndef f(): pass\n")
    assert {(s.name, s.kind) for s in py.symbols} == {("K", "class"), ("K.m", "method"), ("MAX", "constant"), ("f", "function")}
    cs = extract("A.cs", b"namespace N;\npublic sealed class CsvParser\n{\n    public static int Parse(string s)\n    {\n        if (x) {}\n    }\n}\n")
    assert ("CsvParser", "class") in {(s.name, s.kind) for s in cs.symbols}
    assert ("Parse", "method") in {(s.name, s.kind) for s in cs.symbols}
    assert not any(s.name == "if" for s in cs.symbols)
    ps = extract("x.ps1", b"function Get-Thing {\n}\n")
    assert ps.symbols[0].name == "Get-Thing"
    bat = extract("x.cmd", b"@echo off\n:start\ngoto :eof\n")
    assert [s.name for s in bat.symbols] == ["start"]
    cpp = extract("a.cpp", b"namespace n {\nint Rechner::add(int a, int b) const {\n  return a+b;\n}\n}\n")
    assert "Rechner::add" in [s.name for s in cpp.symbols]
    hdev = extract("p.hdev", b'<?xml version="1.0"?><hdevelop><procedure name="main"/><procedure name="find_edges"/></hdevelop>')
    assert [s.name for s in hdev.symbols] == ["main", "find_edges"]
    broken = extract("b.py", b"def (:\n")
    assert broken.error is not None


def big_repo(n: int) -> dict[str, bytes]:
    files = {"src/Core/Parser.cs": b"public class Parser\n{\n    public int Parse(string s) => 1;\n}\n",
             "src/App/Program.cs": b"var p = new Parser();\n"}
    for i in range(n):
        files[f"src/Mod{i % 40}/Feature{i}.cs"] = (
            f"public class Feature{i}\n{{\n    public void Run() {{ new Parser().Parse(\"{i}\"); }}\n}}\n").encode()
    files["tests/test_x.py"] = b"def test_x(): pass\n"
    files["Demo.sln"] = b"x"
    return files


def test_repo_index_grosses_repo_passt_ins_budget(tmp_path, store):
    base = imported(tmp_path, store, big_repo(2100))
    runner = ToolRunner(store, indexer=RepoIndexer(tmp_path / "work"))
    result = runner.run(job(proposal("Repo.Index", [], base.snapshot_id, maxChars=8000), 1), None)
    assert result.status == ResultStatus.Succeeded, result
    text = result.target_results[0].text
    assert len(text) <= 8000
    assert "2104 files" in text and "Demo.sln" in text
    # Meistreferenzierte Datei steht oben
    first = text.split("Most important files")[1].split("\n")[1]
    assert first.startswith("src/Core/Parser.cs")
    assert "more source files not shown" in text
    # Suche über den Index
    hits = runner.run(job(proposal("Repo.Index", [], base.snapshot_id, query="Feature2099"), 2), None)
    assert "src/Mod19/Feature2099.cs:1 class Feature2099" in hits.target_results[0].text


def test_repo_index_nutzt_cache_fuer_abgeleitete_snapshots(tmp_path, store):
    base = imported(tmp_path, store)
    indexer = RepoIndexer(tmp_path / "work")
    indexer.build(base)
    derived = store.derive(base, {"src/neu.py": b"def neu(): pass\n"})
    infos = indexer.build(derived)
    assert any(i.path == "src/neu.py" for i in infos)
    text = build_map(derived, infos, 4000)
    assert "src/neu.py" in text and derived.snapshot_id[:12] in text


def test_einrueckung_im_kontext_falsch_wird_toleriert_und_datei_bleibt_exakt():
    file = patching.TextFile.decode(b"def f():\n    x = 1\n    return x\n", "f.py")
    # Modell hat die Kontextzeilen mit einem Leerzeichen zu wenig eingerückt
    patch = patching.parse("--- a/f.py\n+++ b/f.py\n@@ -1,3 +1,4 @@\n def f():\n    x = 1\n+    y = 2\n    return x\n")
    patching.apply_hunks(file, patch[0].hunks, "f.py")
    assert file.encode() == b"def f():\n    x = 1\n    y = 2\n    return x\n"


def test_kontextzeile_ohne_praefix_von_kleinen_modellen():
    file = patching.TextFile.decode(b"def f():\n\n    return 1\n", "f.py")
    patch = patching.parse("--- a/f.py\n+++ b/f.py\n@@ -1,3 +1,4 @@\ndef f():\n\n+    # neu\n     return 1\nDieser Patch ergänzt einen Kommentar.\n")
    patching.apply_hunks(file, patch[0].hunks, "f.py")
    assert file.encode() == b"def f():\n\n    # neu\n    return 1\n"
