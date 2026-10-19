"""Einstieg für den Broker: ``python -m localai.web run --url … --work <Quarantäne>``.

``run`` startet Fetcher und Content Gate als **getrennte Prozesse** (unter Windows mit eingeschränktem Token auf
niedriger Integritätsstufe in einem Job Object – sie können keine Benutzerdateien verändern; der Quarantäneordner
ist als *Low* gekennzeichnet) und schreibt ``result.json``. ``fetch`` und ``gate`` sind die beiden Stufen.
Der Launcher selbst parst keine Webinhalte.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

from localai.web import fetch as fetch_stage
from localai.web import gate as gate_stage


def _child_env(work: pathlib.Path) -> dict[str, str]:
    temp = work / "tmp"
    temp.mkdir(exist_ok=True)
    env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
           "TEMP": str(temp), "TMP": str(temp)}
    for key in ("SystemRoot", "windir", "SystemDrive", "PATH", "PATHEXT", "COMSPEC", "NUMBER_OF_PROCESSORS",
                "PROCESSOR_ARCHITECTURE", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME"):
        if key in os.environ:
            env[key] = os.environ[key]
    # Keine Proxy-/Zertifikats-Umleitungen (Spezifikation §7): HTTP(S)_PROXY, SSL_CERT_FILE usw. fehlen bewusst.
    return env


def _stage(stage: str, args: list[str], work: pathlib.Path, timeout: int, low: bool) -> tuple[int, bool]:
    command = ["-m", "localai.web", stage, *args]
    log = work / f"{stage}.log"
    env = _child_env(work)
    if os.name == "nt" and low:
        from localai.worker import winproc
        return winproc.run(sys.executable, command, str(work), env, timeout, str(log), True, 512)
    with open(log, "wb") as out:
        try:
            process = subprocess.run([sys.executable, *command], cwd=work, env={**env, "PATH": os.environ.get("PATH", "")},
                                     stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, timeout=timeout, check=False)
            return process.returncode, False
        except subprocess.TimeoutExpired:
            return -1, True


def _label_low(work: pathlib.Path) -> None:
    if os.name != "nt":
        return
    icacls = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"
    run = subprocess.run([str(icacls), str(work), "/setintegritylevel", "(OI)(CI)low"], capture_output=True, timeout=60, check=False)
    if run.returncode != 0:
        raise OSError(f"Quarantäneordner nicht für niedrige Integrität kennzeichenbar (icacls {run.returncode})")


def run(url: str, work: pathlib.Path, max_bytes: int, timeout: int, max_chars: int, low: bool) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    result: dict = {"url": url, "stage": "fetch"}
    try:
        if low:
            _label_low(work)
        code, timed_out = _stage("fetch", ["--url", url, "--out", str(work), "--max-bytes", str(max_bytes),
                                           "--timeout", str(timeout)], work, timeout + 15, low)
        meta_path = work / "meta.json"
        if timed_out or not meta_path.is_file():
            result["error"] = "Abruf abgebrochen (Zeitgrenze)" if timed_out else f"Abrufprozess ohne Ergebnis (Exitcode {code})"
            return result
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        result["fetch"] = meta
        if meta.get("error") or meta.get("redirect") or not (work / "raw.bin").is_file():
            return result
        result["stage"] = "gate"
        code, timed_out = _stage("gate", ["--work", str(work), "--max-chars", str(max_chars)], work, 60, low)
        gate_path = work / "gate.json"
        if timed_out or code != 0 or not gate_path.is_file():
            result["error"] = "Content Gate abgebrochen (Zeitgrenze)" if timed_out else f"Content Gate fehlgeschlagen (Exitcode {code})"
            return result
        result["gate"] = json.loads(gate_path.read_text(encoding="utf-8"))
        result["stage"] = "done"
        return result
    except (OSError, ValueError) as err:
        result["error"] = str(err)
        return result
    finally:
        result["seconds"] = round(time.monotonic() - started, 2)
        (work / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="localai.web")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--url", required=True)
    p_run.add_argument("--work", required=True)
    p_run.add_argument("--max-bytes", type=int, default=fetch_stage.DEFAULT_MAX_BYTES)
    p_run.add_argument("--timeout", type=int, default=int(fetch_stage.DEFAULT_TIMEOUT))
    p_run.add_argument("--max-chars", type=int, default=gate_stage.DEFAULT_MAX_CHARS)
    p_run.add_argument("--no-low-integrity", action="store_true")
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--url", required=True)
    p_fetch.add_argument("--out", required=True)
    p_fetch.add_argument("--max-bytes", type=int, default=fetch_stage.DEFAULT_MAX_BYTES)
    p_fetch.add_argument("--timeout", type=int, default=int(fetch_stage.DEFAULT_TIMEOUT))
    p_gate = sub.add_parser("gate")
    p_gate.add_argument("--work", required=True)
    p_gate.add_argument("--max-chars", type=int, default=gate_stage.DEFAULT_MAX_CHARS)
    args = parser.parse_args(argv)
    if args.command == "run":
        result = run(args.url, pathlib.Path(args.work), max(1, min(args.max_bytes, 16 * 1024 * 1024)),
                     max(1, min(args.timeout, 120)), max(1000, min(args.max_chars, 200_000)), not args.no_low_integrity)
        print(json.dumps({"stage": result.get("stage"), "error": result.get("error", "")}, ensure_ascii=False))
        return 0
    if args.command == "fetch":
        meta, body = fetch_stage.fetch(args.url, args.max_bytes, float(args.timeout))
        fetch_stage.write(pathlib.Path(args.out), meta, body)
        return 0
    gate_stage.run(pathlib.Path(args.work), args.max_chars)
    return 0


if __name__ == "__main__":
    sys.exit(main())
