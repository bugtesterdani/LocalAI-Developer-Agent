"""M4 mit echtem Broker und echtem Worker (dev-local-unsafe):

* Patch.Apply ändert nur die Sandbox-Kopie (abgeleiteter Snapshot), nie den Projektordner; Abwahl pro Datei.
* Toolchain.* wird außerhalb der VM immer abgelehnt (keine Ausführung auf dem Host, SEC-20).
* Repo.Index liefert die Repo-Map; ein vollständiger Run nutzt sie und wendet die Patches an.
"""

from __future__ import annotations

import pathlib

import pytest

from localai.contracts import ActivityKind, ProposalState, ResultStatus
from localai.orchestrator.fake_ollama import FakeOllama
from localai.pipe import BrokerError

from test_orchestrator_broker import collect, setup, summary_done
from test_sandbox_dev_local import approve_and_wait, req

pytestmark = pytest.mark.integration
ROOT = pathlib.Path(__file__).resolve().parents[2]
RIGHTS = ("List", "Read", "Modify", "Create", "Execute")
PATCH = """--- a/src/calc.py
+++ b/src/calc.py
@@ -1,2 +1,4 @@
 def add(a, b):
+    if a is None or b is None:
+        raise ValueError("leer")
     return a + b
--- /dev/null
+++ b/tests/test_calc.py
@@ -0,0 +1,2 @@
+def test_add():
+    pass
"""


@pytest.fixture
def fake_ollama():
    fake = FakeOllama(delay=0.001).start()
    yield fake
    fake.stop()


def sandbox(tmp_path, broker_factory):
    b = setup(tmp_path, broker_factory, ["-c", "import time; time.sleep(120)"], rights=RIGHTS)
    ui, orch = b.ui(), b.orchestrator()
    root = orch.grants()[0].root
    outcome = approve_and_wait(ui, orch, orch.submit(req("Workspace.Snapshot", {"grantId": "p", "root": root})))
    assert outcome.state == ProposalState.Succeeded
    return ui, orch, outcome.result.target_results[0].text, pathlib.Path(root)


def test_patch_nur_in_der_sandbox_mit_abwahl(tmp_path, broker_factory):
    ui, orch, snap, root = sandbox(tmp_path, broker_factory)
    view = orch.submit(req("Patch.Apply", {"grantId": "p", "patch": PATCH}, ["src/calc.py", "tests/test_calc.py"], snap))
    # Abwahl der Testdatei → neuer Vorschlag nur mit src/calc.py
    narrowed = ui.narrow(view.proposal.proposal_id, view.proposal_sha256, ["t1"])
    outcome = approve_and_wait(ui, orch, narrowed)
    assert outcome.state == ProposalState.Succeeded, outcome
    derived = next(r.text for r in outcome.result.target_results if r.target_id == "snapshot")
    assert derived != snap
    read = approve_and_wait(ui, orch, orch.submit(req("File.ReadMany", {"grantId": "p"}, ["src/calc.py", "tests/test_calc.py"], derived)))
    by_id = {r.target_id: r for r in read.result.target_results}
    assert "raise ValueError" in by_id["t1"].text
    assert by_id["t2"].status == ResultStatus.Failed, "abgewählte Datei wurde nicht angelegt"
    # Projektordner auf dem Host unverändert
    assert (root / "src" / "calc.py").read_bytes() == b"def add(a, b):\n    return a + b\n"
    assert not (root / "tests").exists()


def test_patch_ziele_und_rechte_im_broker(tmp_path, broker_factory):
    _ui, orch, snap, _root = sandbox(tmp_path, broker_factory)
    with pytest.raises(BrokerError) as err:
        orch.submit(req("Patch.Apply", {"grantId": "p", "patch": PATCH}, ["src/calc.py"], snap))
    assert err.value.code == 422, "Ziele müssen genau die Dateien des Patches sein"
    with pytest.raises(BrokerError) as err:
        orch.submit(req("Patch.Apply", {"grantId": "p", "patch": "--- a/x\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"}, ["x"], snap))
    assert err.value.code == 403, "Löschen braucht das Recht Delete"


def test_sec20_toolchain_nie_auf_dem_host(tmp_path, broker_factory):
    ui, orch, snap, _root = sandbox(tmp_path, broker_factory)
    assert orch.toolchains_available() == {"enabled": False, "reason": "Toolchain-Aktionen nur in der Betriebsart hyperv bzw. qemu",
                                           "toolchains": []}
    for tool, args in (("Toolchain.Test", {"toolchain": "python", "parameters": {"path": "."}}),
                       ("Toolchain.Run", {"toolchain": "python", "parameters": {"script": "src/calc.py"}})):
        with pytest.raises(BrokerError) as err:
            orch.submit(req(tool, {"grantId": "p", **args}, [], snap))
        assert err.value.code == 403 and "hyperv" in err.value.message
    with pytest.raises(BrokerError) as err:
        orch.submit(req("Shell.Execute", {"grantId": "p", "command": "calc.exe"}, [], snap))
    assert err.value.code == 403
    assert ui.status()["pending"] == 0


def test_repo_index_liefert_map(tmp_path, broker_factory):
    ui, orch, snap, _root = sandbox(tmp_path, broker_factory)
    outcome = approve_and_wait(ui, orch, orch.submit(req("Repo.Index", {"grantId": "p", "maxChars": 4000}, [], snap)))
    assert outcome.state == ProposalState.Succeeded, outcome
    text = outcome.result.target_results[0].text
    assert "src/calc.py [Python" in text and "function add:1" in text


def test_run_wendet_patches_an_und_meldet_fehlende_vm(tmp_path, broker_factory, fake_ollama):
    b = setup(tmp_path, broker_factory, ["-m", "localai.orchestrator", "--models", "{models}", "--ollama", fake_ollama.url],
              rights=RIGHTS)
    ui = b.ui()
    ui.chat_send("Bitte leere Eingaben in add abfangen")
    events, _ = collect(ui, 0, summary_done, timeout=120)
    chats = [e.text or "" for e in events if e.kind == ActivityKind.Chat]
    assert any("nur in der Offline-VM" in c for c in chats), "Hinweis: keine Tests ohne VM"
    coder = next(e.text for e in events if e.agent == "Coder" and e.kind == ActivityKind.Chat)
    assert "angewendet → Stand" in coder, coder
    summary = [c for c in chats if c.startswith("Zusammenfassung")][-1]
    assert "Patch(es) angewendet" in summary and "nicht ausgeführt" in summary
    project = tmp_path / "projekt"
    assert (project / "src" / "calc.py").read_bytes() == b"def add(a, b):\n    return a + b\n"
    tools = [e.text or "" for e in events if e.kind == ActivityKind.Proposal]
    assert any("Repo-Map" in t for t in tools) and not any("Toolchain" in t for t in tools)
    assert ui.orchestrator_status()["running"]
