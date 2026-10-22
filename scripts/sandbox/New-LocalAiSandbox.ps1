#Requires -Version 5.1
<#
.SYNOPSIS
    Baut die Offline-Sandbox-VM neu auf (Spezifikation §8, R17). Braucht Administratorrechte.

.DESCRIPTION
    Wird vom Broker bei jedem Sitzungsstart über UAC gestartet; kann zur Fehlersuche auch von Hand
    in einer Administrator-PowerShell laufen. Ablauf:
      1. Prüfungen  2. alte VM entfernen  3. Abbild öffnen  4. Systemfestplatte anlegen
      5. Windows per DISM anwenden  6. Bootdateien, unattend.xml, Gastdateien  7. Festplatten aushängen,
      Transfer-Laufwerk  8. VM OHNE Netzwerkkarte erstellen  9. VM starten
    Mit hostMount=false (Rechner, auf denen virtuelle Festplatten und ISOs nicht eingebunden werden dürfen) wird nichts
    eingebunden: Windows Setup installiert von der Windows-ISO in die leere Systemfestplatte, Antwortdatei und Gastdateien
    kommen auf einer vom Broker geschriebenen zweiten ISO (unattend.iso). Dauert länger als DISM.
    Fortschritt: Zeilen "STEP n/9 Text", Abschluss "DONE", Fehler "ERROR Text" in -StatusFile.
    Vollständiges Protokoll: <vmDirectory>\build.log
#>
param(
    [Parameter(Mandatory)][string]$RequestFile,
    [Parameter(Mandatory)][string]$StatusFile
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Import-Module (Join-Path $PSScriptRoot 'LocalAiSandbox.psm1') -Force

$steps = 9
function Step([int]$n, [string]$text) {
    Add-LocalAiStatus -StatusFile $StatusFile -Line "STEP $n/$steps $text"
    Write-Host ("[{0:HH:mm:ss}] Schritt {1}/{2}: {3}" -f (Get-Date), $n, $steps, $text)
}

$req = Get-Content -LiteralPath $RequestFile -Raw -Encoding UTF8 | ConvertFrom-Json
$vmName = [string]$req.vmName
$vmDir = [string]$req.vmDirectory
$osDisk = Join-Path $vmDir "$vmName-os.vhdx"
$unattendIso = Join-Path $vmDir "$vmName-unattend.iso"
$hostMount = -not ($req.hostMount -eq $false)
$isoMounted = $null
$vhdMounted = $null
$vhdMethod = 'Hyper-V'
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
    Import-Module Dism
    if (-not (Test-Path -LiteralPath $req.imagePath)) { throw "Windows-Abbild nicht gefunden: $($req.imagePath)" }
    if (-not $hostMount) {
        if ([string]$req.imagePath -notmatch '\.iso$') { throw 'hostMount false: Windows wird von der ISO installiert – imagePath muss eine .iso sein.' }
        if (-not (Test-Path -LiteralPath (Join-Path ([string]$req.guestDirectory) 'unattend.iso'))) { throw 'Aufbaumedium fehlt: unattend.iso' }
    }
    $marker = Join-Path $vmDir $DirectoryMarker
    $foreign = @(Get-ChildItem -LiteralPath $vmDir -Force | Where-Object { $_.Name -notin @('host', 'build.log', $DirectoryMarker, 'gast-kennwort.txt') })
    # (Ein neuer, leerer Ordner bekommt die Markierung; danach gehört er LocalAi. gast-kennwort.txt: bei hostMount false
    # und keepVmOnExit legt der Broker sie schon vor diesem Skript an.)
    if (-not (Test-Path -LiteralPath $marker) -and $foreign.Count -gt 0) {
        throw "Der VM-Ordner $vmDir enthält fremde Dateien und wird nicht verwendet. Leeren Ordner angeben."
    }
    Clear-LocalAiCompression -Path $vmDir
    Set-Content -LiteralPath $marker -Value 'Von LocalAi verwaltet. Inhalt wird bei jedem Sitzungsstart gelöscht.' -Encoding UTF8

    # ------------------------------------------------------------------ 2
    Step 2 'Alte VM entfernen, Speicherplatz prüfen'
    $old = Get-VM -Name $vmName -ErrorAction SilentlyContinue
    if ($old) {
        if (-not (Test-LocalAiOwnedVm -Vm $old)) {
            throw "Es gibt bereits eine VM '$vmName', die nicht von LocalAi stammt. Anderen vmName in config/localai.json wählen."
        }
        $removedDisks = Remove-LocalAiVmWithDisks -Vm $old
        Write-Host "Alte VM entfernt ($removedDisks Festplatten gelöscht)"
    }
    Get-ChildItem -LiteralPath $vmDir -Filter '*.vhdx' -File | Where-Object {
        $_.Name -eq "$vmName-os.vhdx" -or $_.Name -like 'transfer-*.vhdx' -or $_.Name -like 'inbound-*.vhdx'
    } | ForEach-Object { $file = $_.FullName; Invoke-LocalAiRetry { Remove-Item -LiteralPath $file -Force } }
    if (Test-Path -LiteralPath $unattendIso) { Clear-LocalAiFile -Path $unattendIso }
    $vmConfigDir = Join-Path $vmDir $vmName
    if (Test-Path -LiteralPath $vmConfigDir) { Remove-Item -LiteralPath $vmConfigDir -Recurse -Force }

    # Speicherplatz erst prüfen, wenn die alte VM samt Festplatten gelöscht ist (es gibt immer nur eine Sandbox-VM).
    $drive = Get-Item -LiteralPath $vmDir
    $free = (Get-PSDrive -Name $drive.PSDrive.Name).Free
    Write-Host ("Freier Speicher auf {0}: {1:N0} GB" -f $drive.PSDrive.Name, ($free / 1GB))
    if ($free -lt 30GB) { throw ("Zu wenig freier Speicher auf {0}: {1:N0} GB frei, mindestens 30 GB nötig" -f $drive.PSDrive.Name, ($free / 1GB)) }

    if ($hostMount) {
    # Keine Explorer-Fenster für neu eingebundene Laufwerke.
    $shellHw = Get-Service -Name ShellHWDetection -ErrorAction SilentlyContinue
    if ($shellHw -and $shellHw.Status -eq 'Running') { Stop-Service -Name ShellHWDetection -Force } else { $shellHw = $null }
    }

    # ------------------------------------------------------------------ 3
    Step 3 'Windows-Abbild öffnen'
    if (-not $hostMount) {
        Write-Host "Ohne Einbinden: Windows Setup installiert $($req.imagePath) ($(if ($req.imageIndex) { "Index $($req.imageIndex)" } else { $req.imageName }))"
    } else {
    $imagePath = [string]$req.imagePath
    if ($imagePath -match '\.(iso)$') {
        $isoMounted = $imagePath
        Mount-DiskImage -ImagePath $imagePath -StorageType ISO -Access ReadOnly | Out-Null
        $isoLetter = Invoke-LocalAiRetry { $l = (Get-DiskImage -ImagePath $imagePath | Get-Volume).DriveLetter; if (-not $l) { throw 'ISO ohne Laufwerksbuchstaben' }; $l }
        $wim = @("$($isoLetter):\sources\install.wim", "$($isoLetter):\sources\install.esd") | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $wim) { throw 'Im ISO gibt es weder sources\install.wim noch sources\install.esd.' }
    } else {
        $wim = $imagePath
    }
    $images = @(Get-WindowsImage -ImagePath $wim)
    if ($req.imageIndex) {
        $selected = $images | Where-Object { $_.ImageIndex -eq [int]$req.imageIndex }
    } else {
        $selected = $images | Where-Object { $_.ImageName -eq [string]$req.imageName }
    }
    if (-not $selected) {
        $names = ($images | ForEach-Object { "$($_.ImageIndex): $($_.ImageName)" }) -join '; '
        throw "Edition nicht im Abbild gefunden. Vorhanden: $names"
    }
    $index = [int]@($selected)[0].ImageIndex
    $info = Get-WindowsImage -ImagePath $wim -Index $index
    $language = Get-LocalAiImageLanguage -ImageInfo $info
    Write-Host "Abbild: $wim, Index $index ($($info.ImageName)), Sprache $language"
    }

    # ------------------------------------------------------------------ 4
    Step 4 'Systemfestplatte anlegen'
    New-VHD -Path $osDisk -SizeBytes ([int64]$req.osDiskGB * 1GB) -Dynamic | Out-Null
    Clear-LocalAiCompression -Path $osDisk
    Write-Host (Get-LocalAiFileFacts -Path $osDisk)
    if ($hostMount) {
    $mounted = Mount-LocalAiVhd -Path $osDisk
    $vhdMounted = $osDisk
    $vhdMethod = $mounted.Method
    $disk = $mounted.Disk
    Write-Host "Systemfestplatte eingebunden ($vhdMethod), Datenträger $($disk.Number)"
    Initialize-Disk -Number $disk.Number -PartitionStyle GPT
    $basicData = '{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}'
    $sysPart = New-Partition -DiskNumber $disk.Number -Size 260MB -GptType $basicData
    Format-Volume -Partition $sysPart -FileSystem FAT32 -NewFileSystemLabel 'System' -Force -Confirm:$false | Out-Null
    Set-Partition -DiskNumber $disk.Number -PartitionNumber $sysPart.PartitionNumber -GptType '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'
    Add-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $sysPart.PartitionNumber -AssignDriveLetter
    New-Partition -DiskNumber $disk.Number -Size 16MB -GptType '{e3c9e316-0b5c-4db8-817d-f92df00215ae}' | Out-Null
    $winPart = New-Partition -DiskNumber $disk.Number -UseMaximumSize -GptType $basicData
    Format-Volume -Partition $winPart -FileSystem NTFS -NewFileSystemLabel 'Windows' -Force -Confirm:$false | Out-Null
    Add-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $winPart.PartitionNumber -AssignDriveLetter
    $sysLetter = Invoke-LocalAiRetry { $l = (Get-Partition -DiskNumber $disk.Number -PartitionNumber $sysPart.PartitionNumber).DriveLetter; if (-not [char]::IsLetter($l)) { throw 'kein Buchstabe' }; $l }
    $winLetter = Invoke-LocalAiRetry { $l = (Get-Partition -DiskNumber $disk.Number -PartitionNumber $winPart.PartitionNumber).DriveLetter; if (-not [char]::IsLetter($l)) { throw 'kein Buchstabe' }; $l }
    $winRoot = "$($winLetter):\"

    # ------------------------------------------------------------------ 5
    Step 5 'Windows anwenden (DISM, dauert einige Minuten)'
    Expand-WindowsImage -ImagePath $wim -Index $index -ApplyPath $winRoot -LogPath (Join-Path $vmDir 'dism.log') | Out-Null

    # ------------------------------------------------------------------ 6
    Step 6 'Bootdateien, Antwortdatei und Gastdateien'
    & (Join-Path $env:WINDIR 'System32\bcdboot.exe') "$($winLetter):\Windows" /s "$($sysLetter):" /f UEFI | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "bcdboot fehlgeschlagen (Exitcode $LASTEXITCODE)" }
    $panther = Join-Path $winRoot 'Windows\Panther'
    New-Item -ItemType Directory -Force -Path $panther | Out-Null
    $password = New-LocalAiPassword
    $passwordFile = Join-Path $vmDir 'gast-kennwort.txt'
    if (Test-Path -LiteralPath $passwordFile) { Remove-Item -LiteralPath $passwordFile -Force }
    if ($req.keepVmOnExit -eq $true) {
        # Nur zur Fehlersuche (keepVmOnExit): Anmeldung an der VM-Konsole als 'localai'.
        Set-Content -LiteralPath $passwordFile -Value $password -Encoding ASCII
    }
    $unattend = New-LocalAiUnattendXml -UiLanguage $language -Password $password -ProductKey ([string]$req.productKey)
    [System.IO.File]::WriteAllText((Join-Path $panther 'unattend.xml'), $unattend, (New-Object System.Text.UTF8Encoding($false)))
    $setupScripts = Join-Path $winRoot 'Windows\Setup\Scripts'
    New-Item -ItemType Directory -Force -Path $setupScripts | Out-Null
    $guest = [string]$req.guestDirectory
    Copy-Item -LiteralPath (Join-Path $guest 'SetupComplete.cmd') -Destination $setupScripts -Force
    Copy-Item -LiteralPath (Join-Path $guest 'LocalAi') -Destination (Join-Path $winRoot 'LocalAi') -Recurse -Force
    } else {
        Step 5 'Windows Setup vorbereiten (ohne Einbinden, Installation in der VM)'
        Step 6 'Setup-Medium mit Antwortdatei und Gastdateien'
        Move-Item -LiteralPath (Join-Path ([string]$req.guestDirectory) 'unattend.iso') -Destination $unattendIso -Force
        Write-Host ("Setup-Medium {0:N0} MB" -f ((Get-Item -LiteralPath $unattendIso).Length / 1MB))
    }

    # ------------------------------------------------------------------ 7
    Step 7 'Festplatten aushängen, Transfer-Laufwerk anlegen'
    if ($hostMount) {
    Dismount-LocalAiVhd -Path $osDisk -Method $vhdMethod
    $vhdMounted = $null
    if ($isoMounted) { Dismount-DiskImage -ImagePath $isoMounted | Out-Null; $isoMounted = $null }
    }
    $template = [string]$req.transferTemplate
    if (Test-Path -LiteralPath $template) { Remove-Item -LiteralPath $template -Force }
    # Dynamisch: nicht belegte Blöcke liest der Gast garantiert als Nullen. Eine feste VHDX ist nicht auf jedem Datenträger
    # genullt (z. B. schnelle Anlage auf ReFS/Dev Drive oder Speichertreiber) – dann gilt sie im Gast als „nicht leer“.
    New-VHD -Path $template -SizeBytes ([int64]$req.transferDiskMB * 1MB) -Dynamic | Out-Null
    Clear-LocalAiCompression -Path $template
    Copy-Item -LiteralPath $template -Destination ([string]$req.transferDisk) -Force

    # ------------------------------------------------------------------ 8
    Step 8 'VM ohne Netzwerkkarte erstellen'
    $vm = New-VM -Name $vmName -Generation 2 -MemoryStartupBytes ([int64]$req.memoryMB * 1MB) -VHDPath $osDisk -Path $vmDir
    # New-VM legt immer einen (nicht verbundenen) Netzwerkadapter an: vor dem ersten Start entfernen.
    Get-VMNetworkAdapter -VM $vm | Remove-VMNetworkAdapter
    if ((Get-LocalAiNicCount -VmName $vmName) -ne 0) { throw 'Netzwerkadapter ließ sich nicht entfernen – Abbruch (SEC-24).' }
    Set-VM -VM $vm -Notes $VmNotes -CheckpointType Disabled -AutomaticStartAction Nothing -AutomaticStopAction TurnOff
    try { Set-VM -VM $vm -AutomaticCheckpointsEnabled $false } catch { }
    Set-VMMemory -VM $vm -DynamicMemoryEnabled $false -StartupBytes ([int64]$req.memoryMB * 1MB)
    Set-VMProcessor -VM $vm -Count ([int]$req.processors)
    $osDrive = Get-VMHardDiskDrive -VM $vm | Select-Object -First 1
    if ($hostMount) {
        Set-VMFirmware -VM $vm -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows -FirstBootDevice $osDrive
    } else {
        # Erst von der Windows-ISO starten (Setup), danach von der Systemfestplatte; Antwortdatei auf der zweiten DVD.
        $setupDvd = Add-VMDvdDrive -VM $vm -Path ([string]$req.imagePath) -Passthru
        Add-VMDvdDrive -VM $vm -Path $unattendIso
        Set-VMFirmware -VM $vm -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows -FirstBootDevice $setupDvd
    }
    Set-LocalAiIntegrationServices -Vm $vm
    Add-VMHardDiskDrive -VM $vm -ControllerType SCSI -Path ([string]$req.transferDisk)
    if ((Get-LocalAiNicCount -VmName $vmName) -ne 0) { throw 'VM hat eine Netzwerkkarte – Abbruch (SEC-24).' }

    # ------------------------------------------------------------------ 9
    Step 9 'VM starten'
    Start-VM -VM $vm
    if ((Get-LocalAiNicCount -VmName $vmName) -ne 0) {
        Stop-VM -VM $vm -TurnOff -Force
        throw 'VM hat eine Netzwerkkarte – sofort gestoppt (SEC-24).'
    }
    if (-not $hostMount) {
        Write-Host 'Bestätige „Press any key to boot from CD“ (Tastatur der VM) …'
        Send-LocalAiBootKeys -VmName $vmName -Seconds 40
    }
    Write-Host ("Aufbau auf dem Host fertig nach {0:N1} Minuten." -f ((Get-Date) - $started).TotalMinutes)
    Add-LocalAiStatus -StatusFile $StatusFile -Line 'DONE'
    $exitCode = 0
}
catch {
    $message = Get-LocalAiErrorText -ErrorRecord $_
    Write-Host "FEHLER: $message"
    Write-Host $_.ScriptStackTrace
    Write-Host ($_ | Format-List * -Force | Out-String)
    Add-LocalAiStatus -StatusFile $StatusFile -Line "ERROR $message"
}
finally {
    if ($vhdMounted) { try { Dismount-LocalAiVhd -Path $vhdMounted -Method $vhdMethod } catch { } }
    if ($isoMounted) { try { Dismount-DiskImage -ImagePath $isoMounted | Out-Null } catch { } }
    if ($shellHw) { try { Start-Service -Name ShellHWDetection } catch { } }
    if ($transcript) { try { Stop-Transcript | Out-Null } catch { } }
}
exit $exitCode
