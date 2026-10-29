# Konfiguration

Referenz aller Konfigurationsdateien mit Standardwerten. Grundsatz: Die Sicherheitskonfiguration ist gegenüber den
Agenten nur lesbar – kein Agent und keine Modellantwort kann sie ändern.

| Datei | Inhalt | gelesen von |
|---|---|---|
| `config/localai.json` | Betriebsart, Projekt, VM, Toolchain-Ordner, Prozesse, Web | Broker |
| Projektdatei (z. B. `config/demo-project.json`) | freigegebene Ordner mit Rechten, Toolchains, Gastsystem, OS-Debugging | Broker |
| `config/models.json` | Ollama-Adresse, Modellprofile, Rollen, Grenzen, Kalibrierung | Orchestrator |
| `config/models.local.json` | persönliche Abweichungen von `models.json` (nicht im Git) | Orchestrator |
| `toolchains/<name>/toolchain.json` | Manifest einer Toolchain | Broker, Gast – siehe [toolchains/README.md](../toolchains/README.md) |

Relative Pfade in `config/localai.json` und Projektdateien gelten relativ zur Repository-Wurzel. In JSON unter Windows
Backslashes verdoppeln (`"D:\\Agent\\Toolchains"`).

Mitgelieferte Vorlagen:

| Datei | Zweck |
|---|---|
| `config/localai.json` | Windows, Betriebsart `dev-local-unsafe`, Beispielwerte für Hyper-V |
| `config/localai.linux.json` | Linux-Host mit QEMU (M10) – als `config/localai.json` kopieren, `BENUTZER` ersetzen |
| `config/demo-project.json` | CSV-Importer, nur `List`/`Read` |
| `config/m4-*-project.json` … `config/m10-linux-project.json` | Beispielprojekte der Meilensteine (verweisen auf `samples/`) |
| `config/models.local.example.json` | Vorlage für `models.local.json` (Ollama im LAN, eigene Grenzen) |

---

## `config/localai.json`

Suchreihenfolge: `--config <pfad>`, dann `LOCALAI_CONFIG` (Datei, muss vorhanden sein), dann `localai.json` im
Benutzerordner (`%LOCALAPPDATA%\LocalAi\config` bzw. `~/.config/localai`, abweichend mit `LOCALAI_CONFIG_DIR`),
zuletzt `config/localai.json` vom Programmverzeichnis aufwärts. Bei einer Datei im Benutzerordner gelten relative Pfade
relativ zum Installationsordner (siehe [Installation](installation.md#eigene-konfiguration)); `models.local.json` wird
zusätzlich auch dort gelesen.

Läuft das Programm aus einem Entwicklungs-Checkout (Ordner `.git` oder `src/python/.venv` in der Wurzel), wird der
Benutzerordner übersprungen: dann gilt `config/localai.json` des Repositorys (und nur dessen `models.local.json`), auch
wenn auf demselben Rechner eine installierte Fassung mit eigener Konfiguration liegt. `LOCALAI_CONFIG` gilt immer.
Ungültige Werte führen zu einem Startfehler mit Meldung (Exitcode 2).

### Oberste Ebene

| Feld | Standard | Bedeutung |
|---|---|---|
| `project` | – | Pfad zur Projektdatei. Ohne Projekt gibt es keine Ordnerfreigaben |
| `toolchains.directory` | – | Ordner mit den Toolchain-Unterordnern (`<name>-<version>`) |
| `sandbox` | – | Betriebsart und VM-Einstellungen, siehe unten |
| `orchestrator` | `uv run --no-sync --project src/python python -m localai.orchestrator` | Startbefehl (`command`, `arguments`, `workingDirectory`) |
| `orchestrator.heartbeatTimeoutSeconds` | `30` (min. 2) | ohne Lebenszeichen gilt der Orchestrator als hängend |
| `web` | eingeschaltet mit Standardwerten | Webabrufe, siehe unten |
| `fakeAgent` | – | Startbefehl des Fake-Agenten (Knopf „Testlauf starten“, nur zum Testen) |

### `sandbox`

| Feld | Standard | Bedeutung |
|---|---|---|
| `mode` | `none` | `test`, `none`, `dev-local-unsafe`, `hyperv`, `qemu` – siehe [Architektur](architektur.md#betriebsarten). Überschreibbar mit `--mode` |
| `defaultGuestOs` | `Windows` | Gastsystem für Projekte ohne eigene Angabe (`Windows`/`Linux`) |
| `devWorker` | – | Startbefehl des Workers für `dev-local-unsafe` |
| `hyperv` | – | Windows-VM unter Hyper-V |
| `hypervLinux` | – | Linux-VM unter Hyper-V (M8) |
| `qemu` | – | Linux-VM mit QEMU auf einem Linux-Host (M10) |

### VM-Abschnitte `sandbox.hyperv`, `sandbox.hypervLinux`, `sandbox.qemu`

Alle drei Abschnitte haben dieselben Felder; einige gelten nur für bestimmte Gäste.

| Feld | Standard Windows / Linux | Bedeutung |
|---|---|---|
| `imagePath` | **Pflicht** | Windows: ISO bzw. `install.wim`/`install.esd`. Linux: Debian-13-Cloud-Abbild (`debian-13-generic-amd64.raw`, unter QEMU auch `.qcow2`) |
| `imageName` / `imageIndex` | – | nur Windows: Edition im Abbild, z. B. `"Windows 11 Pro"`. Passt der Name nicht, nennt die Meldung die vorhandenen |
| `productKey` | – | nur Windows: Produktschlüssel (Evaluierungsversionen brauchen keinen) |
| `prerequisites` | `<Ordner des Abbilds>/linux-gast` | nur Linux: Offline-Bausteine (Python, Wheels, hyperv-daemons bzw. QEMU-Pakete) aus `Get-LocalAiLinuxPrerequisites.ps1` bzw. `scripts/linux/get-prerequisites.sh` |
| `vmName` | `LocalAi-Sandbox` / `LocalAi-Linux` | Name der VM |
| `vmDirectory` | neben dem Datenordner: `vm`, `vm-linux`, `vm-qemu` | Ordner für VM und virtuelle Festplatten – wird bei jedem Aufbau geleert |
| `memoryMB` | `8192` / `4096` | Arbeitsspeicher der VM |
| `processors` | `4` | virtuelle CPUs |
| `osDiskGB` | `128` / `64` | Größe der Systemfestplatte (dynamisch) |
| `transferDiskMB` | `64` | Größe des Transfer-Laufwerks (begrenzt die Ergebnisgröße je Job) |
| `buildTimeoutMinutes` | `90` / `30` | Zeitgrenze für den VM-Aufbau; wird für große Toolchains automatisch erhöht |
| `jobTimeoutMinutes` | `5` (1–600) | Wartezeit auf die Antwort eines normalen Jobs (Lesen, Patch, Index) |
| `snapshotTimeoutMinutes` | `30` (1–600) | Wartezeit auf die Übernahme eines Snapshots |
| `toolchainExtraMinutes` | `5` (1–600) | Zuschlag auf `timeoutSeconds` einer Toolchain-Aktion |
| `pauseOnNoAnswer` | `true` | bleibt eine Antwort aus: VM anhalten statt sperren; die Oberfläche bietet „VM weiter verwenden“ oder „VM neu aufbauen“. Netzwerkkarten und ungültige Rückgaben sperren immer |
| `idleMinutes` | `30` | nach so vielen Minuten ohne Job anhalten, beim nächsten Job neu starten; `0` = nie |
| `stopAction` | `ShutDown` | wie angehalten wird: `ShutDown` oder `Save` (nur Hyper-V) |
| `reuseVm` | `false` | VM beim Programmende behalten und beim nächsten Start fortsetzen, wenn Abbild, Einstellungen und Python-Laufzeit unverändert sind (geänderter Worker-Code wird per Gast-Update übernommen, geänderte Toolchains werden im Windows-Gast nachinstalliert). Abweichung von R17 auf ausdrücklichen Wunsch |
| `keepVmOnExit` | `false` | VM am Sitzungsende nicht löschen – nur zur Fehlersuche |
| `lowIntegrityTools` | `true` | Werkzeugprozesse im Windows-Gast mit eingeschränktem Token und niedriger Integrität (SEC-21). **Nur zur Fehlersuche** abschalten |
| `nestedVirtualization` | `false` | nur Linux: CPU-Virtualisierung an die VM weiterreichen, damit QEMU für OS-Debugging (M9) KVM nutzen kann |
| `hostMount` | `true` | nur Hyper-V: virtuelle Festplatten beim Aufbau auf dem Host einbinden. `false` für Rechner, auf denen das per Richtlinie gesperrt ist – siehe [Rechner ohne Einbinden](betrieb.md#rechner-ohne-einbinden-virtueller-festplatten) |
| `inbound` | `CopyVMFile` | nur Hyper-V: Weg der Jobs in die VM. `Disk` = Eingangs-Laufwerk statt Copy-VMFile, wenn die Gastdienstschnittstelle gesperrt ist |
| `imageLanguage` | `de-DE` | nur Windows mit `hostMount: false`: Sprache des Windows-Abbilds (lässt sich ohne Einbinden nicht auslesen) |
| `binary` | `qemu-system-x86_64` | nur `sandbox.qemu`: QEMU-Programm |
| `accel` | `Auto` | nur `sandbox.qemu`: `Auto` (KVM, wenn `/dev/kvm` nutzbar, sonst TCG), `Kvm`, `Tcg` |
| `directBoot` | – | nur `sandbox.qemu`, **nur für automatische Tests**: `kernel`, `initrd`, `append` direkt starten |

### `web`

Ohne Abschnitt sind Webabrufe mit den Standardwerten eingeschaltet. Einzelheiten und Fehlerbilder:
[Prüfanleitung M6, Abschnitt 0](pruefung-M6.md#0-vorbereitung).

| Feld | Standard | Bedeutung |
|---|---|---|
| `enabled` | `true` | `false` schaltet `Web.Fetch`/`Web.Content` ab (der Broker weist sie ab) |
| `command` | `uv run … -m localai.web` | Startbefehl des Web-Launchers |
| `maxBytes` | `2097152` (1 KiB–16 MiB) | höchstens so viele Bytes Antwort |
| `timeoutSeconds` | `15` (1–120) | Zeitgrenze je Abruf |
| `maxChars` | `20000` (1.000–200.000) | höchstens so viele Zeichen nach dem Content Gate |
| `lowIntegrity` | `true` | Fetcher und Gate mit niedriger Integrität (Windows) bzw. in bubblewrap (Linux). Nur zur Fehlersuche abschalten |
| `allowedHosts` | `[]` = alle öffentlichen | nur diese Hosts sind vorschlagbar; `*.microsoft.com` = alle Unterdomänen |
| `caFile` | – | zusätzliches Stammzertifikat (PEM) für einen TLS-aufbrechenden Proxy; muss beim Start existieren |
| `proxy.url` | – | HTTP-Proxy `http://host:port` (CONNECT). Umgebungsvariablen wie `HTTPS_PROXY` werden für Webabrufe **nie** verwendet |
| `proxy.username` / `proxy.passwordFile` | – | Basic-Anmeldung; das Passwort steht in einer eigenen Datei |
| `proxy.dnsViaProxy` | `false` | nur setzen, wenn der PC externe Namen nicht selbst auflösen kann |
| `proxy.connectByAddress` | `false` | CONNECT auf die lokal geprüfte IP statt auf den Namen |

### Kommandozeile des Brokers

Normalerweise startet die Oberfläche den Broker. Für Tests und Wartung:

| Option | Bedeutung |
|---|---|
| `--config <pfad>` | andere Konfigurationsdatei |
| `--mode <betriebsart>` | Betriebsart überschreiben |
| `--project <pfad>` | Projektdatei überschreiben |
| `--data <ordner>` | anderer Datenordner |
| `--pipe <name>` | fester Pipe-Name |
| `--ttl-seconds <n>` | Gültigkeit offener Vorschläge (Standard 300) |
| `--verify-journal` | Audit-Journal prüfen, Bericht als JSON ausgeben, beenden (Exitcode 0 = ok, 5 = Befund) |
| `--restore <zip>` | Sicherung wiederherstellen und beenden (Broker darf nicht laufen) |
| `--ignore-stdin`, `--test-adapter-delay-ms <n>` | nur für automatische Tests |

---

## Projektdatei

Eine Projektdatei beschreibt, welche Hostordner das System überhaupt sehen darf und mit welchen Rechten. Alles
andere ist unerreichbar (R09).

```json
{
  "schemaVersion": 1,
  "projectId": "m8-gemischt",
  "name": "Windows-Projekt, Python-Tests zusätzlich unter Linux",
  "guestOs": "Windows",
  "grants": [
    {
      "grantId": "kontakte",
      "root": "samples/m7-demo-python",
      "rights": ["List", "Read", "Modify", "Create", "Execute"],
      "recursive": true,
      "exclude": ["daten"]
    }
  ],
  "toolchains": ["python", "python-linux"],
  "toolchainGuests": { "python-linux": "Linux" }
}
```

| Feld | Pflicht | Bedeutung |
|---|---|---|
| `schemaVersion` | ja | `1` |
| `projectId`, `name` | ja | Kennung und Anzeigename |
| `grants` | ja | freigegebene Ordner, siehe unten |
| `toolchains` | nein | Namen der Toolchains, die beim VM-Aufbau installiert werden. Jede muss registriert sein, sonst bleibt die Sandbox gesperrt |
| `guestOs` | nein | Heimat-VM des Projekts (`Windows`/`Linux`); sonst `sandbox.defaultGuestOs` |
| `toolchainGuests` | nein | Gastsystem je Toolchain; darf dem Manifest nur zustimmen, ein Widerspruch verhindert den Start |
| `osDebug` | nein | eigenes Betriebssystem in QEMU booten und debuggen (M9), siehe unten |
| `autoApprove` | nein | Tools, die in einem Run automatisch genehmigt werden – erst nach Bestätigung zu Beginn jedes Runs, siehe unten |

### `grants[]`

| Feld | Bedeutung |
|---|---|
| `grantId` | Kennung, auf die sich Werkzeugaufrufe beziehen |
| `root` | Ordner auf dem Host (absolut oder relativ zur Repository-Wurzel) |
| `rights` | Auswahl aus `List`, `Read`, `Create`, `Modify`, `Delete`, `Execute`. `Delete` und `Execute` nur, wenn ausdrücklich eingetragen |
| `recursive` | `true` = Unterordner eingeschlossen; sonst nur die Dateien direkt in `root` |
| `exclude` | weitere Datei- oder Ordnernamen, die nie in einen Snapshot gelangen |

Immer ausgeschlossen: `.git`, `.vs`, `.idea`, `bin`, `obj`, `node_modules`, `.venv`, `__pycache__`, `.pytest_cache`,
außerdem Verknüpfungen, Junctions und Dateien mit mehreren Hardlinks. Welche Rechte ein Werkzeug braucht, steht in
[Werkzeuge und Rechte](werkzeuge.md#ordnerrechte).

### `osDebug` (M9)

| Feld | Bedeutung |
|---|---|
| `grantId`, `image` | Grant und Pfad des Abbilds im Snapshot (z. B. `build/os.iso`) |
| `media` | `Cdrom`, `Disk`, `Floppy` oder `Kernel` |
| `machine`, `memoryMB`, `cpus` | QEMU-Maschine (z. B. `pc`), Speicher, CPUs |
| `firmware` | `Bios` oder `Uefi` |
| `accel` | `Tcg` (reine Emulation, Standard) oder `Kvm` (braucht `nestedVirtualization`) |
| `build` | optional: Abbild vorher per Toolchain-Aktion bauen (`toolchain`, `action`, `parameters`) |
| `maxCommands`, `maxMinutes` | Grenzen einer Debug-Sitzung |

Beispiel: `config/m9-danios-project.json`. Ablauf: [Prüfanleitung M9](pruefung-M9.md).

### `autoApprove`

Erspart Klicks für Aktionen, die nur in der Sandbox wirken. Standard: nichts automatisch.

```json
"autoApprove": {
  "File.List": true,
  "File.Read": true,
  "File.ReadMany": true,
  "File.Search": true,
  "Repo.Index": true,
  "Toolchain.Test": false
}
```

- **Bestätigung in jedem Run:** Zu Beginn jedes Runs (nach dem Snapshot) sagt der Orchestrator im Chat an, welche
  Aktionen automatisch laufen sollen, und legt die Liste als eigene Freigabe `Run.AutoApprove` vor („Für diesen Run
  bestätigen“). Erst danach genehmigt der Broker Vorschläge dieser Tools in diesem Run selbst. Ablehnen oder Ablaufen
  lassen = jede Aktion wird wie gewohnt einzeln vorgelegt. Ohne eingeschaltete Tools gibt es keine Rückfrage.
- **Wirkt nur im bestätigten Run:** Not-Aus, Abbruch des Runs, Neustart des Brokers und der nächste Run (neue
  Bestätigung) beenden die automatische Freigabe.
- **Volle Prüfung und Protokoll:** Jeder automatisch genehmigte Vorschlag wird wie jeder andere geprüft (Ziele, Rechte,
  Snapshot, Pfade) und ist im Audit-Journal (`decision: AutoApprove`) und in der Aktivität als „automatisch genehmigt“
  zu sehen.

| Möglich | Wirkung |
|---|---|
| `File.List`, `File.Read`, `File.ReadMany`, `File.Search`, `Repo.Index` | lesen nur den Snapshot in der Sandbox |
| `Patch.Apply` | ändert nur die Arbeitskopie in der Sandbox (Übernahme in den Projektordner bleibt eine eigene Freigabe) |
| `Toolchain.Restore`, `Toolchain.Build`, `Toolchain.Test`, `Toolchain.Run`, `Toolchain.Lint` | führen Code aus – nur in der Offline-VM |

**Nie automatisch** (der Broker startet nicht, wenn eines davon auf `true` steht): `Workspace.Snapshot` und
`Workspace.Diff` (lesen den Projektordner auf dem Rechner), `Workspace.ExportPatch` (schreibt dorthin), `Web.Fetch` und
`Web.Content` (Internet), `OsDebug.*` (eigene Sitzungsfreigabe). `false` ist bei allen erlaubt. Ein unbekannter
Toolname verhindert ebenfalls den Start.

---

## `config/models.json` und `config/models.local.json`

`models.json` ist im Repository, `models.local.json` gehört dir und wird nie überschrieben. Darin stehen nur die
Werte, die von `models.json` abweichen.

### `ollama`

| Feld | Standard | Bedeutung |
|---|---|---|
| `url` | `http://127.0.0.1:11434` | Adresse von Ollama. Nur Loopback, außer mit `allowLan` |
| `allowLan` | `false` | Ollama auf einem anderen Rechner im LAN zulassen |
| `stallWarningSeconds` | `45` | ohne neue Tokens: „hängt?“ beim Agenten |
| `stallAbortSeconds` | `900` | ohne neue Tokens: Anfrage abbrechen |

### `profiles[]` und `roles`

Ein Profil ist ein Modell mit Kontextgröße. Der Orchestrator nimmt je Rolle das erste Profil aus `roles`, das für
diese Hardware und diesen Modell-Digest **kalibriert** ist (`requireCalibration: true`).

| Feld | Bedeutung |
|---|---|
| `name` | Profilname (`standard`, `leicht`, `planung`, `erweitert`, `gross`, `offload`) |
| `model` | Ollama-Tag mit fester Version – `latest` ist unzulässig |
| `contextTokens`, `maxOutputTokens` | Kontextgröße und Ausgabegrenze je Anfrage (höchstens ein Viertel des Kontexts). Eine neue Datei entsteht in einer einzigen Antwort – für große Dateien (z. B. Prüfprogramme `*.tproj.json`) beim Coder großzügig wählen, etwa `16384`; „Antwort an der Ausgabegrenze abgeschnitten“ im Verlauf zeigt, dass es nicht reicht |
| `think` | Thinking anfordern (z. B. `qwen3`) |
| `offload` | CPU-Offload erlaubt (Qualität vor Geschwindigkeit) |
| `temperature` | Standard `0.2` |
| `options` | weitere Ollama-Optionen, z. B. `repeat_penalty` |
| `outputUpToContext` | Standard `false`: Ausgabe höchstens `maxOutputTokens`. `true`: Ausgabe darf den ganzen Rest des Kontexts nutzen (Kontext minus Eingabe minus ~6 % Reserve); `maxOutputTokens` ist dann nur der Teil, der beim Zusammenstellen des Prompts mindestens frei bleibt. **Die Denkphase zählt bei Ollama zur Ausgabe** – Denkmodelle mit großem Kontext (z. B. qwen3.6 mit 256K) brauchen das, sonst endet die Antwort mitten im Denken. Achtung: lange Antworten dauern entsprechend (bei ~57 Token/s rund 10 Minuten je 32K Token) |

### `limits`

| Feld | Standard | Bedeutung |
|---|---|---|
| `maxStepsPerTask` | `20` | Modellschritte je Aufgabe |
| `maxStepsPerRole` | `{}` | Schritte je Rolle, überschreibt `maxStepsPerTask` (z. B. `{"Coder": 25}`) |
| `maxPlanAttempts` | `2` | Versuche für einen gültigen Plan |
| `maxPatchRetries` | `3` | Korrekturen, wenn ein Patch nicht passt |
| `maxDenialsPerTask` | `3` | abgelehnte Vorschläge, danach fragt der Agent nach; `0` = unbegrenzt |
| `maxInvalidAnswersPerTask` | `0` | Antworten im falschen Format; `0` = nur Schrittlimit |
| `maxRepairAttempts` | `3` | Korrekturrunden (Testfehler: Diagnose des Debuggers + Korrektur des Coders; Review-Befunde: Korrektur des Coders) |
| `maxActiveMinutes` | `30` | aktive Laufzeit ohne neue Nutzerinteraktion (Warten auf Freigaben zählt nicht) |
| `maxSandboxJobs` | `2` | gleichzeitige Sandbox-Jobs |
| `maxGpuRequests` | `1` | gleichzeitige Modellanfragen |
| `maxTasks` | `12` | Aufgaben je Plan |

### `history`

Ein Agent arbeitet eine Aufgabe in bis zu `maxStepsPerTask` Modellschritten ab. Jeder Schritt ist eine neue Anfrage an
Ollama. Damit das Modell weiß, was es in den Schritten davor gedacht und vorgehabt hat, steht unter
„Previous steps“ ein Verlauf mit zwei Arten von Einträgen:

- **Rückmeldungen des Orchestrators** (Ergebnis eines Tools, Ablehnung, Formatfehler, Rückmeldung zum Patch): immer
  wörtlich und vollständig.
- **Eigene Notizen des Modells**: seine Begründung und die gewählte Aktion. Codeblöcke und SEARCH/REPLACE-Blöcke werden
  durch einen Platzhalter ersetzt. Hat das Modell nur in der Denkphase geschrieben, wird deren Ende genommen.
  Die neuesten Notizen bleiben wörtlich. Brauchen sie mehr als `maxNoteTokens` oder mehr als der freie Platz im
  Kontext, werden die älteren zu einer laufenden Zusammenfassung verdichtet. Bei knappem Platz bleibt nur die letzte
  Notiz wörtlich.

| Feld | Standard | Bedeutung |
|---|---|---|
| `mode` | `summary` | `summary`: ältere Notizen per Modell zusammenfassen. `trim`: nur mechanisch kürzen (kein zusätzlicher Modellaufruf). `off`: nur Rückmeldungen des Orchestrators (früheres Verhalten) |
| `summaryProfiles` | `[]` | Profile zum Zusammenfassen, Auswahl wie bei `roles` (installiert, kalibriert). Leer: das Modell der jeweiligen Rolle |
| `keepRecentNotes` | `3` | so viele neueste Notizen bleiben wörtlich |
| `maxNoteTokens` | `1500` | ab so vielen Token wörtlicher Notizen wird verdichtet |
| `summaryTokens` | `600` | Höchstlänge der Zusammenfassung |

Zur Wahl von `summaryProfiles`:

- **Leer lassen (empfohlen bei einer GPU):** Die Zusammenfassung läuft mit dem Modell und der Kontextgröße der Rolle.
  Das Modell ist schon geladen, es muss nichts gewechselt werden. Kosten: ein kurzer Aufruf alle paar Schritte.
- **Eigenes kleines Modell** (z. B. ein Profil mit `qwen3:4b`, 8K): Lohnt sich nur, wenn beide Modelle gleichzeitig in
  den Speicher passen (`OLLAMA_MAX_LOADED_MODELS` ≥ 2). Mit `1` lädt Ollama bei jeder Zusammenfassung das kleine
  Modell und danach das der Rolle neu. Das dauert meist länger als die Zusammenfassung selbst.
- Ist keins der eingetragenen Profile nutzbar, fasst das Modell der Rolle zusammen. Der Grund steht im Aktivitätsfeed.

Im Aktivitätsfeed steht jede Verdichtung mit dem Text der Zusammenfassung („Eigene Notizen Schritte 1–4
zusammengefasst“). Scheitert der Aufruf, wird mechanisch gekürzt; der Agent arbeitet weiter.

### `calibration`

| Feld | Standard | Bedeutung |
|---|---|---|
| `runs` | `10` | Läufe je Profil; alle müssen ohne OOM gelingen |
| `minVramReserveGiB` | `2.0` | so viel VRAM muss frei bleiben |
| `outputTokens` | `256` | Ausgabelänge je Messlauf |
| `maxCpuOffload` | `0.05` | höchster CPU-Anteil für Profile ohne `offload` |

Kalibrieren:

```powershell
uv run --project src/python python -m localai.orchestrator.calibrate --profile leicht
uv run --project src/python python -m localai.orchestrator.calibrate --profile standard
```

Die Kalibrierung misst auch Zeichen je Token. Werte außerhalb 1–8 (Messung) bzw. 1,5–6 (Betrieb) gelten als unplausibel; dann rechnet der Orchestrator mit 3 und meldet „unplausibel … Profil neu kalibrieren“ im Verlauf des Agenten.

Ollama auf einem anderen Rechner: zusätzlich `--ohne-vram-messung`. Ergebnis: `calibration.json` und ein Bericht
`kalibrierung-*.md` im Datenordner. Details: [Prüfanleitung M3, Abschnitt 3](pruefung-M3.md).

### Empfohlene Ollama-Umgebung

| Variable | Wert |
|---|---|
| `OLLAMA_HOST` | `127.0.0.1:11434` (bzw. `0.0.0.0:11434` für Zugriff aus dem LAN) |
| `OLLAMA_MAX_LOADED_MODELS` | `1` |
| `OLLAMA_NUM_PARALLEL` | `1` |
| `OLLAMA_FLASH_ATTENTION` | `1` |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` |
| `OLLAMA_NO_CLOUD` | `1` |

`OLLAMA_CONTEXT_LENGTH` nicht global setzen – das Model Gateway setzt den Kontext je Anfrage.
