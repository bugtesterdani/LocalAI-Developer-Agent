# Dokumentation – Local AI Developer Agent

Einstieg in die Dokumentation. Stand: Meilenstein M10 (Version 0.10.0).

Der Local AI Developer Agent ist ein lokaler Entwicklungsagent mit fünf Rollen (Planner, Coder, Tester, Debugger,
Reviewer). Die Modelle laufen über Ollama auf deinem Rechner oder im LAN. **Jeder** Werkzeugaufruf braucht deine
ausdrückliche Freigabe, ausgeführt wird nur in einer Offline-VM ohne Netzwerkkarte.

## Wo anfangen?

| Du willst … | Lies |
|---|---|
| verstehen, was das System macht und wie es aufgebaut ist | [Architektur](architektur.md) |
| es aus den fertigen Paketen installieren (MSI, MSIX, .deb) | [Installation](installation.md) |
| es auf einem Windows-PC einrichten | [Inbetriebnahme auf einem neuen Rechner](inbetriebnahme-neuer-rechner.md) | (aus dem Repository)
| es auf einem Linux-Host einrichten | [Prüfanleitung M10, Abschnitt 0](pruefung-M10.md#0-einrichtung) |
| damit arbeiten (Chat, Freigaben, Übernahme, Not-Aus) | [Bedienung](bedienung.md) |
| eine Einstellung nachschlagen | [Konfiguration](konfiguration.md) |
| wissen, welche Werkzeuge die Agenten haben und was sie dürfen | [Werkzeuge und Rechte](werkzeuge.md) |
| Toolchains (Compiler, Testframeworks) für die VM anlegen | [Toolchains](../toolchains/README.md) |
| Protokolle, Sicherung, Störungen | [Betrieb](betrieb.md) |
| am Code mitarbeiten | [Entwicklung](entwicklung.md) |
| einen Begriff nachschlagen | [Glossar](glossar.md) |

## Übersicht aller Dokumente

### Grundlagen

- [Projektspezifikation](Local-AI-Developer-Agent-Projektspezifikation.md) – verbindliche Anforderungen (R01–R18),
  Sicherheitsinvarianten, Prüffälle SEC-01–24, Abnahmekriterien AC01–17, Runbook. Ältere Fassung im
  [Archiv](archiv/Local-AI-Developer-Agent-Projektspezifikation-v1.0.md).
- [Umsetzungsplan](Umsetzungsplan.md) – Entscheidungen D1–D13, Meilensteine M1–M10 mit dem jeweils umgesetzten Stand,
  Risiken.
- [Anforderungen → Tests](sicherheit/anforderungen-tests.md) – welcher automatische Test welche
  Sicherheitsanforderung belegt.

### Handbuch

- [Architektur](architektur.md) – Komponenten, Prozesse, Vertrauensgrenzen, Datenflüsse.
- [Bedienung](bedienung.md) – Arbeitsablauf aus Sicht des Nutzers.
- [Konfiguration](konfiguration.md) – `config/localai.json`, `config/models.json`, Projektdateien.
- [Werkzeuge und Rechte](werkzeuge.md) – Toolkatalog, Ordnerrechte, Sammelfreigaben, Agentenaktionen.
- [Betrieb](betrieb.md) – Datenordner, Protokolle, Sicherung, Audit-Prüfung, Fehlerbilder.
- [Entwicklung](entwicklung.md) – Repository, Bauen, Testen, Codegenerierung, Pipe-Protokoll, Erweitern.
- [Glossar](glossar.md)

### Einrichtung

- [Installation aus den Paketen](installation.md) – MSI, MSIX, Debian-Paket, eigene Konfiguration, Pakete bauen
- [Inbetriebnahme auf einem neuen Rechner (Windows)](inbetriebnahme-neuer-rechner.md)
- [Toolchains für die Sandbox-VM](../toolchains/README.md)

### Prüfanleitungen je Meilenstein

Jede Prüfanleitung beschreibt, was der Meilenstein neu bringt, wie man ihn auf echter Hardware prüft und welche
Ergebnisse erwartet werden. Für die Einrichtung eines Teilbereichs sind sie weiterhin die genaueste Quelle.

| Meilenstein | Inhalt | Anleitung |
|---|---|---|
| M1 | Freigabe-Kern: Verträge, Approval Broker, Audit, Freigabe-Oberfläche | [pruefung-M1.md](pruefung-M1.md) |
| M2a | Projektordner mit Rechten, Snapshot, Sandbox-Worker (dev-local) | [pruefung-M2a.md](pruefung-M2a.md) |
| M2b | Offline-Hyper-V-VM: Aufbau, Copy-VMFile, Transfer-VHDX | [pruefung-M2b.md](pruefung-M2b.md) |
| M2c | Toolchain-System (Registrierung, Installation beim VM-Aufbau) | [pruefung-M2c.md](pruefung-M2c.md) |
| M3 | Orchestrator, Model Gateway, Kalibrierung, fünf Rollen, Live-Aktivität | [pruefung-M3.md](pruefung-M3.md) |
| M4 | Patch.Apply, Build/Test in der VM, Repo.Index | [pruefung-M4.md](pruefung-M4.md) |
| M5 | C++, PowerShell, Batch, HDevelop | [pruefung-M5.md](pruefung-M5.md) |
| M6 | Web.Fetch mit Content Gate, Übernahme, Not-Aus, Fortsetzen | [pruefung-M6.md](pruefung-M6.md) |
| M7 | Abnahme: Versionsmanifest, Audit-Prüfung, Diagnose, Sicherung, Abnahmebericht | `scripts/abnahme.ps1` (siehe [Betrieb](betrieb.md#abnahmebericht)) |
| M8 | Linux-Gast (Debian 13), mehrere VMs, Start bei Bedarf | [pruefung-M8.md](pruefung-M8.md) |
| M9 | Eigenes Betriebssystem in QEMU booten und debuggen | [pruefung-M9.md](pruefung-M9.md) |
| M10 | Linux-Host mit QEMU und Avalonia-Oberfläche | [pruefung-M10.md](pruefung-M10.md) |

## Sprache und Konventionen der Dokumentation

- Dokumentation und Oberfläche sind Deutsch, die Prompts der Agenten Englisch (R16).
- Pfade sind relativ zur Repository-Wurzel angegeben. Windows-Pfade mit `\`, Linux-Pfade mit `/`.
- „Datenordner“ meint den Datenordner des Brokers: `%LOCALAPPDATA%\LocalAi\broker` unter Windows,
  `~/.local/share/LocalAi/broker` unter Linux.
