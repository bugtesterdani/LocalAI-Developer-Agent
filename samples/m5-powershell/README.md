# M5-Beispiel: Rechner (PowerShell)

Modul `src/Rechner.psm1` mit Pester-5-Tests in `tests/`. Toolchain `powershell` (PowerShell 7) oder
`powershell5` (Windows PowerShell 5.1): Prüflauf `powershell test (path=.)`, Lint `powershell lint (path=.)`
(PSScriptAnalyzer).

Aufgabe für die Abnahme mit Modell: „Füge Get-Quotient -A -B hinzu. Teilen durch 0 soll einen Fehler mit deutscher
Meldung auslösen. Mit Tests.“
