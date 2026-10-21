"""Sandbox-Worker: verarbeitet signierte Jobs aus einem Eingangsordner.

Entwicklung (dev-local-unsafe)::

    python -m localai.worker --inbox DIR --outbox DIR --work DIR --key-file DATEI

In der VM (hyperv)::

    python -I -m localai.worker --inbox C:\\LocalAi\\inbox --work C:\\LocalAi\\work --key-file ...
        --transfer-disk auto --transfer-size BYTES --require-ready

Eingang: ``<sequenz>-<jobId>.job.json`` und optional ``<sequenz>-<jobId>.payload.zip``. Lokal legt der Host
den Job zuletzt atomar ab; in der VM kopiert Copy-VMFile Nutzlast, Job und zuletzt ``<…>.ready`` – erst
dann wird der Job angefasst (``--require-ready``).

Ausgang lokal: ``<jobId>.result.json`` bzw. ``<dateiname>.rejected.txt``. In der VM: ein Container auf der
Transfer-VHDX und das Signal ``result:<sequenz>``.

Bericht nach der Provisionierung (nur VM)::

    python -I -m localai.worker --report BERICHT.json --transfer-disk auto --transfer-size BYTES
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from dataclasses import dataclass

from localai.contracts import ResultEnvelope
from localai.worker.execution import Executor, ToolchainCatalog
from localai.worker.jobs import JobRejected, WorkerState, verify_job, verify_payload
from localai.worker.repoindex import RepoIndexer
from localai.worker.snapshot import SnapshotStore
from localai.worker.tools import ToolRunner
from localai.worker.transfer import Signal, TransferDisk, TransferError, TransferKind
from localai.worker.update import RESTART_EXIT_CODE, UPDATE_TOOL, apply_update


def _write_atomic(path: pathlib.Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _sequence_of(name: str) -> int:
    try:
        return int(name.split("-", 1)[0])
    except ValueError:
        return 0


class DirectorySink:
    """Antworten als Dateien im Ausgangsordner (dev-local-unsafe)."""

    def __init__(self, outbox: pathlib.Path) -> None:
        self.outbox = outbox
        outbox.mkdir(parents=True, exist_ok=True)

    def result(self, envelope: ResultEnvelope) -> None:
        _write_atomic(self.outbox / f"{envelope.job_id}.result.json", json.dumps(envelope.to_json_obj(), ensure_ascii=False))

    def rejected(self, job_name: str, sequence: int, reason: str) -> None:
        _write_atomic(self.outbox / f"{job_name}.rejected.txt", reason)


class TransferSink:
    """Antworten über die Transfer-VHDX plus Signal (VM)."""

    def __init__(self, disk: TransferDisk, signal: Signal) -> None:
        self.disk = disk
        self.signal = signal

    def result(self, envelope: ResultEnvelope) -> None:
        payload = json.dumps(envelope.to_json_obj(), ensure_ascii=False).encode("utf-8")
        self._send(TransferKind.RESULT, envelope.sequence, payload)

    def rejected(self, job_name: str, sequence: int, reason: str) -> None:
        self._send(TransferKind.REJECTED, sequence, reason.encode("utf-8"))

    def _send(self, kind: TransferKind, sequence: int, payload: bytes) -> None:
        self.disk.write(kind, sequence, payload)
        self.signal.send(f"result:{sequence}")


@dataclass
class UpdateTarget:
    """Wohin ein Gast-Update schreibt (VM: C:\\LocalAi und das installierte Paket; Tests: eigene Ordner)."""

    guest_root: pathlib.Path
    package_dir: pathlib.Path


def process_one(job_file: pathlib.Path, sink, key: bytes, state: WorkerState, runner: ToolRunner,
                update: UpdateTarget | None = None) -> bool:
    """Verarbeitet einen Job. True = Worker soll sich nach einem Gast-Update neu starten."""
    restart = False
    stem = job_file.name.removesuffix(".job.json")
    payload = job_file.with_name(stem + ".payload.zip")
    ready = job_file.with_name(stem + ".ready")
    try:
        job = verify_job(job_file.read_bytes(), key, state)
        verify_payload(payload if payload.exists() else None, job.payload_sha256)
    except JobRejected as err:
        sink.rejected(job_file.name, _sequence_of(job_file.name), str(err))
        print(f"[Worker] abgelehnt: {job_file.name}: {err}", flush=True)
    else:
        if job.proposal.tool == UPDATE_TOOL:
            if update is None:
                result = runner.run(job, None)  # unbekanntes Tool → Failed
            else:
                result = apply_update(job, payload if payload.exists() else None, update.guest_root, update.package_dir)
                restart = result.status.value == "Succeeded"
        else:
            result = runner.run(job, payload if payload.exists() else None)
        sink.result(ResultEnvelope(job_id=job.job_id, sequence=job.sequence, result=result))
        print(f"[Worker] {job.proposal.tool} {job.job_id[:8]}: {result.status.value}", flush=True)
    finally:
        job_file.unlink(missing_ok=True)
        payload.unlink(missing_ok=True)
        ready.unlink(missing_ok=True)
    return restart


def pending_jobs(inbox: pathlib.Path, require_ready: bool) -> list[pathlib.Path]:
    jobs = sorted(inbox.glob("*.job.json"), key=lambda p: _sequence_of(p.name))
    if require_ready:
        jobs = [j for j in jobs if j.with_name(j.name.removesuffix(".job.json") + ".ready").exists()]
    return jobs


def run(inbox: pathlib.Path, sink, work: pathlib.Path, key: bytes, once: bool, poll: float,
        require_ready: bool = False, executor: Executor | None = None, update: UpdateTarget | None = None,
        osdebug=None, inbound=None) -> int:
    """Verarbeitet Jobs; Rückgabe 75 = nach Gast-Update neu starten, sonst 0."""
    for d in (inbox, work):
        d.mkdir(parents=True, exist_ok=True)
    state = WorkerState.load(work / "state.json")
    runner = ToolRunner(SnapshotStore(work), executor, RepoIndexer(work), osdebug)
    print(f"[Worker] bereit, Eingang {inbox}", flush=True)
    while True:
        if inbound is not None:
            inbound.poll(inbox)
        for job_file in pending_jobs(inbox, require_ready):
            try:
                if process_one(job_file, sink, key, state, runner, update):
                    print("[Worker] Gast-Update angewendet – Neustart", flush=True)
                    if osdebug is not None:
                        osdebug.stop_all()
                    return RESTART_EXIT_CODE
            except (TransferError, OSError) as err:
                # Ohne Rückweg kann nichts gemeldet werden; der Host läuft in seine Zeitgrenze.
                print(f"[Worker] Rückgabe fehlgeschlagen: {err}", flush=True)
        if once:
            return 0
        if osdebug is not None:
            osdebug.reap()
        time.sleep(poll)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LocalAi Sandbox-Worker")
    parser.add_argument("--inbox", type=pathlib.Path)
    parser.add_argument("--outbox", type=pathlib.Path)
    parser.add_argument("--work", type=pathlib.Path)
    parser.add_argument("--key-file", type=pathlib.Path)
    parser.add_argument("--transfer-disk", help="'auto' (VM) oder Pfad einer Rohdatei (Tests)")
    parser.add_argument("--transfer-size", type=int, default=0)
    parser.add_argument("--signal-file", type=pathlib.Path, help="nur Tests: Signal als Datei statt KVP")
    parser.add_argument("--require-ready", action="store_true")
    parser.add_argument("--report", type=pathlib.Path, help="Bericht der Provisionierung zurückgeben und beenden")
    parser.add_argument("--once", action="store_true", help="vorhandene Jobs verarbeiten und beenden (Tests)")
    parser.add_argument("--poll-ms", type=int, default=100)
    parser.add_argument("--toolchains", type=pathlib.Path,
                        help="nur VM: Manifeste/Paketquellen der installierten Toolchains – schaltet Toolchain.* frei")
    parser.add_argument("--toolchain-root", type=pathlib.Path, help="nur Tests: Installationsort statt C:\\Toolchains")
    parser.add_argument("--guest-root", type=pathlib.Path,
                        help="Gast-Update: Ordner für Gastskripte (Standard: übergeordneter Ordner von --work)")
    parser.add_argument("--package-dir", type=pathlib.Path, help="Gast-Update: installiertes Paket localai (nur Tests)")
    parser.add_argument("--tools-user", help="Linux-Gast: Werkzeugprozesse als dieser Benutzer, ohne Netz (z. B. localai-tools)")
    parser.add_argument("--jobs-root", type=pathlib.Path, help="Arbeitsordner der Toolchain-Jobs (Standard: <work>/jobs)")
    parser.add_argument("--osdebug", action="store_true", help="OsDebug.* (QEMU) freischalten – Linux-Gast bzw. dev-local")
    parser.add_argument("--emu-user", help="Linux-Gast: QEMU als dieser Benutzer, ohne Netz (z. B. localai-emu)")
    parser.add_argument("--qemu", help="nur Tests: Pfad zu qemu-system-x86_64")
    parser.add_argument("--kvp-pool", type=pathlib.Path, help="nur Tests: KVP-Pool-Datei statt /var/lib/hyperv/.kvp_pool_1")
    parser.add_argument("--inbound-disk",
                        help="Jobs vom Eingangs-Laufwerk übernehmen (inbound: Disk): 'auto' = Laufwerke der VM, "
                             "sonst Ordner mit Rohdateien (Tests)")
    parser.add_argument("--no-low-integrity", action="store_true",
                        help="Werkzeugprozesse ohne niedrige Integritätsstufe starten (schwächer, nur zur Fehlersuche)")
    args = parser.parse_args(argv)

    transfer = None
    if args.transfer_disk:
        if args.transfer_size <= 0:
            parser.error("--transfer-size fehlt")
        transfer = TransferSink(TransferDisk(args.transfer_disk, args.transfer_size), Signal(args.signal_file, args.kvp_pool))

    if args.report:
        if transfer is None:
            parser.error("--report braucht --transfer-disk")
        data = args.report.read_bytes()
        transfer.disk.write(TransferKind.REPORT, 0, data)
        transfer.signal.send("report:0")
        print(f"[Worker] Bericht übergeben ({len(data)} Bytes)", flush=True)
        return 0

    if not (args.inbox and args.work and args.key_file) or (transfer is None) == (args.outbox is None):
        parser.error("--inbox, --work, --key-file und genau eines von --outbox / --transfer-disk sind nötig")
    key = bytes.fromhex(args.key_file.read_text(encoding="ascii").strip())
    if len(key) < 32:
        print("Schlüssel zu kurz", file=sys.stderr)
        return 2
    sink = transfer if transfer is not None else DirectorySink(args.outbox)
    executor = None
    if args.toolchains:
        tools_user = None
        if args.tools_user:
            from localai.worker.linproc import ToolUser
            tools_user = ToolUser.lookup(args.tools_user)
        executor = Executor(ToolchainCatalog(args.toolchains, args.toolchain_root), args.jobs_root or args.work / "jobs",
                            low_integrity=not args.no_low_integrity, tools_user=tools_user)
    import localai
    update = None
    if transfer is not None:
        # Gast-Update nur im VM-Modus (Transfer-Laufwerk); im Entwicklungsmodus ersetzt der Worker nie Code.
        update = UpdateTarget(args.guest_root or args.work.resolve().parent,
                              args.package_dir or pathlib.Path(localai.__file__).resolve().parent)
    osdebug = None
    if args.osdebug:
        from localai.worker.osdebug import OsDebugManager
        emu_user = None
        if args.emu_user:
            from localai.worker.linproc import ToolUser
            emu_user = ToolUser.lookup(args.emu_user)
        osdebug = OsDebugManager((args.jobs_root or args.work / "jobs"), emu_user, args.qemu, executor)
    try:
        inbound = None
        if args.inbound_disk:
            from localai.worker.inbound import InboundDisks
            args.work.mkdir(parents=True, exist_ok=True)
            if args.inbound_disk == "auto":
                inbound = InboundDisks(args.work / "inbound.json")
            else:
                folder = pathlib.Path(args.inbound_disk)
                inbound = InboundDisks(args.work / "inbound.json", lambda: [str(p) for p in sorted(folder.iterdir())])
        return run(args.inbox, sink, args.work, key, args.once, args.poll_ms / 1000, args.require_ready, executor, update, osdebug,
                   inbound)
    finally:
        if osdebug is not None and not args.once:
            osdebug.stop_all()


if __name__ == "__main__":
    sys.exit(main())
