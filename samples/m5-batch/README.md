# M5-Beispiel: Rechner (Batch)

`scripts/rechne.cmd add|mul A B` und Tests in `tests/` für das Harness der Toolchain `batch`: jede Datei `*.test.cmd`
ist ein Test (Exitcode 0 = bestanden, `*.expected.txt` daneben = erwartete Ausgabe). Prüflauf
`batch test (path=.)`, statische Prüfung `batch lint (path=.)`.

Aufgabe für die Abnahme mit Modell: „Ergänze in rechne.cmd die Operation sub (A minus B). Mit Tests.“
