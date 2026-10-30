"""Zeitaufschlüsselung eines Runs: wohin die Zeit geht (Modell laden, Prompt, Ausgabe, GPU-Warteschlange, Freigaben,
Sandbox), je Modell, mit Hinweisen, wo es sich zu drehen lohnt.

Die Modellzeiten stammen aus Ollamas eigener Messung jeder Antwort (``load_duration``, ``prompt_eval_duration``,
``eval_duration``); Freigabe- und Sandboxzeiten misst die Brücke zum Broker. Parallel laufende Aufgaben überlappen –
die Summen der Kategorien können deshalb größer sein als die Gesamtdauer.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
from dataclasses import asdict, dataclass, field

# Ab dieser Ladezeit gilt ein Aufruf als „Modell (neu) geladen“ (bereits geladene Modelle melden Millisekunden).
RELOAD_SECONDS = 0.5


@dataclass
class ModelStats:
    calls: int = 0
    loads: int = 0
    load_seconds: float = 0.0
    prompt_tokens: int = 0
    prompt_seconds: float = 0.0
    output_tokens: int = 0
    output_seconds: float = 0.0
    total_seconds: float = 0.0
    gpu_wait_seconds: float = 0.0
    summary_calls: int = 0

    @property
    def output_rate(self) -> float:
        return self.output_tokens / self.output_seconds if self.output_seconds > 0 else 0.0

    @property
    def prompt_rate(self) -> float:
        return self.prompt_tokens / self.prompt_seconds if self.prompt_seconds > 0 else 0.0


@dataclass
class ToolStats:
    calls: int = 0
    automatic: int = 0
    approval_seconds: float = 0.0  # Warten auf deine Entscheidung
    run_seconds: float = 0.0  # Ausführung nach der Freigabe (Sandbox/Broker)


def _fmt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f} s" if seconds >= 10 else f"{seconds:.1f} s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min {rest:02d} s"


def _share(part: float, whole: float) -> str:
    return f" ({part / whole * 100:.0f} %)" if whole > 0 else ""


@dataclass
class RunTimings:
    started: float = field(default_factory=time.monotonic)
    prepare_seconds: float = 0.0
    models: dict[str, ModelStats] = field(default_factory=dict)  # Schlüssel: Modellname
    roles: dict[str, set[str]] = field(default_factory=dict)  # Modell → Rollen
    tools: dict[str, ToolStats] = field(default_factory=dict)  # Schlüssel: Kategorie
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # ------------------------------------------------------------------ erfassen
    def model(self, role: str, result, gpu_wait: float = 0.0, summary: bool = False) -> None:
        """Eine Modellantwort (ChatResult) mit Ollamas Zeiten."""
        with self._lock:
            stats = self.models.setdefault(result.model or "?", ModelStats())
            self.roles.setdefault(result.model or "?", set()).add(role)
            stats.calls += 1
            stats.summary_calls += int(summary)
            if result.load_seconds >= RELOAD_SECONDS:
                stats.loads += 1
            stats.load_seconds += result.load_seconds
            stats.prompt_tokens += result.prompt_tokens
            stats.prompt_seconds += result.prompt_seconds
            stats.output_tokens += result.output_tokens
            stats.output_seconds += result.output_seconds
            stats.total_seconds += result.total_seconds
            stats.gpu_wait_seconds += gpu_wait

    def tool(self, tool: str, approval_seconds: float, run_seconds: float, automatic: bool = False) -> None:
        category = ("Sandbox: Build/Test/Ausführen" if tool.startswith("Toolchain.")
                    else "Webabrufe" if tool.startswith("Web.")
                    else "Snapshot und Übernahme" if tool.startswith("Workspace.")
                    else "Bestätigung autoApprove" if tool == "Run.AutoApprove"
                    else "Debug-Sitzung" if tool.startswith("OsDebug.")
                    else "Lesen, Suchen, Patches")
        with self._lock:
            stats = self.tools.setdefault(category, ToolStats())
            stats.calls += 1
            stats.automatic += int(automatic)
            stats.approval_seconds += max(0.0, approval_seconds)
            stats.run_seconds += max(0.0, run_seconds)

    @property
    def empty(self) -> bool:
        return not self.models and not self.tools

    # ------------------------------------------------------------------ auswerten
    def totals(self) -> dict[str, float]:
        m = self.models.values()
        t = self.tools.values()
        return {
            "wall": time.monotonic() - self.started,
            "prepare": self.prepare_seconds,
            "load": sum(s.load_seconds for s in m),
            "prompt": sum(s.prompt_seconds for s in m),
            "output": sum(s.output_seconds for s in m),
            "model": sum(s.total_seconds for s in m),
            "gpu_wait": sum(s.gpu_wait_seconds for s in m),
            "approval": sum(s.approval_seconds for s in t),
            "tools": sum(s.run_seconds for s in t),
        }

    def hints(self) -> list[str]:
        totals = self.totals()
        hints: list[str] = []
        loads = sum(s.loads for s in self.models.values())
        if loads >= 3 and totals["load"] >= 0.15 * max(totals["model"], 1):
            hints.append(f"Modelle wurden {loads}× (neu) geladen ({_fmt(totals['load'])}). Weniger verschiedene Modelle je "
                         "Rolle oder genug Grafikspeicher für alle (OLLAMA_MAX_LOADED_MODELS) sparen das; auch eine "
                         "andere Kontextgröße (contextTokens) für dasselbe Modell erzwingt ein Neuladen.")
        for name, s in self.models.items():
            if s.output_tokens >= 200 and 0 < s.output_rate < 8:
                hints.append(f"{name} erzeugt nur {s.output_rate:.1f} Token/s – vermutlich liegt ein Teil auf der CPU. "
                             "`ollama ps` zeigt die Aufteilung; kleinerer Kontext, stärkere Quantisierung oder "
                             "OLLAMA_KV_CACHE_TYPE=q8_0 mit OLLAMA_FLASH_ATTENTION=1 helfen.")
            if s.summary_calls >= 3 and s.summary_calls * 2 >= s.calls:
                hints.append(f"{name}: {s.summary_calls} von {s.calls} Aufrufen waren Zusammenfassungen des Verlaufs – "
                             "history.mode \"trim\" oder ein höheres maxNoteTokens spart Aufrufe.")
        if totals["prompt"] > totals["output"] and totals["prompt"] >= 30:
            hints.append(f"Das Einlesen der Prompts ({_fmt(totals['prompt'])}) kostet mehr als die Ausgabe – ein kleineres "
                         "Kontextbudget bzw. weniger Evidenz je Schritt beschleunigt jeden Schritt.")
        if totals["approval"] >= 0.25 * max(totals["wall"], 1) and totals["approval"] >= 60:
            hints.append(f"{_fmt(totals['approval'])} Warten auf Freigaben – Sandbox-Tools lassen sich mit „autoApprove“ in "
                         "der Projektdatei (nach Bestätigung im Run) automatisch freigeben.")
        if totals["gpu_wait"] >= 0.2 * max(totals["wall"], 1) and totals["gpu_wait"] >= 30:
            hints.append(f"Aufgaben warteten {_fmt(totals['gpu_wait'])} auf den GPU-Slot (parallel geplante Aufgaben teilen "
                         "sich eine GPU).")
        return hints

    def report(self) -> str:
        """Deutsche Zusammenfassung für den Chat."""
        totals = self.totals()
        wall = totals["wall"]
        lines = [f"Zeitaufschlüsselung dieses Runs (Gesamtdauer {_fmt(wall)}):"]
        if totals["prepare"] >= 1:
            lines.append(f"- Vorbereitung (Modelle und Hardware prüfen): {_fmt(totals['prepare'])}")
        if self.models:
            other = max(0.0, totals["model"] - totals["load"] - totals["prompt"] - totals["output"])
            lines.append(f"- Modell gesamt: {_fmt(totals['model'])}{_share(totals['model'], wall)} – laden "
                         f"{_fmt(totals['load'])}, Prompt einlesen {_fmt(totals['prompt'])}, Ausgabe {_fmt(totals['output'])}"
                         + (f", Verbindung/Sonstiges {_fmt(other)}" if other >= 1 else ""))
        if totals["gpu_wait"] >= 1:
            lines.append(f"- Warten auf den GPU-Slot: {_fmt(totals['gpu_wait'])}")
        for category, s in sorted(self.tools.items(), key=lambda kv: -(kv[1].approval_seconds + kv[1].run_seconds)):
            auto = f", davon {s.automatic} automatisch genehmigt" if s.automatic else ""
            lines.append(f"- {category}: {s.calls}× – Warten auf Freigabe {_fmt(s.approval_seconds)}, Ausführung "
                         f"{_fmt(s.run_seconds)}{auto}")
        if self.models:
            lines.append("")
            lines.append("Je Modell:")
            for name, s in sorted(self.models.items(), key=lambda kv: -kv[1].total_seconds):
                roles = ", ".join(sorted(self.roles.get(name, ())))
                lines.append(f"- {name} ({roles}): {s.calls} Aufrufe, {s.loads}× geladen ({_fmt(s.load_seconds)}), "
                             f"Prompt {s.prompt_tokens} Token mit {s.prompt_rate:.0f} Token/s, Ausgabe {s.output_tokens} "
                             f"Token mit {s.output_rate:.1f} Token/s")
        hints = self.hints()
        if hints:
            lines.append("")
            lines.append("Hinweise:")
            lines.extend(f"- {h}" for h in hints)
        if len(self.models) + len(self.tools) > 1:
            lines.append("")
            lines.append("Parallel laufende Aufgaben überlappen, die Summen können deshalb größer sein als die Gesamtdauer.")
        return "\n".join(lines)

    def to_json(self, run_id: str) -> dict:
        # Bewusst ohne das Ziel: es kann vertrauliche Angaben enthalten und gehört nicht in ein dauerhaftes Protokoll.
        return {
            "runId": run_id,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "totals": {k: round(v, 2) for k, v in self.totals().items()},
            "models": {name: {**asdict(s), "roles": sorted(self.roles.get(name, ()))} for name, s in self.models.items()},
            "tools": {name: asdict(s) for name, s in self.tools.items()},
        }

    def save(self, path: pathlib.Path, run_id: str) -> None:
        """Eine Zeile je Run (JSON Lines) – zum Vergleichen verschiedener Einstellungen."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as out:
                out.write(json.dumps(self.to_json(run_id), ensure_ascii=False) + "\n")
        except OSError:
            pass
