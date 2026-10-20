# Entwicklung

Für alle, die am Code arbeiten: Aufbau des Repositorys, Bauen, Testen, Codegenerierung, Pipe-Protokoll und wie man
das System erweitert. Architektur und Begriffe: [Architektur](architektur.md), [Glossar](glossar.md).

## Voraussetzungen

| Werkzeug | Version | Hinweis |
|---|---|---|
| .NET SDK | 10 (`global.json`: 10.0.100, `rollForward: latestFeature`) | |
| uv | aktuell | installiert bei Bedarf auch Python; Version in `src/python/.python-version` |
| Git | – | |
| Windows | 11 Pro | nur für WPF-Oberfläche und Hyper-V |
| Linux | – | für Linux-Host (M10): `scripts/linux/install-host.sh` |

## Repository

```text
contracts/            JSON Schema (einzige Quelle der Verträge) und Testvektoren
config/               localai.json, models.json, Projektdateien der Meilensteine
docs/                 Dokumentation (Einstieg: docs/README.md)
samples/              Beispielprojekte je Meilenstein und Sprache
toolchains/           Vorlagen für Toolchain-Ordner mit Anleitung
scripts/              test.ps1, start.ps1, abnahme.ps1 (Windows), test.sh (Linux)
  linux/                Linux-Host: install-host.sh, get-prerequisites.sh, build.sh, start.sh
  sandbox/              Hyper-V: Aufbau, Copy-VMFile, Laufwerkswechsel, KVP, Gast-Provisionierung (guest/, guest-linux/)
  toolchains/           Offline-NuGet-Quelle, Toolchain-Ordner für M5 anlegen
  samples/              großes Beispiel-Repository erzeugen
src/dotnet/
  LocalAi.slnx            Lösung für Windows (mit WPF)
  LocalAi.Linux.slnx      Lösung für Linux (ohne WPF, mit Avalonia)
  LocalAi.Contracts       generierte Records, kanonisches JSON, Pipe-Protokoll, Patch-Köpfe
  LocalAi.Broker          Approval Broker, Policy, Audit-Journal und -Prüfung, Aktivität, Web-/Export-Policy, OS-Debug
  LocalAi.Sandbox         Snapshot, Jobsignatur, Transport, VHDX-Leser, Hyper-V, QEMU, Pool, Toolchains, Export
  LocalAi.Broker.Host     Broker-Prozess: Pipe-Server, Optionen, Kindprozesse, Orchestrator-Aufsicht, Web, System
  LocalAi.Desktop.Core    ViewModels, Broker-Client, deutsche Texte (UiText.resx), inerte Textdarstellung
  LocalAi.Desktop         WPF-Oberfläche (Windows)
  LocalAi.Desktop.Linux   Avalonia-Oberfläche (Linux) + LocalAi.Desktop.Linux.Tests (ohne Bildschirm)
  LocalAi.Tests           xUnit-Tests
src/python/localai/
  pipe.py, broker_client.py   Rahmenprotokoll und Client der Named Pipe
  canonical.py, pathrules.py  kanonisches JSON/Hashes, Pfadregeln (wie in C#)
  contracts/                  generierte Pydantic-Modelle
  orchestrator/               Orchestrator, Agenten, DAG, Gateway, Kontext, Kalibrierung, Prompts
  worker/                     Sandbox-Worker: Snapshot, Transfer, Patches, Ausführung, Repo-Index, HDevelop, OS-Debug
  web/                        Web-Launcher, Fetcher, Content Gate, Netzprüfung
  guest/                      Gasteinrichtung (Linux), Verbindungskanal unter QEMU
  fake_agent.py               Fake-Agent für Tests ohne Modell
src/python/tests/     pytest-Unit-Tests der Python-Teile
tests/integration/    pytest gegen den echten Broker-Prozess (inkl. VM-Abnahmen)
tests/korpus/         Prompt-Injection-Testkorpus
tests/qemu/           Test-Gast (Kern + initramfs) für den echten QEMU-Test
tools/codegen/        Generatoren: Verträge, Testvektoren, Oberflächentexte
tools/abnahme/        Abnahmebericht (bericht.py, kriterien.json)
tools/prereq/         Offline-Bausteine für den Linux-Gast beschaffen
```

## Bauen und testen

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1    # Windows: alles inkl. WPF
powershell -ExecutionPolicy Bypass -File scripts\start.ps1   # Windows: Oberfläche starten
```

```bash
scripts/test.sh          # Linux: alles außer WPF, inkl. Avalonia-Oberfläche
scripts/linux/build.sh   # Linux: Broker und Oberfläche nach out/linux veröffentlichen
scripts/linux/start.sh   # Linux: starten (baut bei Bedarf)
```

Beide Testskripte prüfen zuerst, ob die generierten Verträge aktuell sind, bauen dann C#, führen die xUnit-Tests und
zuletzt `pytest` (Unit- und Integrationstests) aus. Ende: „Alles grün.“

Einzelne Tests:

```bash
dotnet test src/dotnet/LocalAi.Tests --filter Pruefung=SEC-23        # nach Prüffall (Trait)
dotnet test src/dotnet/LocalAi.Tests --filter FullyQualifiedName~M10Tests
cd src/python && uv run pytest -q tests/test_worker_m4.py -k patch
cd src/python && uv run pytest -q ../../tests/integration/test_m6_dev_local.py
```

### Testebenen

| Ebene | Ort | Hinweis |
|---|---|---|
| C# Unit/Integration | `src/dotnet/LocalAi.Tests` | Klassen je Bereich (`BrokerTests`, `SandboxTests`, …) und je Meilenstein (`M6Tests` … `M10Tests`). Sicherheitsprüfungen tragen `[Trait("Pruefung", "SEC-xx")]` – der Abnahmebericht wertet das aus |
| Linux-Oberfläche | `src/dotnet/LocalAi.Desktop.Linux.Tests` | Avalonia.Headless, ohne Bildschirm |
| Python Unit | `src/python/tests` | Ollama durch `orchestrator/fake_ollama.py` ersetzt |
| Integration | `tests/integration` | startet den echten Broker-Prozess und spricht ihn über die Named Pipe an |
| VM-Abnahmen | `tests/integration/test_*_abnahme.py` | nur auf echter Hardware, per Umgebungsvariable eingeschaltet (siehe unten) |
| Kreuztests C#/Python | `contracts/testvectors/` | kanonisches JSON, Pfadregeln, Snapshot-ID, Jobsignatur, Transfer-Container |

Die Zuordnung Anforderung → Test pflegt [sicherheit/anforderungen-tests.md](sicherheit/anforderungen-tests.md). Neue
Sicherheitstests dort eintragen.

### Umgebungsvariablen für Tests

| Variable | Wirkung |
|---|---|
| `LOCALAI_HYPERV_ABNAHME=1` | Hyper-V-Abnahme mit echter VM (`test_hyperv_abnahme.py`) |
| `LOCALAI_HYPERV_TOOLCHAINS`, `LOCALAI_HYPERV_ROUNDTRIPS` | Toolchains bzw. Anzahl Roundtrips für diese Abnahme |
| `LOCALAI_M4_ABNAHME=1` … `LOCALAI_M7_ABNAHME=1` | Abnahme des jeweiligen Meilensteins mit echter VM |
| `LOCALAI_M5_HDEVELOP=0` | HDevelop-Teil der M5-Abnahme auslassen (ohne HALCON-Lizenz) |
| `LOCALAI_M6_WEB`, `LOCALAI_M6_URL`, `LOCALAI_M7_WEB` | echte Webabrufe in den Abnahmen |
| `LOCALAI_TEST_KERNEL=/pfad/vmlinuz` | Kern für den echten QEMU-Test (`test_m10_qemu.py`); ohne wird er übersprungen |

`scripts/test.ps1` entfernt die Abnahme-Variablen für den normalen Testlauf, damit keine stundenlangen VM-Abnahmen
versehentlich starten.

Zur Laufzeit setzt der Broker für seine Kindprozesse `LOCALAI_PIPE`, `LOCALAI_TOKEN`, `LOCALAI_RUN_ID`,
`LOCALAI_INBOX_AFTER` und `LOCALAI_DATA`.

## Verträge ändern

Alle Datenstrukturen zwischen C# und Python stehen **einmal** in `contracts/schemas/contracts.schema.json`.

1. Schema anpassen.
2. `python tools/codegen/generate.py` – erzeugt `src/dotnet/LocalAi.Contracts/Generated/Contracts.g.cs` und
   `src/python/localai/contracts/generated.py`. Generierte Dateien nie von Hand ändern.
3. Bei Änderungen an der Kanonisierung: `python tools/codegen/make_testvectors.py` und den Diff bewusst prüfen.
   Weitere Vektoren: `make_sandbox_vectors.py` (Snapshot-ID, Jobsignatur), `make_transfer_vectors.py`
   (Transfer-Container, VHDX aus `qemu-img`).

`generate.py --check` (Teil der Testskripte) schlägt fehl, wenn die generierten Dateien nicht zum Schema passen.

## Pipe-Protokoll

Kommunikation zwischen Oberfläche, Broker und Orchestrator über eine Named Pipe (`NamedPipeServerStream`; unter Linux
bildet .NET sie auf einen Unix-Domain-Socket ab). Rahmen: 4 Byte Länge (Little Endian) + UTF-8-JSON, höchstens 8 MiB.

```json
{"id": 1, "method": "proposals.submit", "token": "…", "params": { … }}
{"id": 1, "ok": true, "result": { … }}
{"id": 1, "ok": false, "error": {"code": 403, "message": "…"}}
```

Implementierung: `src/dotnet/LocalAi.Contracts/PipeProtocol.cs`, `src/python/localai/pipe.py`. Rechte je Methode:
`src/dotnet/LocalAi.Broker.Host/PipeServer.cs`.

| Methode | UI | Orchestrator | Zweck |
|---|:-:|:-:|---|
| `ping` | ✓ | ✓ | Verbindung prüfen |
| `status` | ✓ | | Betriebsart, Sandbox, Web |
| `proposals.submit` | | ✓ | Vorschlag einreichen |
| `proposals.outcome` | ✓ | ✓ | Ergebnis eines Vorschlags |
| `proposals.pending`, `proposals.get` | ✓ | | offene Vorschläge, ein Vorschlag |
| `approvals.decide` | ✓ | | Entscheidung mit Hash |
| `approvals.narrow` | ✓ | | Sammelfreigabe einschränken → neuer Vorschlag |
| `events.wait` | ✓ | ✓ | Ereignisstrom |
| `run.emergencyStop`, `run.release` | ✓ | | Not-Aus setzen / aufheben |
| `run.cancel`, `agent.cancel` | ✓ | | Run bzw. Agent abbrechen |
| `chat.send` | ✓ | | Chatnachricht |
| `activity.wait` | ✓ | | Live-Aktivität lesen |
| `activity.report` | | ✓ | Live-Aktivität melden (nur für den eigenen Run) |
| `orchestrator.wait` | | ✓ | nächsten Auftrag abholen |
| `orchestrator.status`, `orchestrator.stop` | ✓ | | Zustand, Beenden |
| `project.grants`, `toolchains.available` | ✓ | ✓ | Grants, installierte Toolchains |
| `toolchains.scan`, `toolchains.register`, `toolchains.unregister` | ✓ | | Toolchain-Verwaltung |
| `sandbox.rebuild`, `sandbox.continue` | ✓ | | VM neu aufbauen / weiter verwenden |
| `web.capture` | ✓ | | bereinigter Text für die Inhaltsfreigabe |
| `system.manifest`, `audit.verify`, `diagnostics.export`, `backup.create` | ✓ | | Bereich SYSTEM |
| `dev.startFakeAgent` | ✓ | | Fake-Agent starten |

Fehlercodes: 400 ungültige Anfrage, 403 Policy oder Rolle, 404 unbekannt, 409 Hash-/Snapshotkonflikt, 410 abgelaufen,
422 ungültiges Schema, 429 Grenze erreicht, 500 interner Fehler.

## Konventionen

- **Sprache:** Oberfläche, Meldungen, Kommentare, Dokumentation und Testnamen Deutsch; Prompts Englisch.
  Bezeichner im Code Englisch, Testmethoden beschreiben das erwartete Verhalten auf Deutsch
  (`Ohne_Entscheidung_keine_Ausfuehrung`).
- **C#:** `TreatWarningsAsErrors`, `Nullable` an (`src/dotnet/Directory.Build.props`). Die Version des Produkts steht
  dort (`<Version>`).
- **Oberflächentexte** werden in `tools/codegen/make_uitext.py` gepflegt und nach
  `LocalAi.Desktop.Core/Resources/UiText.resx` erzeugt – nicht die `.resx` direkt bearbeiten. Weitere Sprachen:
  `UiText.<kultur>.resx` daneben.
- **WPF und Avalonia getrennt:** `LocalAi.Desktop` (WPF) bekommt keine Avalonia-Pakete; gemeinsame Logik gehört nach
  `LocalAi.Desktop.Core`. Ein Test prüft das (`DesktopTests.Windows_Oberflaeche_bleibt_ohne_Avalonia`).
- **Prompts** liegen in `src/python/localai/orchestrator/prompts/<rolle>.md` mit Kopfzeile
  `<!-- prompt: <name> · version: N -->`. Bei jeder inhaltlichen Änderung die Version erhöhen; das Versionsmanifest
  führt sie mit Hash.
- **Zeilenenden:** LF, außer `.ps1`, `.psm1`, `.bat`, `.cmd` (CRLF, siehe `.gitattributes`). PowerShell-Skripte als
  UTF-8 mit BOM (Windows PowerShell 5.1); ein Test prüft Syntax und Kodierung.
- **Commits:** Präfix des Meilensteins bzw. Bereichs, z. B. `M10: …`, `M4-Fix 3: …`, `Doku: …`.
- **Untrusted bleibt untrusted:** Daten aus Dateien, Web, Tests oder Modellen nie als Anweisung behandeln, nie ohne
  inerte Darstellung anzeigen (`InertText`), nie in Prompts außerhalb des Evidenzrahmens einsetzen.

## Erweitern

### Neue Sprache oder neues Werkzeug für Build/Test

Kein Code nötig: eine neue **Toolchain** mit `toolchain.json` anlegen (Vorlagen und Format:
[toolchains/README.md](../toolchains/README.md)), registrieren, im Projekt unter `toolchains` eintragen. Der
Orchestrator wählt Prüfläufe anhand der deklarierten Aktionen. Sprachhinweise für die Agenten
(`language_hints` in `orchestrator/run.py`) und eine automatische Prüflaufwahl (`default_verify`) lassen sich
ergänzen.

### Neues Werkzeug

Ein neues Werkzeug berührt alle Schichten – und ist sicherheitsrelevant:

1. **Vertrag:** Argumente und ggf. Ergebnisfelder im Schema, Code neu erzeugen.
2. **Broker:** Definition in `ProposalPolicy` (Argumente, Zielanzahl, Sammelfreigabe ja/nein, benötigte Rechte,
   Snapshot-Pflicht) und ggf. eigene Prüfung in `ExecutionPolicy`.
3. **Ausführung:** Adapter im Worker (`localai/worker/tools.py`, `execution.py`) – in der Sandbox, nie auf dem Host.
4. **Oberfläche:** Nebenwirkungstext und ggf. eigene Kartendarstellung (Texte in `make_uitext.py`).
5. **Orchestrator:** Aktion im Protokoll (`orchestrator/protocol.py`), Übersetzung in `orchestrator/tools.py`,
   Beschreibung im Prompt (Version erhöhen).
6. **Tests:** Ablehnung ohne Freigabe, Hashbindung, Rechte, Pfadregeln, Abweisung im Gast; Eintrag in
   [anforderungen-tests.md](sicherheit/anforderungen-tests.md).

Grundsatz: Kein Werkzeug mit freiem Shellstring; ausführende Werkzeuge bleiben Einzelaufrufe.

### Neuer Oberflächentext

Schlüssel in `tools/codegen/make_uitext.py` ergänzen, `python tools/codegen/make_uitext.py` ausführen, in beiden
Oberflächen binden. `DesktopTests.Xaml_*` prüft, dass alle Schlüssel und Bindings vorhanden sind.

## Dokumentation und Wiki

Die Dokumentation steht in `docs/` (dazu `toolchains/README.md`); das GitHub-Wiki wird daraus erzeugt und **nicht**
von Hand bearbeitet. `tools/wiki/build_wiki.py` schreibt die Seiten, Links zwischen Dokumenten werden auf
Wiki-Seiten umgeschrieben; Navigation und Fußzeile stehen ebenfalls dort. Die Action `.github/workflows/wiki.yml`
veröffentlicht bei jedem Push auf `main`, der `docs/` betrifft (oder von Hand über „Run workflow“). Ein neues Dokument
in `PAGES` und, falls es in die Navigation gehört, in `SIDEBAR` eintragen.

Lokal ansehen:

```bash
python3 tools/wiki/build_wiki.py --out /tmp/wiki
```

## Arbeitsweise

Jeder Meilenstein liefert Code, Tests und eine Prüfanleitung `docs/pruefung-Mx.md` mit den Befehlen für die echte
Hardware und den erwarteten Ergebnissen. Teile, die nur auf Windows mit Hyper-V oder mit echtem Modell prüfbar sind,
werden dort ausdrücklich als „nur bei dir prüfbar“ gekennzeichnet. Siehe [Umsetzungsplan](Umsetzungsplan.md#umsetzungsmodus).
