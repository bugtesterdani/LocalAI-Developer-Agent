<#
  PSScriptAnalyzer über alle Skripte unter -Path. Ausgabe: <datei>:<zeile>: [Schwere] Regel: Meldung.
  Exitcode 1, wenn es Befunde der Schwere Error gibt (Warnungen werden nur gemeldet). Einstellungen aus
  PSScriptAnalyzerSettings.psd1 im Projekt, falls vorhanden.
#>
param(
    [Parameter(Mandatory = $true)][string]$Path
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'LocalAiModules.ps1')

$analyzer = Import-LocalAiModule 'PSScriptAnalyzer'
if (-not $analyzer) { Write-Output 'PSScriptAnalyzer fehlt: Ordner Modules\PSScriptAnalyzer im Toolchain-Ordner (Save-Module PSScriptAnalyzer).'; exit 2 }
$root = (Resolve-Path -LiteralPath $Path).Path
$arguments = @{ Path = $root; Recurse = $true; Severity = @('Error', 'Warning') }
$settings = Join-Path (Get-Location).Path 'PSScriptAnalyzerSettings.psd1'
if (Test-Path -LiteralPath $settings) { $arguments.Settings = $settings }
$findings = @(Invoke-ScriptAnalyzer @arguments)
$base = (Get-Location).Path.TrimEnd('\') + '\'
foreach ($f in $findings) {
    $file = [string]$f.ScriptPath
    if ($file.StartsWith($base, [System.StringComparison]::OrdinalIgnoreCase)) { $file = $file.Substring($base.Length) }
    Write-Output ("{0}:{1}: [{2}] {3}: {4}" -f ($file -replace '\\', '/'), $f.Line, $f.Severity, $f.RuleName, $f.Message)
}
$errors = @($findings | Where-Object { [string]$_.Severity -eq 'Error' }).Count
Write-Output ("PSScriptAnalyzer {0}: {1} Befund(e), davon {2} Fehler" -f $analyzer.Version, $findings.Count, $errors)
if ($errors -gt 0) { exit 1 }
exit 0
