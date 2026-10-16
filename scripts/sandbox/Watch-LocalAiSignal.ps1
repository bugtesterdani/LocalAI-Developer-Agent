#Requires -Version 5.1
# Beobachtet das Hyper-V-KVP-Register der Sandbox-VM und gibt neue Werte von »LocalAiSignal« aus:
#   SIGNAL result:<sequenz> | SIGNAL report:0     – nur dieses feste Format, alles andere wird verworfen
#   NICS <n>                                        – die VM hat plötzlich Netzwerkkarten
#   ERROR <text>                                    – Beobachtung nicht möglich
param(
    [Parameter(Mandatory)][string]$VmName,
    [int]$IntervalMs = 500
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LocalAiSandbox.psm1') -Force
Initialize-LocalAiConsole
Assert-LocalAiVmName -VmName $VmName

$last = $null
$failures = 0
$round = 0
while ($true) {
    try {
        $vm = Get-CimInstance -Namespace 'root\virtualization\v2' -ClassName 'Msvm_ComputerSystem' -Filter "ElementName='$VmName'"
        if (-not $vm) { Write-LocalAiLine 'ERROR VM nicht gefunden'; exit 1 }
        $kvp = Get-CimAssociatedInstance -InputObject @($vm)[0] -ResultClassName 'Msvm_KvpExchangeComponent'
        $value = $null
        if ($kvp) {
            foreach ($item in @($kvp.GuestExchangeItems)) {
                if (-not $item) { continue }
                $xml = [xml]$item
                $props = @($xml.INSTANCE.PROPERTY)
                $name = ($props | Where-Object { $_.NAME -eq 'Name' } | Select-Object -First 1).VALUE
                if ($name -eq 'LocalAiSignal') {
                    $value = [string](($props | Where-Object { $_.NAME -eq 'Data' } | Select-Object -First 1).VALUE)
                }
            }
        }
        if ($value -and $value -ne $last) {
            $last = $value
            if ($value -match '^(result|report):[0-9]{1,18}$') { Write-LocalAiLine "SIGNAL $value" } else { Write-LocalAiLine 'SIGNAL ungueltig' }
        }
        if (($round % 10) -eq 0) {
            $nics = Get-LocalAiNicCount -VmName $VmName
            if ($nics -ne 0) { Write-LocalAiLine "NICS $nics" }
        }
        $failures = 0
    } catch {
        $failures++
        if ($failures -ge 20) {
            Write-LocalAiLine ("ERROR " + ($_.Exception.Message -replace '[\r\n]+', ' '))
            exit 1
        }
    }
    $round++
    Start-Sleep -Milliseconds $IntervalMs
}
