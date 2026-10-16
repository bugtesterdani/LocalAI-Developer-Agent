"""Startet den echten C#-Broker-Prozess für Integrationstests.

Voraussetzung: ``dotnet build src/dotnet/LocalAi.Broker.Host`` wurde ausgeführt.
Pfad überschreibbar mit ``LOCALAI_BROKER_DLL``.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
from dataclasses import dataclass

import pytest

from localai.broker_client import BrokerClient

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_DLL = ROOT / "src" / "dotnet" / "LocalAi.Broker.Host" / "bin" / "Debug" / "net10.0" / "LocalAi.Broker.Host.dll"


@dataclass
class RunningBroker:
    process: subprocess.Popen
    pipe: str
    ui_token: str
    orchestrator_token: str
    data_dir: pathlib.Path

    def ui(self) -> BrokerClient:
        return BrokerClient(self.pipe, self.ui_token)

    def orchestrator(self) -> BrokerClient:
        return BrokerClient(self.pipe, self.orchestrator_token)

    def stop(self, hard: bool = False) -> None:
        if self.process.poll() is not None:
            return
        if hard:
            self.process.kill()
        else:
            assert self.process.stdin is not None
            self.process.stdin.close()
        self.process.wait(timeout=20)


def start_broker(data_dir: pathlib.Path, *extra: str) -> RunningBroker:
    dll = pathlib.Path(os.environ.get("LOCALAI_BROKER_DLL", DEFAULT_DLL))
    if not dll.exists():
        pytest.skip(f"Broker nicht gebaut: {dll}")
    process = subprocess.Popen(
        ["dotnet", str(dll), "--data", str(data_dir), *extra],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    assert process.stdout is not None
    line = process.stdout.readline()
    if not line:
        err = process.stderr.read() if process.stderr else ""
        raise RuntimeError(f"Broker lieferte keinen Handshake (Exitcode {process.wait()}): {err}")
    handshake = json.loads(line)
    broker = RunningBroker(process, handshake["pipe"], handshake["uiToken"], handshake["orchestratorToken"], data_dir)
    deadline = time.monotonic() + 10
    while True:
        try:
            broker.ui().ping()
            return broker
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


@pytest.fixture
def broker(tmp_path):
    running = start_broker(tmp_path / "data")
    yield running
    running.stop()


@pytest.fixture
def broker_factory(tmp_path):
    started: list[RunningBroker] = []

    def factory(*extra: str, data_dir: pathlib.Path | None = None) -> RunningBroker:
        running = start_broker(data_dir or tmp_path / "data", *extra)
        started.append(running)
        return running

    yield factory
    for running in started:
        running.stop()
