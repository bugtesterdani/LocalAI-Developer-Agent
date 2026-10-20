# Glossar

| Begriff | Bedeutung |
|---|---|
| **Abwahl** | Entfernen einzelner Ziele aus einer Sammelfreigabe vor der Entscheidung. Erzeugt einen neuen Vorschlag mit neuem Hash |
| **Agent / Rolle** | einer der fünf Rollen Planner, Coder, Tester, Debugger, Reviewer. Jede hat eigenen Zustand, eigene Aufgaben und eigenen Prompt; sie teilen sich ein Modell |
| **Approval Broker** | C#-Kern, der Vorschläge prüft, Freigaben verwaltet und Capabilities verbraucht. Einzige Instanz mit Freigabeautorität |
| **Audit-Journal** | `audit.db`, SQLite-Journal mit Hashkette über alle Vorschläge, Entscheidungen und Ausführungen |
| **Betriebsart** | `sandbox.mode`: `test`, `none`, `dev-local-unsafe`, `hyperv`, `qemu` |
| **Capability** | einmaliges, kryptografisch zufälliges Recht zur Ausführung genau eines freigegebenen Vorschlags; wird beim Dispatch atomar verbraucht |
| **Content Gate** | eigener Prozess, der abgerufene Webinhalte bereinigt und verdächtige Stellen markiert. Kein Vertrauensbeweis |
| **Copy-VMFile** | Hyper-V-Funktion, mit der der Host Dateien in den Gast kopiert (Weg Host → VM) |
| **DAG** | Aufgabengraph des Planners; legt fest, welche Aufgaben parallel laufen dürfen |
| **Datenordner** | `%LOCALAPPDATA%\LocalAi\broker` bzw. `~/.local/share/LocalAi/broker` |
| **dev-local-unsafe** | Betriebsart, in der der Worker ohne VM auf dem Host läuft – nur zum Entwickeln, rot markiert, ohne `Toolchain.*` |
| **Evidenz (EvidenceBlock)** | Daten, die ein Agent im Kontext sieht (Dateiinhalt, Testbericht, Webtext) – immer mit Herkunft und als untrusted markiert |
| **Freigabekarte** | Darstellung eines Vorschlags in der Oberfläche mit exakten Parametern, Zielen, Nebenwirkungen und Hash |
| **Gast / Gastsystem** | Betriebssystem in der Sandbox-VM: Windows oder Linux (Debian 13) |
| **Grant** | freigegebener Hostordner mit konkreten Rechten (`List`, `Read`, `Create`, `Modify`, `Delete`, `Execute`) |
| **Heimat-VM** | VM des Projekts (`guestOs`), in der alles außer fremden Toolchain-Aktionen läuft |
| **Kalibrierung** | Messung, ob ein Modellprofil auf dieser Hardware stabil läuft (ohne OOM, mit VRAM-Reserve). Ohne Kalibrierung wird ein Profil nicht verwendet |
| **KVP** | Key-Value-Pair-Austausch von Hyper-V; einziger Rückkanal für die Fertigmeldung des Gasts (`result:<Sequenz>`) |
| **Model Gateway** | Teil des Orchestrators, der mit Ollama spricht: Streaming, Abbruch, Watchdog, Kontext je Anfrage |
| **Not-Aus** | stoppt jeden Dispatch, invalidiert alle Freigaben, beendet laufende Ausführung und Webprozesse |
| **Orchestrator** | Python-Prozess mit den fünf Agenten; darf nur vorschlagen, nie freigeben |
| **OutcomeUnknown** | Zustand eines Aufrufs, dessen Ausgang nach einem Absturz unklar ist – wird nie automatisch wiederholt |
| **Profil** | Modell + Kontextgröße + Ausgabegrenze (z. B. `standard` = `qwen2.5-coder:14b` mit 8K) |
| **Prüflauf** | `Toolchain.Build`/`Toolchain.Test`, den der Planner vorsieht oder der Orchestrator automatisch wählt |
| **Quarantäne** | Ablage der Rohdaten eines Webabrufs; erreicht nie einen Agenten |
| **Repo-Index / Repo-Map** | Index eines Snapshots (Dateibaum, Symbole, Volltext) und daraus eine kompakte Übersicht für den Planner |
| **reuseVm** | Option, eine sauber beendete VM beim nächsten Start fortzusetzen statt neu aufzubauen |
| **Run** | Bearbeitung eines Ziels mit allen fünf Rollen, vom Plan bis zur Übernahme |
| **Runjournal** | `run-journal.json`: Stand eines Runs ohne Freigaben; Grundlage für `fortsetzen` |
| **Sammelfreigabe** | ein Vorschlag mit mehreren exakt benannten Zielen, einzeln abwählbar |
| **Sandbox-Worker** | Python-Prozess in der VM, der signierte Jobs prüft und ausführt |
| **SEC-xx / AC-xx / Rxx** | Prüffälle, Abnahmekriterien und Anforderungen der Spezifikation |
| **Selbsttest** | Prüfung nach dem VM-Aufbau: keine Netzwerkkarte, alle Netzwerkversuche scheitern, Toolchains ok. Erst danach Jobs |
| **Snapshot** | unveränderliche, inhaltsadressierte Kopie eines Grants in der Sandbox. Patches erzeugen abgeleitete Snapshots |
| **Staging** | Ablage des Brokers für Änderungen, die in den Projektordner übernommen werden sollen |
| **Toolchain** | vom Nutzer bereitgestelltes Werkzeugpaket (SDK, Compiler, Testframework) mit `toolchain.json`; wird registriert und beim VM-Aufbau installiert |
| **Transfer-VHDX / Transfer-Laufwerk** | zweite virtuelle Festplatte, auf die der Gast Ergebnisse als Container schreibt (Weg VM → Host); einmal benutzt, dann ersetzt |
| **untrusted** | Kennzeichnung aller Inhalte aus Dateien, Web, Tests und Modellantworten: Daten, nie Anweisungen |
| **Verbindungskanal** | virtio-serial-Kanal unter QEMU (`org.localai.link`), ersetzt Copy-VMFile und KVP |
| **Vorschlag (Proposal)** | unveränderliche Beschreibung eines Werkzeugaufrufs mit kanonischen Parametern und Hash |
| **Vorschlags-Hash** | SHA-256 über die kanonisierten Parameter; die Freigabe bindet genau an ihn |
