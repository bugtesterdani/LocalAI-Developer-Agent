#Requires -Version 5.1
<#
.SYNOPSIS
    Nur zur Fehlersuche: holt die Protokolle aus der Sandbox-VM (als Administrator ausführen).

.DESCRIPTION
    Hält die VM an (falls sie läuft), bindet ihre Systemfestplatte SCHREIBGESCHÜTZT ein und kopiert
    die Einrichtungs- und Provisionierungsprotokolle in einen Ordner auf dem Host. Danach wird die
    Festplatte wieder ausgehängt.

    Achtung: Hier liest der Host ein vom Gast beschriebenes Dateisystem. Das ist im normalen Betrieb
    bewusst ausgeschlossen (Spezifikation §8) und nur als manueller Diagnoseschritt gedacht. Die
    kopierten Dateien sind Text und werden nicht ausgeführt.

    Damit die VM nach einem Fehlschlag noch existiert: in config\localai.json "keepVmOnExit": true.
#>
param(
    [string]$VmName = 'LocalAi-Sandbox',
    [string]$Destination = (Join-Path (Get-Location) ("gastprotokolle-" + (Get-Date -Format 'yyyyMMdd-HHmmss')))
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LocalAiSandbox.psm1') -Force
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Bitte in einer PowerShell »Als Administrator ausführen« starten.'
}
Assert-LocalAiVmName -VmName $VmName
$vm = Get-VM -Name $VmName -ErrorAction SilentlyContinue
if (-not $vm) { throw "VM '$VmName' nicht gefunden. Für die Fehlersuche keepVmOnExit auf true setzen und den Aufbau wiederholen." }
if (-not (Test-LocalAiOwnedVm -Vm $vm)) { throw "VM '$VmName' stammt nicht von LocalAi." }

$osDisk = @(Get-VMHardDiskDrive -VM $vm | Where-Object { $_.Path -like '*-os.vhdx' } | Select-Object -ExpandProperty Path)[0]
if (-not $osDisk) { throw 'Systemfestplatte der VM nicht gefunden.' }
if ($vm.State -ne 'Off') {
    Write-Host 'VM wird ausgeschaltet …'
    Stop-VM -VM $vm -TurnOff -Force
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$summary = New-Object System.Collections.Generic.List[string]
$summary.Add("VM: $VmName, Systemfestplatte: $osDisk, Heartbeat zuletzt: $($vm.Heartbeat)")
$mountDir = $null
try {
    $disk = Mount-VHD -Path $osDisk -ReadOnly -Passthru | Get-Disk
    $partition = Get-Partition -DiskNumber $disk.Number | Sort-Object Size -Descending | Select-Object -First 1
    $root = $null
    if ($partition.DriveLetter -and [char]::IsLetter($partition.DriveLetter)) {
        $root = "$($partition.DriveLetter):\"
    } else {
        $mountDir = Join-Path $env:TEMP ("localai-gast-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $mountDir | Out-Null
        Add-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $partition.PartitionNumber -AccessPath $mountDir
        $root = $mountDir + '\'
    }
    Write-Host "Gastlaufwerk schreibgeschützt eingebunden: $root"

    $items = @(
        'LocalAi\logs', 'LocalAi\report.json', 'LocalAi\guest-config.json', 'LocalAi\worker.cmd',
        'Windows\Panther\setupact.log', 'Windows\Panther\setuperr.log',
        'Windows\Panther\UnattendGC\setupact.log', 'Windows\Panther\UnattendGC\setuperr.log',
        'Windows\Setup\State\State.ini', 'Windows\Setup\Scripts\SetupComplete.cmd', 'LocalAi\provision.ps1'
    )
    foreach ($rel in $items) {
        $source = Join-Path $root $rel
        if (Test-Path -LiteralPath $source) {
            $target = Join-Path $Destination ($rel -replace '\\', '_')
            Copy-Item -LiteralPath $source -Destination $target -Recurse -Force
            $summary.Add("vorhanden: $rel")
        } else {
            $summary.Add("FEHLT:     $rel")
        }
    }
    $python = Join-Path $root 'LocalAi\python\python.exe'
    $summary.Add("Python im Gast: $(if (Test-Path -LiteralPath $python) { 'vorhanden' } else { 'FEHLT' })")
} finally {
    if ($mountDir) { try { Remove-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $partition.PartitionNumber -AccessPath $mountDir } catch { } }
    try { Dismount-VHD -Path $osDisk } catch { }
    if ($mountDir) { Remove-Item -LiteralPath $mountDir -Force -ErrorAction SilentlyContinue }
}
$summary | Set-Content -LiteralPath (Join-Path $Destination 'zusammenfassung.txt') -Encoding UTF8
$summary | ForEach-Object { Write-Host $_ }
Write-Host "Protokolle liegen in: $Destination"
