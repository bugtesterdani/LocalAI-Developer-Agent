"""Nachschlageordner mit echtem Broker und Worker (dev-local-unsafe): eigener schreibgeschützter Snapshot,
Reference.Search/Read nur dort, nie Schreiben."""

from __future__ import annotations

import pytest

from localai.contracts import ProposalState, ResultStatus
from localai.pipe import BrokerError

from test_orchestrator_broker import setup
from test_sandbox_dev_local import approve_and_wait, req

pytestmark = pytest.mark.integration


def test_nachschlageordner_suchen_und_lesen(tmp_path, broker_factory):
    hilfe = tmp_path / "hilfe" / "api"
    hilfe.mkdir(parents=True)
    (hilfe / "createfile.htm").write_bytes(b"<HTML><HEAD><TITLE>CreateFile</TITLE><H1>CreateFile</H1>"
                                           b"<P>Creates or opens a file. GENERIC_READ gives read access.")
    (hilfe / "closehandle.htm").write_bytes(b"<title>CloseHandle</title><p>Closes an open handle.</p>")
    b = setup(tmp_path, broker_factory, ["-c", "import time; time.sleep(120)"], rights=("List", "Read", "Modify"),
              project_extra={"references": [{"referenceId": "win32", "root": str(tmp_path / "hilfe"), "title": "Win32"}]})
    ui, orch = b.ui(), b.orchestrator()
    grants = {g.grant_id: g for g in orch.grants()}
    ref = grants["ref-win32"]
    assert ref.reference and ref.title == "Win32" and [r.value for r in ref.rights] == ["List", "Read"]

    snap = approve_and_wait(ui, orch, orch.submit(req("Workspace.Snapshot", {"grantId": "ref-win32", "root": ref.root})))
    assert snap.state == ProposalState.Succeeded
    snapshot = snap.result.target_results[0].text

    found = approve_and_wait(ui, orch, orch.submit(req("Reference.Search", {"grantId": "ref-win32", "query": "GENERIC_READ"},
                                                       snapshot=snapshot)))
    assert found.state == ProposalState.Succeeded, found
    text = found.result.target_results[0].text
    assert "win32:api/createfile.htm – CreateFile" in text and "closehandle" not in text

    page = approve_and_wait(ui, orch, orch.submit(req("Reference.Read", {"grantId": "ref-win32", "maxChars": 2000},
                                                      ["api/createfile.htm"], snapshot)))
    assert page.result.target_results[0].status == ResultStatus.Succeeded
    assert "Creates or opens a file." in page.result.target_results[0].text

    # Nie schreiben, und Reference.* nicht im Projektordner.
    with pytest.raises(BrokerError) as err:
        orch.submit(req("Patch.Apply", {"grantId": "ref-win32", "patch": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"}, ["x"],
                        snapshot))
    assert err.value.code == 403
    with pytest.raises(BrokerError) as err:
        orch.submit(req("Reference.Search", {"grantId": "p", "query": "x"}, snapshot=snapshot))
    assert err.value.code in (403, 409)
