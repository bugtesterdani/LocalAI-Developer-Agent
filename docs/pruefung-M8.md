# Prüfanleitung M8 – Linux-Gast, mehrere VMs, Start bei Bedarf

Ziel: Neben der Windows-VM gibt es eine **Linux-VM (Debian 13)**. Ein Projekt wählt sein Gastsystem; einzelne Toolchains
können in der jeweils anderen VM laufen. VMs laufen **parallel**, starten aber erst, **wenn ein Job sie braucht**, und
fahren nach einer **Leerlaufzeit (Standard 30 min)** wieder herunter – das spart Startzeit und Schreibzugriffe.

## Was ist neu

| Bereich | Neu |
|---|---|
| Gastauswahl | `toolchainGuests` im Projekt (je Toolchain) → `guestOs` im Projekt → `sandbox.defaultGuestOs` in `config\localai.json` (Standard `Windows`). Die **Heimat-VM** (Projekt bzw. Standard) hält die Snapshots; Lesen, Patches, Repo-Index, Diff und Übernahme laufen dort. Toolchain-Aktionen laufen in der VM ihrer Toolchain |
| Toolchains | je System eigene Namen, z. B. `python` (Windows) und `python-linux`. Das Manifest legt fest, wo sie läuft: `"guest": "Linux"`, `guestPath` `/opt/toolchains/<name>`, Binaries als relativer Pfad mit `/` oder `{system}/bash`, `{system}/sh`, `{system}/python3`; Archive `.zip`, `.tar.gz`, `.tgz`, `.tar.xz`; Installer nur `.deb`. Ein **geerbter** Gast (Projekt/Standard) weicht dem Manifest (Hinweis im Ereignisprotokoll), ein **ausdrücklicher** Widerspruch in `toolchainGuests` verhindert den Start |
| Start bei Bedarf | mit `reuseVm` bleibt eine passende VM nach dem Programmstart **aus** („Offline-VM bereit – ausgeschaltet, startet beim nächsten Job“). Der erste Job startet sie (Heartbeat, Netzwerkkarten-Prüfung; Transfer-Laufwerk wie immer nur während des Jobs), danach wie gewohnt |
| Leerlauf | `idleMinutes` (Standard 30, `0` = nie) je VM: danach wird das Gastsystem **heruntergefahren** (`stopAction` `ShutDown`, Standard) oder sein Zustand gespeichert (`Save`). Beim Programmende mit `reuseVm` ebenso – bisher wurde immer gespeichert (8 GB Arbeitsspeicher auf die Platte) |
| Mehrere VMs | jede VM hat eigenen Namen, Ordner, Schlüssel und Sitzung. Kopfzeile: „Windows: … \| Linux: …“. „VM neu aufbauen“ baut gesperrte/angehaltene VMs neu auf (sonst alle), „VM weiter verwenden“ gilt für angehaltene, der Not-Aus für alle. Aufbauten laufen nacheinander (eine UAC-Abfrage zur Zeit) |
| Snapshot-Abgleich | braucht eine Toolchain-Aktion in der anderen VM einen Stand, den diese noch nicht hat, überträgt der Broker ihn: das schon übertragene Snapshot-Archiv und die **bereits freigegebenen** `Patch.Apply`-Vorschläge der Kette, als signierte Jobs. Die Snapshot-IDs (Inhaltshashes) müssen in beiden VMs gleich sein, sonst wird nichts ausgeführt. Ereignis „Snapshot … in die Linux-VM übertragen“ |
| Linux-Aufbau | `scripts\sandbox\New-LocalAiLinuxSandbox.ps1` (UAC): Debian-Rohabbild → dynamische VHDX, Seed-Laufwerk (FAT32 „CIDATA“) für cloud-init mit allen Gastdateien, Transfer-Laufwerk, Generation 2 **ohne Netzwerkkarte**, Secure Boot mit Microsoft-UEFI-CA |
| Linux-Gast | cloud-init startet `provision.sh`: hyperv-daemons (KVP, Copy-VMFile), Python für Linux nach `/opt/localai`, Worker; dann `localai.guest.provision`: Netzprüfungen (alle müssen scheitern), Toolchains (Hashprüfung im Gast), Dienste `localai-worker`, `localai-kvp`, `localai-fcopy`, Sitzungsschlüssel auf dem Seed überschrieben, cloud-init aus, SSH und Update-Timer aus, Bericht über das Transfer-Laufwerk |
| Isolation im Linux-Gast | Werkzeugprozesse laufen als Benutzer `localai-tools` (keine Zusatzgruppen, `no_new_privs`), in einem **eigenen, leeren Netzwerk-Namespace** (nicht einmal Loopback), mit Grenzen für Prozesse/Dateien/Kerndateien. Nach jedem Schritt werden alle Prozesse des Benutzers beendet und seine Dateien in `/tmp`, `/var/tmp`, `/dev/shm` gelöscht. Worker, Python und Toolchains gehören root und sind nur lesbar; der Schlüssel liegt in `/var/lib/localai` (nur root) |
| Agenten | `toolchains.available` nennt das Gastsystem; der Planer sieht „[runs on Linux]“ |
| Korrektur | Not-Aus schaltet die VM jetzt wirklich aus (`Set-LocalAiVmState.ps1 -Action TurnOff` hatte gespeichert statt auszuschalten) |
| Version | 0.8.0 |

**Nicht in der Sandbox getestet (nur auf deinem PC prüfbar):** der echte Debian-Aufbau (Rohabbild schreiben, cloud-init,
hyperv-daemons, Copy-VMFile nach Linux, KVP aus Linux, SCSI-Tausch des Transfer-Laufwerks im laufenden Linux),
Herunterfahren/Starten echter VMs und die Downloads. Hier geprüft: Gastauswahl, Konfiguration, Leerlauf und Start bei
Bedarf, Snapshot-Abgleich zwischen zwei VMs (simulierter Host mit echtem Worker), Seed-Inhalt, Linux-Worker mit
KVP-Pool-Datei und Transfer-Laufwerk, Werkzeugisolation (echter Benutzerwechsel und Netzwerk-Namespace), Toolchain-
Installation im Gast, Broker-Start mit einer bzw. zwei VMs.

## 0. Vorbereitung

**Deine Dateien bleiben unverändert** (`config\localai.json`, `config\demo-project.json`, `config\models.local.json`).

1. Linux-Bausteine laden (einmal, Host mit Internet, ca. 3,5 GB):
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\sandbox\Get-LocalAiLinuxPrerequisites.ps1 -Target D:\Agent\Linux
   ```
   Ergebnis: `D:\Agent\Linux\debian-13-generic-amd64.raw` (gegen `SHA512SUMS` geprüft) und `D:\Agent\Linux\linux-gast\`
   mit `prerequisites.json`, Python für Linux, Wheels (gleiche pydantic-Version wie `src\python\.venv`) und
   `hyperv-daemons_….deb`. Findet das Skript das Debian-Paket nicht: von packages.debian.org (trixie, amd64) laden und
   mit `-HypervDaemonsDeb <Datei>` angeben.
2. In **deiner** `config\localai.json` unter `sandbox` ergänzen (Vorlage: `config\localai.json` im Repository):
   ```json
   "defaultGuestOs": "Windows",
   "hyperv": { "...": "wie bisher", "reuseVm": true, "idleMinutes": 30, "stopAction": "ShutDown" },
   "hypervLinux": {
     "imagePath": "D:\\Agent\\Linux\\debian-13-generic-amd64.raw",
     "prerequisites": "D:\\Agent\\Linux\\linux-gast",
     "vmName": "LocalAi-Linux",
     "vmDirectory": null,
     "memoryMB": 4096,
     "processors": 4,
     "osDiskGB": 64,
     "transferDiskMB": 64,
     "buildTimeoutMinutes": 30,
     "keepVmOnExit": false,
     "reuseVm": true,
     "idleMinutes": 30,
     "stopAction": "ShutDown"
   }
   ```
   `vmDirectory: null` = `%LOCALAPPDATA%\LocalAi\vm-linux` (die Windows-VM bleibt in `…\vm`). `prerequisites` fehlt =
   Ordner `linux-gast` neben dem Abbild.
3. Linux-Toolchain anlegen, z. B. `D:\Agent\Toolchains\python-linux`: Inhalt von `toolchains\python-linux` kopieren,
   dazu `cpython-3.13.5+20250612-x86_64-unknown-linux-gnu-install_only.tar.gz` (python-build-standalone, GitHub
   „astral-sh/python-build-standalone“, Datei **install_only**, nicht *stripped*) und pytest als Wheels für Linux:
   ```powershell
   uvx --from pip pip download pytest --only-binary=:all: --python-version 3.13 --platform manylinux2014_x86_64 --platform any -d D:\Agent\Toolchains\python-linux\wheels
   ```
   Weicht die Version ab: `version`, `displayName`, `install.file`, `install.check.expectOutput` anpassen. Für .NET
   entsprechend `toolchains\dotnet-linux` mit `dotnet-sdk-9.0.306-linux-x64.tar.gz` und `packages\` wie bei `dotnet`.
4. Oberfläche → TOOLCHAINS → prüfen → `python-linux` registrieren (Spalte zeigt „Linux-VM“).

## 1. Projekt nur unter Linux

1. `"project": "config/m8-linux-project.json"` (Toolchain `python-linux`, `guestOs: Linux`) in deiner Konfiguration
   eintragen oder Broker mit `--project config\m8-linux-project.json` starten, Betriebsart `hyperv`.
2. Erwartet: **eine** UAC-Abfrage, Kopfzeile „VM wird aufgebaut – Schritt 3/7: Systemfestplatte aus dem Debian-Abbild …“,
   danach „Linux richtet sich in der VM ein …“, Fortschritt (Hyper-V-Dienste, Netzwerkprüfungen, Toolchains, Worker),
   schließlich „Offline-VM bereit – Selbsttest bestanden (6 Netzwerkprüfungen ohne Verbindung, Python 3.13.x;
   Toolchains: python-linux 3.13.5 (Hallo-Welt ok) …)“. Dauer: wenige Minuten.
3. Hyper-V-Manager: VM `LocalAi-Linux`, **keine Netzwerkkarte**, zwei SCSI-Laufwerke (Seed, Transfer).
4. Im Chat ein kleines Ziel für das Kontakte-Beispiel, z. B. „Füge einen Test für leere Zeilen hinzu“ → Snapshot,
   Patch, `Toolchain.Test (python-linux)` freigeben → Testbericht wie bei Windows. Der Netzversuch des Testcodes
   (`test_testcode_erreicht_kein_netz`) bleibt grün.

## 2. Gemischt: Windows-Projekt, ein Prüflauf unter Linux

1. `config/m8-gemischt-project.json` (Toolchains `python` und `python-linux`, `python-linux` → Linux).
2. Erwartet: Kopfzeile „Windows: … | Linux: …“; die Linux-VM wird gebaut bzw. bleibt bei `reuseVm` ausgeschaltet.
3. Snapshot und Patch laufen in der Windows-VM. Beim ersten `Toolchain.Test` mit `python-linux` startet die Linux-VM,
   im Protokoll erscheint „Snapshot … in die Linux-VM übertragen (n Schritt(e))“, danach der Test. Ein zweiter Test auf
   demselben Stand überträgt nichts mehr.
4. Gegenprobe: in der Projektdatei `"toolchainGuests": { "python-linux": "Windows" }` → Broker startet nicht
   („läuft laut toolchain.json nur unter Linux …“).

## 3. Start bei Bedarf und Leerlauf

1. Zum Testen `idleMinutes: 2` für die Linux-VM setzen, `reuseVm: true`.
2. Nach einem Job zwei Minuten warten: Kopfzeile „Linux: Offline-VM bereit – nach 2 min ohne Job heruntergefahren,
   startet beim nächsten Job“, Hyper-V-Manager zeigt die VM **Aus**.
3. Neuer Job: „Linux-VM wird gestartet (bei Bedarf)“ → nach wenigen Sekunden „bei Bedarf gestartet“, der Job läuft.
4. Programm schließen und neu starten: beide VMs bleiben **aus** („ausgeschaltet, startet beim nächsten Job“), kein
   Neuaufbau, keine UAC-Abfrage.
5. `stopAction: "Save"`: statt Herunterfahren wird gespeichert (Hyper-V-Manager: **Gespeichert**), Start schneller.
6. Not-Aus bei ausgeschalteter VM: „keine laufende Ausführung“, nichts wird gesperrt.

## 4. Sicherheit im Linux-Gast (optional, mit `keepVmOnExit: true`)

Mit `keepVmOnExit` steht ein root-Kennwort in `…\vm-linux\gast-kennwort.txt` (Anmeldung an der VM-Konsole, nur zur
Fehlersuche – danach wieder `false` und neu aufbauen).

- `ip link` → nur `lo`; `ls /sys/class/net` → `lo`.
- `systemctl status localai-worker localai-kvp localai-fcopy` → aktiv; `cloud-init status` → deaktiviert.
- `ls -l /var/lib/localai` → nur root; `/opt/toolchains`, `/opt/localai` → root, nicht schreibbar für andere.
- Protokolle: `/var/log/localai-provision.log`, `/var/lib/localai/logs/worker.log`, `toolchains.log`.

## 5. Automatische Prüfungen

```powershell
scripts\test.ps1
```
M8-Tests: `M8Tests` (C#), `tests\test_worker_m8.py`, `tests\integration\test_m8_gaeste.py`. Die Isolationstests im
Linux-Gast laufen nur unter Linux als root (in der Entwicklungsumgebung), unter Windows werden sie übersprungen.

## Rückmeldung

Bitte melden: Aufbaudauer der Linux-VM, Text der Kopfzeile nach dem Aufbau, ob Copy-VMFile nach Linux klappt (sonst
Meldung „Dienst für Copy-VMFile läuft nicht“ bzw. „Copy-VMFile fehlgeschlagen“ und `/var/log/localai-provision.log`),
und ob Herunterfahren/Start bei Bedarf wie beschrieben funktionieren.
