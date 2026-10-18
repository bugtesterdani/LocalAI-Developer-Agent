#Requires -Version 5.1
# Wiederverwendung der Sandbox-VM (Option reuseVm): startet bzw. setzt eine vorhandene VM fort oder speichert
# ihren Zustand beim Beenden. Startet nie eine VM mit Netzwerkkarte. Ausgabe: JSON {state, nics}.
param(
    [Parameter(Mandatory)][string]$VmName,
    [Parameter(Mandatory)][ValidateSet('Start', 'Save')][string]$Action
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LocalAiSandbox.psm1') -Force
Initialize-LocalAiConsole
try {
    Assert-LocalAiVmName -VmName $VmName
    $vm = Get-VM -Name $VmName -ErrorAction Stop
    $nics = Get-LocalAiNicCount -VmName $VmName
    if ($Action -eq 'Start') {
        if ($nics -ne 0) { throw "VM hat $nics Netzwerkkarte(n) - wird nicht gestartet" }
        if ($vm.State -ne 'Running') { Start-VM -VM $vm }
    } elseif ($vm.State -eq 'Running') {
        Save-VM -VM $vm
    }
    $vm = Get-VM -Name $VmName
    Write-LocalAiResult @{ state = [string]$vm.State; nics = (Get-LocalAiNicCount -VmName $VmName) }
} catch {
    Write-LocalAiLine ("ERROR " + ($_.Exception.Message -replace '[\r\n]+', ' '))
    exit 1
}
