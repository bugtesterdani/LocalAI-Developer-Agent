# Anforderungen → Tests (Stand M1)

Jede Sicherheitsanforderung ist mindestens einem automatischen Test zugeordnet. C#-Tests laufen im selben Prozess mit einstellbarer Uhr, Python-Integrationstests gegen den echten Broker-Prozess über die Named Pipe.

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
| R10 Audit | Hashkette erkennt Manipulation; halbe letzte Zeile toleriert | `Journal_Manipulation_wird_erkannt`, `Unvollstaendige_letzte_Journalzeile_wird_toleriert` | `test_manipuliertes_journal_verhindert_start` |
| B01 | kanonisches JSON + SHA-256 in C# und Python identisch | `ContractTests.*` | `test_contracts.py`, `test_hash_ist_in_csharp_und_python_identisch` |
| R16 / AC16 | Oberfläche Deutsch, alle Text-Schlüssel und Bindings vorhanden | `DesktopTests.Xaml_*`, `Alle_Bindings_*`, `Zustandstexte_*` | – |
| M1-E2E | Broker-Prozess + Python-Fake-Agent + Oberflächenlogik inkl. Abwahl | `EndToEndTests.*` | – |

Noch nicht abgedeckt (folgt): SEC-04/-05 mit echten Handles und Junctions (M2), SEC-06/-07/-13/-18/-19/-21/-24 Sandbox (M2), SEC-08 bis -11/-14 Web (M6), SEC-16/-22 Modell und Orchestrator (M3), SEC-20 Toolchains (M4).
