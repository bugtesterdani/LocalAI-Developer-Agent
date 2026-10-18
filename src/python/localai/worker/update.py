"""Aktualisierung einer laufenden VM durch den Host („Gast-Update“, nur mit reuseVm).

Ändern sich nur Worker-Code oder Gastskripte (nicht Windows-Abbild, Python-Laufzeit, VM-Einstellungen oder
Toolchains), muss die VM nicht neu aufgebaut werden. Der Host schickt dann einen **signierten** Job
``Sandbox.Update`` mit einem ZIP:

* ``localai/…`` – das Python-Paket des Workers (ersetzt das installierte Paket vollständig),
* ``guest/provision.ps1``, ``guest/LocalAiGuest.psm1``, ``guest/update.ps1`` – Gastskripte,
* ``guest-config.json``.

Der Job ist wie jeder andere an Sitzungsschlüssel, Sequenz und Nutzlast-Hash gebunden; Agenten können ihn nie
auslösen (der Broker kennt kein Tool ``Sandbox.Update``). Nach dem Austausch läuft ``update.ps1`` (idempotente
Einrichtungsschritte), dann beendet sich der Worker mit Exitcode 75 und ``worker.cmd`` startet ihn mit dem neuen Code.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import zipfile

from localai import canonical
from localai.contracts import ResultStatus, SandboxJob, TargetResult, ToolResult
from localai.pathrules import PathRuleError, normalize_relative

UPDATE_TOOL = "Sandbox.Update"
RESTART_EXIT_CODE = 75
GUEST_FILES = {"guest/provision.ps1": "provision.ps1", "guest/LocalAiGuest.psm1": "LocalAiGuest.psm1",
               "guest/update.ps1": "update.ps1", "guest-config.json": "guest-config.json"}
MAX_FILES = 2000
MAX_BYTES = 64 * 1024 * 1024


class UpdateError(Exception):
    pass


def _remove(path: pathlib.Path) -> None:
    if path.exists():
        shutil.rmtree(path, onexc=lambda f, p, _e: (os.chmod(p, 0o700), f(p)))


def apply_update(job: SandboxJob, payload: pathlib.Path | None, guest_root: pathlib.Path, package_dir: pathlib.Path,
                 run_script: bool = True) -> ToolResult:
    """Tauscht Paket und Skripte aus; Fehler → Failed (der Host baut dann neu auf)."""
    try:
        if payload is None:
            raise UpdateError("Nutzlast fehlt")
        staging = guest_root / "update" / f"staging-{job.sequence}"
        _remove(staging)
        staging.mkdir(parents=True)
        names = _extract(payload, staging)
        if not any(n.startswith("localai/") for n in names) or "localai/__init__.py" not in names:
            raise UpdateError("Paket localai fehlt in der Nutzlast")
        # 1. Paket tauschen: neu daneben, altes beiseite, umbenennen (kurzes Fenster, Worker verarbeitet nichts parallel).
        new_pkg = package_dir.with_name(package_dir.name + ".neu")
        old_pkg = package_dir.with_name(package_dir.name + f".alt-{job.sequence}")
        _remove(new_pkg)
        shutil.copytree(staging / "localai", new_pkg)
        if package_dir.exists():
            package_dir.replace(old_pkg)
        new_pkg.replace(package_dir)
        _remove(old_pkg)
        # 2. Gastskripte und Konfiguration.
        for name, target in GUEST_FILES.items():
            source = staging.joinpath(*name.split("/"))
            if source.exists():
                shutil.copyfile(source, guest_root / target)
        message = f"{sum(1 for n in names if n.startswith('localai/'))} Paketdateien aktualisiert"
        # 3. Idempotente Einrichtung (z. B. worker.cmd, Integritätsstufen) – nur im Windows-Gast.
        script = guest_root / "update.ps1"
        if run_script and os.name == "nt" and script.exists():
            powershell = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            run = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                 capture_output=True, timeout=600, check=False)
            output = (run.stdout + run.stderr).decode("utf-8", errors="replace")[-1500:]
            if run.returncode != 0:
                raise UpdateError(f"update.ps1 endete mit Exitcode {run.returncode}: {output}")
            message += "; update.ps1 ok"
        _remove(staging)
        results = [TargetResult(target_id="*", status=ResultStatus.Succeeded, message=message)]
        status = ResultStatus.Succeeded
    except (UpdateError, OSError, zipfile.BadZipFile, subprocess.TimeoutExpired) as err:
        results = [TargetResult(target_id="*", status=ResultStatus.Failed, message=f"Gast-Update fehlgeschlagen: {err}"[:2000])]
        status = ResultStatus.Failed
    return ToolResult(invocation_id=job.invocation_id, proposal_id=job.proposal.proposal_id, status=status,
                      snapshot_id=job.proposal.snapshot_id, target_results=results,
                      output_sha256=canonical.sha256_hex([r.message or "" for r in results]), truncated=False, untrusted=True,
                      exit_code=0 if status == ResultStatus.Succeeded else 1)


def _extract(payload: pathlib.Path, target: pathlib.Path) -> set[str]:
    names: set[str] = set()
    total = 0
    with zipfile.ZipFile(payload) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES:
            raise UpdateError("zu viele Dateien")
        for info in infos:
            if info.is_dir():
                continue
            try:
                name = normalize_relative(info.filename)
            except PathRuleError as err:
                raise UpdateError(f"unzulässiger Name {info.filename!r}: {err}") from err
            if not (name.startswith("localai/") or name in GUEST_FILES):
                raise UpdateError(f"unerwartete Datei {name}")
            if name in names:
                raise UpdateError(f"Datei doppelt: {name}")
            total += info.file_size
            if total > MAX_BYTES:
                raise UpdateError("Nutzlast zu groß")
            names.add(name)
            dest = target.joinpath(*name.split("/"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "xb") as out:
                shutil.copyfileobj(src, out)
    return names
