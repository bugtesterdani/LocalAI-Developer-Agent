"""Runjournal (M6): der Stand eines Runs liegt auf der Platte, damit er nach Not-Aus oder Neustart fortgesetzt werden kann.

Gespeichert werden nur Plan, Aufgabenstände, Prüfläufe und die angewendeten Patches – keine Freigaben. Beim Fortsetzen
entsteht ein neuer Snapshot, und jeder Patch wird erneut zur Freigabe vorgelegt (frühere Entscheidungen gelten nicht).
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Any

FILE_NAME = "run-journal.json"
VERSION = 1


def _plain(value: Any) -> Any:
    """Nur JSON-fähige Werte (Ergebnisse enthalten z. B. PatchOutcome-Objekte unter „apply“)."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items() if k != "apply"}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class RunJournal:
    def __init__(self, data_dir: pathlib.Path) -> None:
        self.path = data_dir / FILE_NAME

    def load(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("goal"), str):
            return None
        return data

    def save(self, data: dict[str, Any]) -> None:
        data = _plain({**data, "version": VERSION, "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S")})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(temp, self.path)
        except OSError:
            pass  # Journal ist eine Hilfe zum Fortsetzen, kein Muss für den Run

    def update(self, **fields: Any) -> None:
        data = self.load()
        if data is not None:
            data.update(fields)
            self.save(data)

    @staticmethod
    def resumable(data: dict[str, Any] | None) -> bool:
        """Offene Aufgaben oder angewendete, aber nicht übernommene Änderungen."""
        if data is None:
            return False
        if data.get("status") != "done":
            return True
        return bool(data.get("applied")) and not data.get("exported")
