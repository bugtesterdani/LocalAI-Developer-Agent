# Local AI Developer Agent

Lokaler Entwicklungsagent mit fünf Rollen (Planner, Coder, Tester, Debugger, Reviewer) auf Basis von Ollama.
Jeder Tool-Aufruf braucht eine ausdrückliche Freigabe; ausgeführt wird nur in einer Offline-VM – unter Windows mit
Hyper-V, auf einem Linux-Host mit QEMU (M10).

- Spezifikation: [docs/Local-AI-Developer-Agent-Projektspezifikation.md](docs/Local-AI-Developer-Agent-Projektspezifikation.md)
- Umsetzungsplan: [docs/Umsetzungsplan.md](docs/Umsetzungsplan.md)
- Prüfanleitung aktueller Meilenstein: [docs/pruefung-M10.md](docs/pruefung-M10.md) (vorher: [M9](docs/pruefung-M9.md), [M8](docs/pruefung-M8.md), [M6](docs/pruefung-M6.md), [M5](docs/pruefung-M5.md), [M4](docs/pruefung-M4.md), [M3](docs/pruefung-M3.md), [M2c](docs/pruefung-M2c.md), [M2b](docs/pruefung-M2b.md), [M2a](docs/pruefung-M2a.md), [M1](docs/pruefung-M1.md))
- Auf einem neuen Rechner einrichten: [docs/inbetriebnahme-neuer-rechner.md](docs/inbetriebnahme-neuer-rechner.md)
- Anforderungen → Tests: [docs/sicherheit/anforderungen-tests.md](docs/sicherheit/anforderungen-tests.md)

## Stand: Meilenstein M10 – Linux-Host

Seit M6 dazugekommen: Abnahme (M7), Linux-Gast mit mehreren VMs (M8), Betriebssystem-Debugging mit QEMU in der
Linux-VM (M9) und der Betrieb direkt unter Linux mit QEMU und eigener Avalonia-Oberfläche (M10). Einzelheiten in den
Prüfanleitungen und im Umsetzungsplan.

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
| Web: `Web.Fetch` → Quarantäne → Content Gate → `Web.Content` (zwei Freigaben, SSRF-Schutz, getrennte Prozesse niedriger Integrität) | Python / C# | M6, **auf dem PC zu prüfen** |
| Übernahme in den Projektordner (`Workspace.Diff` → `Workspace.ExportPatch`, Diff je Datei, Sicherung), Not-Aus inkl. Webprozesse und VM, Fortsetzen ohne alte Freigaben | Python / C# | M6, **auf dem PC zu prüfen** |

### Sandbox-Modi (`config/localai.json` → `sandbox.mode`, oder `--mode`)

| Modus | Bedeutung |
|---|---|
| `test` | Testadapter zählt nur, nichts wird gelesen |
| `none` | keine Sandbox – jede Ausführung wird abgelehnt |
| `dev-local-unsafe` | Worker läuft **ohne VM** als normaler Prozess auf diesem PC; nur zum Entwickeln, in der Oberfläche rot. Build/Test (`Toolchain.*`) sind hier immer gesperrt |
| `hyperv` | Offline-VM; einmalig `scripts\sandbox\Enable-LocalAiHyperV.ps1` als Administrator, ISO in `sandbox.hyperv.imagePath` |
| `qemu` | Linux-Host (M10): Offline-Linux-VM mit QEMU, Abschnitt `sandbox.qemu` (Vorlage `config/localai.linux.json`) |

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
  linux/                Linux-Host (M10): install-host.sh, get-prerequisites.sh, build.sh, start.sh
  sandbox/              Hyper-V: Aufbau, Copy-VMFile, Laufwerkswechsel, KVP-Signal, Gast-Provisionierung
  toolchains/           Offline-NuGet-Quelle (dotnet), Toolchain-Ordner für M5 anlegen (New-M5Toolchains.ps1)
  samples/              großes Beispiel-Repository (> 2.000 Dateien) erzeugen
src/dotnet/           LocalAi.slnx (Windows), LocalAi.Linux.slnx (Linux-Host, ohne WPF)
  LocalAi.Contracts     generierte Records, kanonisches JSON, Pipe-Protokoll
  LocalAi.Broker        Approval Broker, Policy, Audit-Journal, Testadapter
  LocalAi.Sandbox       Snapshot, Job-Signatur, Transport, Ergebnisprüfung, VM-Steuerung, VHDX-Leser
  LocalAi.Broker.Host   Broker-Prozess (Named-Pipe-Server)
  LocalAi.Desktop.Core  ViewModels, Broker-Client, deutsche Texte (UiText.resx)
  LocalAi.Desktop       WPF-Oberfläche (Windows)
  LocalAi.Desktop.Linux Avalonia-Oberfläche (Linux-Host, M10) + LocalAi.Desktop.Linux.Tests (ohne Bildschirm)
  LocalAi.Tests         xUnit-Tests (Filter z. B. `dotnet test --filter Pruefung=SEC-23`)
src/python/           Paket localai (Pipe-Client, Fake-Agent, Verträge, Sandbox-Worker mit Patch/Toolchain/Repo-Index, Orchestrator, Web-Fetcher und Content Gate), Tests
tests/integration/    pytest gegen den echten Broker-Prozess
tests/qemu/           Test-Gast (Kern + initramfs) für den echten QEMU-Test
tools/codegen/        Generatoren für Verträge, Testvektoren, Oberflächentexte
```

## Voraussetzungen (Windows)

- .NET 10 SDK
- [uv](https://docs.astral.sh/uv/) (installiert bei Bedarf auch Python; das Projekt ist auf 3.15 festgelegt, siehe `src/python/.python-version`)
- Git

## Voraussetzungen (Linux-Host, M10)

- .NET 10 SDK, uv, Git
- `scripts/linux/install-host.sh`: QEMU, qemu-img, dosfstools, mtools, bubblewrap, Gruppe `kvm`
- Einrichtung und Prüfung: [docs/pruefung-M10.md](docs/pruefung-M10.md)

## Schnellstart

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1    # alles bauen und testen
powershell -ExecutionPolicy Bypass -File scripts\start.ps1   # Oberfläche starten
```

```bash
scripts/test.sh          # Linux: alles außer WPF bauen und testen
scripts/linux/start.sh   # Linux-Oberfläche bauen (falls nötig) und starten
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
