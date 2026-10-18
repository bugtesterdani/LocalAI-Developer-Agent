# Prüfanleitung M4 – Build/Test in der VM, Patch.Apply, große Repositories

Ziel: Die Agenten **wenden ihre Patches an** (in einer Kopie in der Sandbox, nie in deinem Projektordner), **bauen und
testen in der Offline-VM**, und der Debugger repariert anhand **echter Fehlermeldungen**. Für große Projekte bekommt der
Planner eine **Repo-Map** (Symbolindex) statt einer Dateiliste.

Abnahme laut Plan: Ende zu Ende in C# und Python; Repository mit > 2.000 Dateien; SEC-13 und SEC-20 grün.

## Was ist neu

| Tool | Wirkung | Freigabe |
|---|---|---|
| `Patch.Apply` | wendet einen Unified Diff auf den aktuellen Stand **in der Sandbox** an und erzeugt daraus einen neuen, unveränderlichen Snapshot. Dein Projektordner bleibt unverändert (Übernahme folgt mit M6). | Sammelfreigabe: **pro Datei abwählbar**; die Karte zeigt den vollständigen Diff je Datei |
| `Toolchain.Restore/Build/Test/Run/Lint` | startet in der Offline-VM das im `toolchain.json` festgelegte Programm mit **typisierten Parametern** auf einer frischen Kopie des Snapshots; Ergebnis: Exitcode, Log-Auszug, einheitlicher Testbericht (aus Trx/JUnit/NUnit) | Einzelaufruf; die Karte zeigt Toolchain, Aktion und jeden Parameter sowie den Hinweis, dass Build-Skripte/Tests Code in der VM ausführen |
| `Repo.Index` | Symbolindex des Snapshots (Python über `ast`; C#, C/C++, PowerShell, Batch über Muster; HDevelop als XML; SQLite FTS5) und eine **Repo-Map** im Zeichenbudget, wichtigste Dateien zuerst | Einzelaufruf; Agenten können zusätzlich mit `symbols` gezielt nach Definitionen suchen |

Ablauf eines Runs: Snapshot → **Repo-Map** → Planner (Plan + Prüfläufe) → Coder/Tester parallel, Patches nach deiner
Freigabe nacheinander angewendet (passt ein Patch nicht, bekommt der Agent den Grund und korrigiert, höchstens 3×) →
**Build/Test in der VM** → bei Fehlern Debugger auf den echten Meldungen → erneut testen (höchstens 3 Reparaturen) →
Reviewer mit echtem Testbericht → Zusammenfassung mit angewendeten Dateien und Teststatus.

Sicherheit:
- Toolchain-Aktionen gibt es **nur in der Betriebsart `hyperv`**. In `dev-local-unsafe` und `none` lehnt der Broker sie
  immer ab – es wird nie etwas auf dem Host ausgeführt. `Patch.Apply` und `Repo.Index` gehen auch in `dev-local-unsafe`
  (sie verändern nur die Arbeitskopie des Workers).
- Ausführbar ist nur, was das registrierte Manifest deklariert; Parameter werden im Broker **und** im Gast geprüft
  (Typ, Auswahlwerte, Pfad im Snapshot, keine Werte mit `-` am Anfang, keine freien Kommandos – SEC-20).
- Werkzeugprozesse laufen im Gast mit **eingeschränktem Token auf niedriger Integritätsstufe** in einem Job Object:
  Worker, Toolchains, Windows und HKLM sind für sie nicht beschreibbar, am Ende wird der ganze Prozessbaum beendet
  (SEC-21). Jeder Job bekommt ein frisches Arbeitsverzeichnis mit eigenem, leerem Benutzerprofil (SEC-07).
- Neue Ordnerrechte im Projekt: `Modify`/`Create` (Patches ändern bzw. legen Dateien an), `Delete` (Patches löschen
  Dateien – standardmäßig aus), `Execute` (Build/Test). Ohne diese Rechte liefern die Agenten nur Vorschläge.

## 0. Vorbereitung (einmalig)

**Deine Dateien bleiben unverändert** (`config\localai.json`, `config\demo-project.json`, `config\models.local.json`).
Neu im Repository: `config\m4-csharp-project.json`, `config\m4-python-project.json` und die Beispiele
`samples\m4-csharp`, `samples\m4-python`, `samples\m4-sec13`.

1. **dotnet-Toolchain aktualisieren.** Die Vorlage holt Pakete jetzt nur aus der Offline-Quelle (`{packages}`) und
   baut beim Testen selbst (kein getrennter Restore nötig). In deinen Toolchain-Ordner kopieren:
   ```powershell
   copy toolchains\dotnet\toolchain.json D:\Agent\Toolchains\dotnet-9.0.306\toolchain.json
   ```
   (Falls du `version`/`install.file` angepasst hattest: nur den Abschnitt `actions` übernehmen.)
2. **Offline-NuGet-Pakete** für Testprojekte (die Beispiele zielen auf `net10.0`, passend zu einer dotnet-10-Toolchain) (xUnit, Test-SDK) einmal mit Internet laden – braucht `dotnet` auf dem
   Host, führt nur `dotnet restore` eines Vorlagenprojekts aus:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\toolchains\Get-NuGetOfflinePackages.ps1 -ToolchainDirectory D:\Agent\Toolchains\dotnet-9.0.306
   ```
   Eigene Projekte mit weiteren Paketen: `-Project C:\Pfad\Projekt.csproj` zusätzlich angeben.
3. **python-Toolchain:** unverändert (pytest-Wheels aus M2c genügen).
4. Oberfläche starten → **TOOLCHAINS** → prüfen → **dotnet** und **python** neu registrieren (die Hashes haben sich geändert).
5. In **deiner** `config\localai.json`:
   - `"project": "config/m4-csharp-project.json"` (für Abschnitt 3; für Abschnitt 4 `m4-python-project.json`),
   - `"sandbox": { "mode": "hyperv", … }`.
   Optional unter `sandbox.hyperv`: `"lowIntegrityTools": false` schaltet die niedrige Integritätsstufe ab – **nur zur
   Fehlersuche**, falls ein Werkzeug damit nicht startet (dann bitte Log schicken).
6. Die VM wird beim nächsten Start **neu aufgebaut** (Worker und Gastskripte haben sich geändert). In der Kopfzeile danach:
   „Toolchains: dotnet 9.0.306 (Hallo-Welt ok), python 3.15.0 (Hallo-Welt ok)“.

Eigenes Projekt: in der Projektdatei `rights` um `"Modify", "Create", "Execute"` ergänzen und `"toolchains": ["dotnet", "python"]` setzen.

## 1. Automatische Tests

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

| Schritt | Erwartet |
|---|---|
| 3/4 C#-Tests | `Passed: 90, Skipped: 1` (unter Windows; der Toolchain-Lauf mit POSIX-Ersatz wird übersprungen) – neu `ExecutionTests` (Patch-Ziele und -Rechte, SEC-20 im Broker, Toolchain nur mit VM-Katalog, abgeleitete Snapshots, Ende zu Ende mit dem Python-Worker) und Karten-Tests (Patch je Datei, Toolchain-Aufruf) |
| 4/4 Python-Tests | rund `200 passed`, 7–8 übersprungen (unter Windows die Toolchain-Läufe mit POSIX-Ersatz sowie die beiden VM-Abnahmen) – neu `test_worker_m4.py` (Diff-Anwendung mit Versatz/CRLF/BOM/Einrückung, abgeleitete Snapshots, Toolchain-Ausführung mit fester Umgebung, Zeitgrenze, Trx/JUnit, Repo-Map mit 2.100 Dateien), neue Run-Tests (Patch anwenden, Reparatur nach echtem Testfehler, Patch passt nicht → Agent korrigiert, ohne Rechte/VM nur Vorschläge) und `tests/integration/test_m4_dev_local.py` (echter Broker + Worker: Patch nur in der Sandbox mit Abwahl, Toolchain nie auf dem Host, Repo-Map, vollständiger Run) |

## 2. Abnahme-Skript mit echter VM (ohne Modell, ca. 10 Minuten nach dem Aufbau)

Prüft alle Werkzeuge direkt über den Broker – unabhängig von der Modellqualität. Die Oberfläche dabei **schließen**
(gleiche VM).

```powershell
$env:LOCALAI_M4_ABNAHME = "1"
uv run --project src/python pytest -s tests/integration/test_m4_abnahme.py
```

- [ ] C#: Patch (neue Methode `Teilen` + 2 Tests) → `dotnet test` mit **5 bestandenen** Tests; absichtlich falscher Test → Testbericht mit genau **1 Fehler** (`Absichtlich_falsch`).
- [ ] Python: dasselbe mit pytest (5 bestanden, dann 1 Fehler `test_absichtlich_falsch`).
- [ ] **SEC-20:** nicht deklarierte Aktion, freies Kommando, eingeschmuggelte Option, zusätzlicher Parameter, unbekanntes Tool → alle abgewiesen, keine offene Freigabe entsteht.
- [ ] **SEC-13/-21/-07:** Build von `samples\m4-sec13` (MSBuild-Target wie ein bösartiges NuGet-Paket): Schreiben nach `C:\LocalAi\python`, `C:\Toolchains\dotnet`, `C:\Windows`, Registry HKLM, Internet und `%USERPROFILE%\.ssh` scheitern; nur die eigene Arbeitskopie ist beschreibbar. Danach laufen die Python-Tests unverändert weiter.
- [ ] **Großes Repository:** 2.213 generierte Dateien → Snapshot und Repo-Map ≤ 12.000 Zeichen, `src/Core/CsvParser.cs` steht oben.
- [ ] Ergebnisdatei `%LOCALAPPDATA%\LocalAi\abnahme\m4-abnahme.json` (Zeiten, SEC-13-Exitcodes) bitte an Claude schicken; bei Fehlern zusätzlich die Logs `m4-*.log` daneben.

Die Abnahme nutzt denselben VM-Ordner wie die Oberfläche (`%LOCALAPPDATA%\LocalAi\vm`). Mit `reuseVm: true` wird die VM
am Ende gespeichert und beim nächsten Lauf – oder von der Oberfläche – fortgesetzt, solange sich Worker, Gastskripte,
Toolchain-Registrierungen und die Toolchain-Liste des Projekts (`dotnet`, `python`) nicht ändern. Nach einem Update mit
neuem Worker-Code wird einmal neu aufgebaut.

## 3. Ende zu Ende C# mit Modell

Projekt `config/m4-csharp-project.json`, Modus `hyperv`, Ollama wie in M3. Warten bis „Offline-VM bereit“.

Chat: „Füge Rechner.Teilen(a, b) hinzu. Teilen durch 0 soll eine ArgumentException mit deutscher Meldung werfen. Mit Tests.“

- [ ] Freigabe **Planner · Repo.Index** (statt File.List); der Plan nennt unter „Prüfung in der Sandbox“ z. B. `dotnet test (project=tests/Rechner.Tests/Rechner.Tests.csproj, configuration=Debug)`.
- [ ] **Coder · Patch.Apply**: Karte zeigt unter „Änderungen“ den Diff je Datei, links „ändern“/„neu“; eine Datei abwählen → „Auswahl übernehmen“ erzeugt einen neuen Vorschlag, der abgewählte Teil bleibt unverändert („abgewählt (unverändert)“ im Chat).
- [ ] Im Chat: „Patch – angewendet → Stand …“. Passt ein Patch nicht, steht im Verlauf „Patch nicht angewendet (…) – Agent korrigiert“ und der Agent liest die Datei erneut.
- [ ] **Tester · Toolchain.Test**: Karte zeigt „Toolchain: dotnet · Aktion: test“ und den Hinweis auf Codeausführung in der VM.
- [ ] Chat „Prüfung in der Sandbox (Stand …)“: Tests mit Anzahl bestanden/fehlgeschlagen; bei Fehlern die Testnamen.
- [ ] Bei Fehlern: Debugger nennt die echte Fehlermeldung als Hypothese, sein Fix wird angewendet und erneut getestet.
- [ ] Zusammenfassung: „Build/Tests in der Sandbox: bestanden“ (oder Grund), „N Patch(es) angewendet (… Datei(en))“, „Dein Projektordner ist unverändert“. Im Explorer: `samples\m4-csharp` ist tatsächlich unverändert.

## 4. Ende zu Ende Python mit Modell

Projekt `config/m4-python-project.json` (Oberfläche neu starten; die VM wird bei `reuseVm` wiederverwendet, beide Projekte nutzen dieselben Toolchains).

Chat: „Füge teilen(a, b) hinzu. Teilen durch 0 soll einen ValueError mit deutscher Meldung auslösen. Mit Tests.“

- [ ] Wie Abschnitt 3, mit `python test (path=.)`; Testbericht aus pytest (JUnit).

## 5. Großes Repository mit Modell (optional)

```powershell
uv run --project src/python python scripts/samples/make_large_repo.py D:\Agent\Projekte\gross
```
Eigene Projektdatei mit Grant auf diesen Ordner (`List`, `Read`), Ziel z. B. „Wo wird CsvParser benutzt? Füge eine Methode CountEmpty hinzu.“

- [ ] Repo-Map passt ins Budget; der Planner nutzt `symbols`/`search` statt alle Dateien zu lesen.
- [ ] Dauer von Snapshot und Repo.Index notieren (Verlauf des Planners).

## Fehlersuche

| Meldung | Ursache / Abhilfe |
|---|---|
| „Builds und Tests laufen nur in der Offline-VM …“ | Betriebsart nicht `hyperv`, keine registrierte Toolchain im Projekt oder VM noch im Aufbau. |
| „der Projektfreigabe fehlen die Rechte Modify, Create“ / „… Execute“ | Rechte in der Projektdatei ergänzen. |
| `Toolchain '…' ist für dieses Projekt nicht registriert …` | Toolchain unter TOOLCHAINS (neu) registrieren; Projekt muss sie in `toolchains` nennen. |
| Testlauf: `NU1101 Unable to find package …` | Paket fehlt in der Offline-Quelle → Abschnitt 0 Schritt 2 (ggf. mit `-Project`), neu registrieren, VM neu aufbauen. |
| Testlauf: „keine Ergebnisdatei gefunden“ | Build fehlgeschlagen (Log-Auszug im Chat/Verlauf) oder keine Tests im Projekt. |
| Werkzeug startet in der VM nicht („CreateProcess … fehlgeschlagen“ oder Zugriffsfehler beim Build) | Niedrige Integritätsstufe verträgt sich nicht mit dem Werkzeug: Log `C:\LocalAi\logs\worker.log` (Gast, `scripts\sandbox\Get-LocalAiGuestLogs.ps1`) schicken; vorübergehend `"lowIntegrityTools": false`. |
| Exitcode `-532462766` (0xE0434352) bei dotnet | Absturz im MSBuild-Prozess; bis 18.10. fehlten Systemvariablen wie `ProgramFiles(x86)` in der Jobumgebung (NuGet). Ab `M4-Fix 1` behoben; sonst Log `m4-*.log` schicken. |
| `UnauthorizedAccessException … C:\Windows\SystemTemp\MSBuildTemp…` | Windows gibt SYSTEM-Prozessen fest `C:\Windows\SystemTemp` als Temp-Ordner. Ab `M4-Fix 2` kennzeichnet die Provisionierung ihn für niedrige Integrität; der Worker entfernt nach jedem Job, was der Job dort angelegt hat. VM neu aufbauen. |
| „Patch nicht angewendet … Hunk n passt nicht“ | Das Modell hat sich auf einen anderen Dateistand bezogen; der Agent bekommt den Grund und korrigiert (höchstens 3×). |

## Bekannte Einschränkungen

- Die Änderungen bleiben in der Sandbox; die Übernahme in deinen Projektordner (`Workspace.ExportPatch`) folgt mit M6.
- Symbolextraktion ohne tree-sitter/Roslyn (nur Standardbibliothek): Muster für C#, C/C++, PowerShell, Batch können
  Sonderfälle verfehlen; Python ist exakt (`ast`).
- `Toolchain.Lint` nutzt bei dotnet `dotnet format --verify-no-changes` – braucht vorher einen Build im selben Job und ist
  deshalb nicht Teil des Standard-Prüflaufs.
- Toolchain-Prozesse laufen je Job auf einer frischen Kopie: Restore/Build passieren innerhalb von `test` erneut (Zeit
  statt Zwischenstände, dafür keine Seiteneffekte zwischen Jobs).
