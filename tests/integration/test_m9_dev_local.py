"""M9 mit echtem Broker, echtem Worker (dev-local-unsafe) und echtem QEMU: eine Freigabe für die Debug-Sitzung, danach
Befehle im Rahmen der Sitzung; Audit-Prüfung; Not-Aus beendet die Sitzung.

In der Linux-VM (hyperv) läuft derselbe Worker – dort zusätzlich als eigener Benutzer ohne Netz (test_osdebug_m9.py).
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

import pytest
from localai.contracts import AgentRole, ProposalState
from localai.pipe import BrokerError
from test_sandbox_dev_local import approve_and_wait, req

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(shutil.which("qemu-system-x86_64") is None or shutil.which("nasm") is None,
                                 reason="qemu-system-x86_64 und nasm nötig")]
ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = str(ROOT / "src" / "python")
BOOT = """bits 16
org 0x7c00
    cli
    xor ax, ax
    mov ds, ax
    mov si, msg
.print:
    lodsb
    test al, al
    jz done
    mov dx, 0x3f8
    out dx, al
    jmp .print
done:
    hlt
    jmp done
msg: db "Hallo vom Bootsektor", 0
times 510-($-$$) db 0
dw 0xaa55
"""


def debugger(request):
    return request.model_copy(update={"agent": AgentRole.Debugger})


def setup(tmp_path, broker_factory):
    project_dir = tmp_path / "danios"
    (project_dir / "build").mkdir(parents=True)
    (tmp_path / "boot.asm").write_text(BOOT)
    subprocess.run(["nasm", "-f", "bin", str(tmp_path / "boot.asm"), "-o", str(tmp_path / "boot.bin")], check=True)
    (project_dir / "build" / "os.img").write_bytes((tmp_path / "boot.bin").read_bytes().ljust(1440 * 1024, b"\0"))
    project = {"schemaVersion": 1, "projectId": "danios", "name": "DaniOS",
               "grants": [{"grantId": "os", "root": str(project_dir), "rights": ["List", "Read", "Execute"], "recursive": True}],
               "osDebug": {"grantId": "os", "image": "build/os.img", "media": "Floppy", "maxCommands": 6, "maxMinutes": 5}}
    (tmp_path / "projekt.json").write_text(json.dumps(project), encoding="utf-8")
    config_dir = tmp_path / "cfg" / "config"
    config_dir.mkdir(parents=True)
    config = {"sandbox": {"mode": "dev-local-unsafe",
                          "devWorker": {"command": sys.executable, "arguments": ["-m", "localai.worker"], "workingDirectory": PY}},
              "web": {"enabled": False}}
    (config_dir / "localai.json").write_text(json.dumps(config), encoding="utf-8")
    b = broker_factory("--mode", "dev-local-unsafe", "--project", str(tmp_path / "projekt.json"),
                       "--config", str(config_dir / "localai.json"))
    return b, project_dir


SERIAL: list[str] = []


def command(orch, session, name, **args):
    view = orch.submit(debugger(req("OsDebug.Command", {"sessionId": session, "command": name, **args})))
    assert view.state == ProposalState.Approved, view  # im Rahmen der Sitzung – keine Einzelfreigabe
    outcome = orch.wait_final(view.proposal.proposal_id, timeout=60, poll=0.1)
    assert outcome.state == ProposalState.Succeeded, outcome
    data = json.loads(outcome.result.target_results[0].text)
    SERIAL.append(data.get("serial", ""))
    return data


def test_debug_sitzung_mit_echtem_qemu(tmp_path, broker_factory):
    b, project = setup(tmp_path, broker_factory)
    ui, orch = b.ui(), b.orchestrator()
    snap = approve_and_wait(ui, orch, orch.submit(req("Workspace.Snapshot", {"grantId": "os", "root": str(project)})))
    snapshot = snap.result.target_results[0].text
    info = orch.toolchains_available()["osDebug"]
    assert info["config"]["media"] == "Floppy" and info["maxCommands"] == 6

    start = orch.submit(debugger(req("OsDebug.Start", {"grantId": "os", "config": info["config"], "maxCommands": 6, "maxMinutes": 5,
                                                       "allowInput": False, "haltAtStart": True}, [], snapshot)))
    assert start.state == ProposalState.AwaitingApproval  # die Sitzung selbst braucht eine Freigabe
    started = approve_and_wait(ui, orch, start, timeout=60)
    assert started.state == ProposalState.Succeeded, [r.message for r in started.result.target_results]
    session = start.proposal.proposal_id
    assert json.loads(started.result.target_results[0].text)["session"] == session

    assert "gesetzt" in command(orch, session, "break", address="0x7c00")["message"]
    hit = command(orch, session, "continue", waitMs=30000)
    assert "Haltepunkt" in hit["state"]
    assert "EIP=00007c00" in command(orch, session, "registers")["registers"]
    command(orch, session, "continue", waitMs=1000)
    command(orch, session, "serial")
    assert "Hallo vom Bootsektor" in "".join(SERIAL)
    with pytest.raises(BrokerError, match="Tastatur"):
        orch.submit(debugger(req("OsDebug.Command", {"sessionId": session, "command": "keys", "text": "a"})))
    with pytest.raises(BrokerError, match="anderen Run"):
        orch.submit(req("OsDebug.Command", {"sessionId": session, "command": "registers"}))  # Coder statt Debugger
    stop = orch.submit(debugger(req("OsDebug.Stop", {"sessionId": session})))
    assert orch.wait_final(stop.proposal.proposal_id, timeout=30, poll=0.1).state == ProposalState.Succeeded
    with pytest.raises(BrokerError):
        orch.submit(debugger(req("OsDebug.Command", {"sessionId": session, "command": "registers"})))
    audit = ui.audit_verify()
    assert audit["ok"], audit["problems"]
