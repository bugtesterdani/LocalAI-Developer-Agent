"""M2c: Toolchain-Registrierung über den echten Broker (nur UI-Token, Hashbindung, Journal-Ereignis)."""

from __future__ import annotations

import json
import pathlib

import pytest

from localai.pipe import BrokerError

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def setup(broker_factory, tmp_path):
    toolchains = tmp_path / "toolchains"
    demo = toolchains / "python-3.15.0"
    demo.mkdir(parents=True)
    manifest = json.loads((ROOT / "toolchains" / "python" / "toolchain.json").read_text(encoding="utf-8"))
    (demo / "toolchain.json").write_text(json.dumps(manifest), encoding="utf-8")
    (demo / "python-3.15.0-amd64.exe").write_bytes(b"MZ installer")
    (demo / "selftest").mkdir()
    (demo / "selftest" / "hello.py").write_text("print('Hallo aus der Sandbox (Python)')\n", encoding="utf-8")
    project_dir = tmp_path / "projekt"
    project_dir.mkdir()
    project = {"schemaVersion": 1, "projectId": "p", "name": "p", "toolchains": ["python"],
               "grants": [{"grantId": "p", "root": str(project_dir), "rights": ["List", "Read"], "recursive": True}]}
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "projekt.json").write_text(json.dumps(project), encoding="utf-8")
    config = {"project": "config/projekt.json", "toolchains": {"directory": str(toolchains)}, "sandbox": {"mode": "none"}}
    (tmp_path / "config" / "localai.json").write_text(json.dumps(config), encoding="utf-8")
    b = broker_factory("--mode", "none", "--config", str(tmp_path / "config" / "localai.json"))
    return {"broker": b, "dir": demo}


def test_registrierung_nur_ueber_die_oberflaeche(setup):
    b = setup["broker"]
    ui, orch = b.ui(), b.orchestrator()
    with pytest.raises(BrokerError) as err:
        orch.toolchains_scan()
    assert err.value.code == 403
    [info] = ui.toolchains_scan()
    assert info.name == "python" and not info.registered and info.used_by_project and info.problems == []
    with pytest.raises(BrokerError) as err:
        orch.toolchains_register(info.directory, info.digest)
    assert err.value.code == 403

    registration = ui.toolchains_register(info.directory, info.digest)
    assert registration["name"] == "python" and len(registration["files"]) == 2
    [after] = ui.toolchains_scan()
    assert after.registered and after.registered_digest == info.digest
    events = ui.wait_events(0, timeout_ms=100)
    assert any(e.type == "toolchain.registered" for e in events)


def test_veraenderter_ordner_wird_nicht_registriert(setup):
    ui = setup["broker"].ui()
    [info] = ui.toolchains_scan()
    (setup["dir"] / "python-3.15.0-amd64.exe").write_bytes(b"MZ anderer installer")
    with pytest.raises(BrokerError) as err:
        ui.toolchains_register(info.directory, info.digest)
    assert err.value.code == 422 and "verändert" in err.value.message


def test_ungueltiges_manifest_zeigt_probleme(setup):
    ui = setup["broker"].ui()
    manifest_file = setup["dir"] / "toolchain.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["actions"]["run"]["arguments"] = ["-c", "{script}; del *"]
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    [info] = ui.toolchains_scan()
    assert any("ganzes Argument" in p for p in info.problems)
    with pytest.raises(BrokerError):
        ui.toolchains_register(info.directory, info.digest)
