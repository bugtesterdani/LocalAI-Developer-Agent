"""Prozessstart für Toolchain-Jobs unter Windows (SEC-21): niedrige Integrität, Job Object, Zeitgrenze.

Läuft nur unter Windows (z. B. mit ``scripts\\test.ps1`` auf dem Entwicklungs-PC). Gestartet wird ausschließlich
``cmd.exe`` mit festen Testbefehlen – dasselbe Verfahren nutzt der Worker in der VM für Toolchain-Aktionen.
"""

from __future__ import annotations

import os
import pathlib
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="nur unter Windows")

CMD = str(pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe")


def env() -> dict[str, str]:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return {"SystemRoot": root, "PATH": rf"{root}\System32", "TEMP": os.environ.get("TEMP", root)}


def test_niedrige_integritaet_darf_nur_in_markierten_ordner_schreiben(tmp_path):
    if " " in str(tmp_path):
        pytest.skip("Testordner mit Leerzeichen")
    from localai.worker import winproc
    from localai.worker.execution import label_low_integrity

    normal = tmp_path / "normal"
    low = tmp_path / "low"
    normal.mkdir()
    low.mkdir()
    label_low_integrity(low)
    log = tmp_path / "out.log"
    code, timed_out = winproc.run(CMD, ["/c", f"echo x> {normal / 'a.txt'}"], str(tmp_path), env(), 30, str(log), True)
    assert not timed_out and code != 0 and not (normal / "a.txt").exists(), "Schreiben nach oben muss scheitern"
    code, _ = winproc.run(CMD, ["/c", f"echo x> {low / 'b.txt'}"], str(low), env(), 30, str(log), True)
    assert code == 0 and (low / "b.txt").exists(), log.read_text(errors="replace")
    # Ohne niedrige Integrität (nur zur Fehlersuche) gelingt der Schreibzugriff.
    code, _ = winproc.run(CMD, ["/c", f"echo x> {normal / 'c.txt'}"], str(tmp_path), env(), 30, str(log), False)
    assert code == 0 and (normal / "c.txt").exists()


def test_ausgabe_exitcode_und_argumente_ohne_shell_interpretation(tmp_path):
    from localai.worker import winproc

    log = tmp_path / "out.log"
    code, _ = winproc.run(CMD, ["/c", "echo", "Hallo Welt & mehr"], str(tmp_path), env(), 30, str(log), True)
    assert code == 0
    assert "Hallo Welt" in log.read_text(errors="replace")
    code, _ = winproc.run(CMD, ["/c", "exit 7"], str(tmp_path), env(), 30, str(log), True)
    assert code == 7


def test_zeitgrenze_beendet_den_ganzen_prozessbaum(tmp_path):
    from localai.worker import winproc

    log = tmp_path / "out.log"
    started = time.monotonic()
    code, timed_out = winproc.run(CMD, ["/c", "ping -n 30 127.0.0.1 > nul"], str(tmp_path), env(), 2, str(log), True)
    assert timed_out and code == -1
    assert time.monotonic() - started < 15
