# M5-Beispiel: Rechner (C++)

CMake-Projekt mit Bibliothek `rechner` und Tests über CTest (`tests/rechner_tests.cpp`, ein CTest-Eintrag je Fall).
Toolchain `cpp` (MSVC Build Tools) oder `cpp-clang`: Prüflauf `cpp test (project=., configuration=Debug)`.

Aufgabe für die Abnahme mit Modell: „Füge rechner::teilen(a, b) hinzu. Teilen durch 0 soll eine
std::invalid_argument-Ausnahme mit deutscher Meldung werfen. Mit Tests.“
