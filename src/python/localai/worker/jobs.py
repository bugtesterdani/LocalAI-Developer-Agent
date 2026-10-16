"""Prüfung signierter Jobs (Spezifikation §8: signiert, sequenziert, größenbegrenzt, gegen Wiederholung geschützt)."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import pathlib
from dataclasses import dataclass

from localai import canonical
from localai.contracts import SandboxJob, proposal_sha256

MAX_JOB_BYTES = 4 * 1024 * 1024
MAX_PAYLOAD_BYTES = 1024 * 1024 * 1024


class JobRejected(Exception):
    """Job wird nicht ausgeführt (Signatur, Sequenz, Ablauf, Größe …)."""


def compute_mac(job_obj: dict, key: bytes) -> str:
    return hmac.new(key, canonical.canonical_bytes(job_obj), hashlib.sha256).hexdigest()


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.UTC)


@dataclass
class WorkerState:
    """Persistenter Zustand: Sitzung und zuletzt verarbeitete Sequenznummer."""

    path: pathlib.Path
    session_id: str | None = None
    last_sequence: int = 0

    @classmethod
    def load(cls, path: pathlib.Path) -> WorkerState:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(path, data.get("sessionId"), int(data.get("lastSequence", 0)))
        return cls(path)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sessionId": self.session_id, "lastSequence": self.last_sequence}), encoding="utf-8")
        tmp.replace(self.path)


def verify_job(raw: bytes, key: bytes, state: WorkerState, now: dt.datetime | None = None) -> SandboxJob:
    """Prüft Größe, Signatur, Sitzung, Sequenz, Ablauf und Vorschlags-Hash. Wirft JobRejected."""
    if len(raw) > MAX_JOB_BYTES:
        raise JobRejected("Job zu groß")
    try:
        envelope = canonical.parse(raw)
    except (ValueError, canonical.CanonicalJsonError) as err:
        raise JobRejected(f"Job ist kein gültiges JSON: {err}") from err
    if not isinstance(envelope, dict) or set(envelope) != {"job", "mac"}:
        raise JobRejected("Job-Umschlag hat unerwartete Felder")
    job_obj, mac = envelope["job"], envelope["mac"]
    if not isinstance(mac, str) or not hmac.compare_digest(compute_mac(job_obj, key), mac):
        raise JobRejected("Signatur ungültig")

    job = SandboxJob.model_validate(job_obj)
    if job.schema_version != 1:
        raise JobRejected(f"Schemaversion {job.schema_version} nicht unterstützt")
    if state.session_id is not None and job.session_id != state.session_id:
        raise JobRejected("Job gehört zu einer anderen Sitzung")
    if job.sequence <= state.last_sequence:
        raise JobRejected(f"Wiederholter oder veralteter Job (Sequenz {job.sequence} ≤ {state.last_sequence})")
    now = now or dt.datetime.now(dt.UTC)
    if now > parse_time(job.expires_at):
        raise JobRejected("Job ist abgelaufen")
    if proposal_sha256(job.proposal) != job.proposal_sha256:
        raise JobRejected("Vorschlags-Hash passt nicht zum Vorschlag")

    # Erst nach allen Prüfungen verbrauchen: ein abgelehnter Job verschiebt die Sequenz nicht.
    state.session_id = job.session_id
    state.last_sequence = job.sequence
    state.save()
    return job


def verify_payload(path: pathlib.Path | None, expected_sha256: str | None) -> None:
    if expected_sha256 is None:
        if path is not None and path.exists():
            raise JobRejected("Unerwartete Nutzlast")
        return
    if path is None or not path.exists():
        raise JobRejected("Nutzlast fehlt")
    if path.stat().st_size > MAX_PAYLOAD_BYTES:
        raise JobRejected("Nutzlast zu groß")
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise JobRejected("Hash der Nutzlast stimmt nicht")
