"""Modellprofile, Grenzen und Kalibrierungsstand (Spezifikation §5, §9).

Konfiguration: ``config/models.json``. Modellnamen sind austauschbar; bei Ollama sind ``latest`` oder fehlende
Tags unzulässig, bei Edge0 ist der Modellname der Alias des Servers (z. B. ``edge0_35b``). Welcher Modellserver ein
Profil bedient, sagt ``backend`` (oberste Ebene als Standard, je Profil überschreibbar): ``ollama`` oder ``edge0``. Ein Profil wird nur verwendet, wenn die Kalibrierung es für dieses Modell (Digest), diesen
Kontext und diese Hardware als stabil ausweist – außer ``requireCalibration`` ist ausdrücklich ``false``.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Any

from localai.orchestrator.history import HistorySettings

ROLES = ("Planner", "Coder", "Tester", "Debugger", "Reviewer")
BACKENDS = ("ollama", "edge0")
BACKEND_LABELS = {"ollama": "Ollama", "edge0": "Edge0"}
_EDGE0_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


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


def user_config_dir() -> pathlib.Path:
    """Eigene Konfiguration einer installierten Fassung – gleiche Regel wie HostOptions.UserConfigDirectory (C#)."""
    if os.environ.get("LOCALAI_CONFIG_DIR"):
        return pathlib.Path(os.environ["LOCALAI_CONFIG_DIR"])
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / "AppData" / "Local")
        return pathlib.Path(root) / "LocalAi" / "config"
    return pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config") / "localai"


def is_development_checkout(root: pathlib.Path) -> bool:
    """Repository statt Installation – gleiche Regel wie HostOptions.IsDevelopmentCheckout (C#)."""
    return (root / ".git").exists() or (root / "src" / "python" / ".venv").is_dir()


def repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[4]


def validate_model_tag(model: str) -> None:
    if ":" not in model or not model.split(":", 1)[1]:
        raise ConfigError(f"Modell {model!r} ohne Tag – bitte festen Tag angeben (z. B. qwen2.5-coder:14b)")
    if model.split(":", 1)[1].lower() == "latest":
        raise ConfigError(f"Modell {model!r}: Tag 'latest' ist unzulässig (Spezifikation §9)")


def validate_edge0_alias(model: str) -> None:
    """Edge0 bedient ein Modell unter seinem Alias (``edge0-infer serve --alias``, Standard z. B. ``edge0_35b``)."""
    if not _EDGE0_ALIAS.match(model):
        raise ConfigError(f"Edge0-Modell {model!r}: erwartet wird der Alias des Servers, z. B. edge0_35b")


def _backend(value: object, where: str) -> str:
    name = str(value).strip().lower()
    if name not in BACKENDS:
        raise ConfigError(f"{where}: unbekannter Modellserver {value!r} (erlaubt: {', '.join(BACKENDS)})")
    return name


@dataclass(frozen=True)
class ModelProfile:
    name: str
    model: str
    context_tokens: int
    max_output_tokens: int
    think: bool | None = None
    offload: bool = False
    temperature: float = 0.2
    options: tuple[tuple[str, float], ...] = ()  # zusätzliche Ollama-Optionen, z. B. repeat_penalty
    # Ausgabe (Denkphase + Antwort) darf den ganzen Rest des Kontexts nutzen; maxOutputTokens ist dann nur der Anteil,
    # der beim Zusammenstellen des Prompts mindestens frei bleibt.
    output_up_to_context: bool = False
    backend: str = "ollama"  # Modellserver: "ollama" oder "edge0"
    reasoning_effort: str | None = None  # nur Edge0: reasoning_effort je Anfrage (z. B. "none", "low")

    @property
    def key(self) -> str:
        return f"{self.model}@{self.context_tokens}"

    def label(self) -> str:
        server = "" if self.backend == "ollama" else f" · {BACKEND_LABELS[self.backend]}"
        return f"{self.name} · {self.model} · {self.context_tokens // 1024}K{server}"


@dataclass(frozen=True)
class Limits:
    max_steps_per_task: int = 20
    max_repair_attempts: int = 3
    max_active_minutes: float = 30.0
    max_sandbox_jobs: int = 2
    max_gpu_requests: int = 1
    max_tasks: int = 12
    # Modellschritte je Rolle (überschreibt max_steps_per_task), z. B. {"Planner": 10}
    steps_per_role: dict[str, int] = field(default_factory=dict)
    max_plan_attempts: int = 2  # ungültige Pläne, bevor der Planner aufgibt
    max_patch_retries: int = 3  # Patch passt nicht → Agent korrigiert höchstens so oft
    max_denials_per_task: int = 3  # vom Nutzer abgelehnte Vorschläge je Aufgabe, dann Rückfrage (0 = unbegrenzt)
    max_invalid_answers: int = 0  # unbrauchbare Antworten (falsches Format) je Aufgabe (0 = nur Schrittlimit)

    def steps_for(self, role: str) -> int:
        return self.steps_per_role.get(role, self.max_steps_per_task)


@dataclass(frozen=True)
class CalibrationSettings:
    runs: int = 10
    min_vram_reserve_gib: float = 2.0
    output_tokens: int = 256
    max_cpu_offload: float = 0.05  # höchster CPU-Anteil für Profile ohne "offload": true


def _bounded(raw: dict, key: str, default: int, low: int, high: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ConfigError(f"limits.{key} muss eine ganze Zahl von {low} bis {high} sein")
    return value


def _steps_per_role(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise ConfigError("limits.maxStepsPerRole muss ein Objekt sein, z. B. {\"Planner\": 10}")
    result: dict[str, int] = {}
    for role, value in raw.items():
        if role not in ROLES:
            raise ConfigError(f"limits.maxStepsPerRole: unbekannte Rolle {role} (erlaubt: {', '.join(ROLES)})")
        result[role] = _bounded(raw, role, 20, 1, 200)
    return result


@dataclass(frozen=True)
class Edge0Settings:
    """Abschnitt ``edge0``: Adresse von ``edge0-infer serve`` und Verhalten des Gateways."""

    url: str = "http://127.0.0.1:8000"
    allow_lan: bool = False
    api_key_file: str = ""  # absoluter Pfad; Inhalt = Token des Servers (--api-key-file), nötig außerhalb von Loopback
    stall_warning_seconds: float = 45.0
    stall_abort_seconds: float = 900.0
    busy_wait_seconds: float = 600.0  # so lange auf einen belegten Server warten (HTTP 429)


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
    history: HistorySettings = field(default_factory=HistorySettings)
    backend: str = "ollama"  # Standard-Modellserver für Profile ohne eigenes "backend"
    edge0: Edge0Settings = field(default_factory=Edge0Settings)

    def backends_in_use(self) -> list[str]:
        """Modellserver der Profile, der Standard (``backend``) zuerst."""
        used = {p.backend for p in self.profiles.values()}
        return [b for b in dict.fromkeys((self.backend, *BACKENDS)) if b in used]

    @property
    def primary_backend(self) -> str:
        used = self.backends_in_use()
        return used[0] if used else self.backend

    def backend_url(self, backend: str) -> str:
        return self.edge0.url if backend == "edge0" else self.ollama_url

    @property
    def primary_url(self) -> str:
        """Adresse des Standard-Modellservers (Hardware-Fingerabdruck, Meldungen)."""
        return self.backend_url(self.primary_backend)

    def describe_backends(self) -> str:
        return ", ".join(f"{BACKEND_LABELS[b]} {self.backend_url(b)}" for b in self.backends_in_use())

    def gateway(self, url: str | None = None, edge0_url: str | None = None, **overrides):
        """Gateway mit den Einstellungen dieser Konfiguration (eine Stelle für Adressen und LAN-Freigabe).

        Nutzen alle Profile denselben Modellserver, ist es dessen Gateway; sonst verteilt ein RoutingGateway die
        Anfragen nach Modell. ``url`` überschreibt die Ollama-Adresse, ``edge0_url`` die Edge0-Adresse."""
        from localai.orchestrator.gateway import Edge0Gateway, OllamaGateway, RoutingGateway

        gateways = {}
        for backend in self.backends_in_use():
            if backend == "ollama":
                kwargs = {"stall_warning": self.stall_warning_seconds, "stall_abort": self.stall_abort_seconds,
                          "allow_lan": self.allow_lan, **overrides}
                gateways[backend] = OllamaGateway(url or self.ollama_url, **kwargs)
            else:
                e = self.edge0
                kwargs = {"stall_warning": e.stall_warning_seconds, "stall_abort": e.stall_abort_seconds,
                          "allow_lan": e.allow_lan, "api_key_file": e.api_key_file, "busy_wait": e.busy_wait_seconds,
                          **overrides}
                gateways[backend] = Edge0Gateway(edge0_url or e.url, **kwargs)
        if len(gateways) == 1:
            return next(iter(gateways.values()))
        routes = {p.model: p.backend for p in self.profiles.values()}
        return RoutingGateway(gateways, routes, self.primary_backend)

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
        try:
            raw = json.loads(base_path.read_text(encoding="utf-8-sig"))
            source = str(base_path)
            # Repository: config/models.local.json; installierte Fassung: Benutzerordner (siehe user_config_dir).
            # Wie im Broker: in einem Entwicklungs-Checkout gilt nur die Datei im Repository.
            candidates = [base_path.with_name("models.local.json")]
            if not is_development_checkout(repo_root()):
                candidates.append(user_config_dir() / "models.local.json")
            for local_path in dict.fromkeys(candidates):
                if local_path.exists():
                    raw = merge(raw, json.loads(local_path.read_text(encoding="utf-8-sig")))
                    source += f" + {local_path}"
        except (OSError, json.JSONDecodeError) as err:
            raise ConfigError(f"Modellkonfiguration: {err}") from err
        return ModelsConfig.parse(raw, source)

    @staticmethod
    def parse(raw: dict[str, Any], source: str = "") -> ModelsConfig:
        ollama = raw.get("ollama", {})
        default_backend = _backend(raw.get("backend", "ollama"), "backend")
        profiles: dict[str, ModelProfile] = {}
        served_by: dict[str, str] = {}
        for p in raw.get("profiles", []):
            backend = _backend(p.get("backend", default_backend), f"Profil {p.get('name')}: backend")
            effort = p.get("reasoningEffort")
            if effort is not None:
                if backend != "edge0":
                    raise ConfigError(f"Profil {p.get('name')}: reasoningEffort gilt nur für Edge0-Profile "
                                      "(bei Ollama steuert \"think\" die Denkphase)")
                if not isinstance(effort, str) or not effort.strip():
                    raise ConfigError(f"Profil {p.get('name')}: reasoningEffort muss ein Text sein, z. B. \"none\"")
            profile = ModelProfile(
                name=str(p["name"]), model=str(p["model"]), context_tokens=int(p["contextTokens"]),
                max_output_tokens=int(p["maxOutputTokens"]), think=p.get("think"),
                offload=bool(p.get("offload", False)), temperature=float(p.get("temperature", 0.2)),
                output_up_to_context=bool(p.get("outputUpToContext", False)),
                options=tuple(sorted((str(k), float(v)) for k, v in (p.get("options") or {}).items())),
                backend=backend, reasoning_effort=effort.strip() if isinstance(effort, str) else None)
            if backend == "edge0":
                validate_edge0_alias(profile.model)
            else:
                validate_model_tag(profile.model)
            if profile.context_tokens < 2048 or profile.max_output_tokens >= profile.context_tokens:
                raise ConfigError(f"Profil {profile.name}: Kontext/Ausgabe unplausibel")
            if profile.name in profiles:
                raise ConfigError(f"Profil {profile.name} doppelt")
            if served_by.setdefault(profile.model, backend) != backend:
                raise ConfigError(f"Modell {profile.model} steht in Profilen mit verschiedenen Modellservern – je Modell "
                                  "nur ein backend")
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
            max_tasks=int(lim.get("maxTasks", 12)),
            steps_per_role=_steps_per_role(lim.get("maxStepsPerRole") or {}),
            max_plan_attempts=_bounded(lim, "maxPlanAttempts", 2, 1, 10),
            max_patch_retries=_bounded(lim, "maxPatchRetries", 3, 0, 20),
            max_denials_per_task=_bounded(lim, "maxDenialsPerTask", 3, 0, 100),
            max_invalid_answers=_bounded(lim, "maxInvalidAnswersPerTask", 0, 0, 100))
        cal = raw.get("calibration", {})
        calibration = CalibrationSettings(
            runs=int(cal.get("runs", 10)), min_vram_reserve_gib=float(cal.get("minVramReserveGiB", 2.0)),
            max_cpu_offload=float(cal.get("maxCpuOffload", 0.05)),
            output_tokens=int(cal.get("outputTokens", 256)))
        history = _history(raw.get("history") or {}, profiles)
        url = str(ollama.get("url", "http://127.0.0.1:11434"))
        from localai.orchestrator.gateway import GatewayError, validate_loopback

        try:
            validate_loopback(url, bool(ollama.get("allowLan", False)))
        except GatewayError as err:
            raise ConfigError(f"ollama.url: {err}") from err
        edge0 = _edge0(raw.get("edge0") or {})
        return ModelsConfig(
            ollama_url=url,
            stall_warning_seconds=float(ollama.get("stallWarningSeconds", 45)),
            stall_abort_seconds=float(ollama.get("stallAbortSeconds", 900)),
            profiles=profiles, roles=roles, limits=limits, calibration=calibration,
            require_calibration=bool(raw.get("requireCalibration", True)), source=source,
            allow_lan=bool(ollama.get("allowLan", False)), history=history, backend=default_backend, edge0=edge0)


def _edge0(raw: object) -> Edge0Settings:
    if not isinstance(raw, dict):
        raise ConfigError("edge0 muss ein Objekt sein, z. B. {\"url\": \"http://127.0.0.1:8000\"}")
    from localai.orchestrator.gateway import GatewayError, validate_loopback

    defaults = Edge0Settings()
    url = str(raw.get("url", defaults.url))
    allow_lan = bool(raw.get("allowLan", False))
    try:
        validate_loopback(url, allow_lan, "Edge0", "edge0")
    except GatewayError as err:
        raise ConfigError(f"edge0.url: {err}") from err
    key_file = raw.get("apiKeyFile") or ""
    if not isinstance(key_file, str):
        raise ConfigError("edge0.apiKeyFile muss ein Pfad sein")
    if key_file and not (pathlib.PureWindowsPath(key_file).is_absolute() or pathlib.PurePosixPath(key_file).is_absolute()):
        raise ConfigError(f"edge0.apiKeyFile muss ein absoluter Pfad sein, nicht {key_file!r}")
    return Edge0Settings(
        url=url, allow_lan=allow_lan, api_key_file=key_file,
        stall_warning_seconds=float(raw.get("stallWarningSeconds", defaults.stall_warning_seconds)),
        stall_abort_seconds=float(raw.get("stallAbortSeconds", defaults.stall_abort_seconds)),
        busy_wait_seconds=float(raw.get("busyWaitSeconds", defaults.busy_wait_seconds)))


def _history(raw: object, profiles: dict[str, ModelProfile]) -> HistorySettings:
    if not isinstance(raw, dict):
        raise ConfigError("history muss ein Objekt sein, z. B. {\"mode\": \"summary\"}")
    mode = raw.get("mode", "summary")
    if mode not in ("summary", "trim", "off"):
        raise ConfigError("history.mode: erlaubt sind summary, trim, off")
    names = raw.get("summaryProfiles") or []
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ConfigError("history.summaryProfiles muss eine Liste von Profilnamen sein (leer = Modell der Rolle)")
    # Wie bei den Rollen: Profile, die (z. B. durch eine eigene Profilliste in models.local.json) fehlen, übergehen.
    return HistorySettings(
        mode=mode, summary_profiles=tuple(n for n in names if n in profiles),
        keep_recent=_bounded(raw, "keepRecentNotes", 3, 0, 20),
        max_note_tokens=_bounded(raw, "maxNoteTokens", 1500, 200, 100_000),
        summary_tokens=_bounded(raw, "summaryTokens", 600, 100, 4000))


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


def _fingerprint_for(hardware_fingerprint: str | dict[str, str], backend: str) -> str:
    """Ein Fingerabdruck für alle Profile oder einer je Modellserver (``{"ollama": …, "edge0": …}``): Laufen Ollama und
    Edge0 auf verschiedenen Rechnern, gilt eine Messung nur für den Rechner des Servers, der das Profil bedient."""
    if isinstance(hardware_fingerprint, str):
        return hardware_fingerprint
    return hardware_fingerprint.get(backend, "")


def _usable(config: ModelsConfig, store: CalibrationStore, installed: dict[str, str],
            hardware_fingerprint: str | dict[str, str], role: str, names: list[str] | tuple[str, ...]) -> ProfileChoice:
    """Erstes Profil aus ``names``, das installiert und (falls verlangt) stabil kalibriert ist."""
    problems: list[str] = []
    for name in names:
        profile = config.profiles[name]
        if profile.model not in installed:
            if profile.backend == "edge0":
                problems.append(f"{profile.label()}: Edge0 bietet das Modell nicht an (läuft edge0-infer serve mit "
                                f"--alias {profile.model}?)")
            else:
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
            if m.hardware != _fingerprint_for(hardware_fingerprint, profile.backend):
                stale.append("Hardware hat sich seit der Kalibrierung geändert")
            if not m.stable:
                stale.append(f"nicht stabil ({m.reason})")
            elif (m.offload_fraction is not None and m.offload_fraction > config.calibration.max_cpu_offload
                  and not profile.offload):
                # Auch für ältere Messungen: ohne Offload-Profil muss das Modell in den Grafikspeicher passen.
                stale.append(f"{m.offload_fraction:.0%} des Modells auf der CPU – passt nicht in den Grafikspeicher")
            if stale and config.require_calibration:
                problems.append(f"{profile.label()}: " + "; ".join(stale))
                continue
        return ProfileChoice(role, profile, m, problems)
    return ProfileChoice(role, None, None, problems)


def choose_profiles(config: ModelsConfig, store: CalibrationStore, installed: dict[str, str],
                    hardware_fingerprint: str | dict[str, str]) -> dict[str, ProfileChoice]:
    """Wählt je Rolle das erste Profil der Vorzugsliste, das installiert und (falls verlangt) stabil kalibriert ist.

    ``installed``: Modellname → Digest aus ``/api/tags`` (Edge0: Alias aus ``/v1/models``, ohne Digest).
    ``hardware_fingerprint``: einer für alle Profile oder einer je Modellserver (siehe ``_fingerprint_for``).
    """
    return {role: _usable(config, store, installed, hardware_fingerprint, role, config.roles[role]) for role in ROLES}


def choose_summary_profile(config: ModelsConfig, store: CalibrationStore, installed: dict[str, str],
                           hardware_fingerprint: str | dict[str, str]) -> ProfileChoice | None:
    """Eigenes Modell zum Zusammenfassen der Agentennotizen; None = keins eingetragen (Modell der Rolle)."""
    if config.history.mode != "summary" or not config.history.summary_profiles:
        return None
    return _usable(config, store, installed, hardware_fingerprint, "Zusammenfassung", config.history.summary_profiles)
