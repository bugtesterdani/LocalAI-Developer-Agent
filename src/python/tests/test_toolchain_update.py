"""Toolchain-Update in einer laufenden VM (Job Sandbox.Toolchains): Nutzlast prüfen, entpacken, installieren lassen."""

from __future__ import annotations

import json
import pathlib
import sys
import zipfile

import pytest

from localai.contracts import ResultStatus
from localai.worker.toolchain_update import (
    TOOLCHAINS_TOOL,
    ToolchainUpdateError,
    apply_toolchains,
    extract,
)
from tests.test_worker import job, proposal

FAKE = pathlib.Path(__file__).with_name("fake_toolchain_installer.py")


def _zip(path: pathlib.Path, entries: dict[str, str]) -> pathlib.Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in entries.items():
            zf.writestr(name, text)
    return path


def _toolchain(name: str, version: str = "1.0", extra: dict[str, str] | None = None) -> dict[str, str]:
    base = {f"toolchain-src/{name}/toolchain.json": json.dumps({"name": name, "version": version}),
            f"toolchain-src/{name}/pinned.json": json.dumps({"name": name, "files": {}}),
            f"toolchain-src/{name}/pakete/a.nupkg": "x"}
    return {**base, **{f"toolchain-src/{name}/{k}": v for k, v in (extra or {}).items()}}


def test_nutzlast_wird_streng_geprueft(tmp_path):
    names = extract(_zip(tmp_path / "ok.zip", {**_toolchain("dotnet"), **_toolchain("testumgebung")}), tmp_path / "src")
    assert names == ["dotnet", "testumgebung"] and (tmp_path / "src" / "dotnet" / "pakete" / "a.nupkg").is_file()
    for entries, fehler in (
        ({"toolchain-src/../x/toolchain.json": "{}"}, "unzulässig"),
        ({"localai/worker/x.py": ""}, "unerwartete Datei"),
        ({"toolchain-src/Groß/toolchain.json": "{}"}, "Toolchain-Name"),
        ({"toolchain-src/dotnet/toolchain.json": "{}"}, "pinned.json fehlt"),
        ({}, "keine Toolchain"),
    ):
        with pytest.raises(ToolchainUpdateError, match=fehler):
            extract(_zip(tmp_path / "x.zip", entries), tmp_path / f"t{len(entries)}{fehler[:3]}")


def test_installation_je_toolchain_mit_ergebnis(tmp_path):
    guest = tmp_path / "guest"
    guest.mkdir()
    payload = _zip(tmp_path / "p.zip", {**_toolchain("dotnet", "8.0"), **_toolchain("testumgebung", extra={"FAIL": ""})})
    result = apply_toolchains(job(proposal(TOOLCHAINS_TOOL, []), 7), payload, guest, [sys.executable, str(FAKE)])
    assert result.status == ResultStatus.Failed, "eine gescheitert → Failed"
    by_id = {r.target_id: r for r in result.target_results}
    assert by_id["dotnet"].status == ResultStatus.Succeeded and "dotnet 8.0: installiert, Selbsttest ok" in by_id["dotnet"].message
    assert by_id["testumgebung"].status == ResultStatus.Failed and "Selbsttest" in by_id["testumgebung"].message
    assert (guest / "installed" / "dotnet.json").is_file()
    assert not (guest / "toolchain-src").exists(), "Quelle wird danach entfernt"


def test_ohne_installationsweg_klare_meldung(tmp_path):
    result = apply_toolchains(job(proposal(TOOLCHAINS_TOOL, []), 1), _zip(tmp_path / "p.zip", _toolchain("dotnet")), tmp_path)
    if sys.platform != "win32":
        assert result.status == ResultStatus.Failed and "nur im Windows-Gast" in result.target_results[0].message

