#Requires -Version 5.1
<#
.SYNOPSIS
    Baut die Linux-Sandbox-VM neu auf (M8, Debian 13). Braucht Administratorrechte.

.DESCRIPTION
    Wird vom Broker über UAC gestartet (wie New-LocalAiSandbox.ps1 für Windows); kann zur Fehlersuche auch von Hand
    in einer Administrator-PowerShell laufen. Ablauf:
      1. Prüfungen  2. alte VM entfernen  3. Systemfestplatte aus dem Debian-Cloud-Abbild (.raw → VHDX)
      4. Seed-Laufwerk (FAT32, Bezeichnung CIDATA) für cloud-init mit allen Gastdateien  5. Transfer-Laufwerk
      6. VM OHNE Netzwerkkarte erstellen (Generation 2, Secure Boot mit Microsoft-UEFI-CA)  7. VM starten
    Fortschritt: Zeilen "STEP n/7 Text", Abschluss "DONE", Fehler "ERROR Text" in -StatusFile.
    Vollständiges Protokoll: <vmDirectory>\build.log
#>
param(
    [Parameter(Mandatory)][string]$RequestFile,
    [Parameter(Mandatory)][string]$StatusFile
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Import-Module (Join-Path $PSScriptRoot 'LocalAiSandbox.psm1') -Force

$steps = 7
function Step([int]$n, [string]$text) {
    Add-LocalAiStatus -StatusFile $StatusFile -Line "STEP $n/$steps $text"
    Write-Host ("[{0:HH:mm:ss}] Schritt {1}/{2}: {3}" -f (Get-Date), $n, $steps, $text)
}

$req = Get-Content -LiteralPath $RequestFile -Raw -Encoding UTF8 | ConvertFrom-Json
$vmName = [string]$req.vmName
$vmDir = [string]$req.vmDirectory
$osDisk = Join-Path $vmDir "$vmName-os.vhdx"
$seedDisk = Join-Path $vmDir "$vmName-seed.vhdx"
$mountedDisks = New-Object System.Collections.Generic.List[string]
$shellHw = $null
$transcript = $false
$started = Get-Date
$exitCode = 1

try {
    Assert-LocalAiVmName -VmName $vmName
    New-Item -ItemType Directory -Force -Path $vmDir | Out-Null
    try { Start-Transcript -LiteralPath (Join-Path $vmDir 'build.log') -Force | Out-Null; $transcript = $true } catch { }

    # ------------------------------------------------------------------ 1
    Step 1 'Voraussetzungen prüfen'
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Der Aufbau braucht Administratorrechte.'
    }
    Import-Module Hyper-V
    if ([string]$req.guest -ne 'Linux') { throw 'Anfrage ist nicht für einen Linux-Gast.' }
    $image = [string]$req.imagePath
    if (-not (Test-Path -LiteralPath $image)) { throw "Debian-Abbild nicht gefunden: $image" }
    $seedSource = Join-Path ([string]$req.guestDirectory) 'seed'
    foreach ($f in 'user-data', 'meta-data', 'localai\provision.sh') {
        if (-not (Test-Path -LiteralPath (Join-Path $seedSource $f))) { throw "Gastdatei fehlt: $f" }
    }
    $marker = Join-Path $vmDir $DirectoryMarker
    $foreign = @(Get-ChildItem -LiteralPath $vmDir -Force | Where-Object { $_.Name -notin @('host', 'build.log', $DirectoryMarker, 'gast-kennwort.txt') })
    if (-not (Test-Path -LiteralPath $marker) -and $foreign.Count -gt 0) {
        throw "Der VM-Ordner $vmDir enthält fremde Dateien und wird nicht verwendet. Leeren Ordner angeben."
    }
    Clear-LocalAiCompression -Path $vmDir
    Set-Content -LiteralPath $marker -Value 'Von LocalAi verwaltet. Inhalt wird beim Neuaufbau gelöscht.' -Encoding UTF8

    # ------------------------------------------------------------------ 2
    Step 2 'Alte VM entfernen, Speicherplatz prüfen'
    $old = Get-VM -Name $vmName -ErrorAction SilentlyContinue
    if ($old) {
        if (-not (Test-LocalAiOwnedVm -Vm $old)) {
            throw "Es gibt bereits eine VM '$vmName', die nicht von LocalAi stammt. Anderen vmName in config/localai.json (sandbox.hypervLinux) wählen."
        }
        $removedDisks = Remove-LocalAiVmWithDisks -Vm $old
        Write-Host "Alte VM entfernt ($removedDisks Festplatten gelöscht)"
    }
    Get-ChildItem -LiteralPath $vmDir -Filter '*.vhdx' -File | Where-Object {
        $_.Name -eq "$vmName-os.vhdx" -or $_.Name -eq "$vmName-seed.vhdx" -or $_.Name -like 'transfer-*.vhdx'
    } | ForEach-Object { $file = $_.FullName; Invoke-LocalAiRetry { Remove-Item -LiteralPath $file -Force } }
    $vmConfigDir = Join-Path $vmDir $vmName
    if (Test-Path -LiteralPath $vmConfigDir) { Remove-Item -LiteralPath $vmConfigDir -Recurse -Force }
    $drive = Get-Item -LiteralPath $vmDir
    $free = (Get-PSDrive -Name $drive.PSDrive.Name).Free
    Write-Host ("Freier Speicher auf {0}: {1:N0} GB" -f $drive.PSDrive.Name, ($free / 1GB))
    if ($free -lt 10GB) { throw ("Zu wenig freier Speicher auf {0}: {1:N0} GB frei, mindestens 10 GB nötig" -f $drive.PSDrive.Name, ($free / 1GB)) }
    $shellHw = Get-Service -Name ShellHWDetection -ErrorAction SilentlyContinue
    if ($shellHw -and $shellHw.Status -eq 'Running') { Stop-Service -Name ShellHWDetection -Force } else { $shellHw = $null }

    # ------------------------------------------------------------------ 3
    Step 3 'Systemfestplatte aus dem Debian-Abbild schreiben'
    $rawSize = (Get-Item -LiteralPath $image).Length
    $osBytes = [int64]$req.osDiskGB * 1GB
    if ($rawSize -gt $osBytes) { throw "osDiskGB ist kleiner als das Abbild ($([math]::Ceiling($rawSize / 1GB)) GB)" }
    New-VHD -Path $osDisk -SizeBytes $osBytes -Dynamic | Out-Null
    Clear-LocalAiCompression -Path $osDisk
    $disk = Mount-VHD -Path $osDisk -Passthru -NoDriveLetter | Get-Disk
    $mountedDisks.Add($osDisk)
    Write-Host "Systemfestplatte eingebunden, Datenträger $($disk.Number); Abbild $([math]::Round($rawSize / 1MB)) MB"
    # Rohdaten blockweise schreiben; Nullblöcke überspringen (die dynamische VHDX liest sie ohnehin als Null).
    $written = Write-LocalAiRawImage -ImagePath $image -DiskNumber $disk.Number
    Write-Host ("{0:N0} MB Daten geschrieben (Rest leer)" -f ($written / 1MB))
    Dismount-VHD -Path $osDisk
    $mountedDisks.Remove($osDisk) | Out-Null

    # ------------------------------------------------------------------ 4
    Step 4 'Seed-Laufwerk für cloud-init (CIDATA) mit den Gastdateien'
    $payload = (Get-ChildItem -LiteralPath $seedSource -Recurse -File | Measure-Object -Property Length -Sum).Sum
    # FAT32: mindestens 512 MB; nie genau so groß wie das Transfer-Laufwerk (der Gast erkennt dieses an seiner Größe).
    $seedMB = [int64][math]::Max(512, [math]::Ceiling($payload * 1.3 / 1MB) + 256)
    if ($seedMB -eq [int64]$req.transferDiskMB) { $seedMB += 64 }
    if ($seedMB -gt 32000) { throw "Gastdateien zu groß für das Seed-Laufwerk ($([math]::Round($payload / 1GB, 1)) GB) – weniger bzw. kleinere Toolchains wählen" }
    New-VHD -Path $seedDisk -SizeBytes ($seedMB * 1MB) -Dynamic | Out-Null
    Clear-LocalAiCompression -Path $seedDisk
    $sdisk = Mount-VHD -Path $seedDisk -Passthru -NoDriveLetter | Get-Disk
    $mountedDisks.Add($seedDisk)
    Initialize-Disk -Number $sdisk.Number -PartitionStyle MBR
    $part = New-Partition -DiskNumber $sdisk.Number -UseMaximumSize -MbrType FAT32
    Format-Volume -Partition $part -FileSystem FAT32 -NewFileSystemLabel 'CIDATA' -Force -Confirm:$false | Out-Null
    Add-PartitionAccessPath -DiskNumber $sdisk.Number -PartitionNumber $part.PartitionNumber -AssignDriveLetter
    $seedLetter = Invoke-LocalAiRetry { $l = (Get-Partition -DiskNumber $sdisk.Number -PartitionNumber $part.PartitionNumber).DriveLetter; if (-not [char]::IsLetter($l)) { throw 'kein Buchstabe' }; $l }
    Copy-Item -Path (Join-Path $seedSource '*') -Destination "$($seedLetter):\" -Recurse -Force
    Write-Host ("Seed-Laufwerk {0} MB, Gastdateien {1:N0} MB" -f $seedMB, ($payload / 1MB))
    Dismount-VHD -Path $seedDisk
    $mountedDisks.Remove($seedDisk) | Out-Null

    # ------------------------------------------------------------------ 5
    Step 5 'Transfer-Laufwerk anlegen'
    $template = [string]$req.transferTemplate
    if (Test-Path -LiteralPath $template) { Remove-Item -LiteralPath $template -Force }
    New-VHD -Path $template -SizeBytes ([int64]$req.transferDiskMB * 1MB) -Fixed | Out-Null
    Clear-LocalAiCompression -Path $template
    Copy-Item -LiteralPath $template -Destination ([string]$req.transferDisk) -Force

    # ------------------------------------------------------------------ 6
    Step 6 'VM ohne Netzwerkkarte erstellen'
    $vm = New-VM -Name $vmName -Generation 2 -MemoryStartupBytes ([int64]$req.memoryMB * 1MB) -VHDPath $osDisk -Path $vmDir
    Get-VMNetworkAdapter -VM $vm | Remove-VMNetworkAdapter
    if ((Get-LocalAiNicCount -VmName $vmName) -ne 0) { throw 'Netzwerkadapter ließ sich nicht entfernen – Abbruch (SEC-24).' }
    Set-VM -VM $vm -Notes $VmNotes -CheckpointType Disabled -AutomaticStartAction Nothing -AutomaticStopAction ShutDown
    try { Set-VM -VM $vm -AutomaticCheckpointsEnabled $false } catch { }
    Set-VMMemory -VM $vm -DynamicMemoryEnabled $false -StartupBytes ([int64]$req.memoryMB * 1MB)
    Set-VMProcessor -VM $vm -Count ([int]$req.processors)
    if ($req.nestedVirtualization -eq $true) {
        # M9: KVM für QEMU in der VM (verschachtelte Virtualisierung); ohne bleibt QEMU bei reiner Emulation (TCG).
        Set-VMProcessor -VM $vm -ExposeVirtualizationExtensions $true
    }
    $osDrive = Get-VMHardDiskDrive -VM $vm | Select-Object -First 1
    # Debian startet über shim, signiert mit der Microsoft-UEFI-CA (nicht mit der Windows-Vorlage).
    Set-VMFirmware -VM $vm -EnableSecureBoot On -SecureBootTemplate MicrosoftUEFICertificateAuthority -FirstBootDevice $osDrive
    Set-LocalAiIntegrationServices -Vm $vm
    Add-VMHardDiskDrive -VM $vm -ControllerType SCSI -Path $seedDisk
    Add-VMHardDiskDrive -VM $vm -ControllerType SCSI -Path ([string]$req.transferDisk)
    if ((Get-LocalAiNicCount -VmName $vmName) -ne 0) { throw 'VM hat eine Netzwerkkarte – Abbruch (SEC-24).' }

    # ------------------------------------------------------------------ 7
    Step 7 'VM starten'
    Start-VM -VM $vm
    if ((Get-LocalAiNicCount -VmName $vmName) -ne 0) {
        Stop-VM -VM $vm -TurnOff -Force
        throw 'VM hat eine Netzwerkkarte – sofort gestoppt (SEC-24).'
    }
    Write-Host ("Aufbau auf dem Host fertig nach {0:N1} Minuten." -f ((Get-Date) - $started).TotalMinutes)
    Add-LocalAiStatus -StatusFile $StatusFile -Line 'DONE'
    $exitCode = 0
}
catch {
    $message = Get-LocalAiErrorText -ErrorRecord $_
    Write-Host "FEHLER: $message"
    Write-Host $_.ScriptStackTrace
    Add-LocalAiStatus -StatusFile $StatusFile -Line "ERROR $message"
}
finally {
    foreach ($d in @($mountedDisks)) { try { Dismount-VHD -Path $d } catch { } }
    if ($shellHw) { try { Start-Service -Name ShellHWDetection } catch { } }
    if ($transcript) { try { Stop-Transcript | Out-Null } catch { } }
}
exit $exitCode
