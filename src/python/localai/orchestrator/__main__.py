"""Startpunkt des Orchestrators. Wird vom Broker mit LOCALAI_PIPE, LOCALAI_TOKEN (Orchestrator-Token),
LOCALAI_RUN_ID, LOCALAI_INBOX_AFTER und LOCALAI_DATA gestartet.

Optionen: ``--models <models.json>`` (Standard: config/models.local.json, sonst config/models.json),
``--ollama <url>`` überschreibt die Ollama-Adresse (nur Loopback).
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import pathlib
import sys
import traceback

from localai.broker_client import BrokerClient
from localai.orchestrator.models import ConfigError, ModelsConfig, default_data_dir, repo_root
from localai.orchestrator.run import Orchestrator


def models_path(explicit: str | None) -> pathlib.Path:
    if explicit:
        return pathlib.Path(explicit)
    local = repo_root() / "config" / "models.local.json"
    return local if local.exists() else repo_root() / "config" / "models.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="localai.orchestrator")
    parser.add_argument("--models")
    parser.add_argument("--ollama")
    args = parser.parse_args(argv)
    pipe, token, run_id = (os.environ.get(k) for k in ("LOCALAI_PIPE", "LOCALAI_TOKEN", "LOCALAI_RUN_ID"))
    if not pipe or not token or not run_id:
        print("LOCALAI_PIPE, LOCALAI_TOKEN und LOCALAI_RUN_ID fehlen – der Orchestrator wird vom Broker gestartet "
              "(Chatnachricht in der Oberfläche).", file=sys.stderr)
        return 2
    path = models_path(args.models)
    try:
        config = ModelsConfig.load(path)
    except ConfigError as err:
        print(f"Modellkonfiguration ungültig: {err}", file=sys.stderr)
        return 4
    if args.ollama:
        config = dataclasses.replace(config, ollama_url=args.ollama)
    print(f"Orchestrator: Python {sys.version.split()[0]}, Run {run_id}, Modelle aus {path}, Ollama {config.ollama_url}",
          flush=True)
    client = BrokerClient(pipe, token)
    inbox = BrokerClient(pipe, token, timeout=60)
    client.ping()
    orchestrator = Orchestrator(client, inbox, run_id, config, default_data_dir(),
                                inbox_after=int(os.environ.get("LOCALAI_INBOX_AFTER", "0")), echo=True)
    orchestrator.serve()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
