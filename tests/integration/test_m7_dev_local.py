"""M7 mit echtem Broker-Prozess: Versionsmanifest, Audit-Prüfung (AC03), Diagnosepaket, Sicherung/Wiederherstellung
(Runbook) – nur über den UI-Token (SEC-17); Kommandozeile --verify-journal und --restore."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import zipfile

import pytest
from conftest import DEFAULT_DLL, start_broker
from localai.contracts import ProposalState
from localai.pipe import BrokerError
from test_m6_dev_local import ORIGINAL, setup
from test_sandbox_dev_local import approve_and_wait, req

pytestmark = pytest.mark.integration


def _dll() -> pathlib.Path:
    dll = pathlib.Path(os.environ.get("LOCALAI_BROKER_DLL", DEFAULT_DLL))
    if not dll.exists():
        pytest.skip(f"Broker nicht gebaut: {dll}")
    return dll


def test_system_funktionen_nur_fuer_die_oberflaeche(tmp_path, broker_factory):
    b, project = setup(tmp_path, broker_factory)
    ui, orch = b.ui(), b.orchestrator()
    for call in (orch.system_manifest, orch.audit_verify, orch.diagnostics_export, orch.backup_create):
        with pytest.raises(BrokerError) as err:
            call()
        assert err.value.code in (401, 403)

    manifest = ui.system_manifest()
    assert manifest["version"] and manifest["mode"] == "dev-local-unsafe" and "toolchains" in manifest
    assert any(c["item"].startswith("Python-Umgebung") for c in manifest["offline"])
    assert (tmp_path / "data" / "manifest.json").exists(), "beim Start und auf Abruf geschrieben"

    snap = approve_and_wait(ui, orch, orch.submit(req("Workspace.Snapshot", {"grantId": "p", "root": str(project)})))
    assert snap.state == ProposalState.Succeeded
    view = orch.submit(req("File.List", {"grantId": "p"}, ["."], snap.result.target_results[0].text))
    ui.decide(view.proposal.proposal_id, view.proposal_sha256, approve=False)

    audit = ui.audit_verify()
    assert audit["ok"] and audit["invocations"] == 1 and audit["approvals"] == 1 and audit["denials"] == 1, audit

    diag = ui.diagnostics_export(False)
    with zipfile.ZipFile(diag["path"]) as z:
        names = z.namelist()
        content = "".join(z.read(n).decode("utf-8", "replace") for n in names)
    assert "audit.json" in names and not any(n.startswith("protokolle/") for n in names)
    assert str(project) not in content.replace("\\\\", "\\") or "manifest.json" in names  # Projektpfad nur im Manifest
    assert "calc.py" not in content, "keine Inhalte/Argumente"

    backup = ui.backup_create()
    with zipfile.ZipFile(backup["path"]) as z:
        assert "daten/audit.db" in z.namelist() and "sicherung.json" in z.namelist()
    b.stop()

    # Kommandozeile: Journal prüfen, Sicherung wiederherstellen (Broker beendet)
    dll = _dll()
    out = subprocess.run(["dotnet", str(dll), "--data", str(tmp_path / "data"), "--verify-journal"], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["ok"] is True
    out = subprocess.run(["dotnet", str(dll), "--data", str(tmp_path / "data"), "--restore", backup["path"]], capture_output=True,
                         text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0 and "Wiederhergestellt" in out.stdout, out.stderr
    assert list((tmp_path / "data").glob("vor-wiederherstellung-*"))
    assert (project / "src" / "calc.py").read_bytes() == ORIGINAL

    # Nach dem Neustart: Broker startet mit dem wiederhergestellten Journal, alte Freigaben ungültig
    restarted = start_broker(tmp_path / "data", "--mode", "test")
    try:
        assert restarted.ui().audit_verify()["ok"]
    finally:
        restarted.stop()


def test_manipulierte_sicherung_wird_abgewiesen(tmp_path, broker_factory):
    b, _project = setup(tmp_path, broker_factory)
    backup = pathlib.Path(b.ui().backup_create()["path"])
    b.stop()
    bad = tmp_path / "boese.zip"
    with zipfile.ZipFile(backup) as src, zipfile.ZipFile(bad, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "daten/audit.db":
                data = data[:-10] + b"x" * 10
            dst.writestr(item, data)
    out = subprocess.run(["dotnet", str(_dll()), "--data", str(tmp_path / "data"), "--restore", str(bad)], capture_output=True,
                         text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 6 and "nichts geändert" in out.stderr
    assert not list((tmp_path / "data").glob("vor-wiederherstellung-*"))


@pytest.mark.parametrize("lang", ["csharp", "python"])
def test_demo_referenzaenderung_passt_und_wird_uebernommen(tmp_path, broker_factory, lang):
    """Die Referenzänderungen der M7-Abnahme lassen sich mit dem echten Worker anwenden und übernehmen."""
    import shutil

    import m7_referenz as ref
    from test_m5_abnahme import Repo
    from test_m6_dev_local import export_request, prepare

    b, project = setup(tmp_path, broker_factory)
    shutil.copytree(pathlib.Path(__file__).resolve().parents[2] / "samples" / f"m7-demo-{lang}", project, dirs_exist_ok=True)
    changes = ({"src/Kontakte/CsvParser.cs": ref.CS_PARSER, "tests/Kontakte.Tests/CsvParserTests.cs": ref.CS_TESTS} if lang == "csharp"
               else {"kontakte/csv_parser.py": ref.PY_PARSER, "tests/test_csv_parser.py": ref.PY_TESTS})
    ui, orch = b.ui(), b.orchestrator()
    snap = approve_and_wait(ui, orch, orch.submit(req("Workspace.Snapshot", {"grantId": "p", "root": str(project)})))
    base = snap.result.target_results[0].text
    patch = Repo(project).patch(changes)
    out = approve_and_wait(ui, orch, orch.submit(req("Patch.Apply", {"grantId": "p", "patch": patch}, list(changes), base)))
    assert out.state == ProposalState.Succeeded, out
    head = next(r.text for r in out.result.target_results if r.target_id == "snapshot")
    preview = prepare(ui, orch, base, head)
    assert sorted(f["path"] for f in preview["files"]) == sorted(changes)
    out = approve_and_wait(ui, orch, orch.submit(export_request(preview)))
    assert out.state == ProposalState.Succeeded, out
    for path, content in changes.items():
        assert (project / path).read_text(encoding="utf-8") == content
