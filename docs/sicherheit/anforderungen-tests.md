# Anforderungen → Tests (Stand M2a)

Jede Sicherheitsanforderung ist mindestens einem automatischen Test zugeordnet. C#-Tests (xUnit, Trait `Pruefung`) laufen im selben Prozess mit einstellbarer Uhr, Python-Integrationstests gegen den echten Broker-Prozess über die Named Pipe.

| Anforderung / Prüffall | Inhalt | C#-Test (`LocalAi.Tests`) | Python (`tests/integration`) |
|---|---|---|---|
| R05 / SEC-01 | ohne Entscheidung keine Ausführung, Ablauf ≠ Zustimmung | `BrokerTests.Ohne_Entscheidung_keine_Ausfuehrung`, `Ablehnung_fuehrt_nichts_aus` | `test_sec01_*` |
| R05 / SEC-02 | Freigabe nur mit identischem Hash; Parameter nicht nachträglich änderbar | `Falscher_Hash_wird_abgelehnt`, `Idempotenzschluessel_kann_keine_Parameter_aendern` | `test_sec02_*` |
| R05 / SEC-03 | genau eine Ausführung bei parallelen Dispatches/Entscheidungen | `Parallele_Dispatches_fuehren_genau_einmal_aus`, `Zweite_Entscheidung_wird_abgelehnt` | `test_sec03_*` (8 gleichzeitige UI-Verbindungen) |
| R10 / SEC-12 | Neustart: alte Freigaben ungültig, laufende Jobs „OutcomeUnknown“, keine Wiederholung | `Neustart_invalidiert_alte_Freigaben_ohne_Wiederholung` | `test_sec12_*` (harter Prozessabbruch) |
| R10 / SEC-15 | Not-Aus invalidiert, bricht ab, blockiert neue Vorschläge | `NotAus_invalidiert_und_blockiert`, `DesktopTests.NotAus_sperrt_Erlauben_in_allen_Karten` | `test_sec15_*` |
| R02 / SEC-17 | nur UI-Token darf entscheiden; Orchestrator nur Vorschläge; keine execute-Methode | – | `test_sec17_rollentrennung` |
| R05 / SEC-23 | Sammelfreigabe: Abwahl erzeugt neuen Hash, Erweiterung unmöglich, nur Ausgewähltes wird ausgeführt, Agent erfährt Abwahl | `Sammelfreigabe_*`, `DesktopTests.Sammelfreigabe_Karte_*` | `test_sec23_*` |
| R09 / SEC-05 (formal) | `..`, absolute, UNC-, Geräte-, ADS-Pfade abgewiesen | `ContractTests.Pfadregeln_lehnen_Ausbrueche_ab` | `test_unzulaessige_pfade_*` |
| R10 Audit | SQLite-Journal mit Hashkette erkennt UPDATE/DELETE; Prüfung im laufenden Betrieb | `BrokerTests.Journal_*` | `test_manipuliertes_journal_verhindert_start` (sqlite3 UPDATE) |
| B01 | kanonisches JSON + SHA-256 in C# und Python identisch | `ContractTests.*` | `test_contracts.py`, `test_hash_ist_in_csharp_und_python_identisch` |
| R16 / AC16 | Oberfläche Deutsch, alle Text-Schlüssel und Bindings vorhanden | `DesktopTests.Xaml_*`, `Alle_Bindings_*`, `Zustandstexte_*` | – |
| R09 Ordnerrechte | ohne Grant/Recht kein Zugriff, Snapshot nur exakt für die Grant-Wurzel, nicht rekursive Grants, Delete/Execute in M2a gesperrt | `SandboxTests.Ohne_Grant_*`, `Snapshot_muss_zum_Grant_*`, `Nicht_rekursiver_*`, `Projektdatei_lehnt_*` | `test_ordnerrechte_und_snapshot_bindung` |
| SEC-04 | erwarteter Hash weicht ab → Fehlschlag, kein Inhalt | – | `test_hashabweichung_erzwingt_neuen_vorschlag`, `test_worker.py::test_lesen_mit_zeilenbereich_und_hashpruefung` |
| SEC-05 (Dateisystem) | Symlinks/Junctions/Hardlinks werden nicht verfolgt, Ausschlüsse, Limits; gemeinsame Pfadvektoren | `SandboxTests.Verknuepfungen_*`, `Snapshot_enthaelt_*`, `Snapshot_Limits_greifen`, `Pfadregeln_entsprechen_*` | `test_snapshot_und_lesen_ueber_den_worker`, `test_worker.py::test_pfadregeln_*` |
| SEC-18 (Teil) | Worker: Signatur, Sequenz, Sitzung, Ablauf, Vorschlags-Hash, Zip-Slip, fehlende/zusätzliche/veränderte Dateien, Verknüpfungen im Archiv; Broker: Ergebnis streng geprüft, immer „nicht vertrauenswürdig“ | `SandboxTests.Ergebnis_wird_streng_geprueft`, `SnapshotId_und_JobSignatur_entsprechen_Python` | `test_worker.py::*` |
| R05 Snapshot | ohne Freigabe wird nichts gelesen oder übertragen | `EndToEndTests.*` | `test_snapshot_ohne_freigabe_liest_nichts` |
| E2E | Broker-Prozess + Worker (dev-local) + Python-Fake-Agent + Oberflächenlogik: Snapshot, Auflisten, Lesen mit Abwahl, Ablehnung | `EndToEndTests.*` | – |

Noch nicht abgedeckt (folgt): Junctions/Hardlinks unter echtem Windows (Tests vorhanden, laufen nur auf PC 123 voll), SEC-06/-07/-13/-19/-21/-24 und SEC-18 mit Transfer-VHDX (M2b/M2c), SEC-08 bis -11/-14 Web (M6), SEC-16/-22 Modell und Orchestrator (M3), SEC-20 Toolchains (M4).
