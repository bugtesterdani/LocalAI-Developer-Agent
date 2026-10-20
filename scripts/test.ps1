#Requires -Version 5.1
# Baut und testet alles (Windows). Aufruf aus der Repository-Wurzel:
#   powershell -ExecutionPolicy Bypass -File scripts\test.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what fehlgeschlagen (Exitcode $LASTEXITCODE)" } }

# Python-Umgebung zuerst: C#-Tests starten Fake-Agent und Worker mit "uv run --no-sync" und lesen src/python/.venv.
uv sync --project src/python; Check "uv sync"

Step "1/4 Generierte Verträge aktuell?"
uv run --project src/python python tools/codegen/generate.py --check; Check "Vertragsprüfung"

Step "2/4 C# bauen (inkl. WPF-Oberfläche)"
dotnet build src/dotnet/LocalAi.slnx -c Debug; Check "Build"

Step "3/4 C#-Tests"
dotnet test src/dotnet/LocalAi.Tests --no-build; Check "C#-Tests"

Step "4/4 Python-Tests (inkl. Integrationstests gegen den Broker)"
# Die Hyper-V-Abnahme (VM-Aufbau, 15–60 min) gehört nicht in den normalen Testlauf – auch wenn
# LOCALAI_HYPERV_ABNAHME in dieser PowerShell noch gesetzt ist. Sie wird separat gestartet.
$gemerkt = @{}
foreach ($name in 'LOCALAI_HYPERV_ABNAHME', 'LOCALAI_M4_ABNAHME', 'LOCALAI_M5_ABNAHME', 'LOCALAI_M6_ABNAHME', 'LOCALAI_M7_ABNAHME') {
    $wert = [Environment]::GetEnvironmentVariable($name)
    if ($wert) { $gemerkt[$name] = $wert; Remove-Item "Env:$name" }
}
Push-Location src/python
try { uv run pytest -q; Check "Python-Tests" } finally {
    Pop-Location
    foreach ($name in $gemerkt.Keys) { Set-Item "Env:$name" $gemerkt[$name] }
}

Write-Host ""
Write-Host "Alles grün." -ForegroundColor Green
