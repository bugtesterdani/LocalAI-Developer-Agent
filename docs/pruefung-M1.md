# Prüfanleitung M1 – Freigabe-Kern

Ziel: bestätigen, dass ohne ausdrückliche Freigabe nichts ausgeführt wird und die Freigabe-Oberfläche unter Windows wie vorgesehen funktioniert.

In M1 gibt es **noch keine VM, kein Modell und keine echten Dateizugriffe**. Ein Fake-Agent reicht Vorschläge ein, ein Testadapter zählt nur Ausführungen.

## 0. Voraussetzungen

```powershell
dotnet --list-sdks      # 10.0.x muss erscheinen
uv --version            # falls fehlt: winget install --id=astral-sh.uv -e
git -C C:\Users\Daniel\Desktop\app6 log --oneline -3
```

## 1. Automatische Tests

```powershell
cd C:\Users\Daniel\Desktop\app6
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

Erwartet:

| Schritt | Ergebnis |
|---|---|
| 1/4 Verträge | `aktuell` |
| 2/4 Build | ohne Fehler, **inklusive LocalAi.Desktop (WPF)** – das ist der erste WPF-Build überhaupt |
| 3/4 C#-Tests | `Passed! - Failed: 0, Passed: 26` (xUnit) |
| 4/4 Python-Tests | `42 passed` (beim ersten Lauf lädt uv Python-Pakete) |

Falls der WPF-Build scheitert: die komplette Fehlermeldung an Claude schicken.

## 2. Oberfläche starten

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Prüfen:

- [ ] Fenster vollständig auf Deutsch.
- [ ] Kopfzeile: „Verbunden mit Broker (Sitzung …)“, „SANDBOX: noch nicht vorhanden (folgt in M2)“.
- [ ] Links fünf Agenten, alle „bereit“. Rechts „Keine offenen Freigaben.“
- [ ] Ereignisse zeigen „Broker gestartet“.

## 3. Testlauf mit Fake-Agent

„Testlauf starten (Fake-Agent)“ klicken. Der erste Start kann bis zu einer Minute dauern.

- [ ] Grüne Info-Zeile mit PID und Pfad zum Agentenprotokoll.
- [ ] Ereignis „Vorschlag abgewiesen · File.Read: …“ (absichtlich unzulässiger Pfad `..\..\Users\geheim.txt`).
- [ ] Zwei Freigaben: „Coder · File.Read“ und „Tester · File.ReadMany“.

### Einzelfreigabe (Coder · File.Read)

- [ ] Karte zeigt Zweck, Rolle, Tool mit Version, Gültig bis, Snapshot, Sandbox, Hash (64 Zeichen), Parameter `maxBytes`, Ziel `src/Importer.cs` mit „Zeilen 1–160“.
- [ ] Hash und Pfad lassen sich markieren und kopieren.
- [ ] Der Fokus liegt auf „Ablehnen“. Tab wechselt zu „Diesen Aufruf einmal erlauben“. **Enter bestätigt nie das Erlauben.**
- [ ] „Ablehnen“ klicken → Ereignis „Status · … · abgelehnt“, Karte verschwindet.

### Sammelfreigabe (Tester · File.ReadMany, 7 Dateien)

- [ ] Zähler „7 von 7 ausgewählt“, Button „Auswahl einmal erlauben“ aktiv.
- [ ] Filter `tests` eingeben → nur die beiden Testdateien sichtbar. „Alle sichtbaren abwählen“ → „5 von 7 ausgewählt“.
- [ ] Filter leeren. „Auswahl einmal erlauben“ ist jetzt **deaktiviert**, gelber Hinweis sichtbar.
- [ ] „Auswahl übernehmen (erzeugt neuen Vorschlag)“ → neue Karte mit 5 Zielen, **anderem Hash** und Feld „Ersetzt Vorschlag“.
- [ ] „Auswahl einmal erlauben“ → Ereignisse „läuft“ und „erfolgreich“.

### Agentenprotokoll

Die Info-Zeile nennt den Pfad (Standard `%LOCALAPPDATA%\LocalAi\broker\fake-agent.log`). Erwartet:

- [ ] `Unzulässiger Pfad korrekt abgewiesen (403) …`
- [ ] `Ergebnis …: Denied`
- [ ] `Ergebnis …: Succeeded (nach Abwahl als …), ausgeführt: [...], vom Nutzer abgewählt: ['t4', 't5']`
- [ ] Umlaute korrekt dargestellt.

## 4. Not-Aus

1. Testlauf erneut starten und warten, bis zwei Freigaben da sind.
2. „NOT-AUS“ klicken.

- [ ] Roter Hinweis mit Text „NOT-AUS AKTIV – …“ (nicht nur Farbe).
- [ ] Beide Karten verschwinden, Ereignisse „ungültig · Not-Aus“.
- [ ] „Testlauf starten“ ist deaktiviert.
- [ ] „Not-Aus aufheben“ → Hinweis verschwindet, Testlauf wieder möglich.

## 5. Neustart

1. Testlauf starten, Freigaben **offen lassen**, Fenster schließen.
2. Oberfläche erneut starten.

- [ ] Keine offenen Freigaben. Ereignisse zeigen „ungültig · Neustart: alte Vorschläge und Freigaben sind ungültig“.

## 6. Audit-Journal (optional)

Datei: `%LOCALAPPDATA%\LocalAi\broker\audit.jsonl` – eine kanonische JSON-Zeile pro Ereignis mit `prevHash` und `hash`.

Manipulationstest (Oberfläche vorher schließen):

1. Datei **kopieren** (Sicherung).
2. In einer Zeile ein Zeichen in `purpose` ändern, speichern.
3. Oberfläche starten → Statuszeile „Verbindung zum Broker fehlgeschlagen … manipuliert“.
4. Sicherung zurückkopieren oder die Datei umbenennen, um neu zu beginnen.

## Bekannte Einschränkungen in M1

- Der Testadapter liest keine Dateien; Inhalte sind Platzhalter.
- Ordnerrechte, Snapshots und Datei-Hashprüfung folgen in M2.
- Chat, Modelle und Live-Aktivität folgen in M3.
- Orchestrator läuft als derselbe Windows-Benutzer; Trennung über Tokens.

## Rückmeldung an Claude

- Ausgabe von `scripts\test.ps1` (mindestens die letzten Zeilen jedes Schritts).
- Welche Checkboxen nicht erfüllt sind, gern mit Screenshot.
- Auffälligkeiten bei Bedienung, Texten und Layout.
