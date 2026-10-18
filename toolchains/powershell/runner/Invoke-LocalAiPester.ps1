<#
  Führt Pester-Tests (*.Tests.ps1) unter -Path aus und schreibt JUnit-XML nach -Results\pester.xml.
  Exitcode 0 = alle bestanden (oder keine Tests), 1 = Fehler. Teil der Toolchains "powershell" und "powershell5".
#>
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Results
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'LocalAiModules.ps1')
Add-LocalAiPackagePath

$pester = Import-LocalAiModule 'Pester'
if (-not $pester) { Write-Output 'Pester fehlt: Ordner Modules\Pester im Toolchain-Ordner (Save-Module Pester).'; exit 2 }
if ($pester.Version.Major -lt 5) { Write-Output "Pester $($pester.Version) zu alt – Version 5 oder neuer nötig."; exit 2 }
Write-Output "PowerShell $($PSVersionTable.PSVersion) · Pester $($pester.Version)"

New-Item -ItemType Directory -Force -Path $Results | Out-Null
$config = New-PesterConfiguration
$config.Run.Path = (Resolve-Path -LiteralPath $Path).Path
$config.Run.PassThru = $true
$config.Run.Exit = $false
$config.Output.Verbosity = 'Detailed'
try { $config.Output.RenderMode = 'Plaintext' } catch { }  # keine Farbcodes im Log (Pester 5.5+)
$config.TestResult.Enabled = $true
$config.TestResult.OutputFormat = 'JUnitXml'
$config.TestResult.OutputPath = Join-Path $Results 'pester.xml'
$result = Invoke-Pester -Configuration $config
# Pfade im Bericht relativ zum Projekt (statt zum Arbeitsordner des Jobs).
$report = Join-Path $Results 'pester.xml'
if (Test-Path -LiteralPath $report) {
    $prefix = (Get-Location).Path.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $text = [System.IO.File]::ReadAllText($report)
    $text = $text.Replace($prefix, '').Replace($prefix.Replace('\', '/'), '')
    [System.IO.File]::WriteAllText($report, $text, (New-Object System.Text.UTF8Encoding($false)))
}
if ($result.TotalCount -eq 0) { Write-Output 'Keine Pester-Tests gefunden (*.Tests.ps1).'; exit 0 }
Write-Output ("Tests: {0}, bestanden {1}, fehlgeschlagen {2}, übersprungen {3}" -f $result.TotalCount, $result.PassedCount, $result.FailedCount, $result.SkippedCount)
if ($result.FailedCount -gt 0 -or $result.FailedContainersCount -gt 0) { exit 1 }
exit 0
