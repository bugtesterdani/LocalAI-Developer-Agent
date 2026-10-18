# M5-Beispiel: Rechner (HDevelop)

Externe Prozeduren in `procedures/` (`addieren`, `multiplizieren`), ein Programm in `programs/` und ein Testprogramm
`tests/rechner_test.hdev`: jede lokale Prozedur `test_*` ist ein Testfall; ein Fehler (z. B. `throw ('…')`) lässt ihn
scheitern. Toolchain `hdevelop`: Prüflauf `hdevelop test (path=.)` (lädt zuerst alle Dateien – Lade-Test – und führt dann
die Tests aus), `hdevelop lint (path=.)` nur Lade-Test.

Jeder Patch an einer `.hdev`/`.hdvp`/`.hdpl`-Datei wird in der Sandbox zusätzlich als XML geprüft; ein Patch, der die
Datei unlesbar machen würde, wird nicht angewendet (das geht auch ohne HALCON-Lizenz).

Aufgabe für die Abnahme mit Modell: „Füge die externe Prozedur teilen (A, B, Quotient) hinzu. Teilen durch 0 soll
mit throw eine deutsche Fehlermeldung auslösen. Mit Tests in tests/rechner_test.hdev.“
