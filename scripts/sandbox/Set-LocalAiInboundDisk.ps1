#Requires -Version 5.1
# Host -> VM ohne Copy-VMFile (inbound: Disk): hängt das vom Broker geschriebene Eingangs-Laufwerk an die VM bzw. ohne
# -Path das angehängte wieder ab. Auf dem Host wird nichts eingebunden. Ausgabe: JSON {nics}.
param(
    [Parameter(Mandatory)][string]$VmName,
    [string]$Path
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LocalAiSandbox.psm1') -Force
Initialize-LocalAiConsole
try {
    Assert-LocalAiVmName -VmName $VmName
    $nics = Get-LocalAiNicCount -VmName $VmName
    # Nur eigene Eingangs-Laufwerke anfassen (Name inbound-*.vhdx).
    Get-VMHardDiskDrive -VMName $VmName -ControllerType SCSI |
        Where-Object { (Split-Path -Leaf $_.Path) -like 'inbound-*.vhdx' } |
        ForEach-Object { Remove-VMHardDiskDrive -VMHardDiskDrive $_ }
    if ($Path) {
        if ((Split-Path -Leaf $Path) -notlike 'inbound-*.vhdx') { throw "Kein Eingangs-Laufwerk: $Path" }
        Add-VMHardDiskDrive -VMName $VmName -ControllerType SCSI -Path $Path
    }
    Write-LocalAiResult @{ nics = $nics }
} catch {
    Write-LocalAiLine ("ERROR " + ($_.Exception.Message -replace '[\r\n]+', ' '))
    exit 1
}
