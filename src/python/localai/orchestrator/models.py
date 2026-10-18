"""Modellprofile, Grenzen und Kalibrierungsstand (Spezifikation §5, §9).

Konfiguration: ``config/models.json``. Modellnamen sind austauschbar; ``latest`` oder fehlende Tags sind
unzulässig. Ein Profil wird nur verwendet, wenn die Kalibrierung es für dieses Modell (Digest), diesen
Kontext und diese Hardware als stabil ausweist – außer ``requireCalibration`` ist ausdrücklich ``false``.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any

ROLES = ("Planner", "Coder", "Tester", "Debugger", "Reviewer")


class ConfigError(Exception):
    """models.json ungültig."""


def default_data_dir() -> pathlib.Path:
    """Wie der Broker: %LOCALAPPDATA%\\LocalAi\\broker (überschreibbar mit LOCALAI_DATA)."""
    if os.environ.get("LOCALAI_DATA"):
        return pathlib.Path(os.environ["LOCALAI_DATA"])
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / "AppData" / "Local")
    else:
        root = os.environ.get("XDG_DATA_HOME") or str(pathlib.Path.home() / ".local" / "share")
    return pathlib.Path(root) / "LocalAi" / "broker"


def repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[4]


def validate_model_tag(model: str) -> None:
    if ":" not in model or not model.split(":", 1)[1]:
        raise ConfigError(f"Modell {model!r} ohne Tag – bitte festen Tag angeben (z. B. qwen2.5-coder:14b)")
    if model.split(":", 1)[1].lower() == "latest":
        raise ConfigError(f"Modell {model!r}: Tag 'latest' ist unzulässig (Spezifikation §9)")


@dataclass(frozen=True)
class ModelProfile:
    name: str
    model: str
    context_tokens: int
    max_output_tokens: int
    think: bool | None = None
    offload: bool = False
    temperature: float = 0.2

    @property
    def key(self) -> str:
        return f"{self.model}@{self.context_tokens}"

    def label(self) -> str:
        return f"{self.name} · {self.model} · {self.context_tokens // 1024}K"


@dataclass(frozen=True)
class Limits:
    max_steps_per_task: int = 20
    max_repair_attempts: int = 3
    max_active_minutes: float = 30.0
    max_sandbox_jobs: int = 2
    max_gpu_requests: int = 1
    max_tasks: int = 12


@dataclass(frozen=True)
class CalibrationSettings:
    runs: int = 10
    min_vram_reserve_gib: float = 2.0
    output_tokens: int = 256


@dataclass
class ModelsConfig:
    ollama_url: str
    stall_warning_seconds: float
    stall_abort_seconds: float
    profiles: dict[str, ModelProfile]
    roles: dict[str, list[str]]
    limits: Limits
    calibration: CalibrationSettings
    require_calibration: bool = True
    source: str = ""
    allow_lan: bool = False

    def gateway(self, url: str | None = None, **overrides):
        """Gateway mit den Einstellungen dieser Konfiguration (eine Stelle für Adresse und LAN-Freigabe)."""
        from localai.orchestrator.gateway import OllamaGateway

        kwargs = {"stall_warning": self.stall_warning_seconds, "stall_abort": self.stall_abort_seconds,
                  "allow_lan": self.allow_lan, **overrides}
        return OllamaGateway(url or self.ollama_url, **kwargs)

    @staticmethod
    def load(path: pathlib.Path) -> ModelsConfig:
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as err:
            raise ConfigError(f"{path}: {err}") from err
        return ModelsConfig.parse(raw, str(path))

    @staticmethod
    def load_default(explicit: str | pathlib.Path | None = None) -> ModelsConfig:
        """``config/models.json`` plus – falls vorhanden – die persönlichen Abweichungen aus
        ``config/models.local.json`` (nur geänderte Werte nötig, z. B. die Ollama-Adresse).
        Mit ``explicit`` nur genau diese Datei."""
        if explicit:
            return ModelsConfig.load(pathlib.Path(explicit))
        base_path = repo_root() / "config" / "models.json"
        local_path = base_path.with_name("models.local.json")
        try:
            raw = json.loads(base_path.read_text(encoding="utf-8-sig"))
            source = str(base_path)
            if local_path.exists():
                raw = merge(raw, json.loads(local_path.read_text(encoding="utf-8-sig")))
                source += f" + {local_path.name}"
        except (OSError, json.JSONDecodeError) as err:
            raise ConfigError(f"Modellkonfiguration: {err}") from err
        return ModelsConfig.parse(raw, source)

    @staticmethod
    def parse(raw: dict[str, Any], source: str = "") -> ModelsConfig:
        ollama = raw.get("ollama", {})
        profiles: dict[str, ModelProfile] = {}
        for p in raw.get("profiles", []):
            profile = ModelProfile(
                name=str(p["name"]), model=str(p["model"]), context_tokens=int(p["contextTokens"]),
                max_output_tokens=int(p["maxOutputTokens"]), think=p.get("think"),
                offload=bool(p.get("offload", False)), temperature=float(p.get("temperature", 0.2)))
            validate_model_tag(profile.model)
            if profile.context_tokens < 2048 or profile.max_output_tokens >= profile.context_tokens:
                raise ConfigError(f"Profil {profile.name}: Kontext/Ausgabe unplausibel")
            if profile.name in profiles:
                raise ConfigError(f"Profil {profile.name} doppelt")
            profiles[profile.name] = profile
        if not profiles:
            raise ConfigError("Keine Modellprofile konfiguriert")
        roles: dict[str, list[str]] = {}
        for role in ROLES:
            wanted = raw.get("roles", {}).get(role) or []
            # Profile, die (z. B. durch eine eigene Profilliste in models.local.json) fehlen, werden übergangen.
            names = [n for n in wanted if n in profiles] or [next(iter(profiles))]
            roles[role] = names
        lim = raw.get("limits", {})
        limits = Limits(
            max_steps_per_task=int(lim.get("maxStepsPerTask", 20)),
            max_repair_attempts=int(lim.get("maxRepairAttempts", 3)),
            max_active_minutes=float(lim.get("maxActiveMinutes", 30)),
            max_sandbox_jobs=int(lim.get("maxSandboxJobs", 2)),
            max_gpu_requests=int(lim.get("maxGpuRequests", 1)),
            max_tasks=int(lim.get("maxTasks", 12)))
        cal = raw.get("calibration", {})
        calibration = CalibrationSettings(
            runs=int(cal.get("runs", 10)), min_vram_reserve_gib=float(cal.get("minVramReserveGiB", 2.0)),
            output_tokens=int(cal.get("outputTokens", 256)))
        url = str(ollama.get("url", "http://127.0.0.1:11434"))
        from localai.orchestrator.gateway import GatewayError, validate_loopback

        try:
            validate_loopback(url, bool(ollama.get("allowLan", False)))
        except GatewayError as err:
            raise ConfigError(f"ollama.url: {err}") from err
        return ModelsConfig(
            ollama_url=url,
            stall_warning_seconds=float(ollama.get("stallWarningSeconds", 45)),
            stall_abort_seconds=float(ollama.get("stallAbortSeconds", 900)),
            profiles=profiles, roles=roles, limits=limits, calibration=calibration,
            require_calibration=bool(raw.get("requireCalibration", True)), source=source,
            allow_lan=bool(ollama.get("allowLan", False)))


def merge(base: Any, override: Any) -> Any:
    """Objekte werden feldweise überlagert, alles andere (auch Listen) ersetzt."""
    if isinstance(base, dict) and isinstance(override, dict):
        result = dict(base)
        for key, value in override.items():
            if not key.startswith("_"):
                result[key] = merge(base.get(key), value)
        return result
    return override


# ---------------------------------------------------------------------- Kalibrierungsstand

@dataclass
class Measurement:
    """Ergebnis der Kalibrierung für Modell@Kontext auf einer Hardware."""

    model: str
    digest: str
    context_tokens: int
    hardware: str
    runs: int
    failures: int
    oom: bool
    vram_total: int | None
    vram_peak: int | None
    vram_reserve: int | None
    ram_peak_used: int | None
    offload_fraction: float | None
    prompt_tokens: int
    prompt_tps: float
    output_tps: float
    first_token_seconds: float
    chars_per_token: float
    stable: bool
    reason: str
    measured_at: str
    accepted_without_vram: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.model}@{self.context_tokens}"


class CalibrationStore:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.measurements: dict[str, Measurement] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for key, m in data.get("measurements", {}).items():
                    self.measurements[key] = Measurement(**m)
            except (OSError, json.JSONDecodeError, TypeError) as err:
                raise ConfigError(f"Kalibrierungsdatei {path} unlesbar: {err}") from err

    def put(self, m: Measurement) -> None:
        self.measurements[m.key] = m

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"schemaVersion": 1, "measurements": {k: vars(v) for k, v in sorted(self.measurements.items())}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, profile: ModelProfile) -> Measurement | None:
        return self.measurements.get(profile.key)

    def get_key(self, key: str) -> Measurement | None:
        return self.measurements.get(key)


@dataclass
class ProfileChoice:
    role: str
    profile: ModelProfile | None
    measurement: Measurement | None
    problems: list[str]

    def describe(self) -> str:
        if self.profile is None:
            return "kein nutzbares Profil"
        cal = "kalibriert" if self.measurement and self.measurement.stable else "NICHT kalibriert"
        return f"{self.profile.label()} ({cal})"


def choose_profiles(config: ModelsConfig, store: CalibrationStore, installed: dict[str, str],
                    hardware_fingerprint: str) -> dict[str, ProfileChoice]:
    """Wählt je Rolle das erste Profil der Vorzugsliste, das installiert und (falls verlangt) stabil kalibriert ist.

    ``installed``: Modellname → Digest aus ``/api/tags``.
    """
    choices: dict[str, ProfileChoice] = {}
    for role in ROLES:
        problems: list[str] = []
        chosen: ProfileChoice | None = None
        for name in config.roles[role]:
            profile = config.profiles[name]
            if profile.model not in installed:
                problems.append(f"{profile.label()}: Modell nicht installiert (ollama pull {profile.model})")
                continue
            m = store.get(profile)
            if m is None:
                if config.require_calibration:
                    problems.append(f"{profile.label()}: nicht kalibriert")
                    continue
            else:
                stale = []
                if m.digest and installed[profile.model] and m.digest != installed[profile.model]:
                    stale.append("Modell wurde seit der Kalibrierung geändert")
                if m.hardware != hardware_fingerprint:
                    stale.append("Hardware hat sich seit der Kalibrierung geändert")
                if not m.stable:
                    stale.append(f"nicht stabil ({m.reason})")
                elif m.offload_fraction is not None and m.offload_fraction > 0.05 and not profile.offload:
                    # Auch für ältere Messungen: ohne Offload-Profil muss das Modell in den Grafikspeicher passen.
                    stale.append(f"{m.offload_fraction:.0%} des Modells auf der CPU – passt nicht in den Grafikspeicher")
                if stale and config.require_calibration:
                    problems.append(f"{profile.label()}: " + "; ".join(stale))
                    continue
            chosen = ProfileChoice(role, profile, m, problems)
            break
        choices[role] = chosen or ProfileChoice(role, None, None, problems)
    return choices
