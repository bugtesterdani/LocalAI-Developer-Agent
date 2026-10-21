# Local AI Developer Agent

Lokaler Entwicklungsagent mit fünf Rollen (Planner, Coder, Tester, Debugger, Reviewer) auf Basis von Ollama.
Jeder Tool-Aufruf braucht eine ausdrückliche Freigabe; ausgeführt wird nur in einer Offline-VM – unter Windows mit
Hyper-V, auf einem Linux-Host mit QEMU.

**Dokumentation: [docs/README.md](docs/README.md)** – Einstieg mit Architektur, Bedienung, Konfiguration,
Werkzeugen, Betrieb, Entwicklung und Glossar.

| Thema | Dokument |
|---|---|
| Wie ist das System aufgebaut? | [Architektur](docs/architektur.md) |
| Installieren aus den Paketen (MSI, MSIX, .deb) | [Installation](docs/installation.md) |
| Einrichten unter Windows | [Inbetriebnahme auf einem neuen Rechner](docs/inbetriebnahme-neuer-rechner.md) |
| Einrichten unter Linux | [Prüfanleitung M10](docs/pruefung-M10.md#0-einrichtung) |
| Arbeiten mit der Oberfläche | [Bedienung](docs/bedienung.md) |
| Alle Einstellungen | [Konfiguration](docs/konfiguration.md) |
| Toolchains anlegen | [toolchains/README.md](toolchains/README.md) |
| Spezifikation und Plan | [Spezifikation](docs/Local-AI-Developer-Agent-Projektspezifikation.md), [Umsetzungsplan](docs/Umsetzungsplan.md) |
| Anforderungen → Tests | [docs/sicherheit/anforderungen-tests.md](docs/sicherheit/anforderungen-tests.md) |

## Stand: Meilenstein M10 – Linux-Host (Version 0.10.0)

| Teil | Sprache | Meilenstein | Status |
|---|---|---|---|
| Verträge (JSON Schema → C# + Pydantic), kanonisches JSON | C# / Python | M1 | fertig, Kreuztests grün |
| Approval Broker, Capabilities, Audit-Journal (SQLite, Hashkette), Broker-Prozess mit Named Pipe und Rollen-Tokens | C# | M1 | fertig |
| Freigabe-Oberfläche inkl. Sammelfreigabe mit Abwahl, Not-Aus, Sandbox-Anzeige | C# / WPF | M1 | fertig |
| Projektordner mit Rechten, Snapshot (ohne Verknüpfungen), signierte Jobs, Sandbox-Worker | C# / Python | M2a | fertig |
| Offline-Hyper-V-VM: Neuaufbau (DISM, unattend, ohne NIC), Selbsttest, Copy-VMFile hin, Transfer-VHDX zurück | PowerShell / C# / Python | M2b | fertig |
| Toolchains: Manifest, Registrierung mit Hashes, Installation + Hallo-Welt beim VM-Aufbau | C# / PowerShell | M2c | fertig |
| Orchestrator, Model Gateway (Ollama), Kalibrierung, fünf Agenten, Live-Aktivität und Chat | Python / C# | M3 | abgenommen |
| Patch.Apply, Build/Test in der VM (`Toolchain.*`), Repo.Index/Repo-Map, Reparaturschleife | Python / C# | M4 | Abnahme-Skript mit echter VM grün |
| C++ (MSVC/Clang), PowerShell (Pester, PSScriptAnalyzer), Batch (Harness), HDevelop (HDevEngine) | PowerShell / Python / C# | M5 | auf dem PC zu prüfen |
| Web: `Web.Fetch` → Content Gate → `Web.Content`; Übernahme in den Projektordner; Not-Aus inkl. VM; Fortsetzen | Python / C# | M6 | auf dem PC zu prüfen |
| Abnahme: Versionsmanifest, Audit-Prüfung, Diagnose, Sicherung, Abnahmebericht | C# / Python | M7 | fertig |
| Linux-Gast (Debian 13), mehrere VMs, Start bei Bedarf, Leerlauf-Abschaltung | C# / Python / PowerShell | M8 | auf dem PC zu prüfen |
| Eigenes Betriebssystem in QEMU (Linux-VM) booten und debuggen | Python / C# | M9 | auf dem PC zu prüfen |
| Linux-Host: QEMU statt Hyper-V, Avalonia-Oberfläche | C# / Python | M10 | mit Test-Gast geprüft, echter Debian-Gast offen |

Einzelheiten je Meilenstein: [Umsetzungsplan](docs/Umsetzungsplan.md) und die Prüfanleitungen in
[docs/README.md](docs/README.md#prüfanleitungen-je-meilenstein).

## Schnellstart

Voraussetzungen: .NET 10 SDK, [uv](https://docs.astral.sh/uv/) (installiert bei Bedarf auch Python), Git; für Modelle
Ollama. Unter Linux zusätzlich `scripts/linux/install-host.sh` (QEMU, dosfstools, mtools, bubblewrap).

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1    # Windows: alles bauen und testen
powershell -ExecutionPolicy Bypass -File scripts\start.ps1   # Windows: Oberfläche starten
```

```bash
scripts/test.sh          # Linux: alles außer WPF bauen und testen
scripts/linux/start.sh   # Linux: Oberfläche bauen (falls nötig) und starten
```

Zum ersten Ausprobieren ohne VM steht `config/localai.json` auf `dev-local-unsafe` (Worker ohne VM, in der Oberfläche
rot, kein Build/Test). Für echte Isolation `hyperv` bzw. `qemu` einrichten – siehe [Konfiguration](docs/konfiguration.md).

## Struktur

```text
contracts/      JSON Schema (einzige Quelle der Verträge) und Testvektoren
config/         localai.json, models.json, Projektdateien
docs/           Dokumentation
samples/        Beispielprojekte je Meilenstein und Sprache
toolchains/     Vorlagen für Toolchain-Ordner
scripts/        Bauen, Testen, Starten, Abnahme; Hyper-V- und Linux-Skripte
src/dotnet/     Broker, Sandbox-Steuerung, Oberflächen (WPF, Avalonia), Tests
src/python/     Orchestrator, Agenten, Sandbox-Worker, Web Fetcher, Content Gate, Tests
tests/          Integrationstests gegen den echten Broker, Injection-Korpus, QEMU-Test-Gast
tools/          Codegenerierung, Abnahmebericht, Offline-Bausteine für den Linux-Gast
```

Ausführlich: [Entwicklung](docs/entwicklung.md#repository).

## Info: Rechner, auf denen virtuelle Festplatten nicht eingebunden werden dürfen

Manche Firmenrechner sperren per Richtlinie das Einbinden von VHDX- und ISO-Dateien **auf dem Host** (und teils
Copy-VMFile), erlauben aber das **Anhängen an eine VM**. Dafür gibt es in `sandbox.hyperv` und `sandbox.hypervLinux`:

- **`"hostMount": false`** – der Aufbau bindet nichts ein: Linux-Systemfestplatte und Seed schreibt der Broker selbst
  als VHDX bzw. ISO; Windows installiert Windows Setup in der VM von der ISO (dauert länger als DISM, `imagePath` muss
  eine ISO sein, Sprache in `imageLanguage`). Seed- und Setup-ISO werden nach dem Selbsttest abgehängt und gelöscht.
- **`"inbound": "Disk"`** – wenn auch Copy-VMFile gesperrt ist: jeder Job kommt als selbst geschriebenes
  Eingangs-Laufwerk in die VM. Der Rückweg (Transfer-Laufwerk) liest der Broker ohnehin direkt aus der Datei – erst
  nachdem er es von der VM abgehängt hat; angehängt ist es nur während eines Jobs.

Sicherheit wie gehabt: keine Netzwerkkarte, signierte Jobs, streng geprüfte Rückgabe. Einzelheiten, Grenzen und ein
Kurztest, welche Einstellung ein Rechner braucht: [Betrieb → Rechner ohne Einbinden](docs/betrieb.md#rechner-ohne-einbinden-virtueller-festplatten).
Standard bleibt `hostMount: true` und `inbound: "CopyVMFile"`. **Stand:** hier mit Gegenproben (qemu-img, isoinfo,
Linux-Kern, echter Worker) geprüft; auf einem Rechner mit dieser Sperre noch nicht.

## Bekannte Einschränkungen

- **dev-local-unsafe:** Der Worker prüft Signaturen und liest nur aus der Snapshot-Kopie, läuft aber ohne
  VM-Isolation. Nur zum Entwickeln.
- **Ein Job zur Zeit je VM:** Es gibt ein Transfer-Laufwerk je VM; parallele Sandbox-Jobs in derselben VM
  (Spezifikation: bis zu zwei) folgen bei Bedarf.
- **Rollen über Tokens:** Oberfläche und Orchestrator unterscheiden sich über zufällige Tokens je Broker-Start; die
  Pipe ist auf den aktuellen Benutzer beschränkt. Getrennte Benutzerkonten für den Orchestrator gibt es noch nicht.
- **reuseVm** weicht auf ausdrücklichen Wunsch von R17 (Neuaufbau je Sitzung) ab; standardmäßig aus.
- **Kalibrierung:** Profile über 8K und Offload sind nur nutzbar, wenn die Hardware sie stabil trägt.

## Lizenz

Eigener Code und eigene Dokumentation: [MIT](LICENSE).
Abhängigkeiten, Schriften, Laufzeiten, externe Werkzeuge und Modelle behalten
ihre eigenen Bedingungen; siehe [Drittanbieterhinweise](THIRD-PARTY-NOTICES.md)
und [Lizenzprüfung vor Releases](docs/lizenzen.md).
