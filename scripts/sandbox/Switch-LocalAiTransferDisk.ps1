#Requires -Version 5.1
# Transfer-Laufwerk (VM -> Host) hängt nur während eines Jobs an der VM.
#   -Current/-Detached: beschriebenes Laufwerk im laufenden Betrieb abhängen und nach -Detached verschieben; erst danach
#                       liest der Broker es (ohne Einbinden). Bis zum nächsten Job hängt keins an der VM.
#   -Template/-New:     vor einem Job eine frische Kopie der leeren Vorlage anhängen.
# Ausgabe: JSON {nics}.
[CmdletBinding(DefaultParameterSetName = 'Detach')]
param(
    [Parameter(Mandatory)][string]$VmName,
    [Parameter(Mandatory, ParameterSetName = 'Detach')][string]$Current,
    [Parameter(Mandatory, ParameterSetName = 'Detach')][string]$Detached,
    [Parameter(Mandatory, ParameterSetName = 'Attach')][string]$Template,
    [Parameter(Mandatory, ParameterSetName = 'Attach')][string]$New
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LocalAiSandbox.psm1') -Force
Initialize-LocalAiConsole
try {
    Assert-LocalAiVmName -VmName $VmName
    $nics = Get-LocalAiNicCount -VmName $VmName
    if ($PSCmdlet.ParameterSetName -eq 'Detach') {
        $drive = Get-VMHardDiskDrive -VMName $VmName -ControllerType SCSI | Where-Object { $_.Path -ieq $Current }
        if ($drive) {
            Remove-VMHardDiskDrive -VMHardDiskDrive $drive
        }
        # Nicht angehängt, aber vorhanden: Abbruch zwischen Kopieren und Anhängen bzw. Abhängen und Verschieben – nur
        # noch verschieben. Weder noch: Anhängen kam nie so weit, nichts zu tun (kein -Detached, der Broker liest nichts).
        if (Test-Path -LiteralPath $Current) {
            Invoke-LocalAiRetry { Move-Item -LiteralPath $Current -Destination $Detached -Force } | Out-Null
        }
    } else {
        Copy-Item -LiteralPath $Template -Destination $New -Force
        Add-VMHardDiskDrive -VMName $VmName -ControllerType SCSI -Path $New
    }
    Write-LocalAiResult @{ nics = $nics }
} catch {
    Write-LocalAiLine ("ERROR " + ($_.Exception.Message -replace '[\r\n]+', ' '))
    exit 1
}
