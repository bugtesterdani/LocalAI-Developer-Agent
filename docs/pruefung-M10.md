# Prüfanleitung M10 – alles auf einem Linux-Host (QEMU statt Hyper-V)

Ziel: Broker, Orchestrator und Oberfläche laufen direkt unter Linux. Die Offline-VM betreibt **QEMU** auf dem Host
(Linux-Gast, Debian 13) statt Hyper-V. Die Oberfläche ist ein **eigenes Avalonia-Projekt** (`LocalAi.Desktop.Linux`);
die Windows-Oberfläche (WPF) bekommt dadurch keine Avalonia-Pakete.

## Was ist neu

| Bereich | Neu |
|---|---|
| Betriebsart | `sandbox.mode: "qemu"` mit Abschnitt `sandbox.qemu` (Vorlage `config/localai.linux.json`). Felder wie `hypervLinux`, dazu `binary` (Standard `qemu-system-x86_64`) und `accel` (`Auto` = KVM, wenn `/dev/kvm` nutzbar, sonst reine Emulation; `Kvm`; `Tcg`). `stopAction` kennt nur `ShutDown`. Projekte, die Windows verlangen, starten nicht (Meldung beim Start) |
| VM-Aufbau | **ohne root**: Systemfestplatte per `qemu-img` aus dem Debian-Abbild (`.raw` oder `.qcow2`), Seed-Laufwerk (FAT32, CIDATA) per `mkfs.fat`/`mcopy`, Transfer-Laufwerk im Rohformat. Ein Aufbau zur Zeit; Fortschritt wie unter Hyper-V |
| QEMU | feste Befehlszeile: `-nodefaults`, `-nic none`, keine Anzeige, seccomp (`-sandbox on,…`), virtio-Laufwerke, Steuer-Sockets in `$XDG_RUNTIME_DIR/localai-…` (nur für dich, 0700). Netzwerkgeräte werden per QMP nach dem Start, bei jedem Laufwerkstausch und alle 10 s gezählt – eines genügt, und die VM wird beendet bzw. gesperrt. Serielle Konsole → `<VM-Ordner>/console.log` (begrenzt) |
| Verbindung | Statt KVP und Copy-VMFile: **Verbindungskanal** (virtio-serial `org.localai.link`). Gast → Host nur feste Zeilen (Lebenszeichen, Fortschritt, Signal, Bestätigung); Host → Gast nur Dateien in den Eingang des Workers, mit SHA-256 geprüft. Zurück weiterhin nur über das **Transfer-Laufwerk**, das per `blockdev-reopen` gegen ein leeres getauscht und streng gelesen wird |
| Start bei Bedarf / Leerlauf / reuseVm | wie M8: die VM startet beim ersten Job, fährt nach `idleMinutes` herunter (ACPI, sonst nach 2 min hart), mit `reuseVm` bleibt sie zwischen Programmstarts erhalten. Der Seed (mit Sitzungsschlüssel) wird beim ersten Wiederstart gelöscht. QEMU ist ein Kindprozess und endet mit dem Broker |
| Webabruf | unter Linux in **bubblewrap** statt niedriger Integritätsstufe: Dateisystem nur lesbar außer dem Quarantäneordner, eigene Namensräume, Netz nur für den Abruf selbst (`web.lowIntegrity: false` schaltet das ab) |
| Dateirechte | Datenordner des Brokers 0700, gespeicherter Sitzungsschlüssel 0600 (unter Linux ohne DPAPI) |
| Oberfläche | `src/dotnet/LocalAi.Desktop.Linux` (Avalonia 12) mit denselben ViewModels und Texten; Lösung `LocalAi.Linux.slnx`. `LocalAi.slnx` (Windows) enthält sie nicht |
| M9 | OS-Debugging läuft unverändert in der Linux-VM (QEMU in QEMU). Mit `nestedVirtualization: true` und verschachteltem KVM des Hosts (`kvm_intel nested=1` bzw. `kvm_amd nested=1`) nutzt das innere QEMU KVM |
| Skripte | `scripts/linux/install-host.sh` (Pakete, Gruppe kvm), `get-prerequisites.sh` (Debian-Abbild, Python für Linux, Wheels; `--with-qemu` für M9), `build.sh`, `start.sh` |
| Version | 0.10.0 |

**Hier geprüft:** echter Broker mit echtem QEMU 8.2 (ohne KVM) und einem Test-Gast (Ubuntu-Kern + initramfs mit Python,
Worker und Verbindungskanal, `tests/qemu`): Aufbau ohne root, Selbsttest, Snapshot und Lesen in der VM, Laufwerkstausch,
Befehlszeile des laufenden QEMU, Rechte des Datenordners, Programmende ohne Reste, `reuseVm` mit Start bei Bedarf und
gelöschtem Seed (`tests/integration/test_m10_qemu.py`). Webabruf in bubblewrap. Oberfläche ohne Bildschirm
(Avalonia.Headless) und einmal unter Xvfb mit echtem Broker. **Nur bei dir prüfbar:** das echte Debian-Abbild
(cloud-init, systemd-Dienst des Verbindungskanals), KVM, Toolchains in der VM und das Aussehen unter deinem Desktop.

## 0. Einrichtung

1. Repository klonen, dann:
   ```bash
   scripts/linux/install-host.sh                      # QEMU, qemu-img, dosfstools, mtools, bubblewrap, Gruppe kvm
   scripts/linux/get-prerequisites.sh ~/LocalAi/linux # Debian-Abbild, Python für Linux, Wheels (mit --with-qemu für M9)
   cp config/localai.linux.json config/localai.json   # BENUTZER-Pfade anpassen, Projekt eintragen
   ```
   Nach dem Aufnehmen in die Gruppe `kvm` einmal ab- und wieder anmelden.
2. Toolchains für den Linux-Gast wie in M8 (`toolchains/python-linux`, `toolchains/dotnet-linux`) unter
   `toolchains.directory` ablegen und in der Oberfläche registrieren.
3. Bauen und starten:
   ```bash
   scripts/linux/build.sh
   scripts/linux/start.sh
   ```
   Die Kopfzeile zeigt den Aufbau („VM wird mit QEMU aufgebaut – Schritt …“), danach „Offline-VM bereit – Selbsttest
   bestanden …“. Der erste Aufbau dauert mit KVM wenige Minuten (cloud-init, Toolchains), ohne KVM deutlich länger.

## 1. Ablauf wie unter Windows

1. Im Chat ein Ziel für das Projekt `config/m10-linux-project.json` (Kontakte in Python) eingeben.
2. Snapshot, Lesen, Patch, Tests (`Toolchain.Test` mit `python-linux`) jeweils freigeben.
3. Erwartet: Ergebnisse wie in M8; in `qemu-vm.log` (Datenordner) je Job ein Laufwerkstausch, in `console.log` die
   Ausgaben des Gasts.

## 2. Sicherheit prüfen

- `ps -o args= -C qemu-system-x86_64` → `-nic none`, `-nodefaults`, `-sandbox on,…`, keine `-netdev`, keine Anzeige.
- `ls -ld ~/.local/share/LocalAi/broker` → `drwx------`.
- Not-Aus während eines langen Tests → VM aus, Zustand „gesperrt: Not-Aus …“, „VM neu aufbauen“.
- Ohne bubblewrap und mit `lowIntegrity: true` → Webabruf scheitert mit Hinweis auf `install-host.sh`.

## 3. Leerlauf und Wiederverwendung

`idleMinutes: 1` setzen: nach einer Minute ohne Job „Offline-VM bereit – nach 1 min ohne Job heruntergefahren …“,
der nächste Job startet sie wieder. Mit `reuseVm: true` startet das nächste Programm ohne Neuaufbau.

## 4. Automatische Prüfungen

`scripts/test.sh` – M10: `M10Tests`, `DesktopTests.Windows_Oberflaeche_bleibt_ohne_Avalonia`,
`DesktopTests.*Linux*`, `LocalAi.Desktop.Linux.Tests`, `tests/test_linux_host_m10.py`,
`tests/integration/test_m10_qemu.py` (braucht `qemu-system-x86_64`, `qemu-img`, `mkfs.fat`, `mcopy`, `cpio`, `busybox`
und einen Kern: `LOCALAI_TEST_KERNEL=/pfad/vmlinuz` – sonst übersprungen).

## Rückmeldung

Bitte melden: Distribution und Desktop, ob der Aufbau mit dem echten Debian-Abbild durchläuft (sonst `console.log` und
`qemu-vm.log`), Dauer des Aufbaus mit KVM, ob die Oberfläche lesbar ist (Schriften, Skalierung) und ob M9 (QEMU in der
VM) mit verschachteltem KVM läuft.
