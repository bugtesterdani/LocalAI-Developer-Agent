"""Kalibrierung der Modellprofile (Spezifikation §9, SEC-16, AC09).

Für jedes Profil (Modell + Kontext) werden ``runs`` Requests mit nahezu vollem Zielkontext gesendet und dabei
VRAM-/RAM-Spitzen, CPU-Offload, Prompt-/Ausgabegeschwindigkeit und Zeit bis zur ersten Ausgabe gemessen.
Ein Profil gilt als stabil, wenn alle Läufe ohne Fehler/OOM durchlaufen und mindestens ``minVramReserveGiB``
VRAM frei bleiben. Ergebnis: ``<Daten>/calibration.json`` und ein deutscher Bericht ``kalibrierung-*.md``.

Aufruf (auf dem Rechner mit Ollama):
  uv run --project src/python python -m localai.orchestrator.calibrate
  uv run --project src/python python -m localai.orchestrator.calibrate --profile standard --laeufe 10
  uv run --project src/python python -m localai.orchestrator.calibrate --kontexte 4096,8192,16384,32768 --laeufe 2
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import pathlib
import statistics
import sys
import uuid

from localai.orchestrator import hardware
from localai.orchestrator.gateway import ChatRequest, GatewayError, OllamaGateway, OutOfMemory, StreamCallbacks
from localai.orchestrator.models import (
    CalibrationStore,
    ConfigError,
    Measurement,
    ModelProfile,
    ModelsConfig,
    default_data_dir,
)

GIB = hardware.GIB
FILLER_UNITS = [
    "public static int Add{n}(int a, int b) => a + b + {n};",
    "def handle_{n}(items: list[int]) -> int:\n    return sum(i * {n} for i in items if i > 0)",
    "for ($i = 0; $i -lt {n}; $i++) {{ Write-Output \"line $i\" }}",
    "std::vector<int> v{n}(16, {n}); std::sort(v{n}.begin(), v{n}.end());",
    "if exist build_{n}.log del build_{n}.log",
    "read_image (Image{n}, 'part_{n}.png')\nthreshold (Image{n}, Region{n}, 0, {n})",
]


def filler(target_chars: int, nonce: str) -> str:
    """Code-ähnlicher Text in mehreren Zielsprachen; der Nonce verhindert Prompt-Caching zwischen Läufen."""
    parts = [f"// calibration {nonce}"]
    size = len(parts[0])
    n = 0
    while size < target_chars:
        line = FILLER_UNITS[n % len(FILLER_UNITS)].format(n=n)
        parts.append(line)
        size += len(line) + 1
        n += 1
    return "\n".join(parts)


class NoSampler:
    """Keine Messung (Ollama auf einem anderen Rechner)."""

    vram_peak = vram_total = ram_peak_used = None

    def __enter__(self) -> NoSampler:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


@dataclasses.dataclass
class Target:
    profile_name: str
    model: str
    context_tokens: int
    think: bool | None
    offload: bool


def measure(gateway: OllamaGateway, target: Target, runs: int, output_tokens: int, min_reserve_gib: float,
            hw: hardware.Hardware, digest: str, accept_unknown_vram: bool, sampler_factory=hardware.PeakSampler,
            log=print, max_offload: float = 0.05) -> Measurement:
    cpt = 3.0
    fill_tokens = target.context_tokens - output_tokens - 300
    prompt_tokens_seen: list[int] = []
    prompt_tps: list[float] = []
    output_tps: list[float] = []
    first: list[float] = []
    errors: list[str] = []
    oom = False
    vram_peak = vram_total = ram_peak = None
    offload: float | None = None
    ratios: list[float] = []
    for run in range(1, runs + 1):
        text = filler(int(fill_tokens * cpt), uuid.uuid4().hex)
        request = ChatRequest(model=target.model, num_ctx=target.context_tokens, num_predict=output_tokens,
                              temperature=0.0, think=False if target.think else None, messages=[
                                  {"role": "system", "content": "You summarise code."},
                                  {"role": "user", "content": text + "\n\nSummarise the code above in one sentence."}])
        try:
            with sampler_factory() as sampler:
                result = gateway.chat(request, callbacks=StreamCallbacks())
        except OutOfMemory as err:
            oom = True
            errors.append(f"Lauf {run}: Speichermangel: {err}")
            log(f"    Lauf {run}/{runs}: SPEICHERMANGEL – {err}")
            break
        except GatewayError as err:
            errors.append(f"Lauf {run}: {err}")
            log(f"    Lauf {run}/{runs}: Fehler – {err}")
            continue
        if sampler.vram_peak is not None:
            vram_peak = max(vram_peak or 0, sampler.vram_peak)
            vram_total = sampler.vram_total
        if sampler.ram_peak_used is not None:
            ram_peak = max(ram_peak or 0, sampler.ram_peak_used)
        if run == 1:
            try:
                for m in gateway.running():
                    if m.get("name") == target.model or m.get("model") == target.model:
                        size, size_vram = int(m.get("size") or 0), int(m.get("size_vram") or 0)
                        offload = round(1 - size_vram / size, 3) if size else None
            except GatewayError:
                pass
        if result.prompt_tokens:
            prompt_tokens_seen.append(result.prompt_tokens)
            ratios.append(len(request.messages[1]["content"]) / result.prompt_tokens)
            # Tatsächliches Verhältnis Zeichen/Token übernehmen; nahe der Grenze schneidet Ollama still ab → kleiner füllen.
            cpt = statistics.median(ratios)
            if result.prompt_tokens > target.context_tokens - output_tokens - 64:
                fill_tokens = int(fill_tokens * 0.9)
        if result.prompt_seconds > 0:
            prompt_tps.append(result.prompt_tokens / result.prompt_seconds)
        if result.output_seconds > 0:
            output_tps.append(result.tokens_per_second)
        if result.first_token_seconds is not None:
            first.append(result.first_token_seconds)
        reserve = f", VRAM frei {(vram_total - vram_peak) / GIB:.1f} GiB" if vram_total and vram_peak else ""
        log(f"    Lauf {run}/{runs}: {result.prompt_tokens} Token Eingabe, {result.tokens_per_second:.1f} Token/s"
            f", erste Ausgabe nach {result.first_token_seconds or 0:.1f} s{reserve}")
    succeeded = len(output_tps) if output_tps else len(prompt_tokens_seen)
    failures = runs - succeeded if not oom else runs
    reserve = (vram_total - vram_peak) if vram_total is not None and vram_peak is not None else None
    reasons = []
    if oom:
        reasons.append("Speichermangel")
    if failures and not oom:
        reasons.append(f"{failures} von {runs} Läufen fehlgeschlagen")
    if runs < 10:
        reasons.append(f"nur {runs} Läufe (Freigabe erfordert 10)")
    if reserve is None:
        if not accept_unknown_vram:
            where = "Ollama läuft auf einem anderen Rechner" if hw.remote_host else "nvidia-smi fehlt"
            reasons.append(f"VRAM nicht messbar ({where}) – mit --ohne-vram-messung bewusst akzeptieren")
    elif reserve < min_reserve_gib * GIB:
        reasons.append(f"VRAM-Reserve {reserve / GIB:.1f} GiB < {min_reserve_gib:g} GiB")
    if offload is not None and offload > max_offload and not target.offload:
        # Modell passt nicht in den Grafikspeicher: dann ist auch keine VRAM-Reserve übrig (§9). Bewusst zulassen über
        # ein Profil mit "offload": true oder einen höheren Grenzwert calibration.maxCpuOffload.
        reasons.append(f"{offload:.0%} des Modells auf der CPU – passt nicht in den Grafikspeicher "
                       "(kleineres Modell, Profil mit \"offload\": true oder calibration.maxCpuOffload erhöhen)")
    stable = not reasons
    return Measurement(
        model=target.model, digest=digest, context_tokens=target.context_tokens, hardware=hw.fingerprint(), runs=runs,
        failures=failures, oom=oom, vram_total=vram_total, vram_peak=vram_peak, vram_reserve=reserve,
        ram_peak_used=ram_peak, offload_fraction=offload,
        prompt_tokens=int(statistics.mean(prompt_tokens_seen)) if prompt_tokens_seen else 0,
        prompt_tps=round(statistics.mean(prompt_tps), 1) if prompt_tps else 0.0,
        output_tps=round(statistics.mean(output_tps), 1) if output_tps else 0.0,
        first_token_seconds=round(statistics.mean(first), 2) if first else 0.0,
        chars_per_token=round(statistics.median(ratios), 3) if ratios else 0.0,
        stable=stable, reason="; ".join(reasons) or "stabil",
        measured_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        accepted_without_vram=reserve is None and accept_unknown_vram, errors=errors[:20])


def report(path: pathlib.Path, hw: hardware.Hardware, version: str, measurements: list[tuple[Target, Measurement]]) -> None:
    def gib(v: int | None) -> str:
        return f"{v / GIB:.1f}" if v is not None else "–"

    lines = [f"# Kalibrierung {dt.datetime.now():%d.%m.%Y %H:%M}", "", f"- Hardware: {hw.describe()}",
             f"- Fingerabdruck: `{hw.fingerprint()}`", f"- Ollama: {version}", "",
             "| Profil | Modell | Kontext | Läufe | Fehler | VRAM-Spitze GiB | VRAM frei GiB | RAM-Spitze GiB | CPU-Offload "
             "| Prompt Token/s | Ausgabe Token/s | erste Ausgabe s | Ergebnis |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for t, m in measurements:
        off = f"{m.offload_fraction:.0%}" if m.offload_fraction is not None else "–"
        lines.append(f"| {t.profile_name} | {m.model} | {m.context_tokens} | {m.runs} | {m.failures} | {gib(m.vram_peak)} "
                     f"| {gib(m.vram_reserve)} | {gib(m.ram_peak_used)} | {off} | {m.prompt_tps} | {m.output_tps} "
                     f"| {m.first_token_seconds} | {'freigegeben' if m.stable else 'gesperrt: ' + m.reason} |")
    lines += ["", "VRAM-/RAM-Werte sind systemweite Spitzen während der Läufe (inkl. anderer Programme).",
              "Ollama-Einstellungen siehe docs/pruefung-M3.md."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="localai.orchestrator.calibrate", description="Modellprofile kalibrieren")
    parser.add_argument("--models", help="nur diese Datei (Standard: config/models.json + config/models.local.json)")
    parser.add_argument("--profile", action="append", help="nur dieses Profil (mehrfach möglich)")
    parser.add_argument("--laeufe", type=int, help="Läufe pro Profil (Standard aus models.json, 10)")
    parser.add_argument("--kontexte", help="zusätzliche Messreihe je Modell, z. B. 4096,8192,16384,32768")
    parser.add_argument("--ohne-vram-messung", action="store_true",
                        help="Profile auch ohne messbaren VRAM freigeben (z. B. ohne NVIDIA-GPU)")
    parser.add_argument("--daten", help="Datenordner (Standard wie der Broker: %%LOCALAPPDATA%%\\LocalAi\\broker)")
    parser.add_argument("--ollama", help="Ollama-Adresse (nur Loopback)")
    args = parser.parse_args(argv)
    try:
        config = ModelsConfig.load_default(args.models)
    except ConfigError as err:
        print(f"FEHLER: {err}", file=sys.stderr)
        return 2
    try:
        gateway = config.gateway(args.ollama, stall_warning=1e9, stall_abort=1800)
    except GatewayError as err:
        print(f"FEHLER: {err}", file=sys.stderr)
        return 2
    data_dir = pathlib.Path(args.daten) if args.daten else default_data_dir()
    try:
        version = gateway.version()
        installed = {m["name"]: m.get("digest", "") for m in gateway.list_models()}
    except GatewayError as err:
        print(f"FEHLER: {err}", file=sys.stderr)
        return 3
    hw = hardware.detect(gateway.url)
    # Ollama auf einem anderen Rechner: hiesige GPU/RAM-Werte wären falsch, also nicht messen.
    sampler = hardware.PeakSampler if hw.remote_host is None else NoSampler
    print(f"Hardware: {hw.describe()}\nOllama {version}, {len(installed)} Modelle installiert\nDaten: {data_dir}")
    selected: list[ModelProfile] = [p for p in config.profiles.values() if not args.profile or p.name in args.profile]
    if args.profile and len(selected) != len(set(args.profile)):
        print(f"FEHLER: unbekanntes Profil in {args.profile}; vorhanden: {', '.join(config.profiles)}", file=sys.stderr)
        return 2
    targets: list[Target] = []
    for p in selected:
        if p.model not in installed:
            print(f"übersprungen: {p.label()} – Modell nicht installiert (ollama pull {p.model})")
            continue
        targets.append(Target(p.name, p.model, p.context_tokens, p.think, p.offload))
    if args.kontexte:
        for model in dict.fromkeys(t.model for t in list(targets)):
            base = next(t for t in targets if t.model == model)
            for ctx in (int(c) for c in args.kontexte.split(",") if c.strip()):
                if not any(t.model == model and t.context_tokens == ctx for t in targets):
                    targets.append(Target(f"messreihe-{ctx // 1024}K", model, ctx, base.think, base.offload))
    if not targets:
        print("Nichts zu kalibrieren.")
        return 1
    store = CalibrationStore(data_dir / "calibration.json")
    runs = args.laeufe or config.calibration.runs
    done: list[tuple[Target, Measurement]] = []
    try:
        for t in targets:
            print(f"\n{t.profile_name}: {t.model} mit {t.context_tokens} Token Kontext, {runs} Läufe")
            m = measure(gateway, t, runs, config.calibration.output_tokens, config.calibration.min_vram_reserve_gib, hw,
                        installed[t.model], args.ohne_vram_messung, sampler_factory=sampler,
                        max_offload=config.calibration.max_cpu_offload)
            existing = store.get_key(m.key)
            if t.profile_name.startswith("messreihe-") and existing is not None and existing.stable and existing.runs >= m.runs:
                print("  (Messreihe ersetzt die stabile Profilkalibrierung nicht)")
            else:
                store.put(m)
                store.save()
            done.append((t, m))
            print(f"  → {'freigegeben' if m.stable else 'gesperrt: ' + m.reason}")
    except KeyboardInterrupt:
        print("\nAbgebrochen – bisherige Messungen sind gespeichert.")
    finally:
        if done:
            path = data_dir / f"kalibrierung-{dt.datetime.now():%Y%m%d-%H%M}.md"
            report(path, hw, version, done)
            print(f"\nBericht: {path}\nKalibrierung: {store.path}")
    return 0 if all(m.stable for _, m in done) else 1


if __name__ == "__main__":
    raise SystemExit(main())
