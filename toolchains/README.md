# Toolchains für die Sandbox-VM

Toolchains sind Werkzeuge, mit denen die Agenten in der Offline-VM bauen und testen (Spezifikation §8, R11/R12).
Sie werden **von dir** heruntergeladen, in einem eigenen Ordner abgelegt, in der Oberfläche **registriert**
(die Datei-Hashes werden dabei festgehalten) und bei **jedem** VM-Aufbau neu installiert – nur die, die das
Projekt in `toolchains` auswählt.

Vorlagen in diesem Ordner:

| Vorlage | Sprache | Installation in der VM | Testbericht |
|---|---|---|---|
| `dotnet` | C# | ZIP des .NET SDK | Trx (`dotnet test`) |
| `python` | Python | Installer + pytest-Wheels | JUnit (pytest) |
| `cpp` | C++ | Visual Studio Build Tools 2022 aus Offline-Layout (MSVC, CMake, CTest) | JUnit (CTest) |
| `cpp-clang` | C++ | ZIP mit llvm-mingw, CMake, Ninja (Alternative ohne Microsoft-Lizenz) | JUnit (CTest) |
| `powershell` | PowerShell 7 | ZIP von PowerShell + Pester/PSScriptAnalyzer | JUnit (Pester) |
| `powershell5` | Windows PowerShell 5.1 | nichts (Teil von Windows) + Pester/PSScriptAnalyzer | JUnit (Pester) |
| `batch` | Batch | nichts (cmd.exe) + Test-Harness | JUnit (Harness) |
| `hdevelop` | HDevelop | ZIP deiner HALCON-Installation + Lizenz + VC++-Laufzeit | JUnit (HDevEngine-Runner) |

Für die M5-Toolchains legt `scripts\toolchains\New-M5Toolchains.ps1 -Target D:\Agent\Toolchains` die Ordner an und
lädt bzw. sammelt die nötigen Dateien (einmal, auf dem Host mit Internet).

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

## C++ mit MSVC (`cpp`)

- Offline-Layout der **Visual Studio Build Tools 2022** im Unterordner `layout\` (enthält `vs_buildtools.exe`). Das
  Skript erzeugt es mit genau den Komponenten aus `install.arguments` (C++-Werkzeuge x64, CMake, Windows-SDK), Sprache
  en-US. Größe einige GB, Installation in der VM ca. 20–40 Minuten.
- Ohne Internet in der VM: Falls der Installer über Zertifikate klagt, die Dateien aus `layout\certificates` sind
  im Layout enthalten; bei sehr altem Windows-Abbild ein aktuelles ISO verwenden.
- Bauen und Testen: `cmake -S <project> -B {work}\build -G "Visual Studio 17 2022" -A x64`, dann `cmake --build`
  und `ctest --output-junit` (drei Schritte einer Aktion, siehe `then`). `project` ist der Ordner mit
  `CMakeLists.txt`, `configuration` Debug/Release.
- Bibliotheken offline: fertig gebaute Pakete (z. B. `vcpkg export --raw`, Inhalt von `installed\x64-windows`) in
  `packages\` – sie stehen über `CMAKE_PREFIX_PATH` für `find_package` bereit.
- Weicht die Windows-SDK-Komponente ab (ältere Build Tools): Komponente in `install.arguments` und im Skript anpassen.

## C++ mit Clang (`cpp-clang`, optional)

Ein ZIP `cpp-clang.zip` mit den Ordnern `llvm-mingw\` (Release von llvm-mingw, x86_64-ucrt), `cmake\` (CMake-ZIP für
Windows x64) und `ninja\ninja.exe`. Programme werden statisch gelinkt (`-static`), damit die Tests ohne zusätzliche
DLLs laufen. Generator „Ninja Multi-Config“.

## PowerShell (`powershell`, `powershell5`)

- `powershell`: ZIP `PowerShell-<version>-win-x64.zip` (GitHub-Release von PowerShell). `powershell5` nutzt die
  Windows PowerShell 5.1 des Gasts (`{system}\WindowsPowerShell\v1.0\powershell.exe`).
- Module offline in `modules\` (`Save-Module Pester, PSScriptAnalyzer -Path modules`); sie werden nach
  `C:\Toolchains\<name>\Modules` kopiert und von den Runnern direkt geladen. Projektmodule optional in `packages\`.
- Aktionen: `test` (Pester 5, `*.Tests.ps1`, JUnit), `lint` (PSScriptAnalyzer; Befunde der Schwere Error lassen den
  Lauf scheitern, `PSScriptAnalyzerSettings.psd1` im Projekt wird beachtet), `run` (ein Skript).

## Batch (`batch`)

Keine Installation. Das Harness (`harness\Invoke-LocalAiBatchTests.ps1`) führt jede Datei `*.test.cmd`,
`*.test.bat`, `test_*.cmd`, `test_*.bat` mit `cmd /d /c` im Projektordner aus: Exitcode 0 = bestanden; liegt
`<name>.expected.txt` daneben, muss die Ausgabe genau stimmen. `lint` prüft Sprungmarken (`goto`/`call :x`) und
Klammern. Neue Batch-Dateien aus Patches bekommen CRLF-Zeilenenden.

## HDevelop (`hdevelop`)

- `halcon.zip`: Inhalt deiner HALCON-Installation (`bin\`, `procedures\`, …) ohne `examples\` und `doc\`;
  entpackt nach `C:\Toolchains\hdevelop` = `HALCONROOT`.
- `license\`: Lizenzdatei(en) `license*.dat`. **Wichtig:** Die VM hat keine Netzwerkkarte und keinen USB-Zugriff.
  Eine an die MAC-Adresse oder einen Dongle gebundene Lizenz funktioniert dort nicht; nötig ist eine Lizenz, die ohne
  diese Bindung gilt (z. B. eine Evaluierungslizenz von MVTec). Ohne nutzbare Lizenz scheitert der Prüfbefehl beim
  VM-Aufbau mit einem Hinweis; die XML-Prüfung von `.hdev`-Patches funktioniert trotzdem.
- `vc_redist.x64.exe`: VC++-Laufzeit für die HALCON-DLLs (wird beim Aufbau still installiert).
- Runner `runner\Invoke-LocalAiHDevelop.ps1` (Windows PowerShell 5.1 + `halcondotnet.dll`, HDevEngine):
  `test` lädt zuerst alle `.hdev/.hdvp/.hdpl` (Lade-Test, verwendete Prozeduren müssen auflösbar sein) und führt dann
  die Tests aus: Programme `*_test.hdev`/`test_*.hdev`, jede lokale Prozedur `test_*` ohne Parameter ist ein Testfall,
  ein Fehler (z. B. `throw ('…')`) lässt ihn scheitern. `lint` = nur Lade-Test, `run` führt ein Programm aus.
- Unabhängig von HALCON prüft die Sandbox nach **jedem** Patch, ob `.hdev/.hdvp/.hdpl` gültiges HDevelop-XML bleiben.

## Format von `toolchain.json`

| Feld | Bedeutung |
|---|---|
| `name`, `version`, `displayName` | Name (Kleinbuchstaben), Version, Anzeigename |
| `guestPath` | immer `C:\Toolchains\<name>` |
| `license` | `proprietary` (true → beim Registrieren Lizenz bestätigen), `note`, optional `files` |
| `binaries` | erlaubte Programme mit festem Pfad relativ zu `guestPath`, optional `sha256`; außerdem genau `{system}\cmd.exe` und `{system}\WindowsPowerShell\v1.0\powershell.exe` (Windows selbst, ohne Hash) |
| `install` | `kind`: `Archive` (ZIP nach `guestPath` entpacken), `Installer` (.exe/.msi mit `arguments`), `None`; `expectedMinutes`; `check` (Prüfbefehl, `expectOutput`) |
| `deploy` | optional: Dateien/Ordner aus dem Toolchain-Ordner nach `guestPath` kopieren (`source` → `target`, beide relativ), z. B. Runner-Skripte, Module, Lizenzdatei – vor `postInstall` |
| `postInstall` | optionale Befehle nach der Installation (z. B. pip aus `wheels`); Exitcode 0, 3010 oder 1638 gilt als Erfolg |
| `selfTest` | Ordner mit Beispiel, Schritte, erwartete Ausgabe |
| `actions` | Vorlagen für `restore`, `build`, `test`, `run`, `lint`, `format`: Argumente sind Literale oder genau ein `{parameter}`; Parameter typisiert (`Path` = Pfad im Snapshot, `Choice` mit `values`, `Text` mit optionalem `pattern`, `Integer`). Erreichbar über `Toolchain.Restore/Build/Test/Run/Lint` (Lint nutzt `lint`, sonst `format`) – nur in der Offline-VM. Optional `then`: bis zu 5 weitere Schritte (gleiche Regeln), jeder nur nach Exitcode 0 des vorigen, im selben Arbeitsordner (z. B. CMake konfigurieren → bauen → testen) |
| `environment` | feste Umgebungsvariablen (PATH u. ä. nicht erlaubt) |
| `packageSource` | Offline-Paketquelle (`NuGet`, `Pip`, `Vcpkg`, `PSModules`) als Ordner im Toolchain-Ordner |
| `files` | optional: erwartete SHA-256 je Datei; Abweichung → nicht registrierbar |

Platzhalter, die das System selbst einsetzt: `{guest}` (Installationsordner), `{source}` (Kopie des
Toolchain-Ordners in der VM, nur Installation), `{selftest}`, `{work}` (Arbeitsordner des Jobs), `{results}`
(Ergebnisordner für Trx/JUnit), `{packages}` (Offline-Paketquelle, nur lesbar). Freie Shell-Befehle gibt es nicht.
Pfadparameter dürfen keines der Zeichen `& | < > ^ % ! "` enthalten (cmd.exe wertet sie aus).

Ausführung (ab M4): jeder Job bekommt eine frische Kopie des Snapshots und ein eigenes, leeres Benutzerprofil;
der Prozess läuft mit eingeschränktem Token auf niedriger Integritätsstufe in einem Job Object (kann Worker,
Toolchains, Windows und HKLM nicht verändern) und wird nach `timeoutSeconds` (Standard 120 s) samt Kindprozessen
beendet. Testergebnisse (`testResultFormat`: `Trx`, `JUnit`, `NUnit`, `ExitCode`) werden in ein einheitliches Format
übersetzt.
