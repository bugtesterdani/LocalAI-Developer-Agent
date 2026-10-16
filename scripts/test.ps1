#Requires -Version 5.1
# Baut und testet alles (Windows). Aufruf aus der Repository-Wurzel:
#   powershell -ExecutionPolicy Bypass -File scripts\test.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what fehlgeschlagen (Exitcode $LASTEXITCODE)" } }

Step "1/4 Generierte Verträge aktuell?"
uv run --project src/python python tools/codegen/generate.py --check; Check "Vertragsprüfung"

Step "2/4 C# bauen (inkl. WPF-Oberfläche)"
dotnet build src/dotnet/LocalAi.slnx -c Debug; Check "Build"

Step "3/4 C#-Tests"
dotnet run --project src/dotnet/LocalAi.Tests --no-build; Check "C#-Tests"

Step "4/4 Python-Tests (inkl. Integrationstests gegen den Broker)"
Push-Location src/python
try { uv run pytest -q; Check "Python-Tests" } finally { Pop-Location }

Write-Host ""
Write-Host "Alles grün." -ForegroundColor Green
