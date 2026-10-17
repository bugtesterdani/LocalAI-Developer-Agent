# Prüfanleitung M2c – Toolchains in der Offline-VM

Ziel: Toolchains (zuerst .NET und Python) werden **nur nach sichtbarer Registrierung mit Hashprüfung** bei jedem VM-Aufbau installiert, in der VM geprüft und mit einem Hallo-Welt-Beispiel getestet. Jede Veränderung der Dateien nach der Registrierung verhindert den Aufbau.

Voraussetzung: M2b läuft (siehe `docs/pruefung-M2b.md`). Die Aktionen `Toolchain.Restore/Build/Test/Run` für Agenten folgen in M4; die Vorlagen dafür stehen schon in den Manifesten und werden bei der Registrierung geprüft.

## Ablauf (kurz)

1. Du lädst die Installationsdateien selbst herunter und legst je Toolchain einen Ordner mit `toolchain.json` an.
2. **Registrieren** in der Oberfläche: der Broker prüft das Manifest (feste Pfade, keine freien Kommandos, Platzhalter nur als ganzes Argument) und hält die SHA-256 aller Dateien fest.
3. **VM-Aufbau:** nur die im Projekt gewählten Toolchains werden kopiert. Die Kopie wird gegen die Registrierung geprüft (Host), im Gast ein zweites Mal (Gast). Danach Installation, Prüfbefehl, Hallo-Welt. `C:\Toolchains` ist danach für normale Benutzer nur lesbar.
4. Der Bericht der VM nennt jede Toolchain; fehlt eine oder schlägt etwas fehl, bleibt die Sandbox gesperrt.

## 0. Einrichten (PC 123)

Anleitung mit Download-Hinweisen: `toolchains\README.md`. Kurzfassung:

1. `config\localai.json` → `"toolchains": { "directory": "D:\\Agent\\Toolchains" }` (Ordner nach Wunsch).
2. `D:\Agent\Toolchains\dotnet\`: Inhalt von `toolchains\dotnet\` (Manifest + `selftest\`) und das **.NET-SDK-ZIP** (Binaries, Windows x64). Passt die Version nicht zu `dotnet-sdk-9.0.306-win-x64.zip`: in `toolchain.json` `version`, `displayName`, `install.file`, `install.check.expectOutput` anpassen.
3. `D:\Agent\Toolchains\python\`: Inhalt von `toolchains\python\`, den **Python-Installer** (`python-3.12.10-amd64.exe` o. ä.) und pytest als Wheels in `wheels\`:
   ```powershell
   cd D:\Agent\Toolchains\python
   python -m pip download pytest --only-binary=:all: --python-version 3.12 --platform win_amd64 -d wheels
   ```
4. `config\demo-project.json` → `"toolchains": ["dotnet", "python"]`.

## 1. Automatische Tests

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

| Schritt | Erwartet |
|---|---|
| 3/4 C#-Tests | `Passed: 63` – neu `ToolchainTests` (Manifestregeln, Registrierung, Änderungserkennung, Verknüpfungen, Bereitstellung, Bericht, Lizenzbestätigung in der Oberfläche) |
| 4/4 Python-Tests | `107 passed, 2 skipped` – neu `test_toolchains.py` (Registrierung nur mit UI-Token, veränderter Ordner, ungültiges Manifest) |

Der Test `PowerShell_Skripte_sind_gueltig` prüft jetzt auch die Gastfunktionen (Argument-Quoting, Platzhalter, Hashprüfung im Gast).

## 2. Registrieren in der Oberfläche

`scripts\start.ps1` (Modus egal, z. B. `dev-local-unsafe`).

- [ ] Bereich **TOOLCHAINS** → »Toolchain-Ordner prüfen« (bei großen Toolchains dauert das Hashen einige Sekunden).
- [ ] Je Toolchain: Name, Version, »nicht registriert · vom Projekt verwendet«, Dateianzahl, Größe, Prüfsumme, Lizenz.
- [ ] Fehler im Manifest oder fehlende Dateien stehen rot unter dem Eintrag; »Registrieren« ist dann gesperrt.
- [ ] »Registrieren« → Ereignis »Toolchain registriert · dotnet 9.0.306 registriert (N Dateien, Prüfsumme …)«. Bei `proprietary: true` erscheint vorher eine Lizenzabfrage (Nein = nichts passiert).
- [ ] Gegenprobe: eine Datei im Toolchain-Ordner ändern oder hinzufügen → »Toolchain-Ordner prüfen« → rot »Dateien haben sich seit der Registrierung verändert«.

## 3. VM-Aufbau mit Toolchains

`"mode": "hyperv"`, Oberfläche starten (oder die Abnahme aus `pruefung-M2b.md` mit `$env:LOCALAI_HYPERV_TOOLCHAINS = "dotnet,python"`; sie übernimmt die Registrierungen aus der Oberfläche).

- [ ] Nach dem Aufbau: »Offline-VM bereit – Selbsttest bestanden (… Toolchains: dotnet 9.0.306 (Hallo-Welt ok), python 3.12.10 (Hallo-Welt ok)); Aufbau N min«.
- [ ] Aufbauzeit mit und ohne Toolchains notieren (bitte an Claude schicken).
- [ ] Gegenprobe SEC-19/SEC-24: nach der Registrierung eine Datei verändern, Oberfläche neu starten → »gesperrt: Toolchain 'dotnet': Hash weicht ab: … – Aufbau abgebrochen«; es wird **keine** VM gebaut.
- [ ] Gegenprobe: im Projekt eine nicht registrierte Toolchain wählen → »gesperrt: Toolchain '…' ist für das Projekt gewählt, aber nicht registriert«.

Bei Fehlern in der VM (z. B. Installer-Exitcode, Selbsttest-Ausgabe fehlt) steht der Grund in der Kopfzeile. Mit `keepVmOnExit: true` bleibt die VM stehen; Protokolle im Gast: `C:\LocalAi\logs\toolchains.log` und `provision.log`.

## Bekannte Einschränkungen

- Jede Sitzung kopiert und installiert die Toolchains neu (Spezifikation R17); große Toolchains (Visual Studio Build Tools, HALCON) verlängern den Aufbau entsprechend – M5.
- Umgebungsvariablen der Toolchain gelten für Prüfbefehl, Selbsttest und später die Aktionen, nicht systemweit; `PATH` wird nicht verändert (Binaries werden mit festem Pfad aufgerufen).
