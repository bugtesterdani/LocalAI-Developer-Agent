# Betrieb

Wo das System seine Daten ablegt, wie man Protokolle liest, sichert, wiederherstellt und Störungen behebt. Das
vollständige Runbook mit allen Situationen steht in der
[Spezifikation §17](Local-AI-Developer-Agent-Projektspezifikation.md#17-runbook).

## Datenordner

| System | Datenordner des Brokers | VM-Ordner (Standard) |
|---|---|---|
| Windows | `%LOCALAPPDATA%\LocalAi\broker` | `%LOCALAPPDATA%\LocalAi\vm`, `vm-linux` |
| Linux | `~/.local/share/LocalAi/broker` (Rechte 0700) | `~/.local/share/LocalAi/vm-qemu` |

Inhalt des Datenordners:

| Eintrag | Inhalt | Hinweis |
|---|---|---|
| `audit.db` | Audit-Journal (SQLite mit Hashkette) | nie von Hand ändern – ein manipuliertes Journal verhindert den Start |
| `toolchains.json` | registrierte Toolchains mit Datei-Hashes | enthält absolute Pfade; auf einem neuen Rechner neu registrieren |
| `calibration.json`, `kalibrierung-*.md` | Kalibrierung je Hardware und Modell-Digest | gilt nur für diese Hardware |
| `run-journal.json` | Ziel, Plan, Aufgabenstände, angewendete Patches des letzten Runs – keine Freigaben | Grundlage für `fortsetzen` |
| `orchestrator.log` | Ausgaben des Orchestrators | erste Anlaufstelle bei Agentenproblemen |
| `hyperv.log`, `hyperv-linux.log`, `qemu-vm.log` | VM-Steuerung (Aufbau, Start, Laufwerkstausch, Sperren) | erste Anlaufstelle bei VM-Problemen |
| `worker.log` | Entwicklungs-Worker (`dev-local-unsafe`) | |
| `web.log`, `web/` | Webprozesse und Quarantäne der Rohdaten | Aufbewahrung 7 Tage |
| `export/` | Staging der Übernahme und **Sicherungen der Originale** vor dem Schreiben | |
| `diagnose/`, `sicherungen/` | erstellte Diagnosepakete und Sicherungen | |
| `sandbox-dev/`, `sandbox-scratch*/`, `sandbox-abgleich/` | Arbeitsordner der Sandbox-Steuerung | werden vom Broker verwaltet |

Im VM-Ordner liegen außerdem `build.log` (Aufbau) und unter QEMU `console.log` (serielle Konsole des Gasts, begrenzt).

## Protokolle lesen

| Problem | zuerst lesen |
|---|---|
| Agent tut nichts, „Profil nicht kalibriert“, Modellfehler | `orchestrator.log` |
| VM-Aufbau scheitert oder VM gesperrt | Kopfzeile, dann `hyperv.log` / `qemu-vm.log`, `build.log`; unter Linux zusätzlich `console.log`. Gastprotokolle unter Hyper-V: `scripts\sandbox\Get-LocalAiGuestLogs.ps1` |
| Webabruf scheitert | Freigabekarte (Fehlermeldung), `web.log`; Fehlerbilder in [pruefung-M6.md](pruefung-M6.md) |
| Broker startet nicht | Fehlermeldung der Oberfläche; Exitcodes siehe unten |

### Exitcodes des Brokers

| Code | Bedeutung |
|---|---|
| 0 | normal beendet |
| 2 | Konfiguration ungültig (`config/localai.json`, Kommandozeile) |
| 3 | Audit-Journal beschädigt oder manipuliert |
| 4 | Sandbox oder Projekt konnte nicht eingerichtet werden |
| 5 | `--verify-journal`: Befund im Journal |
| 6 | `--restore`: Wiederherstellung abgebrochen, nichts geändert |

## Audit-Journal prüfen

In der Oberfläche: Bereich **SYSTEM** → Audit prüfen. Ohne Oberfläche (Broker beendet):

```powershell
dotnet run --project src/dotnet/LocalAi.Broker.Host -- --verify-journal
```

Geprüft werden die Hashkette und die Freigabeinvarianten (jede Ausführung hat genau eine passende Entscheidung,
Sitzungsfreigaben nur im Rahmen ihrer Debug-Sitzung). Ist das Journal beschädigt, startet der Broker nicht: Journal
sichern, untersuchen, dann umbenennen, um mit einem neuen Journal zu beginnen.

## Sicherung und Wiederherstellung

**Sichern:** Bereich **SYSTEM** → „Sicherung erstellen“. Die ZIP-Datei landet in `sicherungen/` und enthält
Audit-Journal, Toolchain-Registrierungen, Kalibrierung, Runjournal und Konfiguration – **nie** Freigaben.

**Wiederherstellen** (Oberfläche und Broker beendet):

```powershell
dotnet run --project src/dotnet/LocalAi.Broker.Host -- --restore <pfad-zur-sicherung.zip>
```

Die Wiederherstellung prüft die Sicherung (inkl. Journal) und ändert bei einem Fehler nichts.

## Diagnosepaket

Bereich **SYSTEM** → „Diagnosepaket erstellen“. Inhalt: Versionen, Zustände je Werkzeug, Ereignis-IDs, Speicherwerte
und Hashes. **Nicht** enthalten: Code, Webinhalte, Vorschlagsargumente. Protokolle nur, wenn du sie ausdrücklich
auswählst. Geeignet zum Weitergeben bei Fehlermeldungen.

## Abnahmebericht

`scripts/abnahme.ps1` (Windows) führt alle automatischen Tests mit Ergebnisdateien aus, optional die VM-Abnahmen, und
erzeugt einen Bericht über SEC-01–24 und AC01–17:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\abnahme.ps1                 # automatische Tests + Bericht
powershell -ExecutionPolicy Bypass -File scripts\abnahme.ps1 -MitVm          # zusätzlich alle VM-Abnahmen (Stunden)
powershell -ExecutionPolicy Bypass -File scripts\abnahme.ps1 -MitVm -Teile m7,m6
powershell -ExecutionPolicy Bypass -File scripts\abnahme.ps1 -NurBericht     # Bericht aus vorhandenen Ergebnissen
```

Ergebnis: `%LOCALAPPDATA%\LocalAi\abnahme\abnahme-bericht.md` und `.json`. Ein Kriterium ist nur „bestanden“, wenn
jeder Nachweis vorhanden und erfolgreich ist; fehlt etwas, heißt es „unvollständig“. Manuelle Bestätigungen gehören in
`abnahme-manuell.json` (wird beim ersten Lauf als Vorlage angelegt). Die Kriterien stehen in
`tools/abnahme/kriterien.json`. Ohne HALCON-Lizenz vorher `$env:LOCALAI_M5_HDEVELOP = "0"` setzen.

## Aktualisieren

1. Oberfläche schließen, Sicherung erstellen.
2. Neuen Stand holen (`git pull`), dann `scripts\test.ps1` bzw. `scripts/test.sh` – erwartet „Alles grün.“.
3. Starten. Mit `reuseVm` spielt ein **Gast-Update** geänderte Worker- und Gastskripte in die bestehende VM ein, ohne
   Neuaufbau. Geänderte Toolchains oder VM-Einstellungen führen zu einem Neuaufbau.
4. Nach Wechsel von Modell, Ollama-Version oder GPU-Treiber: neu kalibrieren.

## Häufige Störungen

| Meldung / Symptom | Ursache und Abhilfe |
|---|---|
| „Profil nicht kalibriert“ im Chat | Kalibrierung fehlt für diese Hardware oder diesen Modell-Digest → [kalibrieren](konfiguration.md#calibration) |
| „Hash weicht ab“ / „Ordner der registrierten Toolchain nicht gefunden“ | Toolchain-Ordner verändert oder Registrierung von einem anderen Rechner → TOOLCHAINS prüfen und neu registrieren |
| VM-Aufbau „gesperrt: Administratorrechte … abgelehnt“ | UAC bestätigen; Mitglied der Gruppe „Hyper-V-Administratoren“, danach neu anmelden |
| „Edition nicht im Abbild gefunden“ | `imageName` passt nicht zum ISO; die Meldung nennt die vorhandenen Editionen |
| VM wird bei jedem Start neu aufgebaut | `reuseVm` nicht `true`, andere Toolchains als beim letzten Aufbau, oder die letzte Sitzung endete nicht sauber |
| „VM antwortet nicht“ | mit `pauseOnNoAnswer` angehalten → „VM weiter verwenden“ (Auftrag wird nicht wiederholt) oder „VM neu aufbauen“; ggf. `jobTimeoutMinutes` erhöhen |
| Ollama im LAN nicht erreichbar | `allowLan: true` in `models.local.json`, auf dem Ollama-Rechner `OLLAMA_HOST=0.0.0.0:11434` und Firewall für TCP 11434 |
| Webabruf scheitert hinter Firmenproxy | `web.proxy.url` setzen; Umgebungsvariablen gelten für Webabrufe nicht. TLS-aufbrechender Proxy: `web.caFile` |
| Linux: Webabruf scheitert mit Hinweis auf bubblewrap | `scripts/linux/install-host.sh` ausführen |
| Linux: QEMU sehr langsam | kein KVM: Mitglied der Gruppe `kvm`? Neu anmelden; `accel: "Auto"` oder `"Kvm"` |
| `uv sync` / `dotnet restore` hängt | Proxy-Variablen (`HTTPS_PROXY`, `HTTP_PROXY`) für die Einrichtung setzen |
| „Audit-Journal ist beschädigt oder manipuliert“ | siehe [Audit-Journal prüfen](#audit-journal-prüfen) |

Weitere Fehlerbilder je Bereich stehen am Ende der jeweiligen Prüfanleitung und in der
[Inbetriebnahme](inbetriebnahme-neuer-rechner.md#13-typische-stolpersteine).
