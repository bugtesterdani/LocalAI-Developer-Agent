"""M6 im Worker: Workspace.Diff liefert die Änderungen zwischen Basis und abgeleitetem Snapshot (nur lesend)."""

from __future__ import annotations

import base64
import json

from localai.contracts import ResultStatus, ToolTarget
from localai.worker.snapshot import SnapshotStore
from localai.worker.tools import ToolRunner
from tests.test_worker import job, make_snapshot, proposal


def test_workspace_diff_liefert_geaenderte_dateien_mit_inhalt(tmp_path):
    store = SnapshotStore(tmp_path / "work")
    manifest, archive = make_snapshot(tmp_path, {"a.py": b"x = 1\r\n", "b.py": b"weg\n", "c.py": b"bleibt\n"})
    base = store.import_archive(manifest, archive)
    runner = ToolRunner(store)
    patch = ("--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
             "--- a/b.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-weg\n"
             "--- /dev/null\n+++ b/neu.py\n@@ -0,0 +1 @@\n+neu\n")
    targets = [ToolTarget(target_id=f"t{i}", path=p) for i, p in enumerate(["a.py", "b.py", "neu.py"], 1)]
    applied = runner.run(job(proposal("Patch.Apply", targets, base.snapshot_id, patch=patch), 1), None)
    assert applied.status == ResultStatus.Succeeded
    derived = next(r.text for r in applied.target_results if r.target_id == "snapshot")
    result = runner.run(job(proposal("Workspace.Diff", [], derived, baseSnapshotId=base.snapshot_id), 2), None)
    assert result.status == ResultStatus.Succeeded
    changes = json.loads(next(r.text for r in result.target_results if r.target_id == "changes"))
    by_path = {c["path"]: c for c in changes["files"]}
    assert set(by_path) == {"a.py", "b.py", "neu.py"}, "unveränderte Dateien fehlen"
    assert by_path["b.py"]["kind"] == "Delete" and "content" not in by_path["b.py"]
    assert by_path["neu.py"]["kind"] == "Create"
    assert base64.b64decode(by_path["a.py"]["content"]) == b"x = 2\r\n", "Zeilenenden bleiben erhalten"
    assert changes["baseSnapshotId"] == base.snapshot_id and changes["snapshotId"] == derived


def test_workspace_diff_braucht_bekannte_basis(tmp_path):
    store = SnapshotStore(tmp_path / "work")
    manifest, archive = make_snapshot(tmp_path, {"a.py": b"1\n"})
    base = store.import_archive(manifest, archive)
    result = ToolRunner(store).run(job(proposal("Workspace.Diff", [], base.snapshot_id, baseSnapshotId="f" * 64), 1), None)
    assert result.status == ResultStatus.Failed
