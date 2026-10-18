# M4-Sicherheitsprobe (SEC-13, SEC-21, SEC-07)

**Absichtlich „bösartiges“ Projekt.** Beim Build führt ein MSBuild-Target (wie ein manipuliertes NuGet-Paket es
könnte) Befehle im Gast aus und versucht:

| Versuch | Erwartung in der Offline-VM |
|---|---|
| in die Worker-Laufzeit `C:\LocalAi\python` schreiben | verweigert (niedrige Integritätsstufe) |
| in die Toolchain `C:\Toolchains\dotnet` schreiben | verweigert |
| nach `C:\Windows` schreiben | verweigert |
| `HKLM` in der Registry ändern | verweigert |
| Internet erreichen | scheitert (keine Netzwerkkarte) |
| SSH-Schlüssel/Browserprofil lesen (`%USERPROFILE%\.ssh`) | nicht vorhanden (eigenes, leeres Profil je Job) |
| in die eigene Arbeitskopie schreiben | erlaubt – das ist der genehmigte Scope |

Die Ergebnisse stehen als Zeile `SEC13 …` im Build-Log (Exitcode je Versuch, 0 = gelungen).
Nur in der Offline-VM bauen, nie auf dem Host. Wird von `tests/integration/test_m4_abnahme.py` benutzt.
