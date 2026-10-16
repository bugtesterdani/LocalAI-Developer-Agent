# Prüfanleitung M2a – echte Dateizugriffe über Snapshot und Worker

Ziel: bestätigen, dass der Agent Projektdateien **nur** über eine freigegebene Snapshot-Kopie lesen kann, dass Ordnerrechte greifen und dass Abwahl und Ablehnung wirken.

In M2a läuft der Worker im Modus **`dev-local-unsafe`** noch **ohne VM** als normaler Python-Prozess auf diesem PC. Die Oberfläche zeigt das rot an. Die Offline-VM folgt in M2b.

## 0. Voraussetzungen (PC 123)

Projekt wie gewohnt nach `C:\Users\123\Downloads\config` kopieren, dann:

```powershell
cd C:\Users\123\Downloads\config
uv sync --project src/python      # Proxy-Variablen wie bisher setzen
```

## 1. Automatische Tests

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

| Schritt | Erwartet |
|---|---|
| 1/4 Verträge | `aktuell` |
| 2/4 Build | ohne Fehler (inkl. WPF und neuem Projekt `LocalAi.Sandbox`) |
| 3/4 C#-Tests | `Passed: 36` (unter Windows laufen zusätzlich die Junction-/Hardlink-Prüfungen im Snapshot) |
| 4/4 Python-Tests | `97 passed` |

Der Ende-zu-Ende-Test startet Broker **und** Worker; im Testprotokoll erscheinen u. a. „Workspace.Snapshot wird erlaubt“, „7 Dateien gefunden“ und „Broker-Prozess beenden: erledigt“.

## 2. Oberfläche starten

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

- [ ] Kopfzeile zeigt **rot und fett**: „SANDBOX: ENTWICKLUNGSMODUS – UNSICHER, Worker läuft ohne VM auf diesem PC“.
- [ ] Projekt: „Demo-Projekt …“ mit Ordner `samples\demo-csv` (Rechte: Auflisten, Lesen).
- [ ] Im Task-Manager läuft ein `python`-Prozess (der Worker). Er verschwindet beim Schließen der Oberfläche.

## 3. Testlauf mit Fake-Agent

„Testlauf starten (Fake-Agent)“ klicken.

### 3.1 Snapshot

- [ ] Erste Karte: „Workspace.Snapshot“. Nebenwirkungen: der Broker liest den Ordner auf diesem PC (ohne Verknüpfungen) und überträgt eine Kopie. Parameter zeigen `grantId` und `root`.
- [ ] **Vor** der Freigabe existiert unter `%LOCALAPPDATA%\LocalAi\broker\sandbox-dev\work\snapshots` kein Manifest.
- [ ] „Diesen Aufruf einmal erlauben“ → Status „erfolgreich“.

### 3.2 Auflisten

- [ ] Karte „File.List“ mit Ziel `.` und Snapshot-ID. Erlauben.

### 3.3 Lesen mit Abwahl

- [ ] Zwei Karten: „File.Read“ (`README.md`, Zeilen 1–20) und „File.ReadMany“ mit 7 Zielen.
- [ ] Bei File.ReadMany zwei Dateien abwählen → „Auswahl übernehmen“ → neue Karte mit 5 Zielen und neuem Hash → „Auswahl einmal erlauben“.
- [ ] File.Read **ablehnen**.
- [ ] Ereignis „Vorschlag abgewiesen“ für den absichtlich unzulässigen Pfad.

### 3.4 Agentenprotokoll

Pfad steht in der grünen Info-Zeile. Erwartet u. a.:

```text
Snapshot …: 7 Dateien, … Bytes
7 Dateien gefunden
Unzulässiger Pfad korrekt abgewiesen (403): …
… abgelehnt
… erfolgreich … übersprungen: ['t…', 't…']
  t…: … Zeichen, erste Zeile: # Format
```

- [ ] Inhalte der abgewählten Dateien erscheinen **nicht** im Protokoll.
- [ ] `%LOCALAPPDATA%\LocalAi\broker\worker.log` enthält keine Fehler.

## 4. Gegenproben (optional, aber empfohlen)

1. **Verknüpfung wird nicht verfolgt:** als Administrator
   `mklink /J samples\demo-csv\link C:\Windows` und `mklink samples\demo-csv\x.txt C:\Windows\win.ini` anlegen, Testlauf wiederholen.
   Erwartet: weiterhin „7 Dateien“, `link` und `x.txt` fehlen in der Liste. Danach beide wieder löschen.
2. **Kein Read-Recht:** in `config\demo-project.json` bei `rights` nur `"List"` eintragen, Oberfläche neu starten, Testlauf.
   Erwartet: Fake-Agent meldet „Kein freigegebener Ordner mit List- und Read-Recht“; es erscheint keine Karte. Danach zurückstellen.
3. **Ohne Sandbox:** in `config\localai.json` `"mode": "none"` setzen.
   Erwartet: Kopfzeile „SANDBOX: nicht verfügbar – Ausführung gesperrt“; freigegebene Vorschläge schlagen fehl, nichts wird gelesen. Danach zurück auf `dev-local-unsafe`.
4. **Audit:** `%LOCALAPPDATA%\LocalAi\broker\audit.db` ist jetzt eine SQLite-Datei. Wird eine Zeile darin verändert (z. B. mit „DB Browser for SQLite“), startet der Broker nicht mehr (Exit-Code 3).

## Rückmeldung

Bei Fehlern bitte die Konsolenausgabe, das Agentenprotokoll und `worker.log` schicken.
