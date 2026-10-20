#Requires -Version 5.1
# Zustand der Sandbox-VM: Start (bei Bedarf bzw. Fortsetzen), Stop (Gastsystem herunterfahren – Leerlauf und
# Programmende; reagiert der Gast nicht, wird ausgeschaltet), Save (Zustand speichern), TurnOff (Not-Aus, sofort).
# Startet nie eine VM mit Netzwerkkarte. Ausgabe: JSON {state, nics}.
param(
    [Parameter(Mandatory)][string]$VmName,
    [Parameter(Mandatory)][ValidateSet('Start', 'Stop', 'Save', 'TurnOff')][string]$Action
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
    } elseif ($Action -eq 'TurnOff') {
        if ($vm.State -ne 'Off') { Stop-VM -VM $vm -TurnOff -Force }
    } elseif ($Action -eq 'Stop') {
        if ($vm.State -eq 'Saved') {
            # Gespeicherter Zustand: verwerfen genügt (kein laufendes System, das heruntergefahren werden müsste).
            Remove-VMSavedState -VM $vm
        } elseif ($vm.State -ne 'Off') {
            $job = Start-Job -ScriptBlock { param($n) Stop-VM -Name $n -Force } -ArgumentList $VmName
            if (-not (Wait-Job -Job $job -Timeout 180)) {
                Stop-Job -Job $job
                Stop-VM -VM (Get-VM -Name $VmName) -TurnOff -Force
            }
            Remove-Job -Job $job -Force
            if ((Get-VM -Name $VmName).State -ne 'Off') { Stop-VM -VM (Get-VM -Name $VmName) -TurnOff -Force }
        }
    } elseif ($vm.State -eq 'Running') {
        Save-VM -VM $vm
    }
    $vm = Get-VM -Name $VmName
    Write-LocalAiResult @{ state = [string]$vm.State; nics = (Get-LocalAiNicCount -VmName $VmName) }
} catch {
    Write-LocalAiLine ("ERROR " + ($_.Exception.Message -replace '[\r\n]+', ' '))
    exit 1
}
