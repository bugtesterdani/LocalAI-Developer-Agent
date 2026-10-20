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

Der Broker sucht die Datei vom Programmverzeichnis aufwärts (`config/localai.json`) oder nimmt `--config <pfad>`.
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
| `reuseVm` | `false` | VM beim Programmende behalten und beim nächsten Start fortsetzen, wenn Abbild, Einstellungen, Worker und Toolchains unverändert sind. Abweichung von R17 auf ausdrücklichen Wunsch |
| `keepVmOnExit` | `false` | VM am Sitzungsende nicht löschen – nur zur Fehlersuche |
| `lowIntegrityTools` | `true` | Werkzeugprozesse im Windows-Gast mit eingeschränktem Token und niedriger Integrität (SEC-21). **Nur zur Fehlersuche** abschalten |
| `nestedVirtualization` | `false` | nur Linux: CPU-Virtualisierung an die VM weiterreichen, damit QEMU für OS-Debugging (M9) KVM nutzen kann |
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
| `contextTokens`, `maxOutputTokens` | Kontextgröße und Ausgabegrenze je Anfrage |
| `think` | Thinking anfordern (z. B. `qwen3`) |
| `offload` | CPU-Offload erlaubt (Qualität vor Geschwindigkeit) |
| `temperature` | Standard `0.2` |
| `options` | weitere Ollama-Optionen, z. B. `repeat_penalty` |

### `limits`

| Feld | Standard | Bedeutung |
|---|---|---|
| `maxStepsPerTask` | `20` | Modellschritte je Aufgabe |
| `maxStepsPerRole` | `{}` | Schritte je Rolle, überschreibt `maxStepsPerTask` (z. B. `{"Coder": 25}`) |
| `maxPlanAttempts` | `2` | Versuche für einen gültigen Plan |
| `maxPatchRetries` | `3` | Korrekturen, wenn ein Patch nicht passt |
| `maxDenialsPerTask` | `3` | abgelehnte Vorschläge, danach fragt der Agent nach; `0` = unbegrenzt |
| `maxInvalidAnswersPerTask` | `0` | Antworten im falschen Format; `0` = nur Schrittlimit |
| `maxRepairAttempts` | `3` | Debugger-Durchläufe |
| `maxActiveMinutes` | `30` | aktive Laufzeit ohne neue Nutzerinteraktion (Warten auf Freigaben zählt nicht) |
| `maxSandboxJobs` | `2` | gleichzeitige Sandbox-Jobs |
| `maxGpuRequests` | `1` | gleichzeitige Modellanfragen |
| `maxTasks` | `12` | Aufgaben je Plan |

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
