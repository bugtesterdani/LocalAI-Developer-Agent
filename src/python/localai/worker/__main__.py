"""Sandbox-Worker: verarbeitet signierte Jobs aus einem Eingangsordner.

Aufruf:
    python -m localai.worker --inbox DIR --outbox DIR --work DIR --key-file DATEI [--once]

Dateien im Eingang: ``<sequenz>-<jobId>.job.json`` und optional ``<sequenz>-<jobId>.payload.zip``.
Der Host schreibt die Nutzlast zuerst und den Job zuletzt (atomar umbenannt).
Ausgang: ``<jobId>.result.json`` bzw. ``<dateiname>.rejected.txt`` bei abgelehnten Jobs.

In der VM (M2b) laufen Eingang/Ausgang über Copy-VMFile bzw. die Transfer-VHDX; der Worker
selbst ist identisch.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from localai.contracts import ResultEnvelope
from localai.worker.jobs import JobRejected, WorkerState, verify_job, verify_payload
from localai.worker.snapshot import SnapshotStore
from localai.worker.tools import ToolRunner


def _write_atomic(path: pathlib.Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _sequence_of(name: str) -> int:
    try:
        return int(name.split("-", 1)[0])
    except ValueError:
        return 0


def process_one(job_file: pathlib.Path, outbox: pathlib.Path, key: bytes, state: WorkerState,
                runner: ToolRunner) -> None:
    payload = job_file.with_name(job_file.name.removesuffix(".job.json") + ".payload.zip")
    try:
        job = verify_job(job_file.read_bytes(), key, state)
        verify_payload(payload if payload.exists() else None, job.payload_sha256)
    except JobRejected as err:
        _write_atomic(outbox / f"{job_file.name}.rejected.txt", str(err))
        print(f"[Worker] abgelehnt: {job_file.name}: {err}", flush=True)
    else:
        result = runner.run(job, payload if payload.exists() else None)
        envelope = ResultEnvelope(job_id=job.job_id, sequence=job.sequence, result=result)
        _write_atomic(outbox / f"{job.job_id}.result.json", json.dumps(envelope.to_json_obj(), ensure_ascii=False))
        print(f"[Worker] {job.proposal.tool} {job.job_id[:8]}: {result.status.value}", flush=True)
    finally:
        job_file.unlink(missing_ok=True)
        payload.unlink(missing_ok=True)


def run(inbox: pathlib.Path, outbox: pathlib.Path, work: pathlib.Path, key: bytes, once: bool, poll: float) -> None:
    for d in (inbox, outbox, work):
        d.mkdir(parents=True, exist_ok=True)
    state = WorkerState.load(work / "state.json")
    runner = ToolRunner(SnapshotStore(work))
    print(f"[Worker] bereit, Eingang {inbox}", flush=True)
    while True:
        jobs = sorted(inbox.glob("*.job.json"), key=lambda p: _sequence_of(p.name))
        for job_file in jobs:
            process_one(job_file, outbox, key, state, runner)
        if once:
            return
        time.sleep(poll)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LocalAi Sandbox-Worker")
    parser.add_argument("--inbox", type=pathlib.Path, required=True)
    parser.add_argument("--outbox", type=pathlib.Path, required=True)
    parser.add_argument("--work", type=pathlib.Path, required=True)
    parser.add_argument("--key-file", type=pathlib.Path, required=True)
    parser.add_argument("--once", action="store_true", help="vorhandene Jobs verarbeiten und beenden (Tests)")
    parser.add_argument("--poll-ms", type=int, default=100)
    args = parser.parse_args(argv)
    key = bytes.fromhex(args.key_file.read_text(encoding="ascii").strip())
    if len(key) < 32:
        print("Schlüssel zu kurz", file=sys.stderr)
        return 2
    run(args.inbox, args.outbox, args.work, key, args.once, args.poll_ms / 1000)
    return 0


if __name__ == "__main__":
    sys.exit(main())
