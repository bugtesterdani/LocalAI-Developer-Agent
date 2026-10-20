# Prüfanleitung M9 – eigenes Betriebssystem booten und debuggen (QEMU)

Ziel: Ein Agent bootet dein selbst geschriebenes Betriebssystem in **QEMU innerhalb der Linux-Hyper-V-VM**, debuggt den
Bootvorgang (Haltepunkte ab dem ersten Befehl, Einzelschritte, Register, Speicher, Disassemblierung) und bedient das
System danach (Bildschirm, Tastatur, Maus, serielle Ausgabe). Dateien zum Betriebssystem selbst werden nicht kopiert –
QEMU startet das Abbild aus dem Snapshot deines Projekts.

## Was ist neu

| Bereich | Neu |
|---|---|
| Projektdatei `osDebug` | `grantId` (Ordner mit Recht `Execute`), `image` (Pfad im Projekt, z. B. `build/os.iso`), `media` (`Cdrom`, `Disk`, `Floppy`, `Kernel` = Multiboot), `machine` (`pc`, `q35`), `memoryMB` (16–4096), `cpus` (1–4), `firmware` (`Bios`, `Uefi`), `accel` (`Tcg` = reine Emulation, `Kvm`), optional `build` (`toolchain`, `action` `build`/`run`, `parameters`) zum Bauen des Abbilds vor dem Start, `maxCommands` (Standard 500), `maxMinutes` (Standard 60). Ein Projekt mit `osDebug` bekommt automatisch die Linux-VM dazu |
| Freigabe | **eine** Freigabe je Debug-Sitzung (`OsDebug.Start`). Die Karte zeigt Abbild, Maschine, Build und die Grenzen (Befehle, Minuten, Tastatur/Maus). Danach laufen Befehle (`OsDebug.Command`) desselben Runs und Agenten ohne Einzelklick – jeder erscheint als eigener Vorschlag in der Aktivität und im Audit-Journal mit Bezug auf die Sitzung. Andere Agenten/Runs, mehr Befehle, abgelaufene Dauer oder Eingaben ohne Erlaubnis lehnt der Broker ab. `OsDebug.Stop`, Not-Aus und Abbruch beenden die Sitzung |
| Befehle | `continue` (mit Wartezeit), `pause`, `step`, `break` (sw/hw, setzen/entfernen), `registers`, `info` (mem, tlb, pic, lapic, irq, mtree, cpus), `memory` (virtuell/physisch, bis 4 KiB), `disassemble` (16/32/64 Bit bzw. automatisch), `screen-text` (VGA-Textmodus aus 0xB8000), `screenshot` (PNG), `keys` (Text oder Tasten wie `ctrl-alt-delete`), `mouse` (relativ, Tasten), `serial` (COM1), `events` (CPU-Resets, Dreifachfehler, Gastfehler), `reset`. Jede Antwort enthält den Zustand und neue serielle Ausgabe |
| Dreifachfehler | QEMU startet nicht neu, sondern **hält an** (`-action reboot=shutdown,shutdown=pause`); Zustand „angehalten nach Reset/Dreifachfehler“, Register und `events` (mit „Triple fault“) bleiben prüfbar |
| Sicherheit | QEMU in der Linux-VM ohne Netzwerkkarte, zusätzlich `-nic none`, keine Anzeige, **seccomp** (`-sandbox on`: keine neuen Prozesse, keine Rechteerweiterung), eigener Benutzer `localai-emu` ohne Zusatzgruppen, `no_new_privs`, eigener leerer Netzwerk-Namespace, Steuerung nur über Unix-Sockets. Es gibt keine freien Monitor-Befehle – nur feste Vorlagen mit geprüften Zahlen. Wächter beendet QEMU nach der freigegebenen Dauer |
| Agenten | Aktion `{"action": "debug", "command": …}` für den Debugger; der Planer kann bei OS-Zielen Debugger-Aufgaben anlegen; bei Reparaturen bekommt der Debugger den Abschnitt „OS debugging“ und bis zu 40 Schritte. Die Sitzung endet mit der Aufgabe |
| Linux-VM | `Get-LocalAiLinuxPrerequisites.ps1 -WithQemu` lädt QEMU, OVMF und binutils samt Abhängigkeiten (Debian trixie, SHA-256 gegen `Packages`/`Release`). Optional `hypervLinux.nestedVirtualization: true` → QEMU nutzt KVM in der VM (`accel: "Kvm"`), sonst reine Emulation (kein Hardwarezugriff nötig) |
| Version | 0.9.0 |

**Hier geprüft:** echter QEMU 8.2 mit einem nasm-Bootsektor (Haltepunkt auf dem ersten Befehl nach dem BIOS,
Register, Disassemblierung, Speicher, VGA-Text, Screenshot, Tasten, serielle Ausgabe, Dreifachfehler), QEMU als eigener
Benutzer ohne Netz mit seccomp, Build des Abbilds per Toolchain, echter Broker + Worker (dev-local) mit Sitzungsfreigabe
und Audit-Prüfung, Orchestrator mit Debugger-Aktion. **Nur auf deinem PC prüfbar:** QEMU in der echten Linux-VM
(Pakete, Benutzer, KVM mit verschachtelter Virtualisierung).

## 0. Vorbereitung

1. Linux-Bausteine mit QEMU neu laden (bestehende Bausteine bleiben gültig, die Linux-VM wird danach einmal neu
   aufgebaut):
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\sandbox\Get-LocalAiLinuxPrerequisites.ps1 -Target D:\Agent\Linux -WithQemu -SkipImage
   ```
2. Optional für mehr Tempo in **deiner** `config\localai.json` unter `sandbox.hypervLinux`:
   `"nestedVirtualization": true` und im Projekt `"accel": "Kvm"` (ohne KVM fällt QEMU auf Emulation zurück und
   meldet das).
3. Projektdatei für DaniOS nach Vorlage `config\m9-danios-project.json`: `root` auf deinen DaniOS-Ordner, `image` auf
   das gebaute Abbild. Baust du das Abbild in der VM: eine Linux-Toolchain mit deinem Cross-GCC anlegen (Archiv mit
   Compiler, Assembler, `grub-mkrescue`/`xorriso` o. ä., Aktion `build`) und `"build": { "toolchain": "…", "action":
   "build" }` eintragen. Sonst baust du wie gewohnt auf dem Host; der Snapshot nimmt das fertige Abbild mit.

## 1. Bootvorgang debuggen

1. Im Chat z. B.: „Boote DaniOS und finde heraus, warum der Kernel nach dem Wechsel in den Protected Mode neu startet.“
2. Freigabekarte **Debug-Sitzung** prüfen (Abbild, Maschine, Grenzen, Tastatur/Maus) und erlauben.
3. In der Aktivität erscheinen die Befehle des Debuggers (Haltepunkt 0x7c00, continue, registers, step, …) ohne
   weitere Karten; der Zähler „n/500 Befehle“ steht im Zustand jedes Vorschlags.
4. Bei einem Dreifachfehler: „angehalten nach Reset/Dreifachfehler“ und im Ergebnis von `events` „Triple fault“.
5. Ende der Aufgabe: „Debug-Sitzung … beendet“. Not-Aus während der Sitzung beendet sie ebenfalls (die Linux-VM wird
   dabei ausgeschaltet).

## 2. Bedienen

Mit „Tastatur und Maus: erlaubt“ (der Debugger fordert das an; ablehnen ist möglich): `keys` mit Text oder Tasten,
`mouse` mit Bewegung und Klick (PS/2-Maus von QEMU), danach `screen-text` bzw. `screenshot` und `serial`.

## 3. Automatische Prüfungen

`scripts\test.ps1` – M9: `M9Tests`, `DesktopTests.Debug_Sitzung_*` (C#), `tests\test_osdebug_m9.py`,
`tests\test_prereq_m9.py`, `tests\test_orchestrator.py::test_debugger_bootet_*`, `tests\integration\test_m9_dev_local.py`
(braucht `qemu-system-x86_64` und `nasm`, sonst übersprungen).

## Rückmeldung

Bitte melden: ob QEMU in der VM startet (sonst Meldung im Ergebnis von `OsDebug.Start`), Dauer bis zum ersten
Haltepunkt mit TCG bzw. KVM, und ob die Befehle für DaniOS ausreichen (z. B. fehlende `info`-Themen oder Geräte).
