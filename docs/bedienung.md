# Bedienung

Dieses Kapitel beschreibt die Arbeit mit der Oberfläche – vom ersten Ziel im Chat bis zur Übernahme der Änderungen in
deinen Projektordner. Einrichtung: [Inbetriebnahme](inbetriebnahme-neuer-rechner.md) (Windows) bzw.
[Prüfanleitung M10](pruefung-M10.md#0-einrichtung) (Linux).

## Starten

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1    # Windows
```

```bash
scripts/linux/start.sh                                        # Linux
```

Die Oberfläche startet den Broker selbst. Welches Projekt geladen wird, steht in `config/localai.json` unter
`project` (siehe [Konfiguration](konfiguration.md)).

## Aufbau der Oberfläche

| Bereich | Inhalt |
|---|---|
| **Kopfzeile** | Projekt, Zustand der Sandbox (z. B. „VM wird aufgebaut – Schritt 3/5“, „Offline-VM bereit – Selbsttest bestanden“, „gesperrt …“), installierte Toolchains, Knopf **NOT-AUS**. Im Modus `dev-local-unsafe` ist die Zeile rot |
| **AGENTEN** | die fünf Rollen mit Zustand (bereit, wartet, arbeitet, wartet auf Freigabe, wartet auf dich, fertig, gescheitert, abgebrochen). Klick auf einen Agenten öffnet seine Aktivität |
| **CHAT UND PLAN** | deine Nachrichten, Plan, Zwischenstände und Zusammenfassung |
| **Aktivität** | je Agent: aktuelle Aufgabe, Modell, Token/s, verstrichene Zeit, gestreamte Ausgabe (inkl. Thinking, falls das Modell es liefert), Verlauf mit Vorschlägen und Zuständen |
| **FREIGABEN** | offene Freigabekarten |
| **EREIGNISSE** | Ereignisse des Brokers (Vorschläge, Entscheidungen, Ausführungen) |
| **TOOLCHAINS** | Toolchain-Ordner prüfen, Toolchains registrieren bzw. entfernen |
| **SYSTEM** | Versionsmanifest, Audit-Prüfung, Diagnosepaket, Sicherung |

Alle Inhalte aus Dateien, Webseiten und Agentenantworten werden **inert** als Text angezeigt: Steuer- und
ANSI-Zeichen entfernt, Bidi- und unsichtbare Zeichen sichtbar markiert, Länge begrenzt, kein HTML (SEC-14).

## Ein Ziel bearbeiten lassen

1. Im Chat ein Ziel eingeben, z. B. „Ergänze im CSV-Importer eine Prüfung auf leere Zeilen und schreibe Tests dafür“,
   und **Senden** (oder Strg+Enter). Die erste Nachricht startet den Orchestrator mit allen fünf Rollen.
2. **Snapshot:** Der erste Vorschlag ist meist `Workspace.Snapshot` – der Broker kopiert den freigegebenen
   Projektordner in die Sandbox. Nichts wird verändert.
3. **Repo-Index:** `Repo.Index` erstellt in der VM einen Überblick (Dateibaum, Symbole, Volltextindex). Daraus
   bekommt der Planner eine kompakte Repo-Map, statt alle Dateien zu lesen.
4. **Plan:** Der Planner zerlegt das Ziel in Aufgaben mit Abhängigkeiten, Akzeptanzkriterien und Prüfläufen (z. B.
   `Toolchain.Test`). Der Plan erscheint im Chat.
5. **Lesen und Ändern:** Coder und Tester lesen gezielt Dateien (`File.Read`, `File.ReadMany`) und schlagen Patches
   vor (`Patch.Apply`). Jeder Vorschlag ist eine eigene Freigabekarte.
6. **Prüfen:** Build und Tests laufen in der VM (`Toolchain.Build`, `Toolchain.Test`). Bei Fehlern sucht der
   Debugger anhand der echten Fehlermeldungen die Ursache („Diagnose“ im Chat, ohne Codeänderung), der Coder korrigiert
   danach („Korrektur“), dann wird erneut getestet – höchstens `maxRepairAttempts` Runden.
7. **Review und Zusammenfassung:** Der Reviewer prüft den Diff gegen Ziel und Testbericht. Wesentliche Befunde korrigiert
   der Coder direkt (ohne Debugger), danach erneut Test und Review. Ein Review ist **keine** Freigabe – die Entscheidung
   bleibt bei dir.
8. **Übernahme:** Danach wird die Übernahme in deinen Projektordner angeboten (siehe unten).

Die Agenten antworten auf Deutsch. Fragt ein Agent zurück („wartet auf dich“), antwortest du im Chat.

**Zeitaufschlüsselung:** Am Ende jedes Runs (auch nach Abbruch) zeigt der Orchestrator im Chat, wohin die Zeit ging:

- Modell gesamt, aufgeteilt in Laden, Prompt einlesen und Ausgabe; die Zahlen stammen von Ollama selbst;
- Warten auf den GPU-Slot;
- je Werkzeugart: Warten auf deine Freigabe und Ausführung (Sandbox), automatisch genehmigte mitgezählt;
- je Modell: Aufrufe, wie oft es (neu) geladen wurde, Token/s für Prompt und Ausgabe.

Dazu kommen Hinweise, wo es sich zu drehen lohnt: häufige Modellwechsel, verdächtig wenige Token/s (Teil auf der CPU),
viele Zusammenfassungen, lange Prompts, viel Warten auf Freigaben. Jeder Run wird außerdem als eine Zeile in
`%LOCALAPPDATA%\LocalAi\broker\zeiten.jsonl` gespeichert, zum Vergleichen verschiedener Modell-Einstellungen.

## Freigabekarten

Jede Karte zeigt: Rolle, Werkzeug, Zweck, exakte Parameter, Ziele, Snapshot, Sandbox, Gültigkeit (5 Minuten),
erwartete Nebenwirkungen und den Vorschlags-Hash. Bei `Patch.Apply` und der Übernahme siehst du den vollständigen Diff
je Datei, bei Toolchain-Aufrufen das Programm und die Argumente.

Es gibt nur zwei Entscheidungen:

- **Diesen Aufruf einmal erlauben** – gilt genau für diesen Hash, genau einmal.
- **Ablehnen** – der Agent erfährt es und plant um (nach `maxDenialsPerTask` Ablehnungen fragt er nach).

Der Fokus liegt standardmäßig auf „Ablehnen“; Enter genehmigt nichts versehentlich. Es gibt kein „immer erlauben“.
Läuft eine Karte ab, wird nichts ausgeführt – warten ist nie Zustimmung.

### Sammelfreigabe

`File.List`, `File.ReadMany`, `File.Search`, `Patch.Apply` und `Workspace.ExportPatch` können mehrere Ziele haben. Die
Karte zeigt dann eine Liste mit Kontrollkästchen:

1. Mit **Filter** (Pfad oder Endung) eingrenzen, einzelne Ziele oder **Alle sichtbaren abwählen**.
2. **Auswahl übernehmen** – das erzeugt einen neuen Vorschlag mit neuem Hash; der alte wird ersetzt.
3. **Auswahl einmal erlauben**.

Abgewählte Ziele werden nicht gelesen bzw. nicht geändert; der Agent erfährt „vom Nutzer abgewählt“. Ziele lassen
sich nur entfernen, nie ergänzen (SEC-23).

### Webabrufe – zwei Freigaben

1. **`Web.Fetch`** zeigt genau die Anfrage, die gesendet wird (Anfragezeile, Kopfzeilen, Weg direkt/Proxy) und die
   übertragenen Daten einzeln dekodiert. Rote Hinweise warnen vor möglichem Datenabfluss, z. B. wenn ein Agent Code
   aus deinem Projekt in die URL gepackt hat.
2. **`Web.Content`** zeigt nach dem Abruf HTTP-Status, Aussteller des TLS-Zertifikats, Kopfzeilen der Antwort, Hinweise
   des Content Gates und den **bereinigten Text** – und an welchen Agenten er geht. Erst nach dieser zweiten Freigabe
   sieht der Agent den Text, und zwar als untrusted Evidenz.

Ein Webtext, der „Anweisungen“ enthält, bleibt Text; er kann keine Rechte oder Freigaben ändern (SEC-08).

### Debug-Sitzung (M9)

`OsDebug.Start` startet in der Linux-VM ein eigenes Betriebssystem in QEMU. Die Karte zeigt die Grenzen der Sitzung
(Befehlsbudget, Dauer, ob Tastatur-/Mauseingaben erlaubt sind). Mit dieser **einen** Freigabe darf derselbe Agent
im selben Run Debug-Befehle senden, bis Budget oder Dauer erschöpft sind oder die Aufgabe endet. Einzelheiten:
[Prüfanleitung M9](pruefung-M9.md).

### Automatische Freigabe (`autoApprove`)

Steht in der Projektdatei `autoApprove` (siehe [Konfiguration](konfiguration.md#autoapprove)), kündigt der
Orchestrator zu Beginn **jedes** Runs im Chat an, welche Aktionen ohne Einzelfreigabe laufen sollen, und legt eine Karte
`Run.AutoApprove` vor: rot hervorgehoben „Wird in diesem Run automatisch genehmigt“ mit jedem Tool und seiner Wirkung.

- **„Für diesen Run bestätigen“** – der Broker genehmigt Vorschläge genau dieser Tools in diesem Run selbst. Sie
  erscheinen in der Aktivität als „automatisch genehmigt (autoApprove)“, nicht mehr unter Freigaben.
- **Ablehnen** oder ablaufen lassen – alles wird wie gewohnt einzeln vorgelegt.
- Snapshot, Übernahme in den Projektordner, Webabrufe und Debug-Sitzungen sind nie automatisch.
- Not-Aus oder Abbruch des Runs beenden die automatische Freigabe; der nächste Run fragt erneut.

## Übernahme in den Projektordner

Bis hierhin hat sich nur die Arbeitskopie in der Sandbox geändert. Die Übernahme sind zwei Freigaben:

1. **Übernahme vorbereiten** (`Workspace.Diff`, nur lesend): Der Broker holt die geänderten Dateien aus der Sandbox und
   berechnet den Diff gegen deinen **aktuellen** Projektordner.
2. **Übernahme in den Projektordner** (`Workspace.ExportPatch`): Diff je Datei, abwählbar. Nötig sind die Rechte
   `Modify`, `Create` bzw. `Delete` für die jeweiligen Dateien.

Beim Schreiben prüft der Broker jede Datei erneut. Hat sie sich seit der Vorschau verändert, wird **nichts**
geschrieben. Die Originale werden gesichert, das Schreiben erfolgt atomar; bei einem Fehler wird alles zurückgesetzt.

**Nicht angewendete Änderungen:** Läuft die Freigabe für einen Patch ab, wartet der Agent auf deine nächste Nachricht
(z. B. „weiter“) und schlägt denselben Patch erneut vor. Was am Ende trotzdem nicht angewendet ist (abgelehnt, passte
nicht), legt der Orchestrator als Entwurf im Datenordner ab: `%LOCALAPPDATA%\LocalAi\broker\entwuerfe\<Run>\<Zeitpunkt>\<Aufgabe>\` (eigener Ordner je Ziel)
– neue Dateien mit ihrem Inhalt, dazu je Aufgabe die vollständige `.patch`-Datei. Der Pfad steht in der Zusammenfassung
im Chat. In deinen Projektordner kommt nur, was du über die Übernahme freigibst.

## Chat-Befehle

| Befehl | Wirkung |
|---|---|
| `übernehmen` | Übernahme erneut anbieten (z. B. nach einer Ablehnung) |
| `fortsetzen` | unterbrochenen Run fortsetzen – mit neuem Snapshot, jeder Patch wird erneut zur Freigabe vorgelegt, erledigte Aufgaben bleiben erledigt |
| `neu` | unterbrochenen Run verwerfen und das zurückgehaltene neue Ziel beginnen |
| `webinhalt verwerfen` | übergebene Webinhalte als verdächtig verwerfen: alle Aufgaben, die den Text gesehen haben (und ihre abhängigen), sowie die Patches ab dem ersten betroffenen werden verworfen; mit `fortsetzen` wird ohne diese Inhalte neu aufgebaut, die Seiten gelten danach als gesperrt |

Ein neues Ziel ersetzt einen unterbrochenen Run nie stillschweigend – du wirst gefragt, ob du fortsetzen oder neu
beginnen willst.

## Abbrechen und Not-Aus

| Aktion | Wirkung |
|---|---|
| **Abbrechen** beim Agenten | beendet seine laufende Modellanfrage sofort; seine offenen Vorschläge werden ungültig; die anderen Agenten arbeiten weiter |
| **Run abbrechen** | beendet den Run; alle offenen Vorschläge des Runs werden ungültig |
| **Orchestrator beenden** | erscheint, wenn der Orchestrator nicht mehr antwortet; die nächste Chatnachricht startet einen neuen |
| **NOT-AUS** | stoppt jeden Dispatch, invalidiert alle offenen Freigaben, beendet Webprozesse und laufende Ausführung. Läuft gerade ein Job in der VM, wird die VM ausgeschaltet und gesperrt |
| **Not-Aus aufheben** | nimmt wieder Vorschläge an. Alte Freigaben gelten nie wieder |

Watchdog: Bleiben Tokens länger als `stallWarningSeconds` (Standard 45 s) aus, steht beim Agenten „hängt?“ – du
kannst abbrechen oder warten.

## Sandbox-Zustände

| Kopfzeile | Bedeutung | Was tun |
|---|---|---|
| „VM wird aufgebaut – Schritt n/…“ | Aufbau im Hintergrund | warten; Chat und Planung gehen schon |
| „Offline-VM bereit – Selbsttest bestanden“ | Jobs werden angenommen | – |
| „… heruntergefahren, startet beim nächsten Job“ | Leerlauf-Abschaltung | nichts; der nächste Job startet sie |
| „gesperrt: …“ | Netzwerkkarte gefunden, Bericht fehlerhaft, Toolchain-Hash abweichend, Not-Aus oder keine Antwort | Ursache lesen, dann **VM neu aufbauen** (bei ausbleibender Antwort auch **VM weiter verwenden**, ohne Wiederholung des Auftrags) |
| rot „ENTWICKLUNGSMODUS – UNSICHER“ | `dev-local-unsafe`, Worker ohne VM | nur zum Entwickeln verwenden |

## Toolchains registrieren

Bereich **TOOLCHAINS** → „Toolchain-Ordner prüfen“ → je Toolchain **Registrieren** (proprietäre: Lizenz bestätigen).
Die Registrierung hält die Hashes aller Dateien fest; jede spätere Änderung im Ordner lässt den nächsten VM-Aufbau
abbrechen („Hash weicht ab“) – dann erneut prüfen und registrieren. Anlegen von Toolchains:
[toolchains/README.md](../toolchains/README.md).

## System

Bereich **SYSTEM**:

- **Versionsmanifest** – Programmversion und Commit, Betriebssystem, .NET, Python, Prompt-Versionen, Betriebsart,
  Konfiguration und Projektdatei, registrierte Toolchains, VM-Einstellungen, Web-Einstellungen und eine
  Offline-Prüfliste (was für den Betrieb vorhanden ist und was fehlt).
- **Audit prüfen** – prüft Hashkette und Freigabeinvarianten des Journals.
- **Diagnosepaket erstellen** – Versionen, Zustände je Werkzeug, Ereignis-IDs, Speicherwerte und Hashes; **kein**
  Code, keine Webinhalte, keine Vorschlagsargumente. Protokolle nur auf ausdrückliche Wahl.
- **Sicherung erstellen** – Audit-Journal, Toolchain-Registrierungen, Kalibrierung, Runjournal und Konfiguration, nie
  Freigaben. Wiederherstellen siehe [Betrieb](betrieb.md#sicherung-und-wiederherstellung).
