# Toolchains für die Sandbox-VM

Toolchains sind Werkzeuge, mit denen die Agenten in der Offline-VM bauen und testen (Spezifikation §8, R11/R12).
Sie werden **von dir** heruntergeladen, in einem eigenen Ordner abgelegt, in der Oberfläche **registriert**
(die Datei-Hashes werden dabei festgehalten) und bei **jedem** VM-Aufbau neu installiert – nur die, die das
Projekt in `toolchains` auswählt.

Dieser Ordner enthält zwei Vorlagen: `dotnet` (.NET SDK) und `python` (Python mit pytest).

## Einrichten (Beispiel mit `D:\Agent\Toolchains`)

1. `config\localai.json` → `"toolchains": { "directory": "D:\\Agent\\Toolchains" }`.
2. Je Toolchain einen Unterordner anlegen, z. B. `D:\Agent\Toolchains\dotnet-9.0.306`, und den Inhalt der Vorlage
   (`toolchain.json` und `selftest\`) hineinkopieren.
3. Die Installationsdateien dazulegen (siehe unten). Weicht die Version ab: in `toolchain.json` `version`,
   `displayName`, `install.file` und `install.check.expectOutput` anpassen.
4. Im Projekt (`config\demo-project.json`) die Toolchains wählen: `"toolchains": ["dotnet", "python"]`.
5. Oberfläche starten → Bereich **TOOLCHAINS** → »Toolchain-Ordner prüfen« → je Toolchain **Registrieren**.
6. Beim nächsten VM-Aufbau werden die Toolchains installiert, geprüft (`install.check`) und mit dem
   Hallo-Welt-Beispiel aus `selftest\` getestet. Die Kopfzeile zeigt danach z. B.
   »Toolchains: dotnet 9.0.306 (Hallo-Welt ok), python 3.15.0 (Hallo-Welt ok)«.

Wird nach der Registrierung eine Datei verändert, hinzugefügt oder entfernt, bricht der nächste VM-Aufbau ab
(»Hash weicht ab«). Dann erneut prüfen und registrieren.

## .NET SDK (`dotnet`)

- Datei: **Binaries** des SDK als ZIP für Windows x64, z. B. `dotnet-sdk-9.0.306-win-x64.zip`
  (Download-Seite von .NET → SDK → Windows → Binaries x64). Kein Installer nötig.
- Offline-NuGet-Pakete: Ordner `packages\` im Toolchain-Ordner (`.nupkg`-Dateien, flach). Restore, Build und Test
  holen Pakete ausschließlich von dort (`-p:RestoreSources={packages}`). Testframeworks (xUnit, Test-SDK) einmal mit
  Internet laden:
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts\toolchains\Get-NuGetOfflinePackages.ps1 -ToolchainDirectory D:\Agent\Toolchains\dotnet-9.0.306
  ```
  Weitere Projekte mit eigenen Paketen: `-Project C:\Pfad\Projekt.csproj`. Danach neu registrieren.

## Python (`python`)

- Datei: Windows-Installer 64 Bit, z. B. `python-3.15.0-amd64.exe` (python.org).
- pytest offline als Wheels in `wheels\` – auf einem PC mit Internet im Toolchain-Ordner ausführen:
  ```powershell
  python -m pip download pytest --only-binary=:all: --python-version 3.15 --platform win_amd64 -d wheels
  ```

## Format von `toolchain.json`

| Feld | Bedeutung |
|---|---|
| `name`, `version`, `displayName` | Name (Kleinbuchstaben), Version, Anzeigename |
| `guestPath` | immer `C:\Toolchains\<name>` |
| `license` | `proprietary` (true → beim Registrieren Lizenz bestätigen), `note`, optional `files` |
| `binaries` | erlaubte Programme mit festem Pfad relativ zu `guestPath`, optional `sha256` |
| `install` | `kind`: `Archive` (ZIP nach `guestPath` entpacken), `Installer` (.exe/.msi mit `arguments`), `None`; `expectedMinutes`; `check` (Prüfbefehl, `expectOutput`) |
| `postInstall` | optionale Befehle nach der Installation (z. B. pip aus `wheels`) |
| `selfTest` | Ordner mit Beispiel, Schritte, erwartete Ausgabe |
| `actions` | Vorlagen für `restore`, `build`, `test`, `run`, `lint`, `format`: Argumente sind Literale oder genau ein `{parameter}`; Parameter typisiert (`Path` = Pfad im Snapshot, `Choice` mit `values`, `Text` mit optionalem `pattern`, `Integer`). Erreichbar über `Toolchain.Restore/Build/Test/Run/Lint` (Lint nutzt `lint`, sonst `format`) – nur in der Offline-VM |
| `environment` | feste Umgebungsvariablen (PATH u. ä. nicht erlaubt) |
| `packageSource` | Offline-Paketquelle (`NuGet`/`Pip`) als Ordner im Toolchain-Ordner |
| `files` | optional: erwartete SHA-256 je Datei; Abweichung → nicht registrierbar |

Platzhalter, die das System selbst einsetzt: `{guest}` (Installationsordner), `{source}` (Kopie des
Toolchain-Ordners in der VM, nur Installation), `{selftest}`, `{work}` (Arbeitsordner des Jobs), `{results}`
(Ergebnisordner für Trx/JUnit), `{packages}` (Offline-Paketquelle, nur lesbar). Freie Shell-Befehle gibt es nicht.

Ausführung (ab M4): jeder Job bekommt eine frische Kopie des Snapshots und ein eigenes, leeres Benutzerprofil;
der Prozess läuft mit eingeschränktem Token auf niedriger Integritätsstufe in einem Job Object (kann Worker,
Toolchains, Windows und HKLM nicht verändern) und wird nach `timeoutSeconds` (Standard 120 s) samt Kindprozessen
beendet. Testergebnisse (`testResultFormat`: `Trx`, `JUnit`, `NUnit`, `ExitCode`) werden in ein einheitliches Format
übersetzt.
