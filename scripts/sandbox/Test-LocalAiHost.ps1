#Requires -Version 5.1
# Prüft ohne Administratorrechte, ob Hyper-V verfügbar und bedienbar ist. Ausgabe: JSON {ok, message}.
param([Parameter(Mandatory)][string]$VmDirectory)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LocalAiSandbox.psm1') -Force
Initialize-LocalAiConsole

function Result([bool]$ok, [string]$message) {
    Write-LocalAiResult @{ ok = $ok; message = $message }
    exit 0
}

if (-not (Get-Module -ListAvailable -Name Hyper-V)) {
    Result $false 'PowerShell-Modul Hyper-V fehlt – Windows-Feature »Hyper-V« samt Verwaltungstools aktivieren.'
}
Import-Module Hyper-V
$vmms = Get-Service -Name vmms -ErrorAction SilentlyContinue
if (-not $vmms -or $vmms.Status -ne 'Running') {
    Result $false 'Der Dienst »Hyper-V-Verwaltung für virtuelle Computer« (vmms) läuft nicht.'
}
try {
    $null = Get-VM -ErrorAction Stop
    $null = Get-VMHost -ErrorAction Stop
} catch {
    Result $false ("Keine Berechtigung für Hyper-V. Benutzer in die Gruppe »Hyper-V-Administratoren« aufnehmen und neu anmelden. ({0})" -f $_.Exception.Message)
}
$copy = Get-Command -Name Copy-VMFile -ErrorAction SilentlyContinue
if (-not $copy) {
    Result $false 'Copy-VMFile fehlt im Hyper-V-Modul.'
}
Result $true 'Hyper-V verfügbar'
