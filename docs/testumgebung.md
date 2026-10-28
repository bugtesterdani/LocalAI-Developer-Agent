# Prüfprogramme der Testumgebung erstellen und simulieren

Die Agenten können Prüfprogramme für die Testumgebung (Repository `bugtesterdani/Testumgebung`) schreiben, ändern und
aus alten Prüfprogrammen übertragen. Sie prüfen und simulieren sie in der Offline-VM mit der Kommandozeile
`testumgebung-cli`. Das geht ohne Geräte und ohne die WPF-Oberfläche der Testumgebung.

Simulation ersetzt keine Abnahme an realer Hardware: Geräte, Busse und Prüflingskommunikation laufen nur über die
Simulationsdaten im Projekt.

## Bausteine

| Teil | Wo | Aufgabe |
|---|---|---|
| `testumgebung-cli` | Testumgebung: `src/Testumgebung.Cli`, Anleitung `docs/kommandozeile.md` | `check`, `build`, `test` (Szenarien, JUnit), `simulate`, `katalog`, `anleitung` |
| ZIP der Kommandozeile | Testumgebung: `build/publish-cli.ps1` | eigenständig (.NET, C#-/C++-Worker, portable Python), läuft in der VM ohne Installation |
| Toolchain `testumgebung` | LocalAI: `toolchains/testumgebung` | Installation beim VM-Aufbau, Selbsttest, Aktionen `lint`/`build`/`test`/`run` |
| Kurzfassung für die Agenten | LocalAI: `src/python/localai/orchestrator/prompts/testumgebung.md` | Projekt- und Szenarioformat, Bausteinkatalog (nur im Prompt, wenn Prüfprogramme betroffen sind) |

## Dateien im Projekt

- **Prüfprogramm** `*.tproj.json`: Format der Testumgebung (Init, Loop mit Prüflingsgruppe, Finalization, Variablen,
  Bausteine, `simulation` für die abstrakten Ressourcen).
- **Szenario** `*.tsim.json`: ein simulierter Lauf. Es legt fest:
  - welche Werkereingaben gemacht werden (z. B. Seriennummern der Reihe nach),
  - welche Werte die Simulation liefert (fest oder je Lesezugriff ein neuer Wert),
  - ob ein Werkerabbruch passiert,
  - was herauskommen muss: Gesamtergebnis, Ergebnis je Prüfling, Bewertung, Wert und Anzahl einzelner Schritte.

  Vollständiges Format: `docs/kommandozeile.md` in der Testumgebung, oder in der VM `testumgebung-cli anleitung szenario`.

Beispiel: `toolchains/testumgebung/selftest` (Netzteil mit Python-CodeBlock und Szenario mit zwei Prüflingen). Weitere
Szenarien (Unterspannung an der Grenze, Werkerabbruch) liegen in der Testumgebung unter `docs/beispiele/kommandozeile`.

## Ablauf mit den Agenten

1. Planer: Ist die Toolchain verfügbar, bekommt er den Hinweis, die Arbeit so aufzuteilen:
   - ein Coder schreibt bzw. ändert das Programm,
   - ein Tester schreibt Szenarien: Normalfall, Grenzwerte genau an und knapp über der Grenze, FAIL, ungültige
     Eingabe, Werkerabbruch.
2. Coder und Tester bekommen die Kurzfassung des Formats und des Bausteinkatalogs, sobald ihre Aufgabe
   Prüfprogramm-Dateien berührt, Prüfprogramme im Projekt liegen oder das Ziel ein Prüfprogramm nennt.
3. Prüflauf: `testumgebung test` auf dem Projektordner. Das prüft alle Programme, baut alle CodeBlocks und simuliert
   alle Szenarien. Ergebnis als JUnit-Bericht.
4. Bei Fehlern sieht der Debugger die Meldung samt Katalogauszug (falscher Bausteinparameter), Compilerfehler mit Zeile
   bzw. das Ende des Ablaufprotokolls (Schritt, Wert, Grenzen, Werkereingaben). Er übergibt dem Coder eine Diagnose, der korrigiert –
   wie bei jeder anderen Sprache.

| Aktion | Aufruf in der VM | Wofür |
|---|---|---|
| `lint {path}` | `testumgebung-cli check` | Struktur und Bausteinparameter, schnell |
| `build {path}` | `testumgebung-cli build --junit` | zusätzlich C#-/Python-CodeBlocks übersetzen |
| `test {path}` | `testumgebung-cli test --junit` | zusätzlich alle Szenarien |
| `run {script}` | `testumgebung-cli simulate` | ein Szenario mit Ablaufprotokoll |

Welcher Prüflauf gilt, steht im Plan unter „Prüfung in der Sandbox“. `testumgebung test (path=.)` wird gewählt, sobald
die Toolchain registriert ist und entweder Prüfprogramme im Projekt liegen, eine Aufgabe `*.tproj.json`/`*.tsim.json`
schreibt oder das Ziel die Testumgebung, ein Prüf-/Testprogramm oder `testumgebung-cli` nennt. Ist das Ziel ein
Prüfprogramm, prüft der Standard nur dieses (ein Python-Altprojekt daneben nicht). Beispiel für ein Ziel:

> Schreib `Old_Project/testcases/Butler_EFI_Endprüfung.robot` als Prüfprogramm für die Testumgebung um
> (`pruefung/butler_efi.tproj.json`). Der Tester schreibt Szenarien dazu; geprüft wird mit `testumgebung test`.

Erwartungen in Szenarien müssen aus der Prüfvorschrift bzw. dem Ziel stammen. Die Agenten sind angewiesen, keine
Grenzwerte, Geräteprotokolle oder Zeitgrenzen zu erfinden und Annahmen in der Projektbeschreibung zu nennen.

## Einrichten

1. Im Testumgebung-Repository auf dem PC (mit Internet, .NET SDK):
   `powershell -ExecutionPolicy Bypass -File build\publish-cli.ps1 -Version 1.0.0`.
   Ergebnis: `artifacts\Testumgebung-Cli-1.0.0-win-x64.zip`. Vor dem Packen läuft der Selbsttest mit den Beispielen.
2. Toolchain-Ordner `D:\Agent\Toolchains\testumgebung` anlegen. Den Inhalt von `toolchains\testumgebung`
   (`toolchain.json`, `selftest\`) und das ZIP hineinlegen. Andere Version: `version`, `displayName` und `install.file`
   in `toolchain.json` anpassen. Das Programm im ZIP heißt `Testumgebung.Cli.exe` (`testumgebung-cli.cmd` daneben ist nur
   ein Starter für die Konsole); „Toolchain-Ordner prüfen“ meldet, wenn `binaries` auf eine Datei zeigt, die nicht im ZIP liegt.
3. Im Projekt `"toolchains": ["testumgebung"]` eintragen (zusätzlich zu anderen).
4. Oberfläche → TOOLCHAINS → „Toolchain-Ordner prüfen“ → `testumgebung` registrieren → VM neu aufbauen.

## Prüfen

| Prüfung | Erwartet |
|---|---|
| VM-Aufbau | Statuszeile: `testumgebung 1.0.0 (Hallo-Welt ok)`. Der Selbsttest meldet „alles bestanden“ |
| Ziel „Erstelle ein Prüfprogramm für ein Netzteil: Seriennummer, Spannung 220–240 V, Strom 0,5–5 A“ | Plan mit Coder- und Tester-Aufgabe; `*.tproj.json` und mehrere `*.tsim.json`; Prüflauf `testumgebung test (path=.)` grün |
| Grenzwert absichtlich falsch | `test` schlägt mit „Gesamtergebnis: erwartet …, erhalten …“ fehl; der Debugger findet die Ursache, der Coder korrigiert |
| Python-Syntaxfehler im CodeBlock | `build` meldet „Fehler Zeile …“ für den CodeBlock |

## Grenzen

- Nur Windows-Gast (`hyperv`). In der Linux-VM bzw. unter QEMU gibt es die Toolchain nicht.
- C++- und LabVIEW-CodeBlocks werden nur gebaut, wenn das ZIP die C++-Toolchain enthält. Sie wird beim Bauen des ZIPs
  übernommen, wenn sie im Testumgebung-Repository vorhanden ist.
- Auftragsauswahl über ein eigenes Auswahlprojekt, zwei Prüfplätze und Kurvenausgabe an echten Geräten simuliert die
  Kommandozeile nicht gesondert. Sie laufen nur so weit, wie es die Simulationsdaten des Projekts erlauben.
- Die Kurzfassung des Bausteinkatalogs ist ein Stand der Testumgebung. Nach einer neuen Version neu erzeugen mit
  `testumgebung-cli katalog` und `katalog <typ>`. Die Fehlermeldungen von `lint` zeigen immer den aktuellen Katalog.
