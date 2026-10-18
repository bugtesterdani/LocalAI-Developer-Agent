# M4-Beispiel (C#): Rechner

Kleines .NET-Projekt für die Ende-zu-Ende-Prüfung von M4: Patch anwenden, `dotnet test` in der Offline-VM,
Reparatur durch den Debugger. Zielrahmen `net10.0` (passend zur dotnet-Toolchain 10.0.x; bei einer 9er-Toolchain auf `net9.0` ändern).

Benötigte Offline-Pakete (über `scripts/toolchains/Get-NuGetOfflinePackages.ps1`): Microsoft.NET.Test.Sdk,
xunit, xunit.runner.visualstudio.

Beispielziel für den Chat: „Füge Rechner.Teilen(a, b) hinzu. Teilen durch 0 soll eine ArgumentException
mit deutscher Meldung werfen. Mit Tests.“
