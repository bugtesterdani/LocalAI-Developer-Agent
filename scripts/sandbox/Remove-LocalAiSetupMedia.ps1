#Requires -Version 5.1
# hostMount: false – nach dem bestandenen Selbsttest: alle DVD-Laufwerke der VM leeren und die eigenen Aufbaumedien
# (<vm>-seed.iso, <vm>-unattend.iso mit Sitzungsschlüssel bzw. Kennwort) überschreiben und löschen. Die Windows-ISO
# des Benutzers wird nur ausgeworfen, nie gelöscht. Ausgabe: JSON {removed}.
param([Parameter(Mandatory)][string]$VmName)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LocalAiSandbox.psm1') -Force
Initialize-LocalAiConsole
try {
    Assert-LocalAiVmName -VmName $VmName
    $vm = Get-VM -Name $VmName
    if (-not (Test-LocalAiOwnedVm -Vm $vm)) { throw "VM '$VmName' stammt nicht von LocalAi." }
    $removed = 0
    foreach ($dvd in @(Get-VMDvdDrive -VM $vm)) {
        $path = $dvd.Path
        Set-VMDvdDrive -VMDvdDrive $dvd -Path $null
        if (-not $path) { continue }
        $name = Split-Path -Leaf $path
        $folder = Split-Path -Parent $path
        if (($name -ieq "$VmName-seed.iso" -or $name -ieq "$VmName-unattend.iso") -and (Test-Path -LiteralPath (Join-Path $folder $DirectoryMarker))) {
            Clear-LocalAiFile -Path $path
            $removed++
        }
    }
    Write-LocalAiResult @{ removed = $removed }
} catch {
    Write-LocalAiLine ("ERROR " + ($_.Exception.Message -replace '[\r\n]+', ' '))
    exit 1
}
