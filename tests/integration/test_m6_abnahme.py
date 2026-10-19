"""M6-Abnahme mit echter Hyper-V-VM und echtem Netz (nur auf dem Windows-PC).

Voraussetzungen (docs/pruefung-M6.md): Toolchains dotnet und python registriert (wie M4), Internetzugang des Hosts.
Start (PowerShell, im Repository)::

    $env:LOCALAI_M6_ABNAHME = "1"
    uv run --project src/python pytest -s tests/integration/test_m6_abnahme.py

Optional: ``$env:LOCALAI_M6_URL`` (öffentliche Seite, Standard https://example.com/), ``$env:LOCALAI_M6_WEB = "0"``
(ohne Internet: Webteil entfällt).

Geprüft wird ohne Modell, direkt über den Broker (jede Aktion ein genehmigter Vorschlag):

1. SEC-11: Kopie von samples/m4-python → Snapshot → Patch → pytest in der VM grün → Workspace.Diff → Übernahme mit
   Abwahl einer Datei; nur die freigegebene Datei ändert sich, das Original liegt in der Sicherung; manipulierte
   Übernahme wird abgewiesen.
2. SEC-08/09/10: echter Abruf (Fetcher und Content Gate als getrennte Prozesse niedriger Integrität, Quarantäne als
   *Low* gekennzeichnet), Text erst nach zweiter Freigabe; URL-Prüfung (privat, localhost, IP-Schreibweisen, Port,
   Zugangsdaten); DNS auf private Adresse (localtest.me → 127.0.0.1) wird beim Abruf verweigert; Weiterleitung wird
   nicht verfolgt.
3. SEC-15: Not-Aus während eines langen Tests in der VM → VM wird ausgeschaltet und gesperrt, offene Vorschläge
   ungültig, nach dem Lösen läuft nichts ohne neue Freigabe. **Danach wird die VM beim nächsten Start neu aufgebaut.**

Ergebnis: ``%LOCALAPPDATA%\\LocalAi\\abnahme\\m6-abnahme.json``.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import time

import pytest
from conftest import ROOT, start_broker
from localai.contracts import AgentRole, ProposalState
from localai.pipe import BrokerError
from test_m4_abnahme import PY_PATCH, log, report_of, request, run, vm_exit_text

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("LOCALAI_M6_ABNAHME") != "1", reason="nur mit LOCALAI_M6_ABNAHME=1 auf dem Hyper-V-PC"),
]

WEB = os.environ.get("LOCALAI_M6_WEB", "1") != "0"
URL = os.environ.get("LOCALAI_M6_URL", "https://example.com/")
SLOW_TEST = """--- /dev/null
+++ b/tests/test_lang.py
@@ -0,0 +1,5 @@
+import time
+
+
+def test_lang():
+    time.sleep(900)
"""


def reviewer(req):
    return req.model_copy(update={"agent": AgentRole.Reviewer})


def test_m6_abnahme():
    base = pathlib.Path(os.environ.get("LOCALAPPDATA", str(pathlib.Path.home()))) / "LocalAi" / "abnahme"
    base.mkdir(parents=True, exist_ok=True)
    data = base.parent / "broker-m6"
    data.mkdir(parents=True, exist_ok=True)
    registry = base.parent / "broker" / "toolchains.json"
    assert registry.exists(), f"Keine Toolchain-Registrierungen ({registry}) – in der Oberfläche unter TOOLCHAINS registrieren"
    (data / "toolchains.json").write_bytes(registry.read_bytes())
    # Eigene Kopie: die Übernahme schreibt in den Projektordner, das Beispiel im Repository bleibt unverändert.
    project_dir = base / "m6-projekt"
    if project_dir.exists():
        shutil.rmtree(project_dir)
    shutil.copytree(ROOT / "samples" / "m4-python", project_dir, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    original = (project_dir / "rechner" / "core.py").read_bytes()
    rights = ["List", "Read", "Modify", "Create", "Execute"]
    project = {"schemaVersion": 1, "projectId": "m6-abnahme", "name": "M6-Abnahme",
               "grants": [{"grantId": "py", "root": str(project_dir), "rights": rights, "recursive": True}],
               "toolchains": ["dotnet", "python"]}
    project_file = data / "m6-projekt.json"
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

        # 1. SEC-11 Übernahme
        log("SEC-11: Snapshot, Patch, pytest, Übernahme …")
        snap = run(ui, orch, request("Workspace.Snapshot", {"grantId": "py", "root": str(project_dir)}, []))
        assert snap.state == ProposalState.Succeeded, snap
        base_id = snap.result.target_results[0].text
        paths = re.findall(r"^\+\+\+ b/(.+)$", PY_PATCH, re.M)
        out = run(ui, orch, request("Patch.Apply", {"grantId": "py", "patch": PY_PATCH}, paths, base_id))
        assert out.state == ProposalState.Succeeded, out
        head = next(r.text for r in out.result.target_results if r.target_id == "snapshot")
        out = run(ui, orch, request("Toolchain.Test", {"grantId": "py", "toolchain": "python", "parameters": {"path": "."}}, [], head))
        report, text = report_of(out)
        assert report.exit_code == 0 and report.failed == 0, (report, text[-3000:])
        out = run(ui, orch, reviewer(request("Workspace.Diff", {"grantId": "py", "baseSnapshotId": base_id}, [], head)))
        assert out.state == ProposalState.Succeeded, out
        preview = json.loads(next(r.text for r in out.result.target_results if r.target_id == "export"))
        assert {f["path"]: f["kind"] for f in preview["files"]} == {"rechner/core.py": "Modify", "tests/test_teilen.py": "Create"}
        assert (project_dir / "rechner" / "core.py").read_bytes() == original
        export_args = {"grantId": "py", "stagingId": preview["stagingId"], "patch": preview["patch"]}
        forged = reviewer(request("Workspace.ExportPatch", {**export_args, "patch": preview["patch"].replace("Teilen durch 0", "x")},
                                  [f["path"] for f in preview["files"]]))
        with pytest.raises(BrokerError) as err:
            orch.submit(forged)
        assert err.value.code == 422, err.value
        view = orch.submit(reviewer(request("Workspace.ExportPatch", export_args, [f["path"] for f in preview["files"]])))
        keep = [t.target_id for t in view.proposal.targets if t.path == "rechner/core.py"]
        narrowed = ui.narrow(view.proposal.proposal_id, view.proposal_sha256, keep)
        ui.decide(narrowed.proposal.proposal_id, narrowed.proposal_sha256, approve=True)
        out = orch.wait_final(narrowed.proposal.proposal_id, timeout=120, poll=0.5)
        assert out.state == ProposalState.Succeeded, out
        assert b"def teilen" in (project_dir / "rechner" / "core.py").read_bytes()
        assert not (project_dir / "tests" / "test_teilen.py").exists(), "abgewählte Datei bleibt draußen"
        note = next(r.text for r in out.result.target_results if r.target_id == "export")
        backup = pathlib.Path(note.split("Sicherung: ", 1)[1].strip())
        assert (backup / "rechner" / "core.py").read_bytes() == original
        summary["sec11"] = {"exported": ["rechner/core.py"], "backup": str(backup)}
        log("SEC-11 ok")

        # 2. SEC-08/09/10 Web
        if WEB:
            log(f"SEC-09: Abruf {URL} …")
            before = ui.status()["pending"]
            for bad in ("http://example.com/", "https://127.0.0.1/", "https://localhost/", "https://[::1]/", "https://10.0.0.1/",
                        "https://192.168.1.1/", "https://169.254.169.254/latest/meta-data/", "https://0x7f000001/",
                        "https://2130706433/", "https://user:pw@example.com/", "https://example.com:8443/", "https://intranet/",
                        "https://router.local/", "file:///C:/Windows/win.ini"):
                with pytest.raises(BrokerError) as err:
                    orch.submit(request("Web.Fetch", {"url": bad}, []))
                assert err.value.code in (403, 422), (bad, err.value)
            assert ui.status()["pending"] == before
            t0 = time.monotonic()
            out = run(ui, orch, request("Web.Fetch", {"url": URL}, []), timeout=180)
            assert out.state == ProposalState.Succeeded, out
            capture = json.loads(next(r.text for r in out.result.target_results if r.target_id == "capture"))
            assert "text" not in capture, "nach der ersten Freigabe kein Text"
            summary["web_fetch_s"] = round(time.monotonic() - t0, 1)
            quarantine = data / "web" / "quarantine" / capture["captureId"]
            label = subprocess.run(["icacls", str(quarantine)], capture_output=True, text=True, errors="replace").stdout
            summary["quarantine_label"] = "Low" if "Low Mandatory Level" in label or "Niedrige" in label else label[-300:]
            assert summary["quarantine_label"] == "Low", label
            out = run(ui, orch, request("Web.Content", {"captureId": capture["captureId"], "textSha256": capture["textSha256"]}, []))
            assert out.state == ProposalState.Succeeded, out
            text = next(r.text for r in out.result.target_results if r.target_id == "text")
            assert text and "<script" not in text.lower()
            summary["web_text_chars"] = len(text)
            with pytest.raises(BrokerError) as err:
                orch.submit(request("Web.Content", {"captureId": capture["captureId"], "textSha256": "0" * 64}, []))
            assert err.value.code == 409
            log("SEC-10: DNS auf private Adresse …")
            out = run(ui, orch, request("Web.Fetch", {"url": "https://localtest.me/"}, []), timeout=120)
            message = " ".join(r.message or "" for r in out.result.target_results) if out.result else ""
            assert out.state != ProposalState.Succeeded and ("öffentlich" in message or "public" in message.lower()), (out.state, message)
            summary["sec10_dns"] = message[:200]
            out = run(ui, orch, request("Web.Fetch", {"url": "https://google.com/"}, []), timeout=120)
            info = json.loads(next(r.text for r in out.result.target_results if r.target_id == "capture")) if out.result else {}
            summary["redirect"] = info.get("redirect", "")
            assert info.get("redirect"), ("Weiterleitung erwartet", out)
            log("SEC-08/09/10 ok")

        # 3. SEC-15 Not-Aus während eines langen Tests
        log("SEC-15: langer Test in der VM, dann Not-Aus …")
        slow = run(ui, orch, request("Patch.Apply", {"grantId": "py", "patch": SLOW_TEST}, ["tests/test_lang.py"], head))
        assert slow.state == ProposalState.Succeeded, slow
        slow_id = next(r.text for r in slow.result.target_results if r.target_id == "snapshot")
        view = orch.submit(request("Toolchain.Test", {"grantId": "py", "toolchain": "python", "parameters": {"path": "."}}, [], slow_id))
        ui.decide(view.proposal.proposal_id, view.proposal_sha256, approve=True)
        waiting = orch.submit(request("Repo.Index", {"grantId": "py", "maxChars": 4000}, [], slow_id))  # bleibt offen
        deadline = time.monotonic() + 600
        while ui.get(view.proposal.proposal_id).state != ProposalState.Running:
            assert time.monotonic() < deadline, "Test startet nicht"
            time.sleep(1)
        time.sleep(20)
        t0 = time.monotonic()
        ui.emergency_stop()
        out = orch.wait_final(view.proposal.proposal_id, timeout=300, poll=0.5)
        summary["notaus_s"] = round(time.monotonic() - t0, 1)
        assert out.state != ProposalState.Succeeded, out
        status = ui.status().get("sandbox") or ""
        summary["notaus_status"] = status
        assert status.startswith("gesperrt") and "ausgeschaltet" in status, status
        assert ui.get(waiting.proposal.proposal_id).state == ProposalState.Invalidated
        vm = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-VM -Name LocalAi-Sandbox).State"], capture_output=True,
                            text=True, errors="replace").stdout.strip()
        summary["vm_state"] = vm
        ui.release()
        with pytest.raises(BrokerError):
            # Alte Freigaben gelten nicht: der ungültige Vorschlag kann nicht mehr erlaubt werden.
            ui.decide(waiting.proposal.proposal_id, waiting.proposal_sha256, approve=True)
        try:
            out = run(ui, orch, request("Toolchain.Test", {"grantId": "py", "toolchain": "python", "parameters": {"path": "."}}, [], head),
                      timeout=120)
            assert out.state != ProposalState.Succeeded, "gesperrte VM führt nichts aus"
        except BrokerError as err:
            summary["nach_notaus"] = f"abgewiesen ({err.code})"
        summary["ok"] = True
        log(f"Fertig: {json.dumps(summary, ensure_ascii=False)}")
    finally:
        (base / "m6-abnahme.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        log("Broker wird beendet, " + vm_exit_text())
        broker.stop(timeout=300)
