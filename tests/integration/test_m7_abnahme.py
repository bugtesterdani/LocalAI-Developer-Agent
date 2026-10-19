"""M7-Abnahme (Demo §16 ohne Modell) mit echter Hyper-V-VM – nur auf dem Windows-PC.

Voraussetzungen: Toolchains dotnet und python registriert (wie M4), für den Webteil Internet am Host.
Start (PowerShell, im Repository)::

    $env:LOCALAI_M7_ABNAHME = "1"
    # ohne Internet:  $env:LOCALAI_M7_WEB = "0"
    uv run --project src/python pytest -s tests/integration/test_m7_abnahme.py

Je Demo-Projekt (Kopien von samples/m7-demo-csharp und samples/m7-demo-python):

1. Snapshot → Tests in der VM: die Netzwerktests des Testcodes versuchen Internet, LAN und den Ollama-Port – alle
   Versuche scheitern (AC06, Demo „Netzversuch des Testcodes scheitert“).
2. CSV-Parser (RFC 4180) mit Unicode-, Leerwert- und Fehlerfalltests → grün.
3. Absichtlich falscher Test → genau dieser Fehler im Bericht (Auslöser für den Debugger im Modell-Lauf).
4. Workspace.Diff → Workspace.ExportPatch: der geprüfte Stand wird nach eigener Freigabe ins Original übernommen.

Dazu: RFC 4180 als Dokumentationsseite über Web.Fetch + Web.Content (zwei Freigaben), Audit-Prüfung (AC03: jede
Ausführung genau eine passende Erlaubnis), Versions-/Offline-Manifest vollständig (AC01), Diagnosepaket.

Ergebnis: ``%LOCALAPPDATA%\\LocalAi\\abnahme\\m7-abnahme.json``.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import time

import m7_referenz as ref
import pytest
from conftest import ROOT, start_broker
from localai.contracts import AgentRole, ProposalState
from test_m4_abnahme import log, report_of, request, run, vm_exit_text
from test_m5_abnahme import Repo

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("LOCALAI_M7_ABNAHME") != "1", reason="nur mit LOCALAI_M7_ABNAHME=1 auf dem Hyper-V-PC"),
]

WEB = os.environ.get("LOCALAI_M7_WEB", "1") != "0"
DOC_URL = "https://www.rfc-editor.org/rfc/rfc4180.txt"

DEMOS = {
    "csharp": {
        "toolchain": "dotnet",
        "parameters": {"project": "tests/Kontakte.Tests/Kontakte.Tests.csproj", "configuration": "Debug"},
        "changes": {"src/Kontakte/CsvParser.cs": ref.CS_PARSER, "tests/Kontakte.Tests/CsvParserTests.cs": ref.CS_TESTS},
        "failing": {"tests/Kontakte.Tests/AbsichtlichFalschTests.cs": ref.CS_FAILING},
        "network": "Testcode_erreicht_kein_Netz",
        "wrong": "Absichtlich_falsch",
    },
    "python": {
        "toolchain": "python",
        "parameters": {"path": "."},
        "changes": {"kontakte/csv_parser.py": ref.PY_PARSER, "tests/test_csv_parser.py": ref.PY_TESTS},
        "failing": {"tests/test_absichtlich_falsch.py": ref.PY_FAILING},
        "network": "test_testcode_erreicht_kein_netz",
        "wrong": "test_absichtlich_falsch",
    },
}


def reviewer(req):
    return req.model_copy(update={"agent": AgentRole.Reviewer})


def test_m7_abnahme():
    base = pathlib.Path(os.environ.get("LOCALAPPDATA", str(pathlib.Path.home()))) / "LocalAi" / "abnahme"
    base.mkdir(parents=True, exist_ok=True)
    data = base.parent / "broker-m7"
    data.mkdir(parents=True, exist_ok=True)
    registry = base.parent / "broker" / "toolchains.json"
    assert registry.exists(), f"Keine Toolchain-Registrierungen ({registry}) – in der Oberfläche unter TOOLCHAINS registrieren"
    (data / "toolchains.json").write_bytes(registry.read_bytes())
    rights = ["List", "Read", "Modify", "Create", "Execute"]
    grants = []
    for lang in DEMOS:
        target = base / f"m7-{lang}"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(ROOT / "samples" / f"m7-demo-{lang}", target,
                        ignore=shutil.ignore_patterns("bin", "obj", "__pycache__", ".pytest_cache"))
        grants.append({"grantId": lang, "root": str(target), "rights": rights, "recursive": True})
    project = {"schemaVersion": 1, "projectId": "m7-abnahme", "name": "M7-Abnahme", "grants": grants, "toolchains": ["dotnet", "python"]}
    project_file = data / "m7-projekt.json"
    project_file.write_text(json.dumps(project), encoding="utf-8")
    started = time.monotonic()
    broker = start_broker(data, "--mode", "hyperv", "--config", str(ROOT / "config" / "localai.json"), "--project", str(project_file))
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
        summary["sandbox"] = last

        manifest = ui.system_manifest()
        missing = [c for c in manifest["offline"] if c["required"] and not c["ok"]]
        summary["manifest"] = {"version": manifest["version"], "commit": manifest["commit"], "complete": manifest["complete"]}
        assert manifest["complete"], missing

        for lang, demo in DEMOS.items():
            log(f"{lang}: Snapshot, Netzversuch, CSV-Parser, absichtlicher Fehler, Übernahme …")
            root = base / f"m7-{lang}"
            repo = Repo(root)
            snap = run(ui, orch, request("Workspace.Snapshot", {"grantId": lang, "root": str(root)}, []))
            assert snap.state == ProposalState.Succeeded, snap
            base_id = snap.result.target_results[0].text

            def test(snapshot: str, key: str):
                t0 = time.monotonic()
                out = run(ui, orch, request("Toolchain.Test", {"grantId": lang, "toolchain": demo["toolchain"],  # noqa: B023
                                                               "parameters": demo["parameters"]}, [], snapshot))  # noqa: B023
                summary[f"{lang}_{key}_s"] = round(time.monotonic() - t0, 1)
                assert out.state == ProposalState.Succeeded, out
                report, text = report_of(out)
                (base / f"m7-{lang}-{key}.log").write_text(text, encoding="utf-8")
                return report, text

            # 1. Netzversuch des Testcodes scheitert (Tests grün = keine Verbindung möglich)
            report, text = test(base_id, "vorher")
            assert report.exit_code == 0 and report.failed == 0, (report, text[-3000:])
            assert report.total >= 6, report
            summary[f"{lang}_netz"] = "alle Verbindungsversuche gescheitert (4 Ziele inkl. Ollama-Port)"

            # 2. CSV-Parser mit Unicode-, Leerwert- und Fehlerfalltests
            patch = repo.patch(demo["changes"])
            paths = re.findall(r"^\+\+\+ b/(.+)$", patch, re.M)
            out = run(ui, orch, request("Patch.Apply", {"grantId": lang, "patch": patch}, paths, base_id))
            assert out.state == ProposalState.Succeeded, out
            good = next(r.text for r in out.result.target_results if r.target_id == "snapshot")
            report, text = test(good, "parser")
            assert report.exit_code == 0 and report.failed == 0 and report.total >= 10, (report, text[-3000:])

            # 3. Absichtlich falscher Test
            bad_patch = Repo(root).patch(demo["failing"])
            out = run(ui, orch, request("Patch.Apply", {"grantId": lang, "patch": bad_patch}, list(demo["failing"]), good))
            assert out.state == ProposalState.Succeeded, out
            bad = next(r.text for r in out.result.target_results if r.target_id == "snapshot")
            report, _ = test(bad, "fehler")
            assert report.failed == 1 and demo["wrong"] in report.failures[0].name, report

            # 4. Übernahme des geprüften Stands (ohne den falschen Test)
            out = run(ui, orch, reviewer(request("Workspace.Diff", {"grantId": lang, "baseSnapshotId": base_id}, [], good)))
            assert out.state == ProposalState.Succeeded, out
            preview = json.loads(next(r.text for r in out.result.target_results if r.target_id == "export"))
            assert sorted(f["path"] for f in preview["files"]) == sorted(demo["changes"]), preview["files"]
            out = run(ui, orch, reviewer(request("Workspace.ExportPatch", {"grantId": lang, "stagingId": preview["stagingId"],
                                                                           "patch": preview["patch"]},
                                                 [f["path"] for f in preview["files"]])), timeout=300)
            assert out.state == ProposalState.Succeeded, out
            for path in demo["changes"]:
                assert (root / path).exists(), path
            assert not any((root / p).exists() for p in demo["failing"])
            summary[f"{lang}_uebernommen"] = sorted(demo["changes"])
            log(f"{lang} ok")

        if WEB:
            log(f"Dokumentationsseite {DOC_URL} (zwei Freigaben) …")
            out = run(ui, orch, request("Web.Fetch", {"url": DOC_URL}, []), timeout=180)
            assert out.state == ProposalState.Succeeded, out
            capture = json.loads(next(r.text for r in out.result.target_results if r.target_id == "capture"))
            assert "text" not in capture
            out = run(ui, orch, request("Web.Content", {"captureId": capture["captureId"], "textSha256": capture["textSha256"]}, []))
            assert out.state == ProposalState.Succeeded, out
            text = next(r.text for r in out.result.target_results if r.target_id == "text")
            assert "Common Format and MIME Type" in text, text[:500]
            summary["web"] = {"url": DOC_URL, "chars": len(text), "proxy": capture.get("proxy") or "direkt",
                              "tlsIssuer": capture.get("tlsIssuer", "")}

        audit = ui.audit_verify()
        summary["audit"] = {k: audit.get(k) for k in ("ok", "proposals", "approvals", "invocations", "problems")}
        assert audit["ok"], audit["problems"]
        assert audit["invocations"] <= audit["approvals"]
        summary["diagnose"] = ui.diagnostics_export(False)["path"]
        summary["ok"] = True
        log(f"Fertig: {json.dumps(summary, ensure_ascii=False)}")
    finally:
        (base / "m7-abnahme.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        log("Broker wird beendet, " + vm_exit_text())
        broker.stop(timeout=300)
