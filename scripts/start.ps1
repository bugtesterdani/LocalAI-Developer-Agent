#Requires -Version 5.1
# Startet die Oberfläche (startet den Broker automatisch mit).
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
dotnet run --project src/dotnet/LocalAi.Desktop
