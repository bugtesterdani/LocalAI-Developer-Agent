# Local AI Developer Agent

Lokaler Entwicklungsagent mit fünf Rollen (Planner, Coder, Tester, Debugger, Reviewer) auf Basis von Ollama.
Jeder Tool-Aufruf braucht eine ausdrückliche Freigabe; ausgeführt wird nur in einer Offline-VM.

- Spezifikation: [docs/Local-AI-Developer-Agent-Projektspezifikation.md](docs/Local-AI-Developer-Agent-Projektspezifikation.md)
- Umsetzungsplan: [docs/Umsetzungsplan.md](docs/Umsetzungsplan.md)
- Prüfanleitung aktueller Meilenstein: [docs/pruefung-M1.md](docs/pruefung-M1.md)
- Anforderungen → Tests: [docs/sicherheit/anforderungen-tests.md](docs/sicherheit/anforderungen-tests.md)

## Stand: Meilenstein M1 – Freigabe-Kern

| Teil | Sprache | Status |
|---|---|---|
| Verträge (JSON Schema → C# + Pydantic), kanonisches JSON | C# / Python | fertig, Kreuztests grün |
| Approval Broker, Capabilities, Audit-Journal mit Hashkette | C# | fertig |
| Broker-Prozess mit Named Pipe und Rollen-Tokens | C# | fertig |
| Freigabe-Oberfläche inkl. Sammelfreigabe mit Abwahl, Not-Aus | C# / WPF | fertig, **unter Windows zu prüfen** |
| Python-Client und Fake-Agent | Python | fertig |
| Offline-VM, echte Dateizugriffe | – | M2 |
| Modelle, fünf Agenten | – | M3 |

## Struktur

```text
contracts/            JSON Schema (einzige Quelle der Verträge) und Testvektoren
config/localai.json   Startbefehl des Fake-Agenten
docs/                 Spezifikation, Plan, Prüfanleitungen
scripts/              test.ps1 / start.ps1 (Windows), test.sh (Linux)
src/dotnet/           LocalAi.slnx
  LocalAi.Contracts     generierte Records, kanonisches JSON, Pipe-Protokoll
  LocalAi.Broker        Approval Broker, Policy, Audit-Journal, Testadapter
  LocalAi.Broker.Host   Broker-Prozess (Named-Pipe-Server)
  LocalAi.Desktop.Core  ViewModels, Broker-Client, deutsche Texte (UiText.resx)
  LocalAi.Desktop       WPF-Oberfläche
  LocalAi.Tests         Tests (eigener Runner, siehe unten)
src/python/           Paket localai (Pipe-Client, Fake-Agent, Verträge), Tests
tests/integration/    pytest gegen den echten Broker-Prozess
tools/codegen/        Generatoren für Verträge, Testvektoren, Oberflächentexte
```

## Voraussetzungen (Windows)

- .NET 10 SDK
- [uv](https://docs.astral.sh/uv/) (installiert bei Bedarf auch Python 3.12+)
- Git

## Schnellstart

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1    # alles bauen und testen
powershell -ExecutionPolicy Bypass -File scripts\start.ps1   # Oberfläche starten
```

## Verträge ändern

1. `contracts/schemas/contracts.schema.json` anpassen.
2. `python tools/codegen/generate.py` – erzeugt `Contracts.g.cs` und `generated.py`.
3. Bei Änderungen an der Kanonisierung: `python tools/codegen/make_testvectors.py` und den Diff bewusst prüfen.

Oberflächentexte stehen in `tools/codegen/make_uitext.py` und werden nach `UiText.resx` erzeugt.

## Bekannte Übergangslösungen

- **Tests ohne xUnit:** In der Build-Umgebung war NuGet nicht erreichbar. `LocalAi.Tests` nutzt deshalb einen kleinen eigenen Runner (`dotnet run --project src/dotnet/LocalAi.Tests`). Umstieg auf xUnit, sobald NuGet verfügbar ist.
- **Audit-Journal statt SQLite:** Das Journal ist eine hashverkettete JSON-Lines-Datei. SQLite (Spezifikation §10) folgt zusammen mit NuGet.
- **Rollen über Tokens:** Orchestrator und UI unterscheiden sich in M1 über zufällige Tokens pro Broker-Start; die Pipe ist auf den aktuellen Windows-Benutzer beschränkt. Getrennte Windows-Identitäten für den Orchestrator folgen mit M3.
