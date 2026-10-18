# Prüfanleitung M5 – Alle Sprachen (C++, PowerShell, Batch, HDevelop)

Ziel: Die Agenten bauen und testen in der Offline-VM auch **C++** (MSVC/CMake/CTest), **PowerShell** (Pester,
PSScriptAnalyzer), **Batch** (Test-Harness) und **HDevelop** (HDevEngine). Für HDevelop prüft die Sandbox nach jedem
Patch, ob die Datei gültiges HDevelop-XML bleibt, und lädt die Programme mit HDevEngine (Lade-Test).

Abnahme laut Plan: je ein Beispiel pro Sprache gebaut und getestet; Installationszeiten im VM-Aufbau dokumentiert.

## Was ist neu

| Bereich | Neu |
|---|---|
| Toolchain-Vorlagen (`toolchains\`) | `cpp` (Visual Studio Build Tools 2022, CMake, CTest), `cpp-clang` (optional: llvm-mingw, Ninja), `powershell` (PowerShell 7), `powershell5` (Windows PowerShell 5.1), `batch`, `hdevelop` – jeweils mit Hallo-Welt-Selbsttest |
| `toolchain.json` | `then`: mehrstufige Aktionen (z. B. konfigurieren → bauen → testen, Abbruch beim ersten Fehler); `deploy`: Runner-Skripte, Module, Lizenzdatei nach `C:\Toolchains\<name>`; `{system}\cmd.exe` und `{system}\WindowsPowerShell\v1.0\powershell.exe` als einzige erlaubte Windows-Programme |
| Runner (in der VM, eingeschränktes Token, niedrige Integrität) | Pester → JUnit, PSScriptAnalyzer; Batch-Harness (`*.test.cmd`, Exitcode 0, optional `*.expected.txt`) → JUnit; HDevEngine-Runner (Lade-Test, `test_*`-Prozeduren) → JUnit |
| Sandbox | `.hdev/.hdvp/.hdpl`-Patches, die kein gültiges HDevelop-XML ergeben, werden nicht angewendet (Rückmeldung mit Zeile/Spalte an den Agenten); neue `.cmd/.bat` aus Patches mit CRLF |
| Sicherheit | Pfadparameter ohne `& \| < > ^ % ! "` (Broker und Gast, SEC-20); Windows-Programme nur aus fester Liste, Pfade in `deploy` nur relativ |
| Orchestrator | Prüflauf automatisch auch für C++ (Ordner mit `CMakeLists.txt`), PowerShell, Batch, HDevelop; Sprachhinweise im Auftrag (HDevelop-XML, Testkonventionen) |
| VM-Aufbau | Zeitgrenze wächst automatisch mit den erwarteten Installationszeiten (60 min + 3× `expectedMinutes` je Toolchain); Statuszeile zeigt die Installationsdauer je Toolchain, z. B. „cpp 17.14 (Hallo-Welt ok) 25 min“ |
| Beispiele | `samples\m5-cpp`, `m5-powershell`, `m5-batch`, `m5-hdevelop`, Projekte `config\m5-*-project.json` |

**Nicht in der Sandbox getestet (nur auf deinem PC prüfbar):** die Runner unter Windows (Pester-Runner und
Batch-Harness sind unter Linux mit PowerShell 7 bzw. einer Testattrappe für `cmd.exe` geprüft), der Visual-Studio-
Offline-Installer und vor allem der HDevEngine-Runner (ohne HALCON hier nicht ausführbar). Bitte Logs schicken, wenn
etwas klemmt.

## 0. Vorbereitung (einmalig, auf dem Host mit Internet)

**Deine Dateien bleiben unverändert** (`config\localai.json`, `config\demo-project.json`, `config\models.local.json`).

1. Toolchain-Ordner anlegen und Dateien holen (Toolchain-Ordner wie in `config\localai.json` → `toolchains.directory`):
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\toolchains\New-M5Toolchains.ps1 -Target D:\Agent\Toolchains
   ```
   - `batch`: nur Vorlage.
   - `powershell`: PowerShell-ZIP (Version 7.6.7, sonst `-PowerShellVersion`), `Save-Module Pester, PSScriptAnalyzer`.
   - `cpp`: Visual-Studio-Build-Tools-Layout in `D:\Agent\Toolchains\cpp\layout` (einige GB, dauert).
   - `hdevelop`: `halcon.zip` aus `$env:HALCONROOT` (oder `-HalconRoot`), Lizenzdateien nach `license\`, VC++-Laufzeit.
     **Lizenz:** Die VM hat keine Netzwerkkarte und keinen USB-Zugriff. Eine MAC- oder Dongle-gebundene Lizenz
     funktioniert dort nicht – du brauchst eine Lizenz ohne diese Bindung (z. B. Evaluierungslizenz). Ohne solche
     Lizenz: `hdevelop` weglassen (siehe unten), die XML-Prüfung der `.hdev`-Patches läuft trotzdem.
   - Nur einzelne: `-Toolchain batch, powershell`.
2. In jedem neuen Ordner `toolchain.json` prüfen: `version`, `install.file`, `install.check.expectOutput` (z. B. HALCON
   24.11 → anpassen, wenn du eine andere Version hast).
3. Oberfläche starten → **TOOLCHAINS** → prüfen → `cpp`, `powershell`, `batch`, `hdevelop` **registrieren**
   (proprietär: Build Tools und HALCON – Lizenz bestätigen).
4. In **deiner** `config\localai.json`: `"project": "config/m5-cpp-project.json"` (für Abschnitt 2; alle
   M5-Projektdateien wählen dieselben Toolchains, ein Wechsel baut die VM also nicht neu auf).
   Ohne HALCON: in den vier Dateien `config\m5-*-project.json` `"hdevelop"` aus `toolchains` entfernen.
5. Die VM wird beim nächsten Start **neu aufgebaut** (neue Toolchains). Erwartet: 60–90 Minuten (Build Tools
   20–40 Minuten). Danach in der Kopfzeile z. B.
   „Toolchains: cpp 17.14 (Hallo-Welt ok) 25 min, powershell 7.6.7 (Hallo-Welt ok) 15 s, batch 1.0 (Hallo-Welt ok) 3 s,
   hdevelop 24.11 (Hallo-Welt ok) 4 min“ → **diese Zeiten bitte notieren** (Abnahme: Installationszeiten
   dokumentieren).

## 1. Automatische Tests

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```
Erwartet: alle grün (C# 108, Python 253; einige Windows-/Abnahmetests übersprungen).

## 2. Abnahme-Skript mit echter VM (ohne Modell, ca. 20–40 Minuten nach dem Aufbau)

```powershell
$env:LOCALAI_M5_ABNAHME = "1"
# ohne nutzbare HALCON-Lizenz zusätzlich:  $env:LOCALAI_M5_HDEVELOP = "0"
uv run --project src/python pytest -s tests/integration/test_m5_abnahme.py
```
Je Sprache: Snapshot → Tests vorher grün → Patch (neue Funktion + Tests) → Tests grün → Patch mit absichtlich falschem
Test → genau dieser Fehler im Bericht.

- [ ] C++: 2 → 3 Tests bestanden, dann `falsch` fehlgeschlagen (erster Lauf baut länger, MSBuild).
- [ ] PowerShell: 3 → 5 Pester-Tests, dann `falsch`; `Toolchain.Lint` (PSScriptAnalyzer) ohne Fehler.
- [ ] Batch: 3 → 4 Tests, dann `falsch.test.cmd`; Lint grün, ein kaputtes Skript (fehlende Sprungmarke, offene
  Klammer) wird von Lint gemeldet.
- [ ] HDevelop: Patch, der `<` unmaskiert in eine `<l>`-Zeile schreibt, wird **nicht angewendet**; mit HALCON
  zusätzlich 2 → 3 Tests, dann `test_falsch`, Lade-Test ohne Fehler.
- [ ] Ergebnis `%LOCALAPPDATA%\LocalAi\abnahme\m5-abnahme.json` (mit `installTimes` und Laufzeiten), Logs `m5-*.log`.

## 3. Ende zu Ende mit Modell (je Sprache, wie M4 Abschnitt 3)

Projekt in `config\localai.json` umstellen, Oberfläche neu starten (die VM wird wiederverwendet), Chat:

| Projekt | Ziel im Chat |
|---|---|
| `config/m5-cpp-project.json` | „Füge rechner::teilen(a, b) hinzu. Teilen durch 0 soll eine std::invalid_argument-Ausnahme mit deutscher Meldung werfen. Mit Tests.“ |
| `config/m5-powershell-project.json` | „Füge Get-Quotient -A -B hinzu. Teilen durch 0 soll einen Fehler mit deutscher Meldung auslösen. Mit Tests.“ |
| `config/m5-batch-project.json` | „Ergänze in rechne.cmd die Operation sub (A minus B). Mit Tests.“ |
| `config/m5-hdevelop-project.json` | „Füge die externe Prozedur teilen (A, B, Quotient) hinzu. Teilen durch 0 soll mit throw eine deutsche Fehlermeldung auslösen. Mit Tests in tests/rechner_test.hdev.“ |

- [ ] Plan mit Tester-Aufgabe; Prüfung in der Sandbox z. B. `cpp test (project=., configuration=Debug)`,
  `powershell test (path=.)`, `batch test (path=.)`, `hdevelop test (path=.)`.
- [ ] Patches angewendet, Tests in der VM bestanden (bei Fehlern repariert der Debugger anhand der echten Meldung).
- [ ] HDevelop: Wenn ein Patch das XML zerstören würde, steht im Verlauf „Patch nicht angewendet (… HDevelop-Datei nach
  dem Patch ungültig …) – Agent korrigiert“.
- [ ] Zusammenfassung: „Build/Tests in der Sandbox: bestanden“, Projektordner unverändert.

Kleine Modelle (7B) tun sich mit HDevelop-XML und Batch schwerer als mit C#/Python – Beobachtungen bitte notieren
(Schritte je Agent, Korrekturen).

## Fehlersuche

| Meldung | Ursache / Abhilfe |
|---|---|
| `Toolchain cpp nicht installiert: Installer endete mit Exitcode …` | Layout unvollständig oder andere Komponenten-IDs: Layout mit dem Skript neu erzeugen; Log `C:\LocalAi\logs\toolchains.log` im Gast (`scripts\sandbox\Get-LocalAiGuestLogs.ps1`). |
| `cmake … Generator Visual Studio 17 2022 could not find any instance` | Build Tools nicht unter `C:\Toolchains\cpp` installiert oder Instanzabfrage scheitert mit niedriger Integrität: Log schicken; testweise `"lowIntegrityTools": false`. |
| `Pester fehlt` / `PSScriptAnalyzer fehlt` (Exitcode 2) | `modules\Pester`, `modules\PSScriptAnalyzer` im Toolchain-Ordner fehlen → Skript erneut, neu registrieren. |
| `HALCON nicht nutzbar: … licen…` | Lizenz in der VM ungültig (MAC/Dongle-gebunden, abgelaufen) – siehe Abschnitt 0; ohne Lizenz `hdevelop` aus den Projekten nehmen. |
| `halcondotnet.dll nicht gefunden` | `halcon.zip` enthält nicht den Inhalt des HALCON-Ordners (oberste Ebene muss `bin\` sein) oder andere Architektur (`HALCONARCH` im Manifest). |
| `Prozedur … nicht auflösbar` (Lade-Test) | externe Prozedur fehlt; der Runner sucht in allen Ordnern des Projekts mit `.hdvp/.hdpl` und in `%HALCONROOT%\procedures`. |
| `VM-Aufbau dauerte länger als … Minuten` | `sandbox.hyperv.buildTimeoutMinutes` erhöhen (die automatische Mindestzeit reicht bei langsamer Platte nicht). |
| Batch-Test „Zeitüberschreitung nach 60 s“ | Skript wartet auf Eingabe (`pause`, `set /p`): im Test Eingaben vermeiden. |

## Bekannte Einschränkungen

- vcpkg nur als fertig gebaute Pakete in `packages\` (kein Bauen von Ports in der VM).
- HDevelop-Grafik (`dev_display`, Fenster) ist in HDevEngine ohne Wirkung; Tests prüfen Werte, nicht Bilder.
- Die Übernahme der Änderungen in deinen Projektordner folgt mit M6.
