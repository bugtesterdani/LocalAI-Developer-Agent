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

## Rechner ohne Einbinden virtueller Festplatten

Auf manchen Firmenrechnern ist per Richtlinie gesperrt, virtuelle Festplatten (VHDX, ISO) **auf dem Host** einzubinden;
das **Anhängen an eine VM** ist aber erlaubt. LocalAi braucht dann zwei Einstellungen in `sandbox.hyperv` bzw.
`sandbox.hypervLinux`:

```json
"hyperv": { "imagePath": "D:\\Agent\\Windows11.iso", "imageName": "Windows 11 Pro", "imageLanguage": "de-DE",
            "productKey": "…", "hostMount": false, "inbound": "Disk" },
"hypervLinux": { "imagePath": "D:\\Agent\\Linux\\debian-13-generic-amd64.raw", "hostMount": false, "inbound": "Disk" }
```

**`hostMount: false` – Aufbau ohne Einbinden**

| Gast | statt bisher | jetzt |
|---|---|---|
| Linux | Debian-Abbild in eingebundene VHDX schreiben, Seed als FAT32-VHDX | der Broker schreibt die Systemfestplatte selbst als VHDX-Datei; der Seed (cloud-init `CIDATA`) wird eine ISO, die als DVD an die VM kommt |
| Windows | ISO einbinden, DISM in eingebundene VHDX | Windows Setup installiert in der VM von der Windows-ISO (DVD 1); Antwortdatei, Gastdateien und Einrichtungsskript liegen auf einer zweiten, vom Broker geschriebenen ISO (DVD 2). Die Abfrage „Press any key to boot from CD“ bestätigt der Aufbau: er öffnet kurz die VM-Konsole (vmconnect), bestätigt deren Verbindungsdialog und sendet Enter – dafür muss ein Desktop angemeldet sein; ohne nutzbare Konsole Rückfall auf die Tastatur der VM (WMI) |

- Nach dem bestandenen Selbsttest hängt der Broker beide DVDs ab und überschreibt und löscht die eigene Seed- bzw.
  Setup-ISO (sie enthalten Sitzungsschlüssel bzw. Kennwort). Die Windows-ISO wird nur ausgeworfen.
- Windows: `imagePath` muss eine **ISO** sein (`install.wim` geht nur mit DISM). Die Sprache des Abbilds steht in
  `imageLanguage`, weil sie sich ohne Einbinden nicht auslesen lässt. Windows Setup prüft TPM, Secure Boot und
  Arbeitsspeicher nicht (LabConfig in der Antwortdatei; die VM bleibt ohne Netzwerkkarte). Ohne `productKey` kann
  Setup bei manchen ISOs nach einem Schlüssel fragen – dann bleibt der Aufbau stehen; der allgemeine
  Installationsschlüssel der Edition (ohne Aktivierung) reicht.
- Der Aufbau dauert für Windows deutlich länger als mit DISM (Installation in der VM), für Linux etwa gleich lang.
- `scripts\sandbox\Get-LocalAiGuestLogs.ps1` bindet die Systemfestplatte ein und funktioniert dort nicht. Die
  Gastprotokolle liegen in der VM (`C:\LocalAi\logs` bzw. `/var/log/localai-provision.log`); mit
  `keepVmOnExit: true` kann man sich an der VM-Konsole anmelden (Kennwort in `gast-kennwort.txt` im VM-Ordner).

**`inbound: Disk` – Jobs ohne Copy-VMFile**

Ist zusätzlich die Gastdienstschnittstelle (Copy-VMFile) gesperrt, schreibt der Broker die Dateien jedes Jobs als
**Eingangs-Laufwerk** – eine eigene VHDX-Datei, ohne sie auf dem Host einzubinden – und hängt sie nur an die VM. Der
Worker im Gast erkennt das Laufwerk an seinem Kopf (`LAIINBX1`), prüft Sequenz und SHA-256 und legt die Dateien in
seinen Eingang. Nach der Antwort hängt der Broker es wieder ab und löscht es.

Der Rückweg ist unverändert das **Transfer-Laufwerk**: es hängt nur während eines Jobs an der VM; danach hängt der
Broker es ab und liest die VHDX erst dann mit seinem eigenen Leser direkt als Datei – auch das braucht kein Einbinden
auf dem Host. Die Sicherheit bleibt gleich: signierte Jobs mit
Sequenz, streng geprüfte Rückgabe (Containerformat, Prüfsumme, Größengrenze), keine Netzwerkkarte in der VM.

Wechsel von `inbound` bei bestehender VM mit `reuseVm`: der Worker im Gast wird per Gast-Update umgestellt.
`hostMount` wirkt nur beim nächsten Aufbau („VM neu aufbauen“).

**Prüfen, was auf dem Rechner geht** (PowerShell als Administrator, beliebige VM):

```powershell
$v = "$env:TEMP\t.vhdx"; New-VHD -Path $v -SizeBytes 64MB -Dynamic | Out-Null
Mount-VHD -Path $v -NoDriveLetter; Dismount-VHD -Path $v                 # gesperrt → hostMount: false
Add-VMHardDiskDrive -VMName <VM> -Path $v; Remove-VMHardDiskDrive -VMName <VM> -ControllerType SCSI -ControllerNumber 0 -ControllerLocation 1
"x" | Set-Content $env:TEMP\t.txt
Copy-VMFile -Name <VM> -SourcePath $env:TEMP\t.txt -DestinationPath C:\t.txt -CreateFullPath -FileSource Host   # gesperrt → inbound: Disk
```

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
