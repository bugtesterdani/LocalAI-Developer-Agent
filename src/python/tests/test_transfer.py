"""Rückweg über die Transfer-VHDX: Containerformat, Laufwerksregeln, Worker im VM-Modus."""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from localai.contracts import ResultEnvelope, ToolTarget
from localai.worker.__main__ import TransferSink, main, pending_jobs, process_one
from localai.worker.transfer import (
    HEADER_SIZE,
    Signal,
    TransferDisk,
    TransferError,
    TransferKind,
    build_container,
    parse_container,
)
from test_worker import KEY, envelope, env, job, proposal  # noqa: F401  (Fixture)

ROOT = pathlib.Path(__file__).resolve().parents[3]
VECTOR = json.loads((ROOT / "contracts" / "testvectors" / "transfer.json").read_text(encoding="utf-8"))
SIZE = 1024 * 1024


def test_container_entspricht_gemeinsamem_vektor():
    image = build_container(TransferKind(VECTOR["kind"]), VECTOR["sequence"], VECTOR["payloadUtf8"].encode())
    assert image[:64].hex() == VECTOR["headerHex"]
    assert len(image) == VECTOR["imageLength"]
    assert hashlib.sha256(image).hexdigest() == VECTOR["imageSha256"]
    assert parse_container(image) == (TransferKind.RESULT, 42, VECTOR["payloadUtf8"].encode())


def test_leeres_laufwerk_und_kaputte_container():
    assert parse_container(bytes(HEADER_SIZE)) is None
    image = bytearray(build_container(TransferKind.REPORT, 0, b"abc"))
    image[HEADER_SIZE] ^= 1
    with pytest.raises(TransferError):
        parse_container(bytes(image))
    with pytest.raises(TransferError):
        build_container(TransferKind.RESULT, 1, bytes(16 * 1024 * 1024 + 1))


def disk(tmp_path, size=SIZE) -> TransferDisk:
    path = tmp_path / "transfer.raw"
    path.write_bytes(bytes(size))
    return TransferDisk(str(path), SIZE)


def test_laufwerk_wird_genau_einmal_beschrieben(tmp_path):
    d = disk(tmp_path)
    d.write(TransferKind.RESULT, 7, b"hallo")
    assert parse_container(pathlib.Path(d.path).read_bytes()) == (TransferKind.RESULT, 7, b"hallo")
    with pytest.raises(TransferError, match="nicht leer"):
        d.write(TransferKind.RESULT, 8, b"zweites")


def test_falsche_groesse_wird_nicht_beschrieben(tmp_path):
    d = disk(tmp_path, size=SIZE * 2)
    with pytest.raises(TransferError, match="Bytes"):
        d.write(TransferKind.RESULT, 1, b"x")
    assert not any(pathlib.Path(d.path).read_bytes()[:HEADER_SIZE])


def test_worker_vm_modus_wartet_auf_ready_und_meldet(env, tmp_path):  # noqa: F811
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    j = job(proposal("File.Read", [ToolTarget(target_id="t1", path="README.md")], env["snap"]), env["state"].last_sequence + 1)
    stem = f"{j.sequence:012d}-{j.job_id}"
    (inbox / f"{stem}.job.json").write_bytes(envelope(j))
    assert pending_jobs(inbox, require_ready=True) == []
    (inbox / f"{stem}.ready").write_text("", encoding="ascii")
    [job_file] = pending_jobs(inbox, require_ready=True)

    d = disk(tmp_path)
    signal = tmp_path / "signal.txt"
    process_one(job_file, TransferSink(d, Signal(signal)), KEY, env["state"], env["runner"])
    kind, seq, payload = parse_container(pathlib.Path(d.path).read_bytes())
    assert (kind, seq) == (TransferKind.RESULT, j.sequence)
    assert ResultEnvelope.model_validate_json(payload).result.target_results[0].text.startswith("# Demo")
    assert signal.read_text(encoding="ascii") == f"result:{j.sequence}"
    assert list(inbox.iterdir()) == []


def test_abgelehnter_job_im_vm_modus(env, tmp_path):  # noqa: F811
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    j = job(proposal("File.Read", [ToolTarget(target_id="t1", path="README.md")], env["snap"]), env["state"].last_sequence + 1)
    bad = inbox / f"{j.sequence:012d}-{j.job_id}.job.json"
    bad.write_bytes(envelope(j, key=b"z" * 32))
    d = disk(tmp_path)
    process_one(bad, TransferSink(d, Signal(tmp_path / "s")), KEY, env["state"], env["runner"])
    kind, seq, payload = parse_container(pathlib.Path(d.path).read_bytes())
    assert (kind, seq) == (TransferKind.REJECTED, j.sequence) and "Signatur" in payload.decode()


def test_bericht_wird_uebergeben(tmp_path):
    report = tmp_path / "report.json"
    report.write_text('{"ok": true}', encoding="utf-8")
    d = disk(tmp_path)
    signal = tmp_path / "signal.txt"
    assert main(["--report", str(report), "--transfer-disk", d.path, "--transfer-size", str(SIZE),
                 "--signal-file", str(signal)]) == 0
    assert parse_container(pathlib.Path(d.path).read_bytes()) == (TransferKind.REPORT, 0, b'{"ok": true}')
    assert signal.read_text(encoding="ascii") == "report:0"
