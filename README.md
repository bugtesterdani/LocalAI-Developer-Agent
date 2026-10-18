# Local AI Developer Agent

Lokaler Entwicklungsagent mit fünf Rollen (Planner, Coder, Tester, Debugger, Reviewer) auf Basis von Ollama.
Jeder Tool-Aufruf braucht eine ausdrückliche Freigabe; ausgeführt wird nur in einer Offline-VM.

- Spezifikation: [docs/Local-AI-Developer-Agent-Projektspezifikation.md](docs/Local-AI-Developer-Agent-Projektspezifikation.md)
- Umsetzungsplan: [docs/Umsetzungsplan.md](docs/Umsetzungsplan.md)
- Prüfanleitung aktueller Meilenstein: [docs/pruefung-M5.md](docs/pruefung-M5.md) (vorher: [M4](docs/pruefung-M4.md), [M3](docs/pruefung-M3.md), [M2c](docs/pruefung-M2c.md), [M2b](docs/pruefung-M2b.md), [M2a](docs/pruefung-M2a.md), [M1](docs/pruefung-M1.md))
- Anforderungen → Tests: [docs/sicherheit/anforderungen-tests.md](docs/sicherheit/anforderungen-tests.md)

## Stand: Meilenstein M5 – alle Zielsprachen

| Teil | Sprache | Status |
|---|---|---|
| Verträge (JSON Schema → C# + Pydantic), kanonisches JSON | C# / Python | fertig, Kreuztests grün |
| Approval Broker, Capabilities, Audit-Journal (SQLite, Hashkette) | C# | fertig |
| Broker-Prozess mit Named Pipe und Rollen-Tokens | C# | fertig |
| Freigabe-Oberfläche inkl. Sammelfreigabe mit Abwahl, Not-Aus, Sandbox-Anzeige | C# / WPF | fertig |
| Projektordner mit Rechten, Snapshot (ohne Verknüpfungen), signierte Jobs | C# | fertig |
| Sandbox-Worker: Snapshot-Import, File.List/Read/ReadMany/Search | Python | fertig |
| Modus `dev-local-unsafe` (Worker ohne VM, rot markiert) | – | fertig, **nur zum Entwickeln** |
| Offline-Hyper-V-VM: Neuaufbau je Sitzung (DISM, unattend, ohne NIC), Selbsttest, Copy-VMFile hin, Transfer-VHDX zurück (ohne Einbinden gelesen) | PowerShell / C# / Python | fertig, **auf PC 123 zu prüfen** |
| Toolchains: Manifest, Registrierung mit Hashes (UI), Installation + Prüfung + Hallo-Welt beim VM-Aufbau; Vorlagen .NET und Python | C# / PowerShell | fertig, **auf PC 123 zu prüfen** |
| Orchestrator, Model Gateway (Ollama), fünf Agenten, Live-Aktivität und Chat | Python / C# | M3, abgenommen |
| Patch.Apply, Build/Test in der VM (`Toolchain.*`), Repo.Index/Repo-Map, Reparaturschleife | Python / C# | M4 |
| Weitere Sprachen: C++ (MSVC/Clang, CMake, CTest), PowerShell (Pester, PSScriptAnalyzer), Batch (Harness), HDevelop (HDevEngine, XML-Prüfung nach Patches) | PowerShell / Python / C# | M5, **auf dem PC zu prüfen** |

### Sandbox-Modi (`config/localai.json` → `sandbox.mode`, oder `--mode`)

| Modus | Bedeutung |
|---|---|
| `test` | Testadapter zählt nur, nichts wird gelesen |
| `none` | keine Sandbox – jede Ausführung wird abgelehnt |
| `dev-local-unsafe` | Worker läuft **ohne VM** als normaler Prozess auf diesem PC; nur zum Entwickeln, in der Oberfläche rot. Build/Test (`Toolchain.*`) sind hier immer gesperrt |
| `hyperv` | Offline-VM; einmalig `scripts\sandbox\Enable-LocalAiHyperV.ps1` als Administrator, ISO in `sandbox.hyperv.imagePath` |

## Struktur

```text
contracts/            JSON Schema (einzige Quelle der Verträge) und Testvektoren
config/               localai.json (Modus, Worker, Fake-Agent), demo-project.json (Ordner + Rechte), m4-*-project.json
samples/demo-csv/     kleines Demo-Projekt für den Testlauf
samples/m4-*/         M4: Rechner in C# und Python (Build/Test in der VM), Sicherheitsprobe SEC-13
samples/m5-*/         M5: Rechner in C++, PowerShell, Batch und HDevelop
toolchains/           Vorlagen für Toolchain-Ordner (dotnet, python, cpp, cpp-clang, powershell, powershell5, batch, hdevelop) mit Anleitung
docs/                 Spezifikation, Plan, Prüfanleitungen
scripts/              test.ps1 / start.ps1 (Windows), test.sh (Linux)
  sandbox/              Hyper-V: Aufbau, Copy-VMFile, Laufwerkswechsel, KVP-Signal, Gast-Provisionierung
  toolchains/           Offline-NuGet-Quelle (dotnet), Toolchain-Ordner für M5 anlegen (New-M5Toolchains.ps1)
  samples/              großes Beispiel-Repository (> 2.000 Dateien) erzeugen
src/dotnet/           LocalAi.slnx
  LocalAi.Contracts     generierte Records, kanonisches JSON, Pipe-Protokoll
  LocalAi.Broker        Approval Broker, Policy, Audit-Journal, Testadapter
  LocalAi.Sandbox       Snapshot, Job-Signatur, Transport, Ergebnisprüfung, VM-Steuerung, VHDX-Leser
  LocalAi.Broker.Host   Broker-Prozess (Named-Pipe-Server)
  LocalAi.Desktop.Core  ViewModels, Broker-Client, deutsche Texte (UiText.resx)
  LocalAi.Desktop       WPF-Oberfläche
  LocalAi.Tests         xUnit-Tests (Filter z. B. `dotnet test --filter Pruefung=SEC-23`)
src/python/           Paket localai (Pipe-Client, Fake-Agent, Verträge, Sandbox-Worker mit Patch/Toolchain/Repo-Index, Orchestrator), Tests
tests/integration/    pytest gegen den echten Broker-Prozess
tools/codegen/        Generatoren für Verträge, Testvektoren, Oberflächentexte
```

## Voraussetzungen (Windows)

- .NET 10 SDK
- [uv](https://docs.astral.sh/uv/) (installiert bei Bedarf auch Python; das Projekt ist auf 3.15 festgelegt, siehe `src/python/.python-version`)
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

- **dev-local-unsafe:** Der Worker prüft Signaturen und liest nur aus der Snapshot-Kopie, läuft aber ohne VM-Isolation. Für echte Isolation `hyperv` verwenden.
- **Ein Job zur Zeit in der VM:** Es gibt ein Transfer-Laufwerk; parallele Sandbox-Jobs (Spezifikation: bis zu zwei) folgen bei Bedarf mit einem zweiten.
- **Ordnerrechte:** In M2a nur `List` und `Read`; `Create`/`Modify`/`Delete`/`Execute` folgen mit Patches (M4) und Builds.
- **Rollen über Tokens:** Orchestrator und UI unterscheiden sich in M1 über zufällige Tokens pro Broker-Start; die Pipe ist auf den aktuellen Windows-Benutzer beschränkt. Getrennte Windows-Identitäten für den Orchestrator folgen mit M3.
