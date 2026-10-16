"""Tests des Sandbox-Workers (SEC-02/-04/-05/-18 aus Sicht der Sandbox)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import stat
import uuid
import zipfile

import pytest

from localai import canonical
from localai.contracts import (
    AgentRole, ResultEnvelope, ResultStatus, SandboxJob, SnapshotFile, SnapshotManifest, ToolProposal, ToolTarget,
    proposal_sha256,
)
from localai.pathrules import PathRuleError, normalize_relative
from localai.worker.__main__ import DirectorySink, process_one
from localai.worker.jobs import JobRejected, WorkerState, compute_mac, verify_job
from localai.worker.snapshot import SnapshotError, SnapshotStore, snapshot_id
from localai.worker.tools import ToolRunner

ROOT = pathlib.Path(__file__).resolve().parents[3]
KEY = bytes(range(32))
FMT = "%Y-%m-%dT%H:%M:%S.%f"


def ts(delta: float = 0) -> str:
    return (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delta)).strftime(FMT)[:-3] + "Z"


# ---------------------------------------------------------------- Pfadregeln (gemeinsame Vektoren)
PATHS = json.loads((ROOT / "contracts" / "testvectors" / "paths.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("vector", PATHS["valid"], ids=lambda v: v["input"])
def test_pfadregeln_gueltig(vector):
    assert normalize_relative(vector["input"]) == vector["normalized"]


@pytest.mark.parametrize("path", PATHS["invalid"])
def test_pfadregeln_ungueltig(path):
    with pytest.raises(PathRuleError):
        normalize_relative(path)


def test_wurzel_nur_wenn_erlaubt():
    assert normalize_relative(".", allow_root=True) == "."
    with pytest.raises(PathRuleError):
        normalize_relative(".")


# ---------------------------------------------------------------- Hilfen
def make_snapshot(tmp: pathlib.Path, files: dict[str, bytes], grant: str = "g1") -> tuple[SnapshotManifest, pathlib.Path]:
    entries = sorted(
        ({"path": p, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()} for p, d in files.items()),
        key=lambda e: e["path"])
    manifest = SnapshotManifest(schema_version=1, snapshot_id=snapshot_id(grant, entries), grant_id=grant,
                                created_at=ts(), files=[SnapshotFile.model_validate(e) for e in entries])
    archive = tmp / f"{uuid.uuid4().hex}.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for p, d in files.items():
            zf.writestr(p, d)
    return manifest, archive


def proposal(tool: str, targets: list[ToolTarget], snap: str = "none", **args) -> ToolProposal:
    return ToolProposal(
        schema_version=1, proposal_id=str(uuid.uuid4()), run_id="r", task_id="t", agent=AgentRole.Coder, tool=tool,
        tool_version="1", purpose="Test", arguments={"grantId": "g1", **args}, targets=targets, snapshot_id=snap,
        policy_revision=1, sandbox_id="sbx", created_at=ts(), expires_at=ts(300))


def job(prop: ToolProposal, seq: int, session: str = "s1", expires: float = 60, **extra) -> SandboxJob:
    return SandboxJob(schema_version=1, job_id=str(uuid.uuid4()), sequence=seq, session_id=session,
                      invocation_id=str(uuid.uuid4()), proposal=prop, proposal_sha256=proposal_sha256(prop),
                      issued_at=ts(), expires_at=ts(expires), **extra)


def envelope(j: SandboxJob, key: bytes = KEY) -> bytes:
    obj = j.to_json_obj()
    return json.dumps({"job": obj, "mac": compute_mac(obj, key)}).encode("utf-8")


@pytest.fixture
def env(tmp_path):
    store = SnapshotStore(tmp_path / "work")
    state = WorkerState.load(tmp_path / "work" / "state.json")
    files = {
        "README.md": "# Demo\nZeile 2\nZeile 3\n".encode(),
        "src/Parser.cs": b"class Parser { /* TODO: Unicode */ }\n",
        "src/Csv/Tokenizer.cs": b"class Tokenizer {}\n",
        "docs/\xc3\x9cbersicht.md".encode("latin-1").decode("utf-8"): "Größe und Übernahme\n".encode(),
    }
    manifest, archive = make_snapshot(tmp_path, files)
    runner = ToolRunner(store)
    snap_job = job(proposal("Workspace.Snapshot", [], root="X"), 1, snapshot=manifest,
                   payload_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    result = runner.run(verify_job(envelope(snap_job), KEY, state), archive)
    assert result.status == ResultStatus.Succeeded, result
    return {"runner": runner, "state": state, "snap": manifest.snapshot_id, "tmp": tmp_path, "store": store}


# ---------------------------------------------------------------- Job-Prüfung
def test_snapshot_import_meldet_id_und_statistik(env):
    assert env["store"].get(env["snap"], "g1").files["README.md"][0] == 23


def test_falsche_signatur_wird_abgelehnt(env):
    j = job(proposal("File.Read", [ToolTarget(target_id="t1", path="README.md")], env["snap"]), 2)
    with pytest.raises(JobRejected, match="Signatur"):
        verify_job(envelope(j, key=b"x" * 32), KEY, env["state"])


def test_manipulierter_job_wird_abgelehnt(env):
    j = job(proposal("File.Read", [ToolTarget(target_id="t1", path="README.md")], env["snap"]), 2)
    raw = json.loads(envelope(j))
    raw["job"]["proposal"]["targets"][0]["path"] = "src/Parser.cs"  # nach dem Signieren geändert
    with pytest.raises(JobRejected, match="Signatur"):
        verify_job(json.dumps(raw).encode(), KEY, env["state"])


def test_wiederholung_und_alte_sequenz_werden_abgelehnt(env):
    j = job(proposal("File.Read", [ToolTarget(target_id="t1", path="README.md")], env["snap"]), 2)
    verify_job(envelope(j), KEY, env["state"])
    with pytest.raises(JobRejected, match="Wiederholt"):
        verify_job(envelope(j), KEY, env["state"])
    older = job(proposal("File.Read", [ToolTarget(target_id="t1", path="README.md")], env["snap"]), 1)
    with pytest.raises(JobRejected, match="Wiederholt"):
        verify_job(envelope(older), KEY, env["state"])


def test_abgelaufener_und_fremde_sitzung_werden_abgelehnt(env):
    expired = job(proposal("File.Read", [ToolTarget(target_id="t1", path="README.md")], env["snap"]), 5, expires=-1)
    with pytest.raises(JobRejected, match="abgelaufen"):
        verify_job(envelope(expired), KEY, env["state"])
    other = job(proposal("File.Read", [ToolTarget(target_id="t1", path="README.md")], env["snap"]), 6, session="s2")
    with pytest.raises(JobRejected, match="Sitzung"):
        verify_job(envelope(other), KEY, env["state"])


def test_vorschlags_hash_muss_passen(env):
    prop = proposal("File.Read", [ToolTarget(target_id="t1", path="README.md")], env["snap"])
    j = job(prop, 3).model_copy(update={"proposal_sha256": "0" * 64})
    with pytest.raises(JobRejected, match="Vorschlags-Hash"):
        verify_job(envelope(j), KEY, env["state"])


# ---------------------------------------------------------------- Snapshot-Import
@pytest.mark.parametrize("bad_name", ["../ausbruch.txt", "/abs.txt", "a/../../b.txt", "x:stream", "CON"])
def test_zip_slip_und_unzulaessige_namen(tmp_path, bad_name):
    manifest, _ = make_snapshot(tmp_path, {"ok.txt": b"x"})
    archive = tmp_path / "boese.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("ok.txt", b"x")
        zf.writestr(bad_name, b"boese")
    with pytest.raises(SnapshotError):
        SnapshotStore(tmp_path / "w").import_archive(manifest, archive)
    assert not (tmp_path / "ausbruch.txt").exists()


def test_zusaetzliche_fehlende_und_veraenderte_dateien(tmp_path):
    manifest, archive = make_snapshot(tmp_path, {"a.txt": b"aaa", "b.txt": b"bbb"})
    extra = tmp_path / "extra.zip"
    with zipfile.ZipFile(extra, "w") as zf:
        zf.writestr("a.txt", b"aaa"); zf.writestr("b.txt", b"bbb"); zf.writestr("c.txt", b"ccc")  # noqa: E702
    with pytest.raises(SnapshotError, match="außerhalb des Manifests"):
        SnapshotStore(tmp_path / "w1").import_archive(manifest, extra)
    missing = tmp_path / "missing.zip"
    with zipfile.ZipFile(missing, "w") as zf:
        zf.writestr("a.txt", b"aaa")
    with pytest.raises(SnapshotError, match="unvollständig"):
        SnapshotStore(tmp_path / "w2").import_archive(manifest, missing)
    changed = tmp_path / "changed.zip"
    with zipfile.ZipFile(changed, "w") as zf:
        zf.writestr("a.txt", b"aaa"); zf.writestr("b.txt", b"BBB")  # noqa: E702
    with pytest.raises(SnapshotError, match="stimmt nicht"):
        SnapshotStore(tmp_path / "w3").import_archive(manifest, changed)


def test_verknuepfung_im_archiv_wird_abgelehnt(tmp_path):
    manifest, _ = make_snapshot(tmp_path, {"link": b"/etc/passwd"})
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, b"/etc/passwd")
    with pytest.raises(SnapshotError, match="Verknüpfung"):
        SnapshotStore(tmp_path / "w").import_archive(manifest, archive)


def test_snapshot_id_muss_zum_manifest_passen(tmp_path):
    manifest, archive = make_snapshot(tmp_path, {"a.txt": b"a"})
    forged = manifest.model_copy(update={"snapshot_id": "f" * 64})
    with pytest.raises(SnapshotError, match="snapshotId"):
        SnapshotStore(tmp_path / "w").import_archive(forged, archive)


# ---------------------------------------------------------------- Tools
def run(env, tool, targets, **args):
    j = job(proposal(tool, targets, env["snap"], **args), env["state"].last_sequence + 1)
    return env["runner"].run(verify_job(envelope(j), KEY, env["state"]), None)


def test_lesen_mit_zeilenbereich_und_hashpruefung(env):
    readme_sha = hashlib.sha256("# Demo\nZeile 2\nZeile 3\n".encode()).hexdigest()
    result = run(env, "File.ReadMany", [
        ToolTarget(target_id="t1", path="README.md", line_start=2, line_end=3, expected_sha256=readme_sha),
        ToolTarget(target_id="t2", path="src/Parser.cs", expected_sha256="0" * 64),
        ToolTarget(target_id="t3", path="gibt/es/nicht.cs"),
        ToolTarget(target_id="t4", path="docs/Übersicht.md"),
    ])
    by_id = {r.target_id: r for r in result.target_results}
    assert by_id["t1"].text == "Zeile 2\nZeile 3\n"
    assert by_id["t2"].status == ResultStatus.Failed and "Hash" in by_id["t2"].message
    assert by_id["t3"].status == ResultStatus.Failed and "nicht im Snapshot" in by_id["t3"].message
    assert by_id["t4"].text == "Größe und Übernahme\n"
    assert result.untrusted is True and result.status == ResultStatus.Failed


def test_lesen_wird_bei_maxbytes_gekuerzt(env):
    result = run(env, "File.Read", [ToolTarget(target_id="t1", path="README.md")], maxBytes=6)
    assert result.target_results[0].text == "# Demo" and result.truncated


def test_auflisten_und_suchen(env):
    listing = run(env, "File.List", [ToolTarget(target_id="t1", path=".")], depth=1)
    text = listing.target_results[0].text
    assert "README.md  (23 Bytes)" in text and "src/Parser.cs" in text and "src/Csv/ …" in text
    found = run(env, "File.Search", [ToolTarget(target_id="t1", path="src")], query="unicode")
    assert found.target_results[0].text.startswith("src/Parser.cs:1:")


def test_unbekanntes_tool_und_falscher_grant(env):
    assert run(env, "Shell.Execute", []).status == ResultStatus.Failed
    j = job(proposal("File.Read", [ToolTarget(target_id="t1", path="README.md")], env["snap"]).model_copy(
        update={"arguments": {"grantId": "anderer"}}), env["state"].last_sequence + 1)
    result = env["runner"].run(verify_job(envelope(j), KEY, env["state"]), None)
    assert "anderen Ordner-Grant" in result.target_results[0].message


def test_process_one_schreibt_ergebnis_oder_ablehnung(env):
    inbox, outbox = env["tmp"] / "in", env["tmp"] / "out"
    inbox.mkdir(); outbox.mkdir()  # noqa: E702
    j = job(proposal("File.Read", [ToolTarget(target_id="t1", path="README.md")], env["snap"]), env["state"].last_sequence + 1)
    good = inbox / f"{j.sequence}-{j.job_id}.job.json"
    good.write_bytes(envelope(j))
    process_one(good, DirectorySink(outbox), KEY, env["state"], env["runner"])
    result = ResultEnvelope.model_validate_json((outbox / f"{j.job_id}.result.json").read_text(encoding="utf-8"))
    assert result.result.target_results[0].text.startswith("# Demo") and not good.exists()

    bad = inbox / "99-x.job.json"
    bad.write_bytes(envelope(j, key=b"y" * 32))
    process_one(bad, DirectorySink(outbox), KEY, env["state"], env["runner"])
    assert "Signatur" in (outbox / "99-x.job.json.rejected.txt").read_text(encoding="utf-8")


def test_kanonischer_mac_ist_unabhaengig_von_formatierung(env):
    j = job(proposal("File.Read", [ToolTarget(target_id="t1", path="README.md")], env["snap"]), 50)
    obj = j.to_json_obj()
    pretty = json.dumps({"mac": compute_mac(obj, KEY), "job": obj}, indent=4, ensure_ascii=True).encode()
    assert verify_job(pretty, KEY, env["state"]).job_id == j.job_id
    assert canonical.canonicalize(obj)  # kanonisierbar
