#Requires -Version 5.1
# M7-Abnahme: alle automatischen Tests mit Ergebnisdateien, optional die VM-Abnahmen, dann der Abnahmebericht
# (SEC-01–24, AC01–17) in %LOCALAPPDATA%\LocalAi\abnahme\abnahme-bericht.md. Aufruf aus der Repository-Wurzel:
#   powershell -ExecutionPolicy Bypass -File scripts\abnahme.ps1                 # nur automatische Tests + Bericht
#   powershell -ExecutionPolicy Bypass -File scripts\abnahme.ps1 -MitVm          # zusätzlich alle VM-Abnahmen (Stunden)
#   powershell -ExecutionPolicy Bypass -File scripts\abnahme.ps1 -MitVm -Teile m7,m6
#   powershell -ExecutionPolicy Bypass -File scripts\abnahme.ps1 -NurBericht     # Bericht aus vorhandenen Ergebnissen
# Die VM-Abnahmen laufen in der Reihenfolge hyperv, m4, m5, m7, m6 (m6 löst am Ende den Not-Aus aus: danach wird die
# VM beim nächsten Start neu aufgebaut). Ohne HALCON-Lizenz vorher: $env:LOCALAI_M5_HDEVELOP = "0".
param(
    [switch]$MitVm,
    [string[]]$Teile = @('hyperv', 'm4', 'm5', 'm7', 'm6'),
    [switch]$NurBericht
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }

$abnahme = Join-Path $env:LOCALAPPDATA 'LocalAi\abnahme'
$ergebnisse = Join-Path $abnahme 'ergebnisse'
$daten = Join-Path $env:LOCALAPPDATA 'LocalAi\broker'
New-Item -ItemType Directory -Force -Path $ergebnisse | Out-Null
$trx = Join-Path $ergebnisse 'csharp.trx'
$junit = Join-Path $ergebnisse 'python.xml'
$fehler = @()

# VM-Abnahmen nie versehentlich im normalen Testlauf
$abnahmeVariablen = 'LOCALAI_HYPERV_ABNAHME', 'LOCALAI_M4_ABNAHME', 'LOCALAI_M5_ABNAHME', 'LOCALAI_M6_ABNAHME', 'LOCALAI_M7_ABNAHME'
foreach ($name in $abnahmeVariablen) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }

if (-not $NurBericht) {
    Step "1/4 Generierte Verträge aktuell?"
    uv run --project src/python python tools/codegen/generate.py --check
    if ($LASTEXITCODE -ne 0) { $fehler += 'Vertragsprüfung' }

    Step "2/4 C# bauen und testen (Ergebnis: $trx)"
    dotnet build src/dotnet/LocalAi.slnx -c Debug
    if ($LASTEXITCODE -ne 0) { throw "Build fehlgeschlagen" }
    if (Test-Path $trx) { Remove-Item $trx }
    dotnet test src/dotnet/LocalAi.Tests --no-build --logger "trx;LogFileName=csharp.trx" --results-directory $ergebnisse
    if ($LASTEXITCODE -ne 0) { $fehler += 'C#-Tests' }

    Step "3/4 Python-Tests inkl. Integration gegen den Broker (Ergebnis: $junit)"
    Push-Location src/python
    try {
        uv run pytest -q --junitxml="$junit"
        if ($LASTEXITCODE -ne 0) { $fehler += 'Python-Tests' }
    } finally { Pop-Location }

    if ($MitVm) {
        $skripte = [ordered]@{
            hyperv = @('LOCALAI_HYPERV_ABNAHME', 'tests/integration/test_hyperv_abnahme.py')
            m4     = @('LOCALAI_M4_ABNAHME', 'tests/integration/test_m4_abnahme.py')
            m5     = @('LOCALAI_M5_ABNAHME', 'tests/integration/test_m5_abnahme.py')
            m7     = @('LOCALAI_M7_ABNAHME', 'tests/integration/test_m7_abnahme.py')
            m6     = @('LOCALAI_M6_ABNAHME', 'tests/integration/test_m6_abnahme.py')
        }
        foreach ($teil in $skripte.Keys) {
            if ($Teile -notcontains $teil) { continue }
            $variable, $skript = $skripte[$teil]
            Step "VM-Abnahme $teil ($skript)"
            Set-Item "Env:$variable" '1'
            try {
                uv run --project src/python pytest -s $skript
                if ($LASTEXITCODE -ne 0) { $fehler += "VM-Abnahme $teil" }
            } finally { Remove-Item "Env:$variable" -ErrorAction SilentlyContinue }
        }
    }
}

Step "4/4 Abnahmebericht"
uv run --project src/python python tools/abnahme/bericht.py --trx "$trx" --junit "$junit" --abnahme "$abnahme" --daten "$daten"
$berichtCode = $LASTEXITCODE

Write-Host ""
Write-Host "Bericht:  $(Join-Path $abnahme 'abnahme-bericht.md')"
Write-Host "Manuell:  $(Join-Path $abnahme 'abnahme-manuell.json') (Punkte nach eigener Prüfung bestätigen, dann -NurBericht)"
if ($fehler.Count -gt 0 -or $berichtCode -ne 0) {
    Write-Host ("Nicht alles grün: " + (($fehler + $(if ($berichtCode -ne 0) { 'Kriterien fehlgeschlagen' })) -join ', ')) -ForegroundColor Red
    exit 1
}
Write-Host "Keine Fehlschläge. Offene oder unvollständige Kriterien stehen im Bericht." -ForegroundColor Green
