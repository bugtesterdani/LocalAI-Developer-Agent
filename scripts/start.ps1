#Requires -Version 5.1
# Startet die Oberfläche (startet den Broker automatisch mit).
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
# Python-Umgebung für den Fake-Agenten aktuell halten (er startet mit "uv run --no-sync").
uv sync --project src/python
if ($LASTEXITCODE -ne 0) { throw "uv sync fehlgeschlagen" }
dotnet run --project src/dotnet/LocalAi.Desktop
