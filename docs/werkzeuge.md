# Werkzeuge und Rechte

Alles, was ein Agent außerhalb seines Modells tun will, ist ein **Werkzeugaufruf** und braucht eine Freigabe – auch
Lesen und Suchen. Reine Modellinferenz auf bereits freigegebenen Daten, Anzeige und interne Planung sind keine
Werkzeuge. Ein generisches Shell-Werkzeug gibt es nicht: Unbekannte Werkzeuge (z. B. `Toolchain.Shell`,
`Shell.Execute`) weist der Broker ab.

## Werkzeugkatalog

Definiert in `src/dotnet/LocalAi.Broker/ProposalPolicy.cs` (Prüfung) und ausgeführt im Worker
(`src/python/localai/worker/`).

| Werkzeug | Zweck | Ziele | benötigte Rechte | Ausführung |
|---|---|---|---|---|
| `Workspace.Snapshot` | freigegebenen Ordner als Kopie in die Sandbox übertragen | – | `List`, `Read` | Broker liest, Sandbox empfängt |
| `File.List` | Ordnerstruktur auflisten | 1–50, Sammel | `List` | Sandbox |
| `File.Read` | eine Datei lesen (optional Zeilenbereich) | genau 1 | `Read` | Sandbox |
| `File.ReadMany` | mehrere Dateien lesen | 1–200, Sammel | `Read` | Sandbox |
| `File.Search` | Volltextsuche | 1–50, Sammel | `Read` | Sandbox |
| `Repo.Index` | Repo-Map bzw. Symbolsuche (`query`) | – | `List`, `Read` | Sandbox |
| `Patch.Apply` | Patch auf die Arbeitskopie anwenden → neuer Snapshot | 1–200, Sammel, je Datei | `Read` + je Datei `Create`/`Modify`/`Delete` | Sandbox |
| `Toolchain.Restore` | Abhängigkeiten aus der Offline-Paketquelle | – | `Read`, `Execute` | nur Offline-VM |
| `Toolchain.Build` | bauen | – | `Read`, `Execute` | nur Offline-VM |
| `Toolchain.Test` | testen, Bericht als einheitlicher `TestReport` | – | `Read`, `Execute` | nur Offline-VM |
| `Toolchain.Run` | Programm/Skript aus dem Snapshot ausführen | – | `Read`, `Execute` | nur Offline-VM |
| `Toolchain.Lint` | Lint, ersatzweise Format-Prüfung | – | `Read`, `Execute` | nur Offline-VM |
| `Web.Fetch` | eine HTTPS-URL abrufen (1. Webfreigabe) | – | – | Web Fetcher (Host, eigener Prozess) |
| `Web.Content` | bereinigten Text an einen Agenten geben (2. Webfreigabe) | – | – | Broker |
| `Workspace.Diff` | Übernahme vorbereiten: Änderungen ins Staging holen | – | `Read` | Sandbox → Broker |
| `Workspace.ExportPatch` | Übernahme in den Projektordner | 1–200, Sammel, je Datei | `Read` + je Datei `Create`/`Modify`/`Delete` | Broker (Host) |
| `OsDebug.Start` | Debug-Sitzung mit QEMU starten (eigene Freigabe mit Grenzen) | – | `Read`, `Execute` | Linux-VM |
| `OsDebug.Command` | Befehl in der laufenden Debug-Sitzung | – | im Rahmen der Sitzung | Linux-VM |
| `OsDebug.Stop` | Debug-Sitzung beenden | – | im Rahmen der Sitzung | Linux-VM |

„Sammel“ = Sammelfreigabe mit abwählbaren Zielen (siehe [Bedienung](bedienung.md#sammelfreigabe)). Ausführende
Werkzeuge (`Toolchain.*`, `Repo.Index`, `OsDebug.Start`) und `Web.Fetch` sind immer Einzelaufrufe.

### Toolchain-Aktionen

`Toolchain.*` ruft nur Aktionen auf, die das [Toolchain-Manifest](../toolchains/README.md#format-von-toolchainjson)
deklariert:

| Werkzeug | Aktion im Manifest |
|---|---|
| `Toolchain.Restore` | `restore` |
| `Toolchain.Build` | `build` |
| `Toolchain.Test` | `test` |
| `Toolchain.Run` | `run` |
| `Toolchain.Lint` | `lint`, sonst `format` |

Parameter sind typisiert (`Path` im Snapshot, `Choice`, `Text` mit Muster, `Integer`) und werden im Broker **und** im
Gast geprüft. Nicht deklarierte Aktionen, zusätzliche oder fehlende Parameter, freie Kommandos oder Werte mit
führendem `-` werden abgewiesen (SEC-20). PowerShell und Batch sind nur über `Toolchain.Run` mit festem Interpreter
und einem Skriptpfad aus dem Snapshot erreichbar. In `dev-local-unsafe` sind alle `Toolchain.*` gesperrt.

Build- und Testcode kann beliebigen Code ausführen (z. B. MSBuild-Targets eines NuGet-Pakets). Deshalb läuft er nur
in der VM, auf einer frischen Jobkopie, mit eingeschränkten Rechten und ohne Netzwerk – siehe
[Architektur](architektur.md#isolation-im-gast).

## Ordnerrechte

Rechte werden je Grant in der [Projektdatei](konfiguration.md#projektdatei) vergeben. Es gibt keine Vererbung auf
andere Ordner.

| Recht | erlaubt |
|---|---|
| `List` | Snapshot erstellen (zusammen mit `Read`), Ordner auflisten, Repo-Index |
| `Read` | Dateiinhalte lesen, suchen; Grundlage aller anderen Werkzeuge |
| `Create` | neue Dateien per Patch bzw. Übernahme |
| `Modify` | bestehende Dateien ändern |
| `Delete` | Dateien löschen – nur wenn ausdrücklich vergeben |
| `Execute` | Toolchain-Aktionen und Debug-Sitzungen – nur wenn ausdrücklich vergeben |

Bei `Patch.Apply` und `Workspace.ExportPatch` richtet sich das nötige Recht nach der Änderungsart jeder einzelnen
Datei. Patches schreiben nur in abgeleitete Snapshots in der Sandbox – der Projektordner auf dem Host ändert sich
ausschließlich über `Workspace.ExportPatch`.

### Pfadregeln

Abgewiesen werden `..`, absolute Pfade, UNC- und Gerätepfade, Alternate Data Streams sowie alles, was über
Verknüpfungen, Junctions oder Hardlinks aus dem Grant hinausführen könnte (SEC-05). Die Regeln sind in C# und Python
identisch und werden mit gemeinsamen Testvektoren (`contracts/testvectors/paths.json`) geprüft.

### Hashbindung (TOCTOU)

- Lesen ist an den Snapshot und den erwarteten Dateihash gebunden; weicht er ab, scheitert der Aufruf ohne Inhalt
  (SEC-04).
- Schreiben bei der Übernahme prüft den vorherigen Inhalt jeder Datei unmittelbar vor dem Schreiben.

## Grenzen

| Grenze | Wert |
|---|---|
| Ziele je Sammelaufruf | 200 (`File.List`, `File.Search`: 50) |
| Gültigkeit eines Vorschlags | 5 Minuten (`--ttl-seconds`) |
| Zeitgrenze Toolchain-Prozess | `timeoutSeconds` der Aktion (Standard 120 s), plus `toolchainExtraMinutes` |
| Webabruf | 2 MiB, 15 s, 20.000 Zeichen (siehe [Konfiguration](konfiguration.md#web)) |
| Debug-Sitzung | `maxCommands`, `maxMinutes` der Projektdatei |

## Agentenaktionen

Die Agenten sehen die Werkzeuge nicht direkt. Sie antworten mit einem JSON-Block, den der Orchestrator
(`orchestrator/protocol.py`) prüft und in einen Vorschlag übersetzt (`orchestrator/tools.py`):

| Aktion des Agenten | wird zu | Anmerkung |
|---|---|---|
| `list` (`path`, `depth` 1–5) | `File.List` | |
| `read` (`files` mit `path`, optional `start`/`end`) | `File.Read` bzw. `File.ReadMany` | mehrere Dateien = Sammelfreigabe |
| `search` (`query`, `path`) | `File.Search` | |
| `symbols` (`query`) | `Repo.Index` mit Suchbegriff | Klassen, Funktionen, Methoden |
| `fetch` (`url`) | `Web.Fetch`, danach `Web.Content` | nur `https://` |
| `debug` (`command`, …) | `OsDebug.Command` | nur mit laufender Debug-Sitzung |
| `finish` (`result`) | – | Ergebnis der Rolle (Plan, Patch, Testbericht, Review …). Patches als Unified Diff oder SEARCH/REPLACE-Blöcke |

Snapshot, Repo-Map, Patches, Prüfläufe und Übernahme stößt der Orchestrator selbst an – ebenfalls nur als Vorschläge
mit Freigabe. Lehnt der Nutzer ab, erfährt der Agent es und darf denselben Vorschlag nicht unverändert wiederholen.
