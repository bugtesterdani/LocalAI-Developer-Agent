#Requires -Version 5.1
# VM -> Host: hängt das beschriebene Transfer-Laufwerk im laufenden Betrieb ab, verschiebt es nach -Detached
# (der Broker liest es ohne Einbinden) und hängt eine frische Kopie der leeren Vorlage an. Ausgabe: JSON {nics}.
param(
    [Parameter(Mandatory)][string]$VmName,
    [Parameter(Mandatory)][string]$Current,
    [Parameter(Mandatory)][string]$Template,
    [Parameter(Mandatory)][string]$New,
    [Parameter(Mandatory)][string]$Detached
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LocalAiSandbox.psm1') -Force
Initialize-LocalAiConsole
try {
    Assert-LocalAiVmName -VmName $VmName
    $nics = Get-LocalAiNicCount -VmName $VmName
    $drive = Get-VMHardDiskDrive -VMName $VmName -ControllerType SCSI | Where-Object { $_.Path -ieq $Current }
    if (-not $drive) { throw "Transfer-Laufwerk ist nicht angehängt: $Current" }
    Remove-VMHardDiskDrive -VMHardDiskDrive $drive
    Invoke-LocalAiRetry { Move-Item -LiteralPath $Current -Destination $Detached -Force } | Out-Null
    Copy-Item -LiteralPath $Template -Destination $New -Force
    Add-VMHardDiskDrive -VMName $VmName -ControllerType SCSI -Path $New
    Write-LocalAiResult @{ nics = $nics }
} catch {
    Write-LocalAiLine ("ERROR " + ($_.Exception.Message -replace '[\r\n]+', ' '))
    exit 1
}
