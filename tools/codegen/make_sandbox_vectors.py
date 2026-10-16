"""Erzeugt contracts/testvectors/sandbox.json: Snapshot-ID und Job-Signatur (Python-Referenz).

C# prüft SnapshotBuilder.ComputeSnapshotId und JobSigner.ComputeMac gegen diese Werte.
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "python"))

from localai.worker.jobs import compute_mac  # noqa: E402
from localai.worker.snapshot import snapshot_id  # noqa: E402

files = [
    {"path": "README.md", "size": 23, "sha256": "ab" * 32},
    {"path": "docs/Übersicht.md", "size": 0, "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
    {"path": "src/Parser.cs", "size": 4096, "sha256": "01" * 32},
]
key = bytes(range(32))
proposal = {
    "schemaVersion": 1, "proposalId": "00000000-0000-4000-8000-000000000001", "runId": "r", "taskId": "t",
    "agent": "Coder", "tool": "File.Read", "toolVersion": "1", "purpose": "Größe prüfen",
    "arguments": {"grantId": "demo", "maxBytes": 1024},
    "targets": [{"targetId": "t1", "path": "src/Parser.cs", "lineStart": 1, "lineEnd": 10}],
    "snapshotId": "s" * 64, "policyRevision": 1, "sandboxId": "sbx", "createdAt": "2026-10-16T12:00:00.000Z",
    "expiresAt": "2026-10-16T12:05:00.000Z",
}
from localai import canonical  # noqa: E402

job = {
    "schemaVersion": 1, "jobId": "00000000-0000-4000-8000-0000000000aa", "sequence": 7, "sessionId": "sess",
    "invocationId": "00000000-0000-4000-8000-0000000000bb", "proposal": proposal,
    "proposalSha256": canonical.sha256_hex(proposal), "issuedAt": "2026-10-16T12:00:00.000Z",
    "expiresAt": "2026-10-16T12:05:00.000Z",
}
out = {
    "snapshot": {"grantId": "demo", "files": files, "snapshotId": snapshot_id("demo", files)},
    "job": {"keyHex": key.hex(), "job": job, "mac": compute_mac(job, key)},
}
path = ROOT / "contracts" / "testvectors" / "sandbox.json"
path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"geschrieben: {path}")
