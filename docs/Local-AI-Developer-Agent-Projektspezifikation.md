# Local AI Developer Agent – vollständige Projektspezifikation

Version 1.3 · Stand: 16.10.2026 · Status: in Umsetzung (M1–M2b implementiert, siehe `Umsetzungsplan.md`)

**Änderungen in 1.1 gegenüber 1.0:** Implementierung C# (UI, Approval/Tool Broker, Audit) + Python (Orchestrierung, Agenten, Model Gateway, Tools, Sandbox-Worker); Zielsprachen der Agenten C#, Python, C++, HDevelop, PowerShell, Batch über erweiterbare Toolchains; große Repositories ab MVP mit Kontextprofilen über 8K und Repo-Index; Live-Sichtbarkeit der Agenten und nicht blockierende UI; Transfer Host→VM per Copy-VMFile, VM→Host per Transfer-VHDX; Hardwareerkennung statt fester GPU-Annahmen. Zeitplan siehe `Umsetzungsplan.md`.

**Änderungen in 1.2:** Sammelfreigaben für exakt benannte Ziellisten mit Abwahl einzelner Einträge; Sandbox-VM wird bei jedem Sitzungsstart neu installiert, keine VM-Snapshots, die VM erhält nie eine Netzwerkkarte; Lizenzen für Windows-Gast und Toolchains stellt der Nutzer bereit; Oberfläche Deutsch, Agenten-Prompts Englisch.

**Änderungen in 1.3:** Rückweg VM → Host als Container direkt auf dem Rohgerät der Transfer-VHDX (bisher „Härtungsoption“); der Host liest die abgehängte VHDX-Datei mit eigenem, strengem Parser ohne Einbinden. Fertigmeldung des Gasts über einen einzigen KVP-Wert mit festem Format. Laufzeitbefehle über die Gruppe „Hyper-V-Administratoren“, nur der Neuaufbau mit Administratorrechten.

## 1. Herkunft, Ziel und Verbindlichkeit

Grundlage ist die Unterhaltung „Projektplan KI Coding Agent“, Gesprächs-ID `6ad248ac-7528-83eb-91b7-3ed4d3c51723`, einschließlich der aktuellen Anforderungen. Die historischen Nutzerbeiträge konnten gelesen werden. Frühere Assistentenantworten liegen im verfügbaren Export ausschließlich als Inhaltsreferenzen vor und konnten nicht rekonstruiert werden. Dieses Dokument behauptet daher keine vollständige Wiederherstellung dieser Antworten. Es konsolidiert alle verfügbaren Nutzeranforderungen und ergänzt sie um ausdrücklich vorgeschlagene technische Entscheidungen.

Ziel ist ein lokaler Chatbot, der neue und bestehende Softwareprojekte planen, entwickeln, testen, debuggen und prüfen kann. Modelle laufen lokal über Ollama. Der Mensch behält die Entscheidung über jeden Tool-Aufruf und jede Übernahme externer Webinhalte.

**MUSS** ist eine verbindliche Produktanforderung. **SOLL** bezeichnet den vorgesehenen Standard. **OPTIONAL** ist eine spätere Erweiterung. Alle konkreten Bibliotheken, Zeitpläne und Ressourcenbudgets sind Vorschläge und werden durch einen Prototyp validiert.

### Verbindliche Anforderungen

| ID | Anforderung |
|---|---|
| R01 | Windows 11, NVIDIA-GPU mit 16 GB VRAM, 64 GB RAM |
| R02 | Benutzeroberfläche sowie Approval Broker, Tool Broker und Audit in C#/.NET. Orchestrierung, Agenten, Model Gateway, Tool-Adapter, Web Fetcher, Content Gate und Sandbox-Worker in Python. Python-Komponenten besitzen keine Freigabeautorität |
| R03 | Planner, Coder, Tester, Debugger und Reviewer ab dem ersten funktionalen Agentenrelease vorhanden |
| R04 | Unabhängige Aufgaben parallel; abhängige Aufgaben anhand eines Aufgabengraphen geordnet |
| R05 | Explizite Nutzerfreigabe vor JEDEM Tool-Aufruf, auch Lesen, Suche, Statusabfrage und Test. Ein Aufruf darf eine exakt benannte, vollständig angezeigte Zielliste umfassen (Sammelfreigabe); einzelne Einträge sind vor der Freigabe abwählbar |
| R06 | Webabruf außerhalb der Ausführungssandbox; sanitisierten Inhalt vor Übergabe an Agenten separat genehmigen |
| R07 | Externe Inhalte bleiben untrusted; keine Berechtigungsänderung durch Prompt oder Modellantwort |
| R08 | Ausführungssandbox ohne Netzwerk, einschließlich Zugriff auf Host, LAN, Internet und Ollama |
| R09 | Ausschließlich explizit freigegebene Hostordner mit konkreten Rechten; keine implizite Vererbung auf andere Ordner |
| R10 | Lokale Speicherung, nachvollziehbare Ausführung, Abbruch, Wiederaufnahme und überprüfbare Änderungen |
| R11 | Agenten entwickeln, bauen, testen und debuggen in C#, Python, C++, HDevelop (MVTec HALCON), PowerShell und Batch; weitere Sprachen über Toolchain-Erweiterungen ohne Codeänderung am Kern |
| R12 | Toolchains werden vom Nutzer beschafft, in einem separaten Toolchain-Ordner abgelegt und erst nach sichtbarem, bestätigtem Import in der Sandbox verfügbar |
| R13 | Große Repositories ab dem MVP; Kontext nicht auf 8K begrenzt, sondern abhängig von gemessener Hardware; gezielter Zugriff über Repo-Index |
| R14 | UI blockiert nie; Denken (soweit vom Modell geliefert), Ausgaben, Toolvorschläge und Zustände jedes Agenten sind live sichtbar und einzeln abbrechbar. Antwortzeit ist zweitrangig |
| R15 | Keine festen Annahmen zu konkretem CPU- oder GPU-Modell im Code; Ressourcen werden erkannt und kalibriert |
| R16 | Benutzeroberfläche auf Deutsch; System- und Rollenprompts der Agenten auf Englisch |
| R17 | Die Sandbox-VM wird bei jedem Sitzungsstart aus einem Windows-Installationsabbild neu installiert und danach mit den benötigten Toolchains ausgestattet; keine VM-Snapshots; die VM wird bei Sitzungsende gelöscht |
| R18 | Lizenzen für das Gast-Windows (Vollversion oder Evaluierungsversion) und für proprietäre Toolchains stellt der Nutzer bereit; die Software prüft keine Lizenzbedingungen |

### Abgrenzung

Das MVP bearbeitet Repositories in C#, Python, C++, HDevelop, PowerShell und Batch sowie lokale Dateien. Es veröffentlicht, installiert oder versendet nichts automatisch. Git-Commit, Export, Paketinstallation und spätere externe Integrationen sind eigene Tools und verlangen jeweils eine Freigabe. Cloudmodelle und Telemetrie sind standardmäßig aus. Eine Modellantwort ist nie eine Sicherheitsentscheidung.

## 2. Nutzungsablauf

1. Nutzer legt Projekt an und wählt erlaubte Ordner sowie Rechte.
2. Die UI zeigt alle fünf Rollen. Der Planner erstellt aus bereits mitgeteilten Informationen einen Plan.
3. Benötigt ein Agent Repositorydaten, erzeugt er beispielsweise einen einzelnen `File.Read`-Vorschlag. Bis zur Freigabe wird nichts gelesen.
4. Nach genehmigtem Lesen entstehen ein Aufgabengraph und Änderungsvorschläge. Coder und Tester können unabhängige Module bzw. Testfälle entwerfen. Reviewer prüft abgeschlossene Diffs, seine Befunde korrigiert der Coder; Debugger analysiert vorhandene Fehlerdaten und übergibt die Diagnose dem Coder.
5. Jeder Schreib-, Build- oder Testaufruf erhält eine eigene Freigabekarte. Ein Build darf intern zahlreiche Dateien berühren; diese transitive Wirkung wird auf der Karte benannt und bleibt durch die Sandbox begrenzt.
6. Ausführung erfolgt in einer isolierten Arbeitskopie. Ergebnisse und Logs werden als untrusted Daten zurückgegeben.
7. Der Nutzer prüft Diff, Tests und Review. Die Übernahme in den ursprünglichen Projektordner ist ein eigener genehmigter Tool-Aufruf.

Freigegebene Ordner sind keine Dauerfreigabe für Tools. Ein genehmigter Plan ist keine Tool-Freigabe. Warten ohne Antwort bedeutet niemals Zustimmung.

## 3. Technologiestack und Lösungsstruktur

Vorgeschlagen:

- **C#-Teil:** .NET 10 LTS mit festgeschriebener, unterstützter Patchversion (`global.json`); WPF für die Desktopoberfläche; ein eigener Broker-Dienst (Approval Broker, Tool Broker, Audit) mit SQLite; xUnit für Tests. Die Microsoft-Supporttabelle ist die maßgebliche Quelle für Laufzeitpflege [S6].
- **Python-Teil:** gepinnte CPython-Version, eingebettet ausgeliefert; Abhängigkeiten über `uv` mit Lockfile; Pydantic für Verträge; pytest für Tests. Orchestrator, Web Fetcher und Content Gate laufen als getrennte Prozesse mit eigenen, eingeschränkten Windows-Identitäten.
- **Verträge:** einmalig als JSON Schema im Ordner `contracts/` definiert; C#-Records und Pydantic-Modelle werden daraus generiert. Kanonisches JSON und SHA-256 müssen in beiden Sprachen byte-identisch sein und werden mit gemeinsamen Testvektoren geprüft.
- **Kommunikation:** authentifizierte Named Pipes zwischen UI, Broker und Python-Prozessen, Sitzungstoken pro Start; keine TCP-Ports außer Ollama auf Loopback.

Die Sicherheitsdomäne liegt vollständig im C#-Broker und ist unabhängig von UI, Modellanbieter und Orchestrierung. Für das MVP genügt ein eigener kleiner DAG-Scheduler im Python-Orchestrator. Ein zusätzliches Agentenframework ist keine Voraussetzung.

```text
LocalAiDeveloper/
  contracts/                     JSON Schemas, Testvektoren für Hash/Kanonisierung
  src/dotnet/
    LocalAi.Desktop/             WPF: Freigaben, Diff, Chat, Live-Aktivität, Not-Aus
    LocalAi.Broker/              Approval Broker, Tool Broker, Policy, Dispatch
    LocalAi.SandboxController/   VM-Lebenszyklus, Copy-VMFile, Transfer-VHDX-Import
    LocalAi.Persistence/         SQLite, Artefakte, Audit
    LocalAi.Contracts/           generierte DTOs
  src/python/
    orchestrator/                Agenten, DAG-Scheduler, Kontextplanung
    model_gateway/               Ollama-Client, Streaming, Kalibrierung, Hardwareerkennung
    repo_index/                  Repo-Map, Symbol- und Volltextindex
    webfetcher/                  einziger Internetabrufprozess
    content_gate/                Parser, Sanitizer, Quarantäne
    worker/                      Sandbox-Worker und Tool-Adapter (läuft in der VM)
    contracts/                   generierte Pydantic-Modelle
  toolchains/                    Manifest-Schema und Beispielmanifeste
  tests/
    Unit/ Integration/ Security/ EndToEnd/ ModelEval/ CrossLanguage/
  docs/ config/ tools/offline-bootstrap/
```

Der Python-Orchestrator darf ausschließlich Toolvorschläge einreichen und Events lesen. Er hat keine Signierschlüssel, keinen direkten Zugriff auf Projektordner, kein Internet und keinen Entscheidungskanal. Jedes Ausführungstool läuft in der Sandbox, unabhängig davon, in welcher Sprache sein Adapter geschrieben ist.

## 4. Architektur und Vertrauensgrenzen

```mermaid
flowchart TB
  U[Nutzer] --> UI[WPF UI]
  UI --> APP[Python-Orchestrator und DAG Scheduler]
  APP --> AG[Planner / Coder / Tester / Debugger / Reviewer]
  AG --> MG[Model Gateway]
  MG --> OL[Ollama auf Host / nur Loopback]
  AG --> P[Tool-Vorschlag]
  P --> AB[Approval Broker]
  AB --> UI
  UI -->|einzelne explizite Entscheidung| AB
  AB --> TB[Tool Broker / erneute Policy-Prüfung]
  TB --> SC[Sandbox Controller]
  SC -->|signierter Job per Copy-VMFile| SW
  SW -->|Ergebnisse per Transfer-VHDX, read-only Import| SC
  subgraph VM[Isolierte VM / keine Netzwerkkarte]
    SW[Sandbox Worker]
    SW --> WS[isolierte Arbeitskopie]
  end
  TB --> WF[separater Web Fetcher]
  WF --> NET[Internet]
  WF --> Q[Quarantäne und Sanitizer]
  Q --> UI
  UI -->|Inhalt separat freigeben| CG[Content Gate]
  CG -->|untrusted Evidence| AG
  APP --> DB[SQLite und Audit]
  TB --> DB
```

UI, Approval Broker und Tool Broker sind C#; Orchestrator, Agenten, Model Gateway, Web Fetcher, Content Gate und Worker sind Python. Die Sandbox erhält keinen HTTP-Zugang zum Host. Ollama läuft außerhalb der Sandbox. Der Host übermittelt ausschließlich freigegebene Jobdaten; die Sandbox führt keine eigenen Modellaufrufe aus. Der Web Fetcher hat weder Zugriff auf Projektordner noch auf Ollama oder Freigabeschlüssel. Sein Netzwerkzugriff wird pro genehmigter URL begrenzt.

### Komponenten und Verantwortlichkeiten

| Komponente | Verantwortlichkeit | Darf nicht |
|---|---|---|
| Desktop | Nutzeridentität, Vorschau, Entscheidung, Not-Aus | selbst Modellausgaben ausführen |
| Orchestrator (Python) | Aufgaben, Agentenzustände, DAG, Kontextplanung | Tool Broker umgehen, Freigaben erteilen, Signierschlüssel halten |
| Model Gateway (Python) | ausschließlich lokale Inferenz, Streaming, Tokenbudget, Kalibrierung | Tools aus Modellantworten ausführen |
| Approval Broker (C#) | unveränderliche Anfrage, einmalige Capability | globale Auto-Freigaben anbieten |
| Tool Broker (C#) | Schema, Rechte, Preconditions, Dispatch | freie Adapter oder Toolchain-Aktionen nach Modellwunsch laden |
| Sandbox Controller (C#) | attestierte Offline-Umgebung, Copy-VMFile, Transfer-VHDX-Import, Toolchain-Import | still Netzwerk einschalten, Ergebnisdateien ausführen |
| Worker (Python, in VM) | genehmigten Job in isoliertem Scope ausführen | Hostpfade erreichen, eigene Laufzeit oder Toolchains verändern |
| Web Fetcher | begrenzte GET-Abrufe | Skripte, Logins, Downloads ausführen |
| Content Gate | sichere Darstellung und separate Inhaltsfreigabe | Sanitizing als Vertrauensbeweis behandeln |
| Persistence (C#) | Journaling und Wiederanlauf | Freigaben nach Neustart erneut verwenden |
| Repo Index (Python, in VM) | Repo-Map, Symbol- und Volltextindex je Snapshot | ohne Freigabe indizieren oder Index außerhalb des Snapshots nutzen |

## 5. Fünf Agenten und Parallelität

Alle Rollen werden beim Anlegen eines Runs instanziiert. „Vorhanden“ bedeutet eigene Identität, Inbox, Zustand, Arbeitsauftrag und Ergebnisvertrag; es bedeutet nicht fünf residente Modelle oder fünf ununterbrochene Generierungen.

| Rolle | Eingang | Ergebnis | Grenzen |
|---|---|---|---|
| Planner | Nutzerziel, genehmigte Evidenz | DAG, Abhängigkeiten, Akzeptanzfälle | genehmigt keine eigenen Aktionen |
| Coder | eingegrenzte Aufgabe, Basis-Snapshot; Diagnose des Debuggers bzw. Befunde des Reviewers | Patch-Vorschlag, Begründung | schreibt nur über Tool Broker; einzige Rolle, die Produktivcode ändert |
| Tester | Anforderungen, stabiler Snapshot | Testplan, Testpatch, Testbericht | kennzeichnet nicht ausgeführte Tests |
| Debugger | genehmigte Fehlerlogs, Repro (nur bei fehlgeschlagenem Build/Test bzw. OS-Debugging) | Hypothese, Diagnose für den Coder (Datei, Zeile, Ursache, was zu ändern ist) | ändert keinen Code; keine unbegrenzten Reparaturschleifen |
| Reviewer | Diff, Anforderungen, Testbericht | priorisierte Befunde mit Evidenz | darf Review nicht als Nutzerfreigabe ausgeben |

DAG-Knoten enthalten Eingabe-Snapshot, Lesemenge, Schreibmenge und Abhängigkeiten. Schreibaufgaben auf denselben Pfad werden serialisiert. Unbekannte Schreibmengen erhalten einen exklusiven Workspace-Lock. Tests laufen nur auf unveränderlichen Snapshots. Eine Dateiänderung invalidiert betroffene Test- und Reviewergebnisse.

Rollen laufen im Python-Orchestrator. Standard: maximal zwei unabhängige Sandboxjobs, maximal ein GPU-Inferenzrequest. Während ein Test läuft, kann ein anderer Agent lokal planen oder auf der GPU generieren. Echte parallele Inferenz ist ein optionales, vermessenes Profil; keine Voraussetzung für parallele Agentenarbeit.

Scheduler: Round-Robin mit Priorität für Nutzerinteraktion und abschließende Reviews; keine starvebaren Rollen. Pro Aufgabe höchstens drei erfolglose Reparaturversuche, dann Nutzerentscheidung. Limits: 20 Modellschritte pro Aufgabe, 30 Minuten aktive Laufzeit ohne neue Nutzerinteraktion, konfigurierbar. Warten auf Freigabe zählt separat und löst keine automatische Fortsetzung aus.

```mermaid
flowchart LR
  P[Planner: Aufgaben und Kriterien] --> C[Coder: Modul A]
  P --> T[Tester: Tests für Modul B]
  C --> B[genehmigter Build / Test auf Snapshot]
  T --> B
  B -->|Fehler| D[Debugger]
  D --> C
  B -->|grün| R[Reviewer]
  R -->|Befunde| C
  R --> F[Nutzer prüft und genehmigt Übernahme]
```

## 6. Tool-Freigaben als harte Sicherheitsinvariante

### Was als Tool gilt

Jeder Agentenzugriff auf externe Zustände oder Nebenwirkungen ist ein Tool: Datei lesen/suchen/auflisten/schreiben/löschen, Repositorystatus, Build, Test, Prozess, Webabruf, Paketbeschaffung, Export, Git und externe Dienste. Auch Read-only-Aufrufe benötigen Zustimmung. Automatische Dateiwatcher dürfen keine neuen Repositoryinhalte in den Agentenkontext einspeisen.

Reine Modellinferenz auf bereits zugelassenen Eingaben, UI-Rendering, In-Memory-Scheduling und internes Speichern des Betriebszustands sind Infrastruktur, keine Agententools. Diese begrenzte Definition wird in der UI erläutert. Infrastruktur darf weder Repositoryzugriff noch Webabruf tarnen. Downloads von Modellen sowie das Öffnen/Starten einer Sandbox sind sichtbare administrative Aktionen mit expliziter Nutzerbestätigung. Das Produkt bietet keinen Modus zur automatischen Toolfreigabe.

### Freigabekarte

MUSS zeigen: Rolle und Aufgabe, Tool und Version, Zweck, exakte normalisierte Parameter, absolute Zielpfade bzw. URL, Lesemenge/Schreibmenge, ausführbare Datei samt Hash, getrennte Argumentliste, Arbeitsverzeichnis, erlaubte Umgebungsvariablen, Sandbox-ID, Basis-Snapshot, erwartete Nebenwirkungen, Zeit-/Ausgabegrenzen, Ablaufzeit und Datenabfluss. Für Patches vollständiger Diff; für Prozesse Hinweis auf transitive Ausführung durch Buildskripte oder Tests.

Nur „Diesen Aufruf einmal erlauben“ und „Ablehnen“. Änderungen durch den Nutzer erzeugen einen neuen Vorschlag und eine neue Vorschau. Keine Freigabe für spätere, noch unbekannte Aufrufe; keine Checkbox „immer erlauben“.

### Sammelfreigabe

Ein einzelner Tool-Aufruf darf mehrere exakt benannte Ziele umfassen, zum Beispiel `File.ReadMany` mit einer Liste von Dateien samt erwarteten Hashes oder `Patch.Apply` mit Änderungen an mehreren Dateien. Die Karte zeigt die vollständige Liste; jedes Ziel hat ein Kontrollkästchen, standardmäßig ausgewählt, sowie „Alle abwählen“ und Filter nach Ordner bzw. Dateityp.

- Abwählen erzeugt sofort einen neuen, reduzierten Vorschlag mit neuem Hash; die Freigabe bindet genau an diese reduzierte Liste. Abgewählte Ziele werden nicht gelesen bzw. nicht geändert und dem Agenten als „vom Nutzer abgewählt“ gemeldet.
- Nachträgliches Erweitern der Liste ist ausgeschlossen; neue Ziele erfordern einen neuen Vorschlag.
- Sammelfreigaben sind für Lese-, Such-, Listen- und Patch-Aufrufe zulässig. Ausführende Tools (`Toolchain.*`, `Repo.Index`) und `Web.Fetch` bleiben Einzelaufrufe; ein Webabruf umfasst genau eine URL.
- Obergrenzen pro Sammelaufruf, z. B. 200 Dateien bzw. 4 MiB Gesamtgröße, konfigurierbar.
- Bei `Patch.Apply` wird pro Datei abgewählt; abgewählte Dateien bleiben unverändert, der Agent erhält den Hinweis und muss abhängige Änderungen neu planen.

### Zustände und Bindung

```mermaid
stateDiagram-v2
  [*] --> Proposed
  Proposed --> AwaitingApproval: Schema und Vorabpolicy OK
  Proposed --> Rejected: Policy verletzt
  AwaitingApproval --> Approved: Nutzer stimmt genauem Hash zu
  AwaitingApproval --> Denied: Nutzer lehnt ab
  AwaitingApproval --> Expired: Zeit abgelaufen
  Approved --> Running: Capability atomar konsumiert und erneut geprüft
  Approved --> Invalidated: Snapshot / Rechte / Parameter geändert
  Running --> Succeeded
  Running --> Failed
  Running --> Cancelled
```

Standardgültigkeit: fünf Minuten bis zum Start. Broker erzeugt eine kryptografisch zufällige, einmalige Capability, gebunden an Proposal-ID, Run-ID, authentifizierte Nutzersitzung, kanonischen Parameterhash, Toolversion, Policyrevision, Snapshot und Sandbox. Der Broker prüft und konsumiert sie transaktional beim Dispatch. Nur der Broker hält Signierschlüssel; Modelle und Worker besitzen keine Freigabeautorität.

Erneuter Aufruf nach Fehler, Timeout, Absturz oder verweigertem Zugriff braucht eine neue Freigabe. Keine automatischen Tool-Retries. Ein Testprozess darf innerhalb des genehmigten Aufrufs mehrere Testfälle ausführen, aber keine zusätzlichen Hosttools aufrufen. Die Genehmigung gilt für den ausgewiesenen Prozessbaum und dessen Sandboxscope, nicht für beliebige Folgeaktionen.

TOCTOU-Schutz: Für Lesen erwarteten Dateihash bzw. Snapshot binden; für Schreiben vorherigen Inhaltshash prüfen; für neue Dateien Nichtexistenz prüfen. Nach Abweichung neuer Vorschlag. Pfade vor und unmittelbar bei Zugriff anhand endgültiger Datei-Handles validieren. Reparse Points, Junctions, Symlinks, Alternate Data Streams, Gerätepfade, UNC-Pfade und Hardlinks werden im MVP abgelehnt. Ein bloßer String-Präfixvergleich genügt nicht.

Not-Aus stoppt Dispatch, invalidiert offene Capabilities und beendet aktive Prozessbäume. Bereits erfolgte externe Wirkungen lassen sich nicht pauschal zurückrollen; lokale Änderungen bleiben in der Arbeitskopie prüfbar.

## 7. Web und Schutz vor Prompt Injection

Zwei getrennte menschliche Entscheidungen sind zwingend:

1. **Fetch-Freigabe:** exakte URL, Host, Zweck, Größenlimit und Weiterleitungsregeln genehmigen.
2. **Content-Freigabe:** nach Abruf exakten sanitisierten Text samt Herkunft prüfen; erst dann diesen unveränderlichen Inhalt für benannte Empfänger freigeben.

```mermaid
sequenceDiagram
  participant A as Agent
  participant B as Broker
  participant U as Nutzer
  participant F as Web Fetcher
  participant G as Content Gate
  A->>B: Web.Fetch Vorschlag
  B->>U: URL und Grenzen anzeigen
  U->>B: einmal erlauben
  B->>F: genehmigten Abruf ausführen
  F->>G: Rohbytes in Quarantäne
  G->>G: isoliert parsen und sanitizieren
  G->>U: Textvorschau, Quelle, Warnmarkierungen, Hash
  U->>G: exakt diesen Text für Agent A freigeben
  G->>A: EvidenceBlock mit Untrusted-Kennzeichnung
```

Fetcher: HTTPS GET, keine Cookies oder Browserprofile, keine Authentifizierung, kein JavaScript, keine Formulare, keine externen Subressourcen. Standard: 2 MiB Antwort, 15 Sekunden, maximal 20.000 Zeichen nach Extraktion. Weiterleitungen werden angehalten und als neue URL separat genehmigt. Kein Hintergrund-Crawl und kein Prefetch von Links.

SSRF-Abwehr: private, Loopback-, Link-local-, Multicast-, reservierte und Metadatenadressen für IPv4/IPv6 sperren; sämtliche DNS-Antworten prüfen, Verbindung an geprüfte Zieladresse binden, TLS-Hostname prüfen, Proxy-/Umgebungsumleitungen deaktivieren. DNS-Rebinding, alternative IP-Schreibweisen und IPv4-in-IPv6 testen. Fehlende sichere Bindung bedeutet Abruch. Netzwerkausgang ist nur für diesen Fetchprozess zulässig.

Sanitizer entfernt Skripte, Styles, versteckte Elemente, Eventhandler, eingebettete Objekte, aktive Links und Steuerzeichen. Extraktion in einen inerten Plaintext-Viewer; HTML niemals direkt im privilegierten UI-WebView rendern. Links werden als Text mit Ziel angezeigt. Unicode-Bidi und unsichtbare Zeichen werden sichtbar markiert. Archive, Binärdateien und PDFs werden im MVP abgelehnt; spätere Parser laufen separat isoliert.

Inhaltsfreigabe bindet Rohhash, sanitisierten Hash, Sanitizerversion, Quell- und finale URL, Zeitpunkt, ausgewählte Textspanne, Empfängerrolle und Run. Jede nachträgliche Änderung oder erneuter Abruf benötigt neue Inhaltsfreigabe. Abgelehnte Inhalte bleiben außerhalb aller Agentenkontexte und Suchindizes.

**Sanitizing verhindert Prompt Injection nicht zuverlässig.** Auch normaler Text kann bösartige Anweisungen enthalten. Deshalb wird jeder EvidenceBlock als Datenquelle mit unveränderlichem Herkunftslabel transportiert. Er darf nicht Systemprompt, Tooldefinition, Policy, Freigabe oder Agentenrolle ersetzen. Ein Injection-Detektor liefert Hinweise, aber keine Sicherheitsgarantie. Die harten Schutzschichten bleiben Broker, Nutzerentscheidung und Sandbox.

Dasselbe gilt für README-Dateien, Kommentare, Testausgaben, Dateinamen und Antworten anderer Agenten. „Ignoriere Regeln“ in einer Datei bleibt Dateitext. Zusammenfassungen behalten die Herkunft ihrer Quellen; sie werden durch Zusammenfassung nicht trusted.

## 8. Offline-Sandbox und Ordnerrechte

### Produktionsziel

Vorgeschlagen ist eine dedizierte Hyper-V-VM der Generation 2 **ohne virtuelle Netzwerkkarte** und mit eingeschränktem Workerbenutzer. Die VM erhält zu keinem Zeitpunkt eine Netzwerkkarte, auch nicht während der Installation. Kein NAT, kein Default Switch, kein Hostloopback, keine Netzwerkfreigaben. Toolausführung auf dem Host ist verboten. Windows-Edition und Virtualisierung müssen bei Installation geprüft werden; falls die Isolation nicht bereitsteht, bleibt Ausführung gesperrt.

**Neuaufbau bei jedem Sitzungsstart (R17):** Beim Start der Oberfläche baut der Sandbox Controller die VM im Hintergrund neu auf, während Planung und Chat bereits nutzbar sind:

1. Neue VHDX erzeugen, Windows-Abbild aus dem vom Nutzer bereitgestellten ISO bzw. `install.wim` per DISM direkt anwenden, Bootdateien einrichten.
2. Offline in das Abbild schreiben: `unattend.xml` (lokales Konto, kein Netzwerk-/Konto-Schritt, Datenschutzoptionen aus), Worker-Python-Laufzeit, Installationspakete der für das Projekt benötigten Toolchains, Lizenzdateien, Provisionierungsskript.
3. VM ohne Netzwerkkarte starten; das Provisionierungsskript installiert die Toolchains unbeaufsichtigt und meldet Fertigstellung über die Transfer-VHDX.
4. Host prüft Installationsbericht und führt den Sicherheitsselbsttest aus; erst danach werden Jobs angenommen.
5. Beim Beenden der Oberfläche, bei Not-Aus mit Verdacht oder auf Knopfdruck wird die VM samt Festplatten gelöscht.

Es gibt keine VM-Snapshots und keine vorgefertigten Abbilder mit installierten Toolchains. Der Fortschritt des Aufbaus ist in der Oberfläche sichtbar; Werkzeuge, die die Sandbox benötigen, bleiben bis zum Abschluss gesperrt. Pro Projekt werden nur die ausgewählten Toolchains installiert, um die Aufbauzeit zu begrenzen.

Die VM nutzt eine eigene virtuelle Festplatte mit dem Gastbetriebssystem. „Nur freigegebene Ordner“ bezieht sich auf Zugriff auf Hostdaten; interne Betriebssystem- und temporäre Gastdateien sind für den Betrieb erforderlich und werden separat begrenzt. Host-Benutzerprofil, SSH-Schlüssel, Browserdaten und Hostmodellverzeichnis werden niemals eingebunden.

Datenaustausch erfolgt ausschließlich über zwei hostkontrollierte Offline-Wege. Keine SMB-Brücke, kein TCP-Forwarding, kein Hyper-V-Socket.

**Host → VM: Copy-VMFile.** Der Sandbox Controller überträgt signierte Jobpakete (Job-JSON, Patch bzw. Snapshot-Archiv) mit `Copy-VMFile -FileSource Host` in ein festes Eingangsverzeichnis des Gasts. Voraussetzung ist der Hyper-V-Gastdienst „Guest Service Interface“. Der Worker prüft Signatur, Sequenznummer, Größe und Wiederholungsschutz, bevor er etwas entpackt.

**VM → Host: Transfer-VHDX.** Eine zweite virtuelle Festplatte am SCSI-Controller dient ausschließlich der Ergebnisrückgabe:

1. Der Worker schreibt einen Container (Kopf mit Art, Sequenz, Länge und SHA-256, danach die Nutzlast) **direkt ab Sektor 0 auf das Rohgerät** – ohne Dateisystem. Er beschreibt nur ein Laufwerk exakt der erwarteten Größe, dessen erste 4 KiB leer sind; damit ist jedes Transfer-Laufwerk nur einmal nutzbar und das Systemlaufwerk ausgeschlossen. Danach flusht er und setzt den KVP-Wert `LocalAiSignal = result:<Sequenz>`.
2. Der Host hängt die VHDX im laufenden Betrieb ab (`Remove-VMHardDiskDrive`). Ist Hot-Detach nicht stabil, wird die VM vorher pausiert.
3. Der Broker liest die abgehängte VHDX-**Datei** mit einem eigenen, strengen Parser (Header- und Regionsprüfsummen, kein offenes Log, keine Elternlaufwerke, alle Offsets innerhalb der Datei, Größenlimit). Es wird nichts eingebunden; kein Dateisystemtreiber des Hosts berührt Gastdaten.
4. Der Container wird geprüft (Kennung, Version, Art, Länge, SHA-256, Sequenz passend zum Job) und danach als untrusted Ergebnis an die Ergebnisprüfung gegeben.
5. Die VHDX wird durch eine Kopie der leeren Vorlage ersetzt, nicht formatiert, und wieder angehängt.

Die Fertigmeldung über KVP ist der einzige Rückkanal außer der Transfer-VHDX; der Host akzeptiert dort nur `result:<Zahl>` bzw. `report:0`. Bleibt die Meldung aus, wird die Sandbox für die Sitzung gesperrt (keine Wiederholung). Ergebnisse gelten als untrusted; der Host führt Ergebnisdateien niemals aus.

### Rechteprofil – Beispiel

| Hostpfad | Gastbezug | Rechte | Durchsetzung |
|---|---|---|---|
| `D:\Projects\Example` | importierter Snapshot unter `C:\Work\repo` | Host: Lesen nach Einzelgenehmigung | kein direkter VM-Mount; genehmigter Snapshotimport |
| `D:\Agent\Runs\<run>\staging` | `C:\Work\repo` und Rückexport | Lesen, Erstellen, Ändern; Löschen nur explizit | Brokerrechte + getrennte Gastarbeitskopie |
| `D:\Agent\OfflinePackages` | `C:\Packages` | Lesen | beim VM-Aufbau offline ins Abbild geschrieben; im Gast für Jobs read-only |
| `D:\Agent\WindowsImage` | – | Lesen | vom Nutzer bereitgestelltes Windows-ISO bzw. `install.wim`; nur Quelle für den Neuaufbau |
| `D:\Agent\Toolchains\<name>-<version>` | `C:\Toolchains\<name>` | Lesen | nur nach bestätigter Registrierung; beim VM-Aufbau installiert; im Gast für Jobs read-only |
| `D:\Agent\Transfer` | Transfer-VHDX | Gast: Schreiben; Host: Lesen | read-only Mount ohne Laufwerksbuchstabe, Import in Outbox |
| `D:\Agent\Runs\<run>\outbox` | Ergebnisübergabe | Ergebnisdateien erstellen | eigenes Verzeichnis, Größen- und Typprüfung |
| alle anderen Hostpfade | keiner | keine | nicht eingebunden; Broker verweigert |

Ordnerfreigaben modellieren Read/List/Create/Modify/Delete/Execute getrennt, Rekursion explizit. Delete und Execute sind standardmäßig aus. Read-only-Paketcache bedeutet nicht, dass darin enthaltene Programme trusted sind. NuGet/MSBuild-Targets können beim Build Code ausführen und gehören in die VM.

Im Gast kann bösartiger Buildcode die für seinen Prozess beschreibbare Arbeitskopie verändern. Die Hostpolicy kann nicht jeden internen Dateizugriff eines beliebigen Programms einzeln kontrollieren. Deshalb erhält jeder Prozess nur den minimalen vollständigen Jobscope; strengere Rechte zwischen Jobs werden durch getrennte Benutzer/ACLs und frische Arbeitsverzeichnisse je Job durchgesetzt. Bei Verdacht auf Manipulation wird die VM vollständig neu aufgebaut. Nur ausdrücklich genehmigte Änderungen werden später ins Original übernommen.

Limits: 4–8 vCPUs als Startwert, 12 GiB RAM für Standardjobs, maximal 16 GiB bei großem Build (z. B. C++); Prozesslaufzeit standardmäßig 120 Sekunden, Testlauf maximal 10 Minuten nach sichtbarer Konfiguration; 10 MiB Logausgabe, 2 GiB Ergebnisquota. Prozessbäume über Job Objects im Gast begrenzen; VM dient als zusätzliche Abbruchgrenze. Geräte, Clipboard, Audio, Kamera, Drucker und Host-Laufwerke nicht durchreichen.

### Windows Sandbox als Prototyp

Windows Sandbox kann für einen frühen isolierten Einzellauf verwendet werden. Die dokumentierten `.wsb`-Optionen umfassen deaktiviertes Netzwerk und read-only bzw. read/write Ordner [S5]. Sie bieten keine feingranularen Create/Modify/Delete-Rechte pro Tool. Daher ist sie kein Ersatz für Brokerprüfung und isolierte Stagingkopien.

```xml
<Configuration>
  <Networking>Disable</Networking>
  <vGPU>Disable</vGPU>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <MemoryInMB>12288</MemoryInMB>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>D:\Agent\OfflinePackages</HostFolder>
      <SandboxFolder>C:\Packages</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>D:\Agent\Runs\demo\staging</HostFolder>
      <SandboxFolder>C:\Work</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
</Configuration>
```

Beispiel vor Nutzung an bestehende, ausdrücklich freigegebene Verzeichnisse anpassen. Keine automatische Ausführung durch `LogonCommand` im Beispiel. Die Konfiguration allein ist kein Nachweis für bestandene Isolationstests.

### Toolchains und Zielsprachen

Die Ausführung in der Sandbox ist über **Toolchains** erweiterbar. Der Nutzer lädt Werkzeuge selbst herunter und legt sie unter `D:\Agent\Toolchains\<name>-<version>\` ab, zusammen mit einer `toolchain.json`:

- Name, Version, Hashes aller Dateien, Zielpfad im Gast, Lizenzhinweis (Pflicht bei proprietären Toolchains)
- erlaubte Binaries mit festem Pfad und Hash
- Aktionen als Vorlagen mit typisierten Parametern: `restore`, `build`, `test`, `run`, `lint`, `format`; kein freier Shellstring
- Testergebnisformat: TRX, JUnit-XML, NUnit-XML oder Exit-Code mit Ausgabevergleich
- erlaubte Umgebungsvariablen und Offline-Paketquelle des Ökosystems
- Installationsschritt für den VM-Aufbau: Installer bzw. Archiv, unbeaufsichtigte Argumente, erwartete Dauer, Prüfbefehl nach der Installation
- optionale Lizenzdateien, die beim Aufbau ins Abbild geschrieben werden

Die **Registrierung** einer Toolchain ist eine sichtbare administrative Aktion mit Hashprüfung. Installiert wird sie bei jedem VM-Aufbau neu. Agenten sehen nur installierte Toolchains und deren deklarierte Aktionen und können keine eigenen definieren. Die Python-Laufzeit des Workers ist von der Python-Toolchain getrennt, mit der Agenten-Projekte gebaut und getestet werden; Jobs können die Worker-Laufzeit nicht verändern.

| Zielsprache | Toolchain (vom Nutzer bereitgestellt) | Build / Ausführung | Test | Lint / Format |
|---|---|---|---|---|
| C# | .NET SDK, NuGet-Offlinecache | `dotnet build` | `dotnet test` (TRX) | Roslyn-Analyzer, `dotnet format` |
| Python | CPython, Wheelhouse | Syntax-/Importprüfung | pytest (JUnit-XML) | z. B. ruff |
| C++ | MSVC Build Tools (Offline-Layout) oder LLVM/Clang; CMake, Ninja; vcpkg mit Offline-Binary-Cache | CMake-Presets | CTest (`--output-junit`), GoogleTest/Catch2 | clang-tidy, clang-format |
| HDevelop | MVTec HALCON (Runtime + HDevelop) | HDevEngine bzw. Kommandozeilenwerkzeug der installierten Version | eigener HDevEngine-Test-Runner mit Testbildern und Toleranzen (JUnit-XML); XML-Validierung und Lade-Test nach jedem Patch | – |
| PowerShell | Windows PowerShell 5.1 (Gast), PowerShell 7; Pester, PSScriptAnalyzer offline | Parserprüfung | Pester (NUnit/JUnit-XML) | PSScriptAnalyzer |
| Batch | `cmd.exe` (Gast) | Ausführung über `Toolchain.Run` | Harness (Pester oder pytest) prüft Exit-Code, Ausgabe, erzeugte Dateien | Regeln im Harness |
| Prüfprogramme der Testumgebung (`*.tproj.json`) | `testumgebung-cli` (eigenständiges ZIP aus dem Testumgebung-Repository) | `build`: Projekt prüfen, CodeBlocks übersetzen | Szenarien `*.tsim.json` in der Projektsimulation (JUnit-XML) | `check`: Struktur und Bausteinparameter |

PowerShell und Batch sind selbst Shells. Sie werden nur über `Toolchain.Run` mit festem Interpreter und einem Skriptpfad aus dem Snapshot gestartet; freie `-Command`- bzw. `/c`-Argumente sind ausgeschlossen. Der Skriptinhalt ist über den freigegebenen Diff bekannt.

**Lizenzen (R18):** Windows-Gast (Vollversion oder Evaluierungsversion), HALCON, Visual Studio/MSVC Build Tools und weitere proprietäre Werkzeuge lizenziert der Nutzer selbst. Lizenzdateien werden über das Toolchain-Manifest beim VM-Aufbau eingebracht. Bei einer Veröffentlichung der Software entscheidet jede Person selbst über die eingesetzten Toolchains und deren Lizenzen.

### Offline-Abhängigkeiten

SDKs, Testplattformen und notwendige Pakete aller Ökosysteme werden während einer separaten, nutzerbestätigten Provisionierung außerhalb der Ausführungssandbox beschafft, auf Versionen/Hashes geprüft und in ein read-only Offline-Repository übernommen. Webseitentext darf keine Pakete einschleusen. Paketdownloads sind ein eigener zukünftiger Tooltyp mit sichtbarer Provenienz.

Pro Ökosystem gilt: nur lokale Quellen (NuGet mit `<clear />` und Locked Mode, pip mit `--no-index --find-links`, vcpkg mit Offline-Binary-Cache, PowerShell-Module aus lokalem Repository). Im Gast zeigt `NuGet.Config` ausschließlich auf die lokale Quelle, alle geerbten Quellen werden mit `<clear />` entfernt. Restore nutzt Lockfiles und Locked Mode; Build und Test nutzen `--no-restore`. Bei fehlendem Paket stoppt der Job mit einem Beschaffungsvorschlag. Die Sandbox wird niemals vorübergehend online geschaltet. Netzwerkabhängige Tests werden durch lokale In-Process-Fakes ersetzt oder sichtbar als nicht ausführbar ausgewiesen.

## 9. Ollama, Modelle, Kontexte und Ressourcen

### Konkrete Startauswahl

| Profil | Modell | Verwendung | Kontext / max. Ausgabe |
|---|---|---|---|
| Standard | `qwen2.5-coder:14b`, Q4_K_M | zunächst alle fünf Rollen über separate Prompts | 8.192 / 2.048 Token |
| Leicht | `qwen2.5-coder:7b`, Q4_K_M | schwächerer Speicherzustand, kurze Tests und Änderungen | 8.192 / 2.048 Token |
| Planung experimentell | `qwen3:8b`, Q4_K_M | Planner und Reviewer nach Vergleichstest | 8.192 / 2.048 Token einschließlich ggf. Thinking-Budget |
| Erweitert | Standardmodell mit größerem Kontext | große Repositories, zusammenhängende Analyse | 16.384 / 4.096 Token, nach Kalibrierung |
| Groß | Standard- oder leichtes Modell | große Repositories | 32.768 Token oder mehr, nur wenn Kalibrierung Stabilität nachweist |
| Offload | größeres Modell mit CPU-Offload | Qualität vor Geschwindigkeit | nach Messung; Latenz ausdrücklich zweitrangig (R14) |

Die Modellnamen sind eine austauschbare Startkonfiguration. Der Code enthält keine festen Annahmen zu Modell, CPU oder GPU (R15): VRAM, RAM und GPU werden beim Start erkannt, Profile werden durch Kalibrierung freigeschaltet.

Die Registry nennt etwa 9,0 GB für 14B, 4,7 GB für 7B und 5,2 GB für Qwen3 8B; das sind Modelldateigrößen, keine vollständigen VRAM-Verbrauchsangaben [S1–S3]. Diese Modelle sind eine konservative Baseline, kein Anspruch auf die beste Modellqualität im Oktober 2026. Austausch wird durch lokale Evaluation entschieden. Modelltag, Digest, Quantisierung, Lizenz und Ollamaversion werden im Installationsmanifest festgeschrieben; `latest` ist unzulässig.

`qwen3-coder:30b` ist mit rund 19 GB Modelldatei kein Standard für vollständige GPU-Residenz auf 16 GB VRAM. Wenige aktive MoE-Parameter bedeuten nicht, dass nur diese Gewichte gespeichert werden. Da Antwortzeit zweitrangig ist, ist CPU-Offloading mit 64 GB RAM ein zulässiges, kalibriertes Profil, ohne Latenzversprechen [S7].

### Speicherplanung

Planwerte in **GiB**, keine Messwerte der konkreten GPU:

| Bereich | Standardbudget |
|---|---:|
| Windows/Display/GPU-Reserve | mindestens 2 GiB VRAM frei halten |
| Ollama Gewichte + KV + Compute | Ziel maximal 13–14 GiB VRAM gesamt |
| Windows und Hintergrundprogramme | 14 GiB RAM |
| Desktop, Host, Datenhaltung | 6 GiB RAM |
| Ollama CPU-Seite / Ladepuffer | 16 GiB RAM Reserve |
| Sandbox | 12 GiB RAM |
| verbleibender RAM-Puffer | 16 GiB RAM |

Die genaue RAM-Verteilung ist ein Admission-Budget und keine Zusicherung gleichzeitiger Belegung. Displaynutzung und Shared GPU Memory sind gesondert zu beobachten. Kein automatisches Überbuchen anhand freier Auslagerungsdatei.

KV-Speicher wächst grundsätzlich mit Kontext und parallelen Sequenzen. Näherung für konventionelle Attention: `2 × Layer × KV-Heads × Head-Dimension × Token × Byte-pro-Element`; Quantisierungsmetadaten, Compute-Puffer und Modellspezifika kommen hinzu. Aus Dateigröße allein lässt sich der maximale Kontext nicht seriös berechnen.

Startwerte für den Ollama-Dienst, nach Versionsprüfung und Neustart:

```text
OLLAMA_HOST=127.0.0.1:11434
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_NUM_PARALLEL=1
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0
OLLAMA_NO_CLOUD=1
```

Die Ollama-FAQ beschreibt Parallelität und Cachequantisierung; deren Unterstützung muss auf installierter Version und GPU geprüft werden [S4]. Wenn q8_0 oder Flash Attention nicht funktioniert, kontrolliert auf f16 und kleineren Kontext wechseln, nicht still Speichergrenzen erhöhen. `OLLAMA_CONTEXT_LENGTH` wird nicht global festgelegt; das Model Gateway setzt den Kontext pro Request entsprechend dem aktiven, kalibrierten Profil. 14B und 8B werden im Standard nicht gleichzeitig geladen. Modellwechsel werden gruppiert, ohne Rollen dauerhaft zu benachteiligen.

**Kontextaufteilung:** relativ zum aktiven Profil, Richtwerte 12,5 % System-/Rollenregeln, 12,5 % Aufgabe und Kurzverlauf, 44 % genehmigte Code-/Logevidenz, 25 % Ausgabe, 6 % Sicherheitsreserve; Regeln haben eine feste Untergrenze und werden nie gekürzt. Bei 8.192 Token entspricht das 1.024 / 1.024 / 3.584 / 2.048 / 512. Alle Kategorien zählen zusammen. Tokenzählung mit passendem Tokenizer; falls nicht verfügbar konservative Schätzung und zusätzliche Reserve. Kein stilles Abschneiden von Regeln. Große Dateien werden nach ausdrücklicher Lesefreigabe abschnittsweise verarbeitet. Der Kurzverlauf enthält die Rückmeldungen des Orchestrators zu jedem Schritt vollständig sowie die eigenen Notizen des Modells (Begründung, gewählte Aktion); ältere Notizen werden bei Platzbedarf zusammengefasst, nie die Rückmeldungen.

**Große Repositories:** Das freigabepflichtige Tool `Repo.Index` erzeugt in der Sandbox für einen Snapshot Dateibaum, Sprachverteilung, Symbolindex (tree-sitter; Roslyn für C#; XML-Parser für `.hdev`/`.hdvp`) und Volltextindex (SQLite FTS5). Daraus entsteht eine kompakte Repo-Map als Startkontext für den Planner; Agenten laden danach gezielt Abschnitte über `File.Read` mit Zeilenbereich. Der Index ist an den Snapshot-Hash gebunden; jede Änderung erfordert eine neue, freigegebene Indexierung. Kein automatisches Repositoryembedding im MVP; semantische Suche ist Erweiterung B13.

### Kalibrierung vor Produktivbetrieb

1. Modellversion und NVIDIA-Treiber dokumentieren; andere GPU-Last erfassen.
2. Jedes installierte Modell mit 4K, 8K, 16K und 32K Kontext (und darüber, falls sinnvoll), einem Request und identischen Aufgaben in mehreren Zielsprachen messen; Offload-Profile separat.
3. VRAM-Peak, RAM-Peak, CPU-Offload, Prompt- und Generierungsgeschwindigkeit sowie Zeit bis zur ersten Antwort erfassen. `ollama ps` und GPU-Monitoring werden im Produkt als administrative Diagnose sichtbar gestartet; agentenseitige Statusabfragen bleiben genehmigungspflichtige Tools.
4. 14B/8K freigeben, wenn mindestens 2 GiB VRAM Reserve verbleiben und zehn aufeinanderfolgende Läufe ohne OOM funktionieren. Andernfalls 7B/8K oder 14B/4K.
5. Jedes größere Kontextprofil nur aktivieren, wenn die gleiche Reserve und Stabilität gemessen wurden; Offload-Profile benötigen Stabilität, aber keine Latenzgrenze. Zwei Inferenzslots nur mit eigenem Benchmark; Standard bleibt einer.

Antwortzeit ist zweitrangig (R14). Verbindlich ist stattdessen: Die UI blockiert nie, Generierungen werden live gestreamt, und ein Watchdog meldet ausbleibende Tokens, sodass der Nutzer einzelne Requests abbrechen kann. Qualität und sichere Ausführung haben Vorrang vor Tokenrate.

## 10. Domänen-, API- und Persistenzmodelle

### Zentrale Verträge (C#-Skizze; Quelle ist das JSON Schema, Pydantic-Modelle werden identisch generiert)

```csharp
public enum AgentRole { Planner, Coder, Tester, Debugger, Reviewer }
public enum ApprovalState { Pending, Approved, Denied, Expired, Invalidated }
[Flags]
public enum FolderRight { None=0, List=1, Read=2, Create=4,
    Modify=8, Delete=16, Execute=32 }

public sealed record FolderGrant(Guid Id, string CanonicalRoot,
    FolderRight Rights, bool Recursive, long PolicyRevision);

public sealed record ToolProposal(Guid Id, Guid RunId, Guid TaskId,
    AgentRole Agent, string Tool, string ToolVersion,
    string CanonicalArgumentsJson, string ArgumentsSha256,
    string SnapshotId, long PolicyRevision, string SandboxId,
    DateTimeOffset ExpiresAt);

public sealed record UserDecision(Guid ProposalId, string ProposalSha256,
    ApprovalState Decision, string UserSessionId, DateTimeOffset At);

public sealed record EvidenceBlock(Guid Id, string Kind, string Source,
    string ContentSha256, string Text, bool Untrusted,
    Guid? ContentApprovalId, string[] DerivedFrom);

public enum ToolchainAction { Restore, Build, Test, Run, Lint, Format }

public sealed record ToolchainManifest(string Name, string Version,
    string GuestPath, IReadOnlyDictionary<string,string> FileSha256,
    IReadOnlyList<string> AllowedBinaries, string TestResultFormat,
    string? LicenseNote, string ManifestSha256);

public sealed record ToolResult(Guid InvocationId, Guid ProposalId,
    string Status, int? ExitCode, string SnapshotId,
    string[] ArtifactIds, string OutputSha256, bool Truncated);
```

Weitere Entitäten: Project, Run, AgentInstance, TaskNode, TaskDependency, Snapshot, Patch, Invocation, ApprovalCapability, WebCapture, ContentDecision, Artifact, AuditEvent, ModelProfile, HardwareProfile, Toolchain, RepoIndex und ActivityEvent. IDs sind UUIDs; Zeitstempel UTC mit UI-Anzeige in Nutzerzeitzone; Hashes SHA-256; DTOs besitzen `schemaVersion`. Zustände werden durch den Server validiert, niemals durch frei gelieferten Clienttext gesetzt.

### API-Vorschlag

Der C#-Broker ist der einzige API-Server. Kommunikation über authentifizierte Named Pipes mit per Sitzung erzeugtem Token; keine CORS-Freigabe. UI, Python-Orchestrator, Web Fetcher und Content Gate erhalten getrennte Identitäten; der Orchestrator darf nur Vorschläge einreichen, Events lesen und Aktivitätsereignisse (Streaming, Zustände) melden. Bei HTTP: Host-/Origin-Prüfung, Anti-CSRF für Browserzugriffe, keine Credentials in Logs. Agenten und optionales Python erhalten getrennte, eingeschränkte Identitäten. Nur der UI-Entscheidungskanal darf freigeben.

| Methode / Route | Bedeutung | Zugriff |
|---|---|---|
| `POST /v1/projects` | Projektmetadaten anlegen | UI |
| `PUT /v1/projects/{id}/folder-grants` | explizite Ordnerrechte ändern | UI; invalidiert betroffene Vorschläge |
| `POST /v1/runs` | Run mit fünf Rollen anlegen | UI |
| `POST /v1/tool-proposals` | unveränderlichen Vorschlag einreichen | Agent |
| `GET /v1/approvals/pending` | UI-Freigabequeue | UI |
| `POST /v1/approvals/{id}/decision` | konkrete Entscheidung mit Hash | ausschließlich UI |
| `POST /v1/content/{id}/decision` | sanitisierten Inhalt zulassen/ablehnen | ausschließlich UI |
| `GET /v1/runs/{id}/events` | sequenzierter Eventstream | UI, Orchestrator (lesend) |
| `POST /v1/runs/{id}/activity` | Live-Aktivität (Tokens, Thinking, Zustände) melden | Orchestrator; reine Anzeige, keine Zustandsänderung an Freigaben |
| `POST /v1/toolchains/register` | Toolchain aus Toolchain-Ordner registrieren | ausschließlich UI, administrative Aktion |
| `POST /v1/sandbox/rebuild` | VM verwerfen und neu aufbauen | ausschließlich UI, administrative Aktion |
| `POST /v1/approvals/{id}/narrow` | Ziele einer Sammelfreigabe abwählen; erzeugt neuen Vorschlag | ausschließlich UI |
| `POST /v1/runs/{id}/cancel` | Dispatch stoppen und aktive Jobs abbrechen | UI |
| `POST /v1/runs/{id}/resume` | sicheren Zustand wieder aufnehmen | UI; keine alten Freigaben |

Es gibt keine öffentlich erreichbare `execute(command)`-Route. Dispatch erfolgt brokerintern. HTTP 409 bei Snapshot-/Hashkonflikt, 403 bei Policyverstoß, 410 bei abgelaufener Freigabe, 422 bei ungültigem Schema, 429 bei Ressourcenlimit. API-Idempotenz verhindert doppelte Vorschläge und Entscheidungen; sie wiederholt niemals eine Ausführung.

### Toolkatalog im MVP

| Tool | Eingabe | Grenze |
|---|---|---|
| `File.List` | grantId, relativer Pfad, Tiefe | keine Inhalte, Anzahl begrenzt |
| `File.Read` | Pfad, erwarteter Hash, Zeilenbereich | z. B. 256 KiB |
| `File.ReadMany` | Liste aus Pfad, erwartetem Hash, Zeilenbereich | Sammelfreigabe mit Abwahl; max. 200 Dateien / 4 MiB |
| `File.Search` | genehmigte Pfade, Suchtext | keine globale Laufwerkssuche |
| `Patch.Apply` | Basis-Snapshot, vollständiger Patch | nur isolierte Arbeitskopie; Abwahl pro Datei |
| `Repo.Index` | Snapshot, Sprachen | nur im Snapshot; Index an Hash gebunden |
| `Toolchain.Restore` | Toolchain, Projekt/Lockfile | nur lokale Paketquelle |
| `Toolchain.Build` | Toolchain, Projekt, Konfiguration | festes Binary aus Manifest, kein Shellstring |
| `Toolchain.Test` | Toolchain, Projekt, Filter, Timeout | offline, stabiler Snapshot |
| `Toolchain.Run` | Toolchain, Skript-/Programmpfad, typisierte Argumente | fester Interpreter; keine freien Kommandos |
| `Toolchain.Lint` | Toolchain, Pfade | nur lesend auf Snapshot |
| `Web.Fetch` | exakte HTTPS-URL | Quarantäne; keine direkte Agentenrückgabe |
| `Workspace.ExportPatch` | Diff, Zielgrant, Vorher-Hashes | explizite Übernahme |

Ein generisches Shelltool ist kein MVP-Bestandteil; PowerShell und Batch sind nur über `Toolchain.Run` mit Skriptpfad erreichbar. Auch typisierte Buildtools können über MSBuild-Targets beliebigen Gastcode starten; deshalb werden sie nicht als harmlos eingestuft. Argumente mit `ProcessStartInfo.ArgumentList`, feste Binarypfade und minimale Umgebung verhindern zusätzliche Shellinterpretation, ersetzen aber keine VM-Isolation.

### Datenhaltung

SQLite speichert Beziehungen und Zustände; große Texte und Artefakte liegen content-addressed außerhalb von Projektordnern. Transaktional: Approvalverbrauch + Invocationanlage + Dispatch-Outbox. Der Dispatcher verarbeitet jeden Eintrag höchstens einmal; nach unklarem Absturzstatus keine erneute Ausführung, sondern Zustand „OutcomeUnknown“ und Nutzerprüfung.

Audit enthält Event-ID, Sequenz, Run, Agent, Tool-/Parameterhash, Entscheidung, Nutzeridentität, Policyrevision, Start/Ende und Ergebnis. Hashverkettung erkennt nachträgliche Änderungen nur zusammen mit einem getrennt geschützten Checkpoint; sie schützt nicht gegen einen vollständig kompromittierten Hostadministrator. Schlüssel über Windows-Datenschutzmechanismen und Benutzer-ACLs schützen. Geheimnisse redigieren, Rohwebinhalte nicht unbeschränkt loggen.

Standardaufbewahrung als Vorschlag: Metadaten 90 Tage, Rohwebquarantäne 7 Tage, große Logs 30 Tage; Nutzer kann exportieren und löschen. Löschung ist eine sichtbare administrative Aktion. Projektcode verlässt den Host nicht über den Web Fetcher.

## 11. UI-Mockup und Interaktionen

```text
+----------------------------------------------------------------------------------+
| Local AI Developer | Projekt: Example | SANDBOX BEREIT (offline)  | [NOT-AUS]        |
+-------------------+-------------------------------------+--------------------------+
| AGENTEN           | CHAT UND PLAN                       | FREIGABEN (2)            |
| Planner   bereit  | Du: Ergänze CSV-Import.              | Coder · File.Read        |
| Coder     wartet |                                     | src/Importer.cs          |
| Tester    aktiv  | Plan:                               | Zeilen 1–160 · nur Lesen |
| Debugger  bereit | 1. Schnittstelle prüfen              | Snapshot a81f...         |
| Reviewer  bereit | 2. Parser + Grenzfalltests           | [Vorschau]               |
|                   | 3. Build, Review, Übernahme          | [Einmal erlauben]        |
| GPU: 1 Slot       |                                     | [Ablehnen]               |
| Kontext: 8K       | Tester entwirft unabhängige Fälle.   |                          |
+-------------------+-------------------------------------+--------------------------+
| [Diff] [Tests] [Aktivität] [Audit] [Ordnerrechte] [Toolchains] [Web] [Modelle]       |
| Tests: noch nicht ausgeführt | Originalprojekt: Übernahme ausstehend               |
+----------------------------------------------------------------------------------+
```

**Sprache:** Alle Texte der Oberfläche sind Deutsch und liegen in Ressourcendateien, sodass weitere Sprachen ergänzt werden können. System- und Rollenprompts der Agenten sind Englisch; Agenten antworten im Chat auf Deutsch, sofern der Nutzer nichts anderes verlangt. Code, Commit-Texte und Kommentare folgen den Konventionen des jeweiligen Repositorys.

**Sammelfreigabe-Karte:** Liste aller Ziele mit Kontrollkästchen, Größe und Hash, Filter nach Ordner und Dateityp, Zähler „17 von 23 ausgewählt“. Erst nach Abschluss der Auswahl wird „Auswahl einmal erlauben“ aktiv.

**Sandbox-Status:** Kopfzeile zeigt „VM wird aufgebaut (Schritt 3/5: Toolchains)“, „bereit“ oder „gesperrt“; Knopf „VM neu aufbauen“.

**Live-Aktivität:** Ein Klick auf einen Agenten öffnet seinen Aktivitätsfeed mit Status, aktueller Aufgabe, gestreamter Ausgabe, Thinking (falls vom Modell geliefert), Toolvorschlägen, Wartezuständen, Token/s und verstrichener Zeit. Inhalte werden inert als Text gerendert. Ein Watchdog markiert ausbleibende Tokens als „hängt?“ mit Abbruchmöglichkeit; Abbruch ist pro Agent und global möglich. Die UI blockiert nie: Eventstream gepuffert, Rendering gedrosselt, lange Listen virtualisiert. Ein Bereich „Toolchains“ zeigt installierte Toolchains und den Importstatus.

Webfreigabe zeigt Quelle, Abrufzeit, sichtbare Textgrenzen, markierte verdächtige Passagen und Empfänger. Buttons: „Diesen Text an Coder übergeben“ / „Ablehnen“. Kein aktives HTML, kein automatisches Öffnen enthaltener Links.

Freigabevorschau darf Tooltexte nicht als UI-Anweisung rendern. Exakte Pfade und Argumente werden kopierbar angezeigt. Fokus liegt standardmäßig auf Ablehnen oder einem neutralen Element; Enter genehmigt nicht versehentlich. Vollständige Tastaturbedienung, Screenreader-Beschriftung, ausreichender Kontrast und Statusanzeige ohne reine Farbcodierung sind Abnahmekriterien. Während einer Freigabe bleibt der übrige Run sichtbar.

## 12. Sicherheitsregeln und Bedrohungsmodell

Geschützt werden Hostdateien, Geheimnisse, Netzwerkgrenzen, Nutzerentscheidungen, Originalrepository und Auditintegrität. Angreiferquellen sind Webtext, Repositorycode, Buildtargets, Pakete, Modellantworten und manipulierte Workerresultate. Ein kompromittierter Windows-Kernel oder Hostadministrator liegt außerhalb der zugesicherten Schutzgrenze.

| Angriff | Gegenmaßnahme | verbleibende Grenze |
|---|---|---|
| Prompt Injection aus Web/README | Herkunftslabel, getrennte Rollen, Broker, einzelne Freigabe | Nutzer kann schädlichen Vorschlag dennoch genehmigen |
| Toolargument nach Freigabe geändert | unveränderlicher Hash, atomarer Consume | Hostkompromittierung außerhalb Scope |
| Pfadausbruch / Junction-Race | Handleprüfung, Snapshot, Reparse-Verbot, VM | Implementierung muss adversarial getestet werden |
| Exfiltration durch Tests | VM ohne NIC, keine Geheimnisse, begrenzte Mounts | Ausgabe kann genehmigte Daten enthalten; Export sichtbar |
| SSRF / DNS-Rebinding | geprüfte Ziel-IP-Bindung, Sperrbereiche | Fetcher darf nur freigegebene öffentliche Ziele erreichen |
| Bösartige Buildtargets | keine Hostausführung, Quoten, Gastidentität | beschreibbare Gastarbeitskopie bleibt angreifbar |
| Freigabereplay / Doppelklick | einmalige Capability, Transaktion, Nonce | kein automatischer Retry |
| Gefälschtes Testergebnis | Resultatlabel, reproduzierbarer Snapshot, eigene Auswertung | Tests sind Evidenz, kein Beweis allgemeiner Korrektheit |
| Denial of Service | Token-, Zeit-, Speicher-, Prozess- und Disklimits | Nutzer muss bei Limits umplanen |
| Kompromittierter Python-Orchestrator | eigener Prozess und Identität, nur Vorschlagsrechte, keine Schlüssel | kann schädliche Vorschläge erzeugen; Nutzer entscheidet |
| Manipulierte Transfer-VHDX | read-only Mount ohne Laufwerksbuchstabe, eigener Importprozess, Manifest- und Größenprüfung | Dateisystemparser des Hosts bleibt Angriffsfläche |
| Manipulierte Toolchain | Hashprüfung bei Registrierung und vor jedem VM-Aufbau, read-only im Gast | vom Nutzer beschaffte Software ist nicht automatisch vertrauenswürdig |
| Persistenz eines Angreifers in der VM | Neuaufbau bei jedem Sitzungsstart, keine Snapshots, frische Arbeitsverzeichnisse je Job | innerhalb einer Sitzung können Jobs denselben Gast sehen; Rechtetrennung über Benutzer/ACLs |
| Unbemerkt erweiterte Sammelfreigabe | Freigabe an Hash der exakten, ggf. reduzierten Liste gebunden | Nutzer kann zu viele Dateien auswählen |
| Kommandoinjektion über PowerShell/Batch | nur `Toolchain.Run` mit Skriptpfad, typisierte Argumente, keine freien Kommandos | Skriptinhalt selbst kann bösartig sein; läuft nur in der VM |
| Abweichende Hashes C#/Python | gemeinsame Testvektoren, Kreuztests | Fehler führen zu Ablehnung, nicht zu Ausführung |

Weitere Invarianten: deny by default; keine versteckten Netzwerkrückfälle; kein `eval` auf Modelltext; kein selbsttätiges Pluginladen; keine Richtlinienänderung durch Agenten; keine Weitergabe von Freigabetokens an Prompts; keine unkontrollierte Vererbung von Hostumgebung und Credentials. Sicherheitskonfiguration ist gegenüber Agenten read-only.

## 13. Installation und reproduzierbarer Start

### Voraussetzungen

Windows 11 Pro mit Hyper-V und Gastdiensten, aktivierte Hardwarevirtualisierung, ausreichend freier SSD-Speicher (Planwert 200 GiB für Modelle, Windows-Abbild, VM, Toolchains, Pakete und Arbeitskopien), ein vom Nutzer bereitgestelltes Windows-Installationsabbild samt Lizenz oder Evaluierungsversion, NVIDIA-Treiber, .NET-10-SDK, gepinnte Python-Laufzeit mit `uv` und Ollama. Toolchains für die Zielsprachen beschafft der Nutzer selbst. Versionsstände vor Download anhand offizieller Quellen prüfen. Installation und notwendige Administratoraktionen erfolgen bewusst durch den Nutzer; Agenten erhalten keine Administratorrechte.

### Reihenfolge

1. Treiber und Virtualisierung prüfen; sichere Baseline des Hosts erstellen.
2. .NET-SDK und Ollama installieren; Versionen, Installerhashes und Quellen protokollieren.
3. Modelle bewusst herunterladen; keine Cloudtags wählen:

```powershell
ollama pull qwen2.5-coder:14b
ollama pull qwen2.5-coder:7b
# Optional nach Baseline-Evaluation:
ollama pull qwen3:8b
```

4. Ollama-Startkonfiguration aus Abschnitt 9 anwenden (ohne globale Kontextlänge) und Dienst neu starten. Bindung ausschließlich an Loopback prüfen; Cloud-/Telemetrieoptionen versionsbezogen prüfen und deaktivieren.
5. Windows-ISO bzw. `install.wim` in `D:\Agent\WindowsImage` ablegen. Eine manuelle VM-Vorbereitung entfällt; die Anwendung baut die VM bei jedem Start selbst auf (§8).
5a. Toolchains (C#, Python, C++, PowerShell, Batch, HDevelop) mit Installern, Lizenzdateien und `toolchain.json` in `D:\Agent\Toolchains` ablegen und über die UI registrieren.
6. Hostordner für Staging, Pakete, Artefakte und Audit mit getrennten ACLs anlegen. Noch keinen Zugriff auf Originalprojekte vergeben.
7. Anwendung aus künftig signiertem Release installieren oder nach Implementierung aus dem Repository bauen. Dieses Dokument selbst enthält noch keinen lauffähigen Installer oder Quellcode.
8. Beim ersten Start Nutzeridentität, Rechte und Ressourcenprofil festlegen. Fünf Rollen müssen sichtbar sein, auch wenn alle zunächst „bereit“ anzeigen.
9. Sicherheitsselbsttest ausführen: kein Tool ohne Entscheidung; kein Netzwerk aus VM; kein Zugriff außerhalb genehmigter Ordner. Bei Fehler Ausführung sperren.
10. Modellkalibrierung aus Abschnitt 9 und Demo aus Abschnitt 16 durchführen; Manifest speichern.

Beispiel der späteren lokalen Paketkonfiguration:

```xml
<configuration>
  <packageSources>
    <clear />
    <add key="offline" value="C:\Packages" />
  </packageSources>
</configuration>
```

Die Erstbeschaffung des Offlinecaches geschieht vor dem Offlinebetrieb in einem getrennten Provisionierungsschritt. Fehlende Pakete sind ein erwarteter, sichtbarer Fehlerzustand.

## 14. Priorisiertes Backlog und Roadmap

Reihenfolge ist verbindlicher als die grobe Schätzung. Aufwand als Personentage für eine erfahrene Entwicklerin bzw. einen erfahrenen Entwickler, ohne Garantie und ohne Wartezeiten; Sicherheitsreview kann zusätzlichen Aufwand verursachen.

| ID | Priorität | Arbeitspaket | Abhängigkeit | Aufwand | Fertig, wenn |
|---|---|---|---|---:|---|
| B01 | P0 | JSON-Schema-Verträge (C# + Python), Bedrohungsmodell, Sicherheitsinvarianten | – | 4 | Anforderungen auf Tests abgebildet; Hash-Kreuztest grün |
| B02 | P0 | Approval Broker, atomare Capabilities, Audit (C#) | B01 | 5 | Read ohne Freigabe unmöglich; Replaytests grün |
| B03 | P0 | deutsche UI mit exakter Vorschau, Einzel- und Sammelfreigabe mit Abwahl (C#) | B02 | 6 | Hash und Pfad sichtbar; kein Autoapprove; Abwahl erzeugt neuen Hash |
| B04 | P0 | VM-Neuaufbau (DISM + unattend, ohne NIC), Copy-VMFile, Transfer-VHDX | B01 | 11 | unbeaufsichtigter Aufbau; Hostausbruch- und Transfertests grün |
| B16 | P0 | Toolchain-System, Registrierung, Installation beim VM-Aufbau; Toolchains C# und Python | B04 | 5 | zwei Sprachen bauen/testen in der VM |
| B05 | P0 | Rechteprüfung, Snapshots, File-Tools | B02–B04 | 5 | Pfad-/TOCTOU-Tests grün |
| B06 | P0 | Python-Orchestrator, Model Gateway, Kalibrierung, Hardwareerkennung | B01, B05 | 5 | Profile 8K–32K+ gemessen; Streaming und Abbruch |
| B07 | P0 | fünf Rollen und DAG-Scheduler (Python) | B05–B06 | 5 | alle Rollen aktivierbar; unabhängige Jobs parallel |
| B17 | P0 | Live-Aktivität, Watchdog, nicht blockierende UI | B07 | 3 | UI flüssig unter Last; Abbruch pro Agent |
| B08 | P0 | Patch.Apply, Toolchain.Restore/Build/Test/Run sprachneutral | B04–B07, B16 | 7 | E2E auf isoliertem Snapshot in C# und Python |
| B12 | P0 | große Repositories: Repo.Index, Repo-Map | B05–B08 | 6 | großes Repo indiziert; gezielter Zugriff |
| B19 | P0 | Toolchains C++, PowerShell, Batch | B08 | 5 | Build/Test je Sprache in der VM |
| B20 | P0 | Toolchain HDevelop (HALCON) | B08 | 4 | HDevEngine-Tests grün |
| B09 | P0 | Web Fetcher, SSRF-Schutz, Content Gate (Python) | B02–B03 | 7 | kein Inhalt vor zweiter Entscheidung |
| B10 | P0 | Diffübernahme, Abbruch und Wiederanlauf | B08 | 4 | Original nur nach separater Freigabe geändert |
| B11 | P0 | Sicherheits-/E2E-Abnahme, Dokumentation | B01–B20 | 9 | alle Musskriterien nachgewiesen |
| B13 | P1 | Modellvergleich, Embeddings, Dualslot | B11 | 3–5 | gemessener Nutzen ohne Sicherheitsregression |
| B14 | P1 | Gittools, kontrollierte Paketbeschaffung | B11 | 4–7 | eigene Vorschau und Freigabe je Aktion |
| B18 | P2 | Linux-Gast-VM als zweite Sandbox | B11 | 5–8 | gleiche Transferwege und Tests |

Die frühere Erweiterung B15 (optionale Python-Orchestrierung) entfällt, da Python Kernbestandteil ist. Zeitplan mit Kalenderdaten: siehe `Umsetzungsplan.md` (Start 19.10.2026, MVP 09.03.2027, mit Puffer 18.03.2027, eine Person in Vollzeit). Alle fünf Rollen werden gemeinsam mit B07 integriert, nicht als spätere funktionale Erweiterung.

### Nächster konkreter Implementierungsschritt

Ein Sicherheitsdurchstich mit **einem einzigen Tool `File.Read`**, Fake-Modell und echtem Approval Broker:

1. Repository mit `contracts/`, C#-Projekten (Broker, Desktop, Contracts, Tests) und Python-Paketen (orchestrator, contracts, Tests) anlegen; Codegenerierung aus JSON Schema einrichten.
2. Unveränderliches Proposal mit Pfad, Hash, Zeilenbereich, Policyrevision und Ablaufzeit erstellen.
3. Ein Fake-Agent im Python-Orchestrator reicht den Vorschlag über die Named Pipe ein; die UI zeigt ihn exakt an; ein Testadapter zählt Ausführungen.
4. Nach einmaliger Entscheidung den Broker transaktional konsumieren lassen.
5. Folgende Fälle müssen vor echter Dateiausführung grün sein: keine Entscheidung = null Aufrufe; Ablehnung = null; Parameteränderung = null; Replay = null zusätzliche; zwei gleichzeitige Dispatches = genau einer; Neustart = alte Freigabe ungültig; Python-Prozess ruft Entscheidungsendpunkt = verweigert; C#- und Python-Hash identisch.
6. Danach echten Readadapter ausschließlich über die Offline-Sandbox anbinden. Erst anschließend Modelle mit Toolvorschlägen verbinden.

Dieser Schritt verhindert, dass spätere Agentenfunktionen auf einer lediglich promptbasierten Freigabelogik aufbauen.

## 15. Teststrategie und Nachweise

### Testebenen

- Unit: kanonisches JSON, Hashbindung, Rechteberechnung, DAG, Zustandswechsel, Tokenbudget, Sanitizerausgabe.
- Integration: SQLite-Transaktion, Broker/Worker-Verträge, Ablaufzeiten, Named-Pipe-Identität, Offlinepakete, Ergebnisquarantäne.
- Security: manipulierte Vorschläge, Race Conditions, DNS-Rebinding, Pfadvarianten, Workerresultate, Prompt Injection und Netzwerkausbruch.
- Cross-Language: kanonisches JSON und Hashes C# ↔ Python mit gemeinsamen Testvektoren; generierte Verträge aktuell.
- E2E: Nutzerauftrag → fünf Rollen → separate Freigaben → Patch → Offlinebuild → Tests → Review → genehmigte Übernahme, für jede Zielsprache.
- Modellevaluation: festes C#-Aufgabenset, kompilierbare Patches, Regressionsquote, Toolschema-Konformität und Umgang mit untrusted Inhalten. Modellqualität und Brokerisolation getrennt bewerten.

### Verbindliche Prüffälle

| Test | Angriff / Szenario | Erwartung |
|---|---|---|
| SEC-01 | Agent fordert File.Read ohne Entscheidung | Worker erhält keinen Job |
| SEC-02 | Freigabe für A, Argument wird zu B | Hashfehler, keine Ausführung |
| SEC-03 | zwei Dispatcher / wiederholte Capability | exakt eine Invocation |
| SEC-04 | Datei nach Vorschau verändert | Konflikt, neue Freigabe |
| SEC-05 | `..`, Junction, ADS, UNC, Gerätepfad | verweigert |
| SEC-06 | HTTP/HTTPS/DNS/IPv6 aus Gast zu Host/LAN/Internet | alle scheitern; fehlende NIC bestätigt |
| SEC-07 | Build versucht Browserprofil/SSH-Key zu lesen | Daten nicht vorhanden/zugänglich |
| SEC-08 | Webtext fordert Freigaben oder Dateiexport | nur Text; keine Rechteänderung |
| SEC-09 | Fetch genehmigt, Content nicht genehmigt | keine Bytes im Agentenkontext |
| SEC-10 | Text nach Content-Freigabe verändert | Hashfehler, erneute Prüfung |
| SEC-11 | öffentliche URL lenkt auf private IP um | Redirect stoppt; private Ziele gesperrt |
| SEC-12 | Timeouts oder Hostabsturz während Tool | kein automatischer Retry |
| SEC-13 | bösartiges NuGet-Target startet Gastprozess | nur genehmigter VM-Scope; kein Netz/Hostzugriff |
| SEC-14 | Log enthält HTML/ANSI/Bidi/Anweisung | inerte, begrenzte Darstellung |
| SEC-15 | Not-Aus bei laufendem Test | Dispatch gesperrt, Prozessbaum beendet |
| SEC-16 | Standardmodell zehnmal mit Zielkontext | kein OOM, Reserven dokumentiert |
| SEC-17 | Agent oder Python-Orchestrator ruft UI-Decision-Endpunkt auf | Authentifizierung/Autorisierung verweigert |
| SEC-18 | Transfer-VHDX mit übergroßen Dateien, falschen Hashes, `..`-Namen oder beschädigtem Dateisystem | Import verweigert, nichts ausgeführt |
| SEC-19 | Toolchain mit verändertem Datei-Hash importieren | Import verweigert |
| SEC-20 | Agent fordert nicht deklarierte Toolchain-Aktion oder freies PowerShell-/Batch-Kommando | Schemafehler, keine Ausführung |
| SEC-21 | Job versucht Worker-Laufzeit oder Toolchain im Gast zu verändern | Zugriff verweigert; spätestens beim nächsten VM-Neuaufbau verworfen |
| SEC-22 | Python-Orchestrator abgestürzt oder hängt | kein Tool ohne Freigabe; UI bleibt bedienbar; Not-Aus wirkt |
| SEC-23 | Sammelfreigabe: abgewählte Datei, nachträglich ergänzte Datei, geänderter Hash einer Datei | abgewählte und ergänzte Dateien werden nicht gelesen; Hashabweichung verwirft den Aufruf |
| SEC-24 | VM-Aufbau: Netzwerkkarte zu irgendeinem Zeitpunkt vorhanden, Toolchain-Hash abweichend oder Installationsbericht fehlt | Aufbau abgebrochen, Ausführung gesperrt |

Netzwerkisolation wird durch Konfigurationsinspektion und aktive Probes nachgewiesen; ein einzelner fehlgeschlagener Ping genügt nicht. Freigabeinvarianten werden zusätzlich mit konkurrierenden und propertybasierten Tests geprüft. Alle Sicherheitsprüfungen laufen mit Fake-Modellen deterministisch; reale Modelle ergänzen adversariale Fälle.

## 16. Acceptance Criteria und Definition of Done

| Kriterium | Abnahmeevidenz |
|---|---|
| AC01 – R01/R02 | Installation auf Windows-11-Pro-Zielhardware; UI und Broker in C#, Orchestrierung und Tools in Python; Manifest mit Versionen |
| AC02 – R03/R04 | alle fünf Rollen ab Runstart sichtbar; zwei unabhängige Aufgaben überlappen zeitlich; konfligierende Writes serialisiert |
| AC03 – R05 | jeder Agententoolaufruf besitzt genau eine vorherige explizite Entscheidung mit identischem Hash; SEC-01–04 und 12/17 grün |
| AC04 – R06 | Webfetch und Inhaltsübergabe als getrennte Entscheidungen; abgelehnter Inhalt nie im Kontext |
| AC05 – R07 | Injection-Testkorpus kann keine Policies oder Freigaben verändern; untrusted Herkunft bleibt erhalten |
| AC06 – R08 | VM ohne NIC, alle Netzwerkprobes negativ; keine Ollamaverbindung aus Gast |
| AC07 – R09 | alle Hostpfade außerhalb expliziter Grants unerreichbar; Pfadausbruchtests grün |
| AC08 – R10 | Crash-Recovery ohne doppelte Ausführung; Audit, Not-Aus und Diffübernahme demonstriert |
| AC09 – Hardware | Standardprofil nach Kalibrierung ohne OOM; Kontext- und Speichermessungen gespeichert |
| AC10 – Nutzbarkeit | Entscheidungen per Tastatur/Screenreader möglich; exakte Parameter sichtbar; keine implizite Zustimmung |
| AC11 – R11/R12 | Agenten bauen und testen je ein Beispiel in C#, Python, C++, PowerShell, Batch und HDevelop; Toolchains nur nach bestätigter Registrierung |
| AC12 – R13 | Repository mit mehr als 2.000 Dateien indiziert; Planner findet relevante Stellen ohne Volllesen; Kontextprofile über 8K gemessen |
| AC13 – R14 | Live-Feed je Agent; UI reagiert während Generierung und Builds; Watchdog und Abbruch pro Agent demonstriert |
| AC14 – R15 | Start auf anderer GPU-/RAM-Konfiguration ohne Codeänderung; Profile werden neu kalibriert |
| AC15 – R05 | Sammelfreigabe mit Abwahl einzelner Dateien demonstriert; nur ausgewählte Dateien im Agentenkontext |
| AC16 – R16 | Oberfläche vollständig deutsch; Prompts englisch, in Ressourcen getrennt |
| AC17 – R17/R18 | VM wird bei Sitzungsstart unbeaufsichtigt neu installiert, ohne NIC, mit ausgewählten Toolchains; bei Sitzungsende gelöscht |

Demo: Ein kleines C#-Projekt und ein Python-Projekt erhalten jeweils einen CSV-Parser mit Unicode-, Leerwert- und Fehlerfalltests. Planner zerlegt die Aufgabe; Coder und Tester arbeiten auf unabhängigen Teilen. Ein absichtlich fehlgeschlagener Test aktiviert Debugger; Reviewer prüft den finalen Diff. Jeder tatsächlich ausgeführte Toolaufruf wird separat genehmigt. Eine Dokumentationsseite wird erst nach Fetch- und Inhaltsfreigabe verwendet. Ein absichtlicher Netzversuch des Testcodes scheitert. Zum Schluss wird der geprüfte Patch nach eigener Freigabe ins Original übernommen. Zusätzlich je eine Kurzdemo in C++, PowerShell, Batch und HDevelop.

Definition of Done: Code gebaut, relevante Tests grün, Musskriterien belegt, bekannte Einschränkungen dokumentiert, Offlineinstallationsmanifest vollständig, Runbook erprobt und keine offenen kritischen Sicherheitsbefunde. „Nicht ausgeführt“ darf niemals als „bestanden“ erscheinen. Kein automatischer Commit oder Publish als Teil der Abnahme.

## 17. Runbook

| Situation | Vorgehen |
|---|---|
| Normaler Start | Versionsmanifest prüfen; VM-Neuaufbau im Hintergrund abwarten; Selbsttest; Projektrechte anzeigen; Run mit fünf Rollen starten |
| VM-Aufbau scheitert | Installationsbericht anzeigen (fehlendes Abbild, Toolchain-Installer, Lizenzdatei); Ursache beheben; „VM neu aufbauen“ |
| Freigabe wartet | Parameter/Diff prüfen und einzeln entscheiden; keine automatische Fortsetzung |
| OOM / GPU stark belegt | laufende Generierung abbrechen; Modell entladen; auf 7B oder 4K reduzieren; neue Messung; keine Toolwiederholung ohne Freigabe |
| Modell hängt | Inferenzrequest abbrechen, Queue erhalten; keine automatisch erzeugten Folgeaktionen ausführen |
| Toolchain fehlt oder Import scheitert | Job stoppen; fehlende Toolchain bzw. Hashabweichung anzeigen; Nutzer legt Toolchain ab und importiert erneut |
| Transfer-VHDX-Import scheitert | Ergebnis verwerfen, VHDX aus Vorlage ersetzen, Job als fehlgeschlagen markieren; keine automatische Wiederholung |
| Orchestrator abgestürzt | Broker sperrt neue Vorschläge des Prozesses; Orchestrator neu starten; Runs aus Journal fortsetzen ohne alte Freigaben |
| Offlinepaket fehlt | Job stoppen; Paket und Version als Beschaffungsvorschlag anzeigen; außerhalb VM provisionieren und erneut prüfen |
| Netzwerkisolation unklar | sämtliche Ausführung sperren; VM verwerfen und aus geprüfter Offlinebasis neu erzeugen |
| Inhalt verdächtig | Inhalt ablehnen, Capture-ID protokollieren; bereits abgeleitete Kontexte invalidieren und aus sauberer Evidenz neu aufbauen |
| Freigabe abgelaufen | neuen unveränderten oder aktualisierten Vorschlag anzeigen; keine Verlängerung ohne Entscheidung |
| Snapshotkonflikt | neue Basis lesen lassen, Diff neu erzeugen und erneut genehmigen |
| Not-Aus | Queue stoppen, Capabilities invalidieren, Prozessbäume/VM beenden; Staging zur Prüfung erhalten |
| Neustart nach Absturz | Journal lesen; laufende Jobs als unklar markieren; nie automatisch erneut ausführen; Original und Staging vergleichen |
| Backup / Restore | SQLite konsistent sichern, Artefaktmanifest und Policy exportieren; Freigabecapabilities nicht wiederherstellen |
| Update | neue Version in separater Umgebung prüfen; Sicherheitsregressionen und Kalibrierung wiederholen; Manifest aktualisieren |

Diagnosepaket enthält Versionen, anonymisierte Zustände, Event-IDs, Speicherstatistiken und Hashes. Code, Rohwebinhalte und Geheimnisse werden nur nach sichtbarer Auswahl exportiert. Diagnoseexport ist ein eigener Toolaufruf, wenn ihn ein Agent veranlasst.

## 18. Architekturentscheidungen und offene Punkte

| ADR | Entscheidung | Begründung / Konsequenz |
|---|---|---|
| 001 | C#/.NET und WPF für UI sowie Approval/Tool Broker und Audit | Nutzerwunsch; Sicherheitskern in eigenem Prozess, native Windowsintegration |
| 002 | eigener kleiner DAG-Scheduler im Python-Orchestrator | überprüfbarer Ablauf, keine Frameworkpflicht |
| 003 | fünf Rollen, ein Inferenzslot | Rollenparallelität bei begrenztem VRAM |
| 004 | Explizite Genehmigung vor jedem Tool; Sammelfreigabe nur für exakt benannte Listen mit Abwahl | harte Grenze unabhängig von Modellgehorsam; weniger Freigabemüdigkeit bei großen Repositories |
| 005 | zwei Webfreigaben | Zugriff und Inhaltsweitergabe sind verschiedene Entscheidungen |
| 006 | VM ohne NIC, auch während der Installation | keine Abhängigkeit von Prompt oder Gast-Firewall für Netzwerksperre |
| 007 | Original nur über geprüften Export ändern | Konflikte und irreversible Überschreibungen begrenzen |
| 008 | typisierte Tools statt freier Shell; PowerShell/Batch nur über `Toolchain.Run` | überprüfbare Vorschau und kleinere Angriffsfläche |
| 009 | kalibrierte Profile statt fester Modell-/Kontextvorgabe; 14B als Start, 7B als Fallback | große Repositories; Hardwareunabhängigkeit; reale Speicherprüfung zwingend |
| 010 | Python für Orchestrierung, Agenten und Tools, ohne Freigabeautorität | bessere LLM-/Parser-Bibliotheken; Sicherheitsentscheidungen bleiben im C#-Broker |
| 011 | Verträge als JSON Schema mit Codegenerierung | eine Quelle für C# und Python; identische Hashes prüfbar |
| 012 | Copy-VMFile Host→VM, Transfer-VHDX VM→Host | kein Netzwerk- oder Socketkanal; Host-Mount nur read-only ohne Laufwerksbuchstabe |
| 013 | Toolchains als nutzerbereitgestellte, manifestbeschriebene Pakete | Mehrsprachigkeit ohne Kernänderung; Registrierung sichtbar und hashgeprüft |
| 014 | Repo-Index + Repo-Map statt Volltext im Kontext | große Repositories bei begrenztem VRAM |
| 015 | Live-Aktivität und Watchdog statt Latenzziel | Transparenz und Bedienbarkeit wichtiger als Geschwindigkeit |
| 016 | VM-Neuinstallation je Sitzung per DISM + unattend, keine Snapshots | kein Zustand zwischen Sitzungen; längere Startzeit, durch Hintergrundaufbau abgefedert |
| 017 | Oberfläche Deutsch, Prompts Englisch | Nutzerwunsch; Modelle folgen englischen Anweisungen zuverlässiger |
| 018 | Lizenzen in Verantwortung des Nutzers | Software bleibt lizenzneutral und veröffentlichbar |

Geklärt (Stand 16.10.2026): Windows 11 Pro mit Hyper-V; keine festen CPU-/GPU-Modelle im Code; große Repositories ab MVP; Zielsprachen C#, Python, C++, HDevelop, PowerShell, Batch; Aufbewahrung wie vorgeschlagen; Antwortzeit zweitrangig, UI muss reagieren; Transfer per Copy-VMFile und Transfer-VHDX; Sammelfreigaben mit Abwahl; VM-Neuinstallation je Sitzung ohne Snapshots; Lizenzen (Windows, HALCON, Visual Studio) beim Nutzer; Oberfläche Deutsch, Prompts Englisch.

Keine offenen Punkte. Bei fehlender VM-Unterstützung darf die Anwendung planen und Vorschläge zeigen, aber keine Tools auf dem Host als Ersatz ausführen.

Bewusste Einschränkungen: Keine Garantie, alle Prompt Injections zu erkennen; keine Qualitätsgarantie aus Modellgröße; keine feingranulare Hostdateipolicy allein durch `.wsb`; keine automatische Dependencybeschaffung; kein vollständiger Schutz bei kompromittiertem Host. Diese Grenzen ändern nichts an den verbindlichen Freigabe- und Isolationsanforderungen.

## 19. Quellen und Wartung

Offizielle Quellen, am 16.10.2026 eingesehen. Technische Tatsachen zu Produkten stammen aus diesen Quellen; Architektur, Budgets, Backlog und Zeitabschätzungen sind die in diesem Dokument vorgeschlagenen Entscheidungen.

- **S1:** [Ollama – Qwen2.5-Coder 14B, Größe und Quantisierung](https://ollama.com/library/qwen2.5-coder:14b)
- **S2:** [Ollama – Qwen2.5-Coder 7B, Größe und Quantisierung](https://ollama.com/library/qwen2.5-coder:7b)
- **S3:** [Ollama – Qwen3 8B, Größe und Quantisierung](https://ollama.com/library/qwen3:8b)
- **S4:** [Ollama FAQ – Speicher, Parallelität, Konfiguration und lokale Nutzung](https://docs.ollama.com/faq)
- **S5:** [Microsoft – Windows Sandbox konfigurieren](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file)
- **S6:** [Microsoft – .NET Support Policy](https://dotnet.microsoft.com/en-us/platform/support/policy/dotnet-core)
- **S7:** [Ollama – Qwen3-Coder Modellfamilie](https://ollama.com/library/qwen3-coder)

Bei Änderungen an Modell, Ollama, Treiber, SDK, VM-Basis, Tooladapter oder Sanitizer werden Manifest und betroffene Tests aktualisiert. Sicherheitsinvarianten und Acceptance Criteria werden vor jedem Release erneut geprüft.
