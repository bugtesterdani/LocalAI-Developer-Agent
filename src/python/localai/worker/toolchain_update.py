"""Toolchains in einer laufenden VM neu installieren („Toolchain-Update“, ohne Neuaufbau von Windows).

Ändert sich eine registrierte Toolchain (z. B. fehlende Offline-Pakete beschafft und neu registriert), schickt der Host
einen **signierten** Job ``Sandbox.Toolchains`` mit einem ZIP der bereitgestellten Toolchain-Ordner
(``toolchain-src/<name>/…`` samt ``pinned.json``). Der Worker entpackt sie und lässt sie im Gast neu installieren
(Windows: ``toolchain-update.ps1`` → ``Install-Toolchain`` wie beim Aufbau, Hashes werden im Gast erneut geprüft),
danach stehen sie den Toolchain-Jobs wieder zur Verfügung.

Wie das Gast-Update ist der Job an Sitzungsschlüssel, Sequenz und Nutzlast-Hash gebunden; Agenten können ihn nie
auslösen (der Broker kennt kein Tool ``Sandbox.Toolchains``).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import zipfile

from localai import canonical
from localai.contracts import ResultStatus, SandboxJob, TargetResult, ToolResult
from localai.pathrules import PathRuleError, normalize_relative

TOOLCHAINS_TOOL = "Sandbox.Toolchains"
MAX_PAYLOAD_BYTES = 64 * 1024 ** 3
MAX_FILES = 200_000
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
PREFIX = "toolchain-src/"


class ToolchainUpdateError(Exception):
    pass


def _remove(path: pathlib.Path) -> None:
    if path.exists():
        shutil.rmtree(path, onexc=lambda f, p, _e: (os.chmod(p, 0o700), f(p)))


def extract(payload: pathlib.Path, target: pathlib.Path) -> list[str]:
    """Entpackt ``toolchain-src/<name>/…`` nach ``target/<name>/…``; liefert die Namen (jede braucht pinned.json)."""
    names: list[str] = []
    seen: set[str] = set()
    total = 0
    with zipfile.ZipFile(payload) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES:
            raise ToolchainUpdateError("zu viele Dateien")
        for info in infos:
            if info.is_dir():
                continue
            try:
                name = normalize_relative(info.filename)
            except PathRuleError as err:
                raise ToolchainUpdateError(f"unzulässiger Name {info.filename!r}: {err}") from err
            if not name.startswith(PREFIX) or name.count("/") < 2:
                raise ToolchainUpdateError(f"unerwartete Datei {name}")
            toolchain = name[len(PREFIX):].split("/", 1)[0]
            if not _NAME.match(toolchain):
                raise ToolchainUpdateError(f"unzulässiger Toolchain-Name {toolchain!r}")
            if name in seen:
                raise ToolchainUpdateError(f"Datei doppelt: {name}")
            total += info.file_size
            if total > MAX_PAYLOAD_BYTES:
                raise ToolchainUpdateError("Nutzlast zu groß")
            seen.add(name)
            if toolchain not in names:
                names.append(toolchain)
            dest = target.joinpath(*name[len(PREFIX):].split("/"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "xb") as out:
                shutil.copyfileobj(src, out)
    if not names:
        raise ToolchainUpdateError("keine Toolchain in der Nutzlast")
    for toolchain in names:
        if not (target / toolchain / "pinned.json").is_file() or not (target / toolchain / "toolchain.json").is_file():
            raise ToolchainUpdateError(f"{toolchain}: toolchain.json oder pinned.json fehlt")
    return names


def default_installer(guest_root: pathlib.Path) -> list[str] | None:
    """Windows-Gast: toolchain-update.ps1 (Install-Toolchain aus LocalAiGuest.psm1). Sonst keiner."""
    script = guest_root / "toolchain-update.ps1"
    if os.name != "nt" or not script.exists():
        return None
    powershell = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)]


def apply_toolchains(job: SandboxJob, payload: pathlib.Path | None, guest_root: pathlib.Path,
                     installer: list[str] | None = None, timeout: float = 4 * 3600) -> ToolResult:
    """Installiert die Toolchains der Nutzlast neu. Ergebnis je Toolchain; Failed, wenn eine scheitert."""
    source = guest_root / "toolchain-src"
    results: list[TargetResult] = []
    try:
        if payload is None:
            raise ToolchainUpdateError("Nutzlast fehlt")
        command = installer or default_installer(guest_root)
        if command is None:
            raise ToolchainUpdateError("Nachinstallieren gibt es nur im Windows-Gast (toolchain-update.ps1 fehlt) – VM neu aufbauen")
        _remove(source)
        source.mkdir(parents=True)
        names = extract(payload, source)
        report = guest_root / "work" / f"toolchain-update-{job.sequence}.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.unlink(missing_ok=True)
        run = subprocess.run([*command, "-Names", ",".join(names), "-SourceRoot", str(source), "-ResultFile", str(report)],
                             capture_output=True, timeout=timeout, check=False)
        output = (run.stdout + run.stderr).decode("utf-8", errors="replace")[-1500:]
        if not report.exists():
            raise ToolchainUpdateError(f"kein Ergebnis (Exitcode {run.returncode}): {output}")
        entries = json.loads(report.read_text(encoding="utf-8-sig"))
        report.unlink(missing_ok=True)
        if isinstance(entries, dict):
            entries = [entries]
        by_name = {str(e.get("name")): e for e in entries if isinstance(e, dict)}
        for name in names:
            entry = by_name.get(name) or {"error": "kein Ergebnis"}
            ok = bool(entry.get("installed")) and not entry.get("error")
            text = (f"{name} {entry.get('version', '')}: installiert, Selbsttest {entry.get('selfTest', '?')} "
                    f"({entry.get('seconds', 0)} s)" if ok else f"{name}: {entry.get('error') or 'nicht installiert'}")
            results.append(TargetResult(target_id=name, status=ResultStatus.Succeeded if ok else ResultStatus.Failed,
                                        message=text[:2000]))
        status = ResultStatus.Succeeded if all(r.status == ResultStatus.Succeeded for r in results) else ResultStatus.Failed
    except (ToolchainUpdateError, OSError, zipfile.BadZipFile, subprocess.TimeoutExpired, json.JSONDecodeError) as err:
        results = [TargetResult(target_id="*", status=ResultStatus.Failed, message=f"Toolchain-Update fehlgeschlagen: {err}"[:2000])]
        status = ResultStatus.Failed
    finally:
        _remove(source)
    return ToolResult(invocation_id=job.invocation_id, proposal_id=job.proposal.proposal_id, status=status,
                      snapshot_id=job.proposal.snapshot_id, target_results=results,
                      output_sha256=canonical.sha256_hex([r.message or "" for r in results]), truncated=False, untrusted=True,
                      exit_code=0 if status == ResultStatus.Succeeded else 1)
