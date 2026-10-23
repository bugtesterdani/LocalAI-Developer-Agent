# Prüfanleitung M3 – Agenten, Model Gateway, Live-Aktivität

Ziel: Eine Chatnachricht startet einen Run mit **fünf Rollen** (Planner, Coder, Tester, Debugger, Reviewer). Die Agenten arbeiten mit einem lokalen Ollama-Modell, jede Dateiaktion ist ein Vorschlag mit deiner Freigabe, und jeder Agent ist live sichtbar und einzeln abbrechbar. Profile werden nur nach **Kalibrierung** verwendet (SEC-16). Stürzt der Orchestrator ab oder hängt er, bleibt alles gesperrt und die Oberfläche bedienbar (SEC-22).

Abgrenzung: `Patch.Apply` und Build/Test kommen mit M4. In M3 sind Patches nur **Vorschläge** im Chat, Tests sind als **nicht ausgeführt** gekennzeichnet. Bis dahin arbeitet der Debugger auf den Review-Befunden.

## Überblick

| Teil | Wo |
|---|---|
| Orchestrator (Python) | `src/python/localai/orchestrator/` – startet mit der ersten Chatnachricht, nur mit Orchestrator-Token |
| Rollen-Prompts (englisch, versioniert) | `src/python/localai/orchestrator/prompts/*.md` |
| Modellprofile und Grenzen | `config/models.json`; persönliche Abweichungen (nur die geänderten Werte) in `config/models.local.json` – wird beim Aktualisieren nie überschrieben, Vorlage `config/models.local.example.json` |
| Kalibrierung | `%LOCALAPPDATA%\LocalAi\broker\calibration.json` und `kalibrierung-*.md` |
| Protokoll des Orchestrators | `%LOCALAPPDATA%\LocalAi\broker\orchestrator.log` |

Ablauf eines Runs: Snapshot (Freigabe) → Dateiliste (Freigabe) → Planner liest gezielt und erstellt einen Plan → Coder und Tester arbeiten parallel → Reviewer prüft → bei Befunden Debugger → erneutes Review (höchstens 3 Reparaturen) → Zusammenfassung. Grenzen: 20 Modellschritte pro Aufgabe, 30 Minuten aktive Zeit ohne neue Nachricht (Warten auf Freigaben zählt nicht), höchstens 1 GPU-Request und 2 Sandboxjobs gleichzeitig.

`config/localai.json` braucht keinen neuen Eintrag: Ohne Abschnitt `orchestrator` startet der Broker `uv run --no-sync --project src/python python -m localai.orchestrator`. Optional (z. B. anderes Zeitlimit für den Hänger-Hinweis):

```json
"orchestrator": {
  "command": "uv",
  "arguments": ["run", "--no-sync", "--project", "src/python", "python", "-m", "localai.orchestrator"],
  "workingDirectory": ".",
  "heartbeatTimeoutSeconds": 30
}
```

## 0. Ollama einrichten (einmalig)

1. Umgebungsvariablen für Ollama (Systemsteuerung → Umgebungsvariablen → Benutzervariablen), danach Ollama beenden (Taskleiste) und neu starten:

   | Variable | Wert |
   |---|---|
   | `OLLAMA_HOST` | `127.0.0.1:11434` |
   | `OLLAMA_MAX_LOADED_MODELS` | `1` |
   | `OLLAMA_NUM_PARALLEL` | `1` |
   | `OLLAMA_FLASH_ATTENTION` | `1` |
   | `OLLAMA_KV_CACHE_TYPE` | `q8_0` |
   | `OLLAMA_NO_CLOUD` | `1` |

   `OLLAMA_CONTEXT_LENGTH` **nicht** setzen – der Kontext kommt pro Request vom Gateway.
2. Version und Modelle prüfen:
   ```powershell
   ollama --version
   ollama list
   ```
   Benötigt (feste Tags, nie `latest`): `qwen2.5-coder:14b`, `qwen2.5-coder:7b`. Optional: `qwen3:8b` (Profil „planung“), `qwen3-coder:30b` (Profil „offload“). Fehlende mit `ollama pull qwen2.5-coder:14b` usw.

### Ollama auf einem anderen Rechner (z. B. 192.168.178.39)

Nur **eine** Datei auf diesem Rechner: `config\models.local.json`

```json
{ "ollama": { "url": "http://192.168.178.39:11434", "allowLan": true } }
```

Auf dem Ollama-Rechner `OLLAMA_HOST` = `0.0.0.0:11434` statt `127.0.0.1:11434` setzen, Ollama neu starten und in der Windows-Firewall eingehend TCP 11434 erlauben – am besten nur für die IP dieses Rechners (Ollama hat keine Anmeldung; jeder, der den Port erreicht, kann es benutzen). Prüfen von hier: `curl.exe http://192.168.178.39:11434/api/version`.

Erlaubt sind nur private LAN-Adressen als Zahl (10.x, 172.16–31.x, 192.168.x) und nur mit `allowLan: true`; öffentliche Adressen und Namen werden abgewiesen. Die Kalibrierung kann GPU/RAM des anderen Rechners nicht messen und gibt Profile deshalb nur mit `--ohne-vram-messung` frei (Speicherreserve dann selbst auf dem Ollama-Rechner prüfen, z. B. mit `nvidia-smi` und `ollama ps` während der Läufe).

## 1. Automatische Tests

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

| Schritt | Erwartet |
|---|---|
| 3/4 C#-Tests | `Passed: 78` – neu `ActivityTests` (Aktivitätsstrom, Journal, Abbruch pro Agent, Agentenansicht mit „hängt?“, gedrosseltes Rendering, Chat, Hänger-Anzeige) |
| 4/4 Python-Tests | `156 passed`, 1–2 übersprungen – neu `test_orchestrator.py` (Gateway mit simuliertem Ollama, Abbruch, Watchdog, Kontextbudget, Protokoll, DAG, Scheduler, Limits, Kalibrierung, vollständiger Run) und `tests/integration/test_orchestrator_broker.py` (echter Broker + Orchestrator-Prozess, Rechte, Abbruch, SEC-22) |

## 2. Vorführung ohne Modell (Oberfläche prüfen)

Zuerst ohne echtes Modell, damit Oberfläche und Freigaben unabhängig vom Modell geprüft sind.

```powershell
# Terminal 1: simulierter Ollama-Server auf Port 11435 (Strg+C beendet)
uv run --project src/python python -m localai.orchestrator.fake_ollama --port 11435 --delay 0.03
# Terminal 2: Demo-Profil aktivieren und Oberfläche starten
copy config\models.demo.json config\models.local.json
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Modus: `dev-local-unsafe` oder `hyperv` (egal). Im Reiter **Chat** z. B. schreiben: „Ergänze im CSV-Importer eine Prüfung auf leere Zeilen und schreibe Tests dafür“ → **Senden** (oder Strg+Enter).

- [ ] Links erscheinen alle fünf Agenten sofort mit Zustand (Text, nicht nur Farbe); darunter „Orchestrator gestartet (PID …, Run …)“.
- [ ] Rechts: Freigabe **Planner · Workspace.Snapshot**, danach **Planner · File.List**, danach Lesevorschläge. Erst nach deiner Freigabe geht es weiter; ablehnen → der Agent erfährt nur „denied“, nie den Inhalt.
- [ ] Chat: Plan des Planners (deutsch), Patch-Vorschlag des Coders, Testfälle des Testers („NICHT ausgeführt“), Review („keine Freigabe – die Entscheidung liegt bei dir“), Reparatur des Debuggers, zweites Review, Zusammenfassung.
- [ ] Klick auf einen Agenten → Reiter **Aktivität**: Zustand, aktuelle Aufgabe, Modell/Token/s/Zeit, Live-Ausgabe mit „── Schritt n/20 ──“-Trennern, Verlauf (Zustände, Vorschläge, Messungen).
- [ ] Während Coder und Tester arbeiten: Fenster verschieben, zwischen Agenten wechseln, Freigabekarte öffnen – nichts friert ein.
- [ ] **Abbrechen** beim Coder, solange er auf eine Freigabe wartet: seine Karte verschwindet (ungültig), Zusammenfassung „T1 … abgebrochen“, Tester arbeitet weiter.
- [ ] **Run abbrechen** während eines Laufs → „Run abgebrochen …“ im Chat; offene Vorschläge des Runs ungültig.
- [ ] **NOT-AUS** während eines Laufs → Orchestrator wird beendet („Orchestrator gestoppt“), Senden gesperrt; „Not-Aus aufheben“ → nächste Nachricht startet einen neuen Orchestrator.

Danach: `del config\models.local.json` (bzw. deine eigene Datei wiederherstellen) und den simulierten Server beenden.

## 3. Kalibrierung (SEC-16, AC09)

Ohne Kalibrierung verwendet der Orchestrator kein Profil und meldet das im Chat. Kalibrieren (Ollama läuft, andere GPU-Last möglichst schließen):

```powershell
# alle installierten Profile aus config/models.json, je 10 Läufe mit nahezu vollem Zielkontext
uv run --project src/python python -m localai.orchestrator.calibrate
# nur das Standardprofil (schneller Einstieg)
uv run --project src/python python -m localai.orchestrator.calibrate --profile standard
# zusätzliche Messreihe 4K–32K je Modell (2 Läufe, nur zur Dokumentation)
uv run --project src/python python -m localai.orchestrator.calibrate --profile standard --kontexte 4096,8192,16384,32768 --laeufe 2
```

Pro Lauf: Eingabetoken, Token/s, Zeit bis zur ersten Ausgabe, freier VRAM. Am Ende „freigegeben“ oder „gesperrt: Grund“, der Pfad zum Bericht `kalibrierung-JJJJMMTT-HHMM.md` und zu `calibration.json`.

- [ ] `standard` (14B, 8K): 10 Läufe ohne Fehler, VRAM frei ≥ 2 GiB → **freigegeben**. Sonst Grund notieren (dann `leicht` = 7B/8K).
- [ ] `erweitert` (16K) und `gross` (32K): Ergebnis notieren. Freigegebene größere Profile werden automatisch bevorzugt (Reihenfolge in `roles` in `config/models.json`).
- [ ] Bericht `kalibrierung-*.md` bitte an Claude schicken (Messwerte für die Doku).

Gesperrt bleibt ein Profil, wenn mehr als 5 % des Modells auf der CPU laufen (passt nicht in den Grafikspeicher – dann ist auch keine Reserve übrig; bewusst zulassen nur über ein Profil mit `"offload": true`), bei Speichermangel, Fehlern, weniger als 10 Läufen, VRAM-Reserve < 2 GiB oder wenn VRAM nicht messbar ist (ohne `nvidia-smi`; bewusst übergehen mit `--ohne-vram-messung`). Wird ein Modell neu gezogen (anderer Digest) oder ändert sich die Hardware, ist eine neue Kalibrierung nötig.

## 4. Abnahme: fünf Rollen mit echtem Modell

`config\models.local.json` darf nicht mehr das Demo-Profil enthalten (nur z. B. deine Ollama-Adresse). Oberfläche starten, Projekt `config/demo-project.json` (CSV-Importer).

Chat: „Ergänze im CSV-Importer eine Prüfung auf leere Zeilen und schreibe Tests dafür.“

- [ ] Agentenliste: je Rolle „bereit · gross/erweitert/standard · qwen2.5-coder:14b · NK (kalibriert) · Prompts common v1 …“ (im Verlauf des Agenten).
- [ ] Alle fünf Rollen arbeiten: Plan, Patch, Testfälle, Review, ggf. Debugger; Chatnachrichten auf Deutsch.
- [ ] Jede Dateiaktion erscheint vorher als Freigabe; nach deiner Entscheidung setzt der Agent fort.
- [ ] Live-Ausgabe läuft flüssig mit; Token/s und Eingabetoken stehen unter „Modell und Tempo“.
- [ ] Sammelfreigabe beim Lesen mehrerer Dateien: eine Datei abwählen → der Agent erhält nur die ausgewählten Inhalte („deselected by the user“ im Verlauf).

Antwortet das Modell nicht im vorgegebenen Format, erscheint „Antwort nicht verwertbar“ im Verlauf und der Agent versucht es erneut (zählt als Schritt). Bitte Häufigkeit notieren.

- [ ] Verlauf über die Schritte: Ab dem zweiten Schritt einer Aufgabe zeigt die Ausgabe, dass der Agent an seine vorige Überlegung anknüpft (statt dieselbe Datei erneut anzufragen). Bei längeren Aufgaben steht im Verlauf des Agenten „Eigene Notizen Schritte 1–n zusammengefasst, ~N Token“ mit dem Text; die Agentenliste nennt zusätzlich „summarizer v1“. Schrittzahl je Aufgabe mit `history.mode` `summary` und `off` vergleichen und notieren.

## 5. Live-Ansicht unter 32K bzw. Offload

1. Profil `gross` (32K) kalibrieren; für Offload `qwen3-coder:30b` ziehen und `--profile offload` kalibrieren (CPU-Offload ist zulässig, Latenz zweitrangig).
2. Offload für alle Rollen erzwingen: in `config\models.local.json` zusätzlich
   `"roles": { "Planner": ["offload"], "Coder": ["offload"], "Tester": ["offload"], "Debugger": ["offload"], "Reviewer": ["offload"] }` eintragen.
3. Run wie in Abschnitt 4.

- [ ] Oberfläche bleibt während langer Prompt-Verarbeitung und Generierung bedienbar (Fenster verschieben, Reiter wechseln, Freigaben entscheiden).
- [ ] Bleiben Tokens länger als 45 s aus, steht beim Agenten „arbeitet · hängt?“ (rot, mit Text) und im Verlauf „Seit N s keine Tokens – hängt? Abbruch ist möglich.“; **Abbrechen** beendet den Request sofort.
- [ ] Ollama-Speicher während des Laufs: `ollama ps` (zeigt GPU/CPU-Anteil).

## 6. SEC-22: Orchestrator abgestürzt oder hängt

Absturz:
1. Run starten, warten bis eine Freigabe offen ist.
2. Task-Manager → Details → den `python.exe` des Orchestrators beenden (seine PID steht in `orchestrator.log` im Datenordner in der Zeile „Orchestrator: PID …“).

- [ ] Ereignis „Orchestrator beendet · Orchestrator unerwartet beendet (Exitcode …); 1 offene Vorschläge ungültig“, Text links rot.
- [ ] Die offene Freigabekarte verschwindet; erlauben ist nicht mehr möglich.
- [ ] Oberfläche bedienbar, Not-Aus wirkt; die nächste Chatnachricht startet einen neuen Orchestrator.

Hänger:
1. Run starten, `resmon` (Ressourcenmonitor) → CPU → Rechtsklick auf den `python.exe` des Orchestrators (PID wie oben) → **Prozess anhalten**.

- [ ] Nach ca. 30 s: „Orchestrator antwortet nicht · … – hängt?“ und links der Knopf **Orchestrator beenden**.
- [ ] Oberfläche bleibt bedienbar; offene Freigaben können abgelehnt werden.
- [ ] **Orchestrator beenden** oder **NOT-AUS** beendet ihn („Orchestrator gestoppt“).

## 7. VM wiederverwenden (Option `reuseVm`)

Auf Wunsch: Wird nur die Software neu gestartet, muss die VM nicht neu aufgebaut werden. In deiner `config\localai.json` unter `sandbox.hyperv`:

```json
"reuseVm": true
```

Verhalten:
- Beim Beenden der Oberfläche wird eine **bereite** VM gespeichert (Hyper-V „Gespeichert“) statt gelöscht. Sitzungsschlüssel und Sequenz liegen in `sandbox-state.json` im VM-Ordner (Schlüssel mit Windows-DPAPI an dein Konto gebunden).
- Beim nächsten Start wird sie fortgesetzt, wenn der **Fingerabdruck** passt: Windows-Abbild (Pfad, Größe, Datum), VM-Einstellungen, Python-Laufzeit, Worker-Code, Gastskripte und die Registrierungen der Projekt-Toolchains. Änderungen am Orchestrator oder an der Oberfläche zählen nicht.
- Neu aufgebaut wird, wenn sich etwas davon geändert hat, die VM fehlt, eine Netzwerkkarte hat, nicht innerhalb von 5 Minuten antwortet, oder die letzte Sitzung gesperrt bzw. abgestürzt ist.
- Knopf **VM neu aufbauen** in der Kopfzeile erzwingt jederzeit einen Neuaufbau (mit Rückfrage).

Sicherheitshinweis: Das weicht bewusst von „Neuinstallation je Sitzung“ (R17) ab. Daten früherer Snapshots bleiben in der VM, und eine während einer Sitzung veränderte VM lebt weiter. Bei Zweifeln deshalb **VM neu aufbauen**.

- [ ] Mit `reuseVm: true` starten, warten bis „bereit“, Oberfläche schließen → in Hyper-V-Manager Zustand „Gespeichert“.
- [ ] Erneut starten → nach Sekunden „Offline-VM bereit – wiederverwendet (aufgebaut TT.MM. HH:mm)“; Snapshot und Lesen funktionieren.
- [ ] Toolchain neu registrieren (oder `memoryMB` ändern) → beim nächsten Start Neuaufbau.
- [ ] **VM neu aufbauen** → Rückfrage, danach Aufbau mit Fortschritt in der Kopfzeile.
- [ ] Oberfläche während des Aufbaus schließen bzw. Not-Aus mit gesperrter VM → beim nächsten Start Neuaufbau.

## Fehlersuche

| Meldung | Ursache / Abhilfe |
|---|---|
| „Ollama ist nicht erreichbar“ | Ollama läuft nicht oder hört nicht auf 127.0.0.1:11434 (`OLLAMA_HOST`). |
| „… nicht kalibriert“ / „nicht stabil“ | Abschnitt 3; der Grund steht hinter dem Profil. |
| „Modell nicht installiert (ollama pull …)“ | Tag exakt wie in `config/models.json` ziehen. |
| „Speichermangel bei …“ | Profil zu groß für den aktuellen Speicherzustand; neu kalibrieren oder kleineres Profil vorziehen. |
| „Kein freigegebener Projektordner …“ | Projektdatei braucht einen Grant mit `List` und `Read`. |
| Orchestrator startet nicht | `orchestrator.log` im Datenordner; `uv sync --project src/python` ausführen. |

## Bekannte Einschränkungen

- Patches werden nicht angewendet, Tests nicht ausgeführt (M4: `Patch.Apply`, `Toolchain.Build/Test`). Der Planner bekommt bis M4 die Dateiliste (Tiefe 4) statt einer Repo-Map (`Repo.Index`).
- Ein Snapshot gilt für alle Ziele, solange der Orchestrator läuft; für einen neuen Projektstand den Orchestrator beenden (oder Not-Aus und neu starten).
- Tokenzählung im Budget ist eine konservative Schätzung; nach der Kalibrierung wird das gemessene Verhältnis Zeichen/Token verwendet.
