#Requires -Version 5.1
# Zustand der Sandbox-VM. Ausgabe: JSON {exists, state, nics}.
param([Parameter(Mandatory)][string]$VmName)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LocalAiSandbox.psm1') -Force
Initialize-LocalAiConsole
try {
    Assert-LocalAiVmName -VmName $VmName
    $vm = Get-VM -Name $VmName -ErrorAction SilentlyContinue
    if (-not $vm) {
        Write-LocalAiResult @{ exists = $false; state = ''; nics = 0 }
    } else {
        Write-LocalAiResult @{ exists = $true; state = [string]$vm.State; nics = (Get-LocalAiNicCount -VmName $VmName) }
    }
} catch {
    Write-LocalAiLine ("ERROR " + ($_.Exception.Message -replace '[\r\n]+', ' '))
    exit 1
}
