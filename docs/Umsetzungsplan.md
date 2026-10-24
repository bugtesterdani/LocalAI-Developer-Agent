# Local AI Developer Agent – Umsetzungsplan

Basis: `Local-AI-Developer-Agent-Projektspezifikation.md` v1.3 · Plan v4 · Stand: 16.10.2026

**Umsetzungsstand:** M1 und M2 (M2a–M2c) abgenommen. **M3** (Orchestrator, Model Gateway, Kalibrierung, fünf Rollen mit DAG und Limits, Live-Aktivität mit Chat in der Oberfläche) abgenommen am 18.10.2026 (echter Lauf mit `qwen2.5-coder:7b` über Ollama im LAN, SEC-22 bestätigt). **M4** (Patch.Apply, Toolchain-Aktionen in der VM, Repo.Index, Verify-/Reparaturschleife) implementiert, Abnahme-Skript mit echter VM grün (18.10.2026), Ende zu Ende mit Modell läuft (`docs/pruefung-M4.md`). **M5** (C++, PowerShell, Batch, HDevelop) implementiert; Prüfung offen (`docs/pruefung-M5.md`).

## Entscheidungen

| # | Thema | Entscheidung |
|---|---|---|
| D1 | Hardware | Windows 11 Pro mit Hyper-V; kein festes CPU-/GPU-Modell im Code, Erkennung + Kalibrierung |
| D2 | Ollama | bereits installiert inkl. Modelle |
| D3 | Repositories | große Repos ab MVP; Kontextprofile über 8K; Repo-Index + Repo-Map |
| D4 | Implementierung | **C#:** UI (WPF), Approval/Tool Broker, Audit, Sandbox Controller. **Python:** Orchestrator, Agenten, Model Gateway, Tools, Web Fetcher, Content Gate, Worker |
| D5 | Zielsprachen | C#, Python, C++, HDevelop, PowerShell, Batch – erweiterbar über Toolchains |
| D6 | Toolchains | vom Nutzer heruntergeladen und in eigenem Ordner abgelegt, mit `toolchain.json` |
| D7 | Aufbewahrung | 90 / 7 / 30 Tage |
| D8 | Antwortzeit | zweitrangig; UI hängt nie; Live-Ansicht pro Agent |
| D9 | Transfer | Copy-VMFile Host → VM, Transfer-VHDX VM → Host |
| D10 | Sammelfreigaben | erlaubt für Lesen/Suchen/Listen/Patches mit exakter Liste; einzelne Dateien abwählbar |
| D11 | VM-Lebenszyklus | **bei jedem Sitzungsstart Neuinstallation** (DISM + unattend), dann Toolchains; keine Snapshots; VM nie mit NIC; Löschung bei Sitzungsende. **Option `reuseVm` (18.10.2026, auf Wunsch):** eine sauber beendete VM wird gespeichert und beim nächsten Start fortgesetzt, wenn Abbild, Einstellungen, Worker und Toolchains unverändert sind; gesperrte oder abgestürzte Sitzungen werden nie wiederverwendet; Knopf „VM neu aufbauen“ |
| D12 | Lizenzen | Windows-Gast (Voll- oder Evaluierungsversion), HALCON, Visual Studio Professional: alles vorhanden bzw. Sache des Nutzers |
| D13 | Sprache | Oberfläche Deutsch, Prompts Englisch |

**Offene Punkte: keine.**

## Umsetzungsmodus

Die Umsetzung erfolgt durch Claude, Meilenstein für Meilenstein. Nach jedem Meilenstein prüft Dani auf dem Windows-PC und gibt Rückmeldung, bevor der nächste beginnt.

| Bereich | Wo gebaut und getestet |
|---|---|
| Verträge, Broker, Persistenz (C#-Bibliotheken) | Claude: Build + Unit-/Integrationstests |
| Python-Teile (Orchestrator, Gateway, Index, Fetcher, Gate, Worker-Logik) | Claude: Build + Tests (Ollama durch Fake ersetzt) |
| WPF-Oberfläche | Claude: kompiliert; **Ausführung nur auf Windows** → Dani |
| Hyper-V, DISM, Copy-VMFile, Transfer-VHDX | Claude: Code + PowerShell-Skripte; **Ausführung nur auf Windows mit Admin** → Dani |
| Echte Modelle, Kalibrierung, Toolchain-Installation | **nur auf Danis PC** → Dani, mit bereitgestellten Prüfskripten |

Jeder Meilenstein liefert: Code, Tests, eine kurze Prüfanleitung (`docs/pruefung-Mx.md`) mit den Befehlen, die auf Windows auszuführen sind, und die erwarteten Ergebnisse.

## Arbeitspakete und Meilensteine

Aufwand in Personentagen als Größenordnung; Referenztermine für eine Person in Vollzeit ab 19.10.2026 mit Weihnachtspause.

| Meilenstein | Pakete | Aufwand | Referenz |
|---|---|---:|---|
| **M1 Freigabe-Kern** | B01 Verträge (JSON Schema → C# + Pydantic, Hash-Kreuztests), B02 Approval Broker + Audit, B03 deutsche Freigabe-UI inkl. **Sammelfreigabe mit Abwahl** | 15 | bis 09.11.2026 |
| **M2 Sandbox** | B04 **VM-Neuaufbau** (DISM, unattend, ohne NIC) + Copy-VMFile + Transfer-VHDX, B16 Toolchain-System (Registrierung, Installation beim Aufbau; C# + Python), B05 Rechte, File.Read/ReadMany/List/Search | 21 | bis 08.12.2026 |
| **M3 Agenten** | B06 Python-Orchestrator + Model Gateway + Kalibrierung, B07 fünf Rollen + DAG (Prompts Englisch), B17 Live-Aktivität | 13 | bis 08.01.2027 |
| **M4 Build/Test & große Repos** | B08 Toolchain.Restore/Build/Test/Run, Patch.Apply mit Abwahl pro Datei, B12 Repo.Index + Repo-Map | 13 | bis 27.01.2027 |
| **M5 Alle Sprachen** | B19 C++, PowerShell, Batch; B20 HDevelop | 9 | bis 09.02.2027 |
| **M6 Web & Übernahme** | B09 Web Fetcher + Content Gate, B10 Übernahme, Abbruch, Wiederanlauf | 11 | bis 24.02.2027 |
| **M7 Abnahme (MVP)** | B11 SEC-01–24, AC01–17, Demo, Doku | 9 | bis 09.03.2027 |
| Puffer | | 7 | bis 18.03.2027 |
| **M8 Linux-Gast** (vorgezogen) | B18 Linux-Gast (Debian 13), mehrere VMs mit Start bei Bedarf, Gastauswahl je Projekt/Toolchain | – | vor Doku/Paketierung |
| **M9 OS-Debugging** | QEMU in der Linux-VM: eigenes Betriebssystem booten, debuggen, bedienen; Debug-Sitzung mit einer Freigabe | – | vor Doku/Paketierung |
| **M10 Linux-Host** | Broker, Orchestrator und Oberfläche unter Linux (eigenes Avalonia-Projekt, WPF bleibt ohne Avalonia-Pakete), QEMU statt Hyper-V | – | umgesetzt 20.10.2026 |
| Erweiterungen | B13 Modellvergleich/Embeddings, B14 Git/Paketbeschaffung | – | danach |

Kritischer Pfad: B01 → B02 → B04 → B16 → B05 → B06 → B07 → B08 → B12 → B19 → B10 → B11.

---

## M1 – Freigabe-Kern

- **B01:** `contracts/` mit JSON Schemas (ToolProposal inkl. Zielliste, UserDecision, Narrowing, EvidenceBlock, ToolResult, FolderGrant, ToolchainManifest, ActivityEvent); Generatoren für C#-Records und Pydantic; kanonisches JSON + SHA-256 mit gemeinsamen Testvektoren; Bedrohungsmodell; Anforderungs-→-Test-Matrix.
- **B02 (C#):** Zustandsautomat, einmalige Capability, transaktionaler Consume (SQLite), Outbox, `OutcomeUnknown`, hashverkettetes Audit, Named-Pipe-Server mit getrennten Identitäten (UI / Orchestrator).
- **B03 (C#, WPF, Deutsch):** Freigabe-Queue, Karte mit exakten Parametern; **Sammelfreigabe-Karte** mit Kontrollkästchen pro Ziel, Filter, Zähler, „Auswahl einmal erlauben“; Abwahl → `narrow` → neuer Vorschlag/Hash; Not-Aus; Texte in `.resx`.
- Fake-Agent in Python reicht `File.Read` und `File.ReadMany` ein; zählender Testadapter.

**Abnahme:** SEC-01, -02, -03, -12, -17, -23 grün; C#/Python-Hash identisch; UI bleibt während 60 s Fake-Arbeit bedienbar.

## M2 – Sandbox

- **B04 VM-Neuaufbau:**
  1. VHDX anlegen, `install.wim` aus Nutzer-ISO per DISM anwenden, `bcdboot`.
  2. Offline ins Abbild: `unattend.xml` (lokales Konto, OOBE ohne Netzwerk, Datenschutz aus, Gastdienste), Worker-Python, Provisionierungsskript, Toolchain-Installer + Lizenzdateien.
  3. Gen-2-VM **ohne NIC** erstellen, Transfer-VHDX anhängen, starten.
  4. Provisionierung installiert Toolchains unbeaufsichtigt, schreibt Bericht auf Transfer-VHDX.
  5. Host liest Bericht (read-only Mount), Selbsttest (Netzwerkprobes negativ), Status „bereit“.
  6. Bei Sitzungsende/Not-Aus/Knopf: VM + Festplatten löschen.
  - Aufbau läuft im Hintergrund mit Fortschritt in der Kopfzeile; Chat und Planung sind vorher nutzbar.
- **Transfer:** Copy-VMFile für signierte Jobs; Transfer-VHDX für Ergebnisse (Hot-Detach, `Mount-VHD -ReadOnly -NoDriveLetter`, Manifestprüfung, Ersatz aus leerer Vorlage).
- **B16 Toolchains:** Manifest-Schema inkl. Installationsschritt, Prüfbefehl, Lizenzdateien; Registrierung in der UI mit Hashprüfung; Projekt wählt benötigte Toolchains; C#- und Python-Manifest als Referenz.
- **B05:** Rechteprüfung, Handle-Validierung, Pfadverbote, Hashbindung; `File.List/Read/ReadMany/Search`.

**Abnahme:** VM baut sich unbeaufsichtigt auf, zu keinem Zeitpunkt mit NIC (SEC-24); SEC-04 bis -07, -18, -19, -21 grün; 50 Transfer-Roundtrips; .NET- und Python-Hello-World in der VM. Aufbauzeit wird gemessen und dokumentiert.

## M3 – Agenten

- **B06:** Orchestrator-Prozess (eingebettetes Python, `uv`), Ollama-Streaming und Abbruch, Hardwareerkennung, Kalibrierung 8K/16K/32K+/Offload, nur stabile Profile freigeben.
- **B07:** fünf Rollen, DAG, Limits; **Prompts Englisch**, Chatantworten an den Nutzer Deutsch; Prompts als versionierte Dateien.
- **B17:** Aktivitätsfeed je Agent (Ausgabe, Thinking, Vorschläge, Zustände), Watchdog, Abbruch pro Agent, gedrosseltes Rendering.

**Abnahme:** fünf Rollen mit echtem Modell; Live-Ansicht flüssig unter 32K/Offload; SEC-16, SEC-22 grün.

**Umgesetzt (Stand 18.10.2026):** `localai.orchestrator` (Gateway mit Streaming/Abbruch/Watchdog, Hardwareerkennung, Kalibrierung mit Bericht, Rollen mit Prompts in `prompts/*.md`, DAG mit Schreibmengen-Locks, fairer GPU-Slot, Limits); Broker: Aktivitätsstrom, Aufträge an den Orchestrator, Überwachung (Absturz/Hänger, Not-Aus); Oberfläche: Chat, Aktivität je Agent, Abbruch pro Agent/Run. Patches bleiben bis M4 Vorschläge, Tests werden als nicht ausgeführt gekennzeichnet; der Debugger arbeitet bis dahin auf Review-Befunden.

**Abgenommen (18.10.2026):** fünf Rollen mit echtem Modell (7B, 8K, 0 % CPU-Offload, ~40 Token/s), SEC-16 und SEC-22 bestätigt. 32K/Offload auf der RTX 2060 (6 GB) nicht stabil kalibrierbar – Profile bleiben gesperrt, bis Hardware sie trägt.

## M4 – Build/Test & große Repos

- **B08:** `Patch.Apply` (Abwahl pro Datei), `Toolchain.Restore/Build/Test/Run/Lint`, Offline-Paketquellen, einheitliches Testergebnisformat.
- **B12:** `Repo.Index` (tree-sitter, Roslyn, FTS5, `.hdev`-XML), Repo-Map, gezieltes Nachladen über `File.ReadMany`.

**Abnahme:** E2E in C# und Python; Repo mit > 2.000 Dateien; SEC-13, SEC-20 grün.

**Umgesetzt (Stand 18.10.2026):** `Patch.Apply` im Worker (toleranter Unified-Diff-Applier, abgeleitete unveränderliche Snapshots, alles oder nichts, Abwahl pro Datei; Broker: Ziele = Dateien des Patches, Rechte `Modify`/`Create`/`Delete` je Änderungsart); `Toolchain.Restore/Build/Test/Run/Lint` nur mit VM-Katalog (Betriebsart hyperv) und Recht `Execute`, Parameter typisiert im Broker und im Gast geprüft (SEC-20), frische Jobkopie, eingeschränktes Token mit niedriger Integrität + Job Object (SEC-21, abschaltbar nur zur Fehlersuche), Trx/JUnit/NUnit → `TestReport`; Offline-NuGet-Quelle über `{packages}` (Skript `scripts/toolchains/Get-NuGetOfflinePackages.ps1`); `Repo.Index` mit Standardbibliothek (Python `ast`, Muster für C#/C++/PowerShell/Batch, `.hdev`-XML) statt tree-sitter/Roslyn, Symbolcache je Inhalt, FTS5, Repo-Map nach Referenzen; Orchestrator: Repo-Map als Startevidenz, Patches nach Freigabe angewendet (Rückmeldung bei Nichtpassen), Prüfläufe aus Plan oder automatisch, Debugger auf echten Testfehlern, Reviewer mit echtem Bericht; Oberfläche: Diff je Datei und Toolchain-Aufruf in der Freigabekarte. Abnahme-Skript `tests/integration/test_m4_abnahme.py` (C#/Python E2E, SEC-13/-20/-21, 2.213 Dateien).

## M5 – Alle Sprachen

- **B19:** C++ (MSVC Build Tools aus VS Professional-Offline-Layout oder Clang; CMake, Ninja, CTest, vcpkg offline), PowerShell (5.1/7, Pester, PSScriptAnalyzer), Batch (Harness).
- **B20:** HDevelop über HDevEngine, eigener Test-Runner (JUnit-XML), XML-Validierung + Lade-Test nach jedem Patch; HALCON-Lizenzdatei über das Manifest.

**Abnahme:** je ein Beispiel pro Sprache gebaut und getestet; Installationszeiten im VM-Aufbau dokumentiert.

**Umgesetzt (Stand 18.10.2026):** Toolchain-Vorlagen `cpp` (Visual Studio Build Tools 2022 aus Offline-Layout, CMake mit VS-Generator, CTest → JUnit), `cpp-clang` (llvm-mingw, Ninja Multi-Config), `powershell` (PowerShell 7) und `powershell5` (Windows PowerShell 5.1) mit Pester 5 → JUnit und PSScriptAnalyzer, `batch` (Harness: `*.test.cmd`, Exitcode, `*.expected.txt` → JUnit; statische Prüfung), `hdevelop` (HALCON-ZIP, Lizenz über `deploy`, VC++-Laufzeit, HDevEngine-Runner in Windows PowerShell 5.1: Lade-Test aller `.hdev/.hdvp/.hdpl`, Tests = lokale Prozeduren `test_*`). Manifest erweitert um `then` (mehrstufige Aktionen), `deploy` und `{system}` (nur `cmd.exe` und Windows PowerShell); Pfadparameter ohne cmd-Sonderzeichen. Worker: HDevelop-XML-Prüfung nach jedem Patch (alles oder nichts), CRLF für neue Batch-Dateien. Orchestrator: Prüfläufe und Sprachhinweise für die neuen Sprachen. VM-Aufbau: Zeitgrenze aus den erwarteten Installationszeiten, Installationsdauer je Toolchain in der Statuszeile. Hilfsskript `scripts/toolchains/New-M5Toolchains.ps1`, Beispiele `samples/m5-*`, Abnahme-Skript `tests/integration/test_m5_abnahme.py`.

## M6 – Web & Übernahme

- **B09:** Web Fetcher + Content Gate als getrennte Python-Prozesse, SSRF-Schutz, zwei Freigaben.
- **B10:** `Workspace.ExportPatch`, Not-Aus inkl. Python-Prozesse und VM, Resume ohne alte Freigaben.

**Abnahme:** SEC-08 bis -11, -14, -15 grün.

**Umgesetzt (Stand 19.10.2026):** `Web.Fetch` (1. Freigabe: genau eine HTTPS-URL; URL-Prüfung im Broker und im Fetcher, jede DNS-Antwort öffentlich, Verbindung an die geprüfte Adresse gebunden, keine Weiterleitungen/Cookies/Proxys, Größen-, Zeit- und Typgrenzen) und `Web.Content` (2. Freigabe: exakt der bereinigte Text, an SHA-256 und Empfänger-Agent gebunden; Text in der Karte, Hinweise des Gates). Launcher `python -m localai.web` startet Fetcher und Content Gate als getrennte Prozesse mit niedriger Integrität (Job Object), Quarantäne als *Low* gekennzeichnet, Aufbewahrung 7 Tage. Content Gate: sichtbarer Text ohne Skripte/versteckte Elemente/aktive Links, ANSI/Steuerzeichen entfernt, Bidi markiert, Injection-Hinweise. Übernahme: `Workspace.Diff` (Worker liefert geänderte Dateien, Broker legt sie im Staging ab und berechnet den Diff selbst gegen den aktuellen Projektordner) → `Workspace.ExportPatch` (Diff je Datei, abwählbar; Patch und Ziele müssen exakt der Vorbereitung entsprechen; Rechte je Änderungsart; Hash-Prüfung beim Schreiben, Sicherung, atomares Schreiben, Rückabwicklung). Orchestrator: Aktion `fetch` für alle Rollen, Übernahme nach der Zusammenfassung, Chat-Befehle `übernehmen`/`fortsetzen`/`neu`, Runjournal (ohne Freigaben) und Fortsetzen mit neuem Snapshot und erneuten Patch-Freigaben. Not-Aus beendet Webprozesse, schaltet die VM während einer Ausführung aus (gesperrt, Neuaufbau) bzw. startet den dev-local-Worker neu. Oberfläche zeigt untrusted Text inert (SEC-14). Prüfanleitung `docs/pruefung-M6.md`, Abnahme-Skript `tests/integration/test_m6_abnahme.py`.

## M7 – Abnahme (MVP)

Alle SEC-01–24 und AC01–17; Demo (§16) in allen Sprachen; Runbook durchgespielt; Doku auf Deutsch.

## M8 – Linux-Gast (vorgezogen)

- **B18:** Debian 13 (Cloud-Abbild, generic, amd64) als zweite Offline-VM ohne Netzwerkkarte; Aufbau aus dem Rohabbild,
  Einrichtung über cloud-init (Seed-Laufwerk CIDATA), Rückweg wie unter Windows über Transfer-VHDX und KVP.
- Mehrere VMs je Sitzung (Windows und Linux), jede startet erst bei Bedarf und fährt nach einer einstellbaren
  Leerlaufzeit herunter (Standard 30 min).
- Gastauswahl: `toolchainGuests` (je Toolchain) → `guestOs` (Projekt) → `sandbox.defaultGuestOs` (Konfiguration);
  Toolchains haben je System eigene Namen (`dotnet` / `dotnet-linux`), ihr Manifest legt `guest` fest.

**Umgesetzt (Stand 20.10.2026):** Verträge (`GuestOs`, `ProjectConfig.guestOs/toolchainGuests`, `ToolchainManifest.guest`),
Gastauswahl mit Fehlern bei ausdrücklichem Widerspruch, Linux-Regeln im Toolchain-Validator (`/opt/toolchains/<name>`,
`{system}/bash|sh|python3`, tar.gz/tar.xz, `.deb`). `HyperVSandbox`: Zustand „ausgeschaltet, startet beim nächsten Job“,
Leerlauf-Abschaltung (`idleMinutes`, `stopAction` ShutDown/Save), Start bei Bedarf vor dem Erzeugen des Jobs,
Copy-VMFile-Wiederholung direkt nach dem Start, ein Aufbau zur Zeit. `SandboxPool` (Sammelzustand, Neuaufbau,
Not-Aus für alle), `MultiGuestToolAdapter` (Toolchain-Aktionen in der VM ihres Systems, Snapshot-Abgleich durch
Wiederholung von Basisarchiv und bereits freigegebenen Patch.Apply-Vorschlägen mit Prüfung der Snapshot-IDs).
Linux-Aufbau `New-LocalAiLinuxSandbox.ps1` (Rohabbild → dynamische VHDX ohne Nullblöcke, FAT32-Seed, Secure Boot mit
Microsoft-UEFI-CA), Bausteine `Get-LocalAiLinuxPrerequisites.ps1` (Debian-Abbild, python-build-standalone, Wheels,
hyperv-daemons; `prerequisites.json` mit Hashes, Prüfung beim Aufbau). Gast: `provision.sh` + `localai.guest.provision`
(Netzprüfungen, Toolchains, systemd-Dienste für Worker/KVP/Dateikopie, Seed unbrauchbar machen, cloud-init aus, Bericht),
Worker mit KVP-Pool-Datei, Transfer-Laufwerk über `/sys/block`, Werkzeugprozesse als `localai-tools` mit eigenem
Netzwerk-Namespace, `no_new_privs`, Grenzen und Aufräumen. Vorlagen `toolchains/python-linux`, `toolchains/dotnet-linux`,
Projekte `config/m8-linux-project.json`, `config/m8-gemischt-project.json`. Prüfanleitung `docs/pruefung-M8.md`.

## M9 – Betriebssystem-Debugging (QEMU)

**Umgesetzt (Stand 20.10.2026):** Projektdatei `osDebug` (Abbild, Medium, Maschine, Firmware, Beschleunigung, optional
Build per Toolchain, Grenzen), Werkzeuge `OsDebug.Start` (eigene Freigabe) sowie `OsDebug.Command`/`OsDebug.Stop`
im Rahmen der freigegebenen Sitzung (Broker: gleicher Run und Agent, Befehlsbudget, Dauer, Eingaben nur mit
allowInput; Journal `osdebug.granted/closed`, Entscheidung `SessionGrant`; Audit-Prüfung kennt beides; Not-Aus und
Abbruch schließen die Sitzung, Not-Aus schaltet die VM mit QEMU aus). Worker: QEMU mit festen Argumenten (`-nic none`,
seccomp, Unix-Sockets, Dreifachfehler hält an), QMP- und GDB-Client, Befehle für Haltepunkte, Einzelschritt, Register,
Speicher, Disassemblierung (Capstone oder objdump), VGA-Text, Screenshot, Tastatur, Maus, serielle Ausgabe, Ereignisse;
QEMU als `localai-emu` ohne Netz mit `no_new_privs`. Orchestrator: Aktion `debug`, Debugger-Aufgaben des Planers,
Sitzung endet mit der Aufgabe. Linux-VM: QEMU-Pakete samt Abhängigkeiten (`tools/prereq/debian_closure.py`),
`nestedVirtualization`. Oberfläche: Freigabekarte der Debug-Sitzung. Prüfanleitung `docs/pruefung-M9.md`.

## M10 – Linux-Host (QEMU)

**Umgesetzt (Stand 20.10.2026):** Betriebsart `qemu` mit Abschnitt `sandbox.qemu` (Vorlage `config/localai.linux.json`,
Projekt `config/m10-linux-project.json`). `QemuVmHost` ersetzt die PowerShell-/Hyper-V-Seite hinter derselben
VM-Steuerung (Aufbau, Selbsttest, Start bei Bedarf, Leerlauf, Wiederverwendung, Not-Aus): Aufbau ohne root
(`qemu-img`, `mkfs.fat`/`mcopy`), QEMU mit fester Befehlszeile (`-nodefaults`, `-nic none`, seccomp, Unix-Sockets),
Netzwerkgeräte per QMP gezählt, Verbindungskanal über virtio-serial (Gastdienst `localai.guest.qemulink` statt KVP und
Copy-VMFile), Transfer-Laufwerk im Rohformat mit Tausch per `blockdev-reopen`, Konsolenprotokoll. Gast:
Provisionierung wählt Link-Dienst statt hyperv-daemons, Transfer-Laufwerk auch als virtio (`/dev/vdX`, Seitencache
verworfen). Linux-Host: Datenordner 0700, Schlüssel 0600, Webabruf in bubblewrap, Diagnose prüft QEMU, Werkzeuge und
KVM. Oberfläche: eigenes Projekt `LocalAi.Desktop.Linux` (Avalonia 12) nur mit `LocalAi.Desktop.Core`, Lösung
`LocalAi.Linux.slnx`, Kopftests ohne Bildschirm; asynchrone Rückfragen im Core. Skripte `scripts/linux/*`,
`tools/prereq/linux_prerequisites.py`. Echter QEMU-Test mit Test-Gast (`tests/qemu`, `test_m10_qemu.py`).
Prüfanleitung `docs/pruefung-M10.md`.

## Erweiterungen nach M10

**Hyper-V ohne Einbinden auf dem Host (Stand 22.10.2026):** Für Rechner, die das Einbinden von VHDX/ISO auf dem Host
und teils Copy-VMFile sperren: `hostMount: false` (Broker schreibt Systemfestplatte bzw. Setup-/Seed-Medien selbst als
VHDX/ISO, Windows Setup in der VM) und `inbound: "Disk"` (Jobs als eigenes Eingangs-Laufwerk). Das Transfer-Laufwerk
hängt nur während eines Jobs an der VM und wird vor dem Lesen abgehängt. Die Datenträgersignatur, die Windows auf leere
Laufwerke schreibt, gilt als leer. Einzelheiten: `docs/betrieb.md` (Rechner ohne Einbinden virtueller Festplatten).

**Prüfprogramme der Testumgebung (Stand 22.10.2026):** Toolchain `testumgebung` mit der Kommandozeile
`testumgebung-cli` aus dem Testumgebung-Repository. Die Agenten schreiben Prüfprogramme (`*.tproj.json`) und
Szenarien (`*.tsim.json`) und simulieren sie in der VM. Der Planer schlägt `testumgebung test` als Prüflauf vor, Coder
und Tester bekommen eine Kurzfassung von Format und Bausteinkatalog. Anleitung: `docs/testumgebung.md`. Offen: Prüfung
mit dem veröffentlichten ZIP in der echten Windows-VM.

**Verlauf über die Modellschritte (Stand 23.10.2026):** Bisher sah ein Agent in jedem Schritt nur die Rückmeldungen
des Orchestrators, nicht, was er selbst zuvor überlegt hatte. Jetzt stehen zusätzlich seine eigenen Notizen
(Begründung und Aktion) im Prompt. Die neuesten bleiben wörtlich, ältere werden per Modell zusammengefasst;
Rückmeldungen des Orchestrators bleiben immer vollständig. Einstellungen: `history` in `models.json`
(`docs/konfiguration.md`). Offen: Wirkung auf Schrittzahl und Qualität mit echten Modellen messen.

**Funktionsaufrufe von gpt-oss (Stand 24.10.2026):** gpt-oss schreibt die Aktion oft nicht als Text, sondern ruft eine
„Funktion“ auf (Ollama liefert sie in `message.tool_calls`, der Antworttext bleibt leer). Solche Aufrufe werden jetzt
als Aktion übernommen (`read_file` → `read`, `list_dir` → `list`, …). Eine Antwort nur aus der Denkphase bekommt eine
eigene Rückmeldung statt „No JSON object found“. Unplausible Kalibrierwerte für Zeichen je Token (außerhalb 1,5–6) werden
nicht übernommen, sonst würde der Prompt um ein Vielfaches unterschätzt.

## Risiken

| Risiko | Gegenmaßnahme |
|---|---|
| VM-Aufbau dauert lange (C++-Toolchain, HALCON) | Hintergrundaufbau, nur projektrelevante Toolchains; Dauer in M2/M5 messen |
| Unbeaufsichtigte Installer verhalten sich unerwartet ohne Netzwerk | Prüfbefehl je Toolchain; Bericht in der UI; Offline-Layouts verwenden |
| Hot-Detach der Transfer-VHDX instabil | VM vor Wechsel pausieren |
| Hash C# ≠ Python | gemeinsame Testvektoren |
| HALCON-Lizenz in der VM ohne Netzwerkkarte/USB (MAC- oder Dongle-Bindung greift nicht) | Lizenz ohne Bindung (z. B. Evaluierung) über `deploy`; ohne Lizenz bleibt die XML-Prüfung der `.hdev`-Patches, Toolchain `hdevelop` entfällt |
| Windows-/WPF-/Hyper-V-Teile nur auf Danis PC testbar | Prüfanleitung + Skripte je Meilenstein, kurze Feedbackschleifen |
| Linux-Gast: hyperv-daemons (KVP, Dateikopie über uio ab Kernel 6.10) verhalten sich je Debian-Stand anders | eigene systemd-Einheiten, Prüfung im Bericht („Dienst für Copy-VMFile läuft nicht“), Protokoll `/var/log/localai-provision.log` |
| Linux-Host: Debian-Abbild, cloud-init und KVM nur auf echter Hardware prüfbar; Avalonia-Darstellung je Desktop verschieden | Test-Gast mit echtem QEMU für alle Host-Wege, `console.log` mit Provisionierungsprotokoll, Prüfanleitung M10 |
