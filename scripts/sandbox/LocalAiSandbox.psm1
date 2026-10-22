# Gemeinsame Funktionen der Sandbox-Skripte (Windows PowerShell 5.1 und PowerShell 7).
Set-StrictMode -Version 3.0

# Integrationsdienste über ihre festen IDs (die Namen sind je nach Windows-Sprache übersetzt).
$script:IntegrationIds = @{
    GuestServiceInterface = '6C09BB55-D683-4DA0-8931-C9BF705F6480'
    KeyValuePairExchange  = '2A34B1C2-FD73-4043-8A5B-DD2159BC743F'
    Heartbeat             = '84EAAE65-2F2E-45F5-9BB5-0E857DC8EB47'
    Shutdown              = '9F8233AC-BE49-4C79-8EE3-E7E1985B2077'
    TimeSynchronization   = '2497F4DE-E9FA-4204-80E4-4B75C46419C0'
    Vss                   = '5CED1297-4598-4915-A5FC-AD21BB4D02A4'
}

$script:VmNotes = 'LocalAi-Sandbox: automatisch erzeugt, wird bei jedem Sitzungsstart neu aufgebaut und danach geloescht.'
$script:DirectoryMarker = '.localai-vm'

function Initialize-LocalAiConsole {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
}

function Write-LocalAiResult {
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Result)
    [Console]::Out.WriteLine(($Result | ConvertTo-Json -Compress -Depth 6))
}

function Write-LocalAiLine {
    param([Parameter(Mandatory)][string]$Line)
    [Console]::Out.WriteLine($Line)
    [Console]::Out.Flush()
}

function Add-LocalAiStatus {
    param([Parameter(Mandatory)][string]$StatusFile, [Parameter(Mandatory)][string]$Line)
    # Ohne BOM anhängen: der Broker liest die Datei fortlaufend.
    [System.IO.File]::AppendAllText($StatusFile, $Line + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
}

function Assert-LocalAiVmName {
    param([Parameter(Mandatory)][string]$VmName)
    if ($VmName -notmatch '^[A-Za-z0-9-]{1,40}$') {
        throw "Ungültiger VM-Name: $VmName"
    }
}

function Get-LocalAiNicCount {
    param([Parameter(Mandatory)][string]$VmName)
    return @(Get-VMNetworkAdapter -VMName $VmName -ErrorAction Stop).Count
}

function Test-LocalAiOwnedVm {
    param([Parameter(Mandatory)]$Vm)
    return ([string]$Vm.Notes).StartsWith('LocalAi-Sandbox:')
}

function Get-LocalAiIntegrationService {
    param([Parameter(Mandatory)]$Vm, [Parameter(Mandatory)][string]$Name)
    $id = $script:IntegrationIds[$Name]
    return Get-VMIntegrationService -VM $Vm | Where-Object { ([string]$_.Id).ToUpperInvariant().EndsWith($id) }
}

function Set-LocalAiIntegrationServices {
    param([Parameter(Mandatory)]$Vm)
    foreach ($name in 'GuestServiceInterface', 'KeyValuePairExchange', 'Heartbeat', 'Shutdown', 'TimeSynchronization') {
        $service = Get-LocalAiIntegrationService -Vm $Vm -Name $name
        if (-not $service) { throw "Integrationsdienst $name nicht gefunden" }
        Enable-VMIntegrationService -VMIntegrationService $service
    }
    $vss = Get-LocalAiIntegrationService -Vm $Vm -Name 'Vss'
    if ($vss) { Disable-VMIntegrationService -VMIntegrationService $vss }
}

function Get-LocalAiAdminGroup {
    param([Parameter(Mandatory)][string]$UiLanguage)
    switch -Regex ($UiLanguage) {
        '^de' { return 'Administratoren' }
        '^fr' { return 'Administrateurs' }
        '^(es|pt)' { return 'Administradores' }
        default { return 'Administrators' }
    }
}

function New-LocalAiPassword {
    $bytes = New-Object byte[] 24
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return 'Lai-' + [Convert]::ToBase64String($bytes).Replace('+', 'x').Replace('/', 'y').Replace('=', '') + '9!'
}

function New-LocalAiUnattendXml {
    param(
        [Parameter(Mandatory)][string]$UiLanguage,
        [Parameter(Mandatory)][string]$Password,
        [string]$ProductKey,
        [string]$AdminGroup,
        [string]$ComputerName = 'LOCALAI-SBX'
    )
    if ($UiLanguage -notmatch '^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$') { throw "Ungültige Sprache: $UiLanguage" }
    if (-not $AdminGroup) { $AdminGroup = Get-LocalAiAdminGroup -UiLanguage $UiLanguage }
    $e = { param($s) [System.Security.SecurityElement]::Escape($s) }
    $common = 'processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"'
    $key = ''
    if ($ProductKey) { $key = "      <ProductKey>$(& $e $ProductKey)</ProductKey>`r`n" }
    return @"
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" $common>
      <ComputerName>$(& $e $ComputerName)</ComputerName>
$key      <TimeZone>W. Europe Standard Time</TimeZone>
    </component>
    <component name="Microsoft-Windows-Deployment" $common>
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Path>reg.exe add HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE /v BypassNRO /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" $common>
      <InputLocale>de-DE</InputLocale>
      <SystemLocale>de-DE</SystemLocale>
      <UILanguage>$(& $e $UiLanguage)</UILanguage>
      <UserLocale>de-DE</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" $common>
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <ProtectYourPC>3</ProtectYourPC>
        <SkipMachineOOBE>true</SkipMachineOOBE>
        <SkipUserOOBE>true</SkipUserOOBE>
      </OOBE>
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>localai</Name>
            <DisplayName>LocalAi</DisplayName>
            <Group>$(& $e $AdminGroup)</Group>
            <Password>
              <Value>$(& $e $Password)</Value>
              <PlainText>true</PlainText>
            </Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
      <TimeZone>W. Europe Standard Time</TimeZone>
    </component>
  </settings>
</unattend>
"@
}

function Get-LocalAiImageLanguage {
    param([Parameter(Mandatory)]$ImageInfo)
    $languages = @($ImageInfo.Languages)
    if ($languages.Count -eq 0) { return 'en-US' }
    $index = 0
    if ($ImageInfo.PSObject.Properties['DefaultLanguageIndex'] -and $ImageInfo.DefaultLanguageIndex -lt $languages.Count) {
        $index = [int]$ImageInfo.DefaultLanguageIndex
    }
    return ([string]$languages[$index]).Split(' ')[0]
}

function Get-LocalAiErrorText {
    # Vollständige Fehlerkette (Hyper-V-/CIM-Fehler verstecken den eigentlichen Grund oft in InnerException).
    param([Parameter(Mandatory)]$ErrorRecord)
    $parts = New-Object System.Collections.Generic.List[string]
    $e = $ErrorRecord.Exception
    while ($e) {
        $m = ([string]$e.Message).Trim()
        if ($m -and -not $parts.Contains($m)) { $parts.Add($m) }
        $e = $e.InnerException
    }
    if ($ErrorRecord.ErrorDetails -and $ErrorRecord.ErrorDetails.Message) {
        $m = ([string]$ErrorRecord.ErrorDetails.Message).Trim()
        if (-not $parts.Contains($m)) { $parts.Add($m) }
    }
    return (($parts -join ' | ') -replace '[\r\n]+', ' ')
}

function Get-LocalAiFileFacts {
    param([Parameter(Mandatory)][string]$Path)
    try {
        $item = Get-Item -LiteralPath $Path -Force
        $root = [System.IO.Path]::GetPathRoot($item.FullName)
        $fs = (New-Object System.IO.DriveInfo($root)).DriveFormat
        return "Attribute: $($item.Attributes); Größe: $($item.Length); Dateisystem: $fs"
    } catch { return "Datei nicht lesbar: $($_.Exception.Message)" }
}

function Clear-LocalAiCompression {
    # Hyper-V verlangt unkomprimierte, unverschlüsselte und nicht spärliche VHDX-Dateien.
    # Für einen Ordner: Komprimierung abschalten, damit neue Dateien darin unkomprimiert entstehen.
    param([Parameter(Mandatory)][string]$Path)
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [System.IO.FileAttributes]::Encrypted) {
        throw "$Path ist verschlüsselt (EFS). Hyper-V kann darin keine virtuellen Festplatten nutzen – anderen vmDirectory wählen."
    }
    if ($item.Attributes -band [System.IO.FileAttributes]::Compressed) {
        Write-Host "NTFS-Komprimierung wird entfernt: $Path"
        & compact.exe /u /q $Path | Out-Null
    }
    if (-not $item.PSIsContainer -and ($item.Attributes -band [System.IO.FileAttributes]::SparseFile)) {
        Write-Host "Sparse-Kennzeichen wird entfernt: $Path"
        & fsutil.exe sparse setflag $Path 0 | Out-Null
    }
    $after = (Get-Item -LiteralPath $Path -Force).Attributes
    if ($after -band [System.IO.FileAttributes]::Compressed) { throw "$Path ist weiterhin komprimiert. Bitte Komprimierung für den Ordner aufheben (Eigenschaften → Erweitert)." }
}

function Mount-LocalAiVhd {
    # Bindet eine VHDX ein: zuerst Mount-VHD (Hyper-V), sonst Mount-DiskImage (Speicherverwaltung).
    # Liefert @{ Disk = <Get-Disk-Objekt>; Method = 'Hyper-V' | 'Storage' }.
    param([Parameter(Mandatory)][string]$Path)
    $first = $null
    try {
        $disk = Mount-VHD -Path $Path -Passthru -ErrorAction Stop | Get-Disk -ErrorAction Stop
        return @{ Disk = $disk; Method = 'Hyper-V' }
    } catch {
        $first = Get-LocalAiErrorText -ErrorRecord $_
        Write-Host "Mount-VHD fehlgeschlagen: $first"
        Write-Host (Get-LocalAiFileFacts -Path $Path)
        try { Dismount-VHD -Path $Path -ErrorAction SilentlyContinue } catch { }
    }
    try {
        Write-Host 'Zweiter Versuch mit Mount-DiskImage …'
        Mount-DiskImage -ImagePath $Path -StorageType VHDX -Access ReadWrite -NoDriveLetter -ErrorAction Stop | Out-Null
        $disk = Invoke-LocalAiRetry { Get-DiskImage -ImagePath $Path -ErrorAction Stop | Get-Disk -ErrorAction Stop }
        return @{ Disk = $disk; Method = 'Storage' }
    } catch {
        $second = Get-LocalAiErrorText -ErrorRecord $_
        throw "Systemfestplatte ließ sich nicht einbinden. Mount-VHD: $first – Mount-DiskImage: $second – $(Get-LocalAiFileFacts -Path $Path)"
    }
}

function Dismount-LocalAiVhd {
    param([Parameter(Mandatory)][string]$Path, [string]$Method = 'Hyper-V')
    if ($Method -eq 'Storage') { Dismount-DiskImage -ImagePath $Path | Out-Null } else { Dismount-VHD -Path $Path }
}

function Remove-LocalAiVmWithDisks {
    # Löscht die Sandbox-VM und ihre Festplatten – auch wenn sie in einem anderen VM-Ordner liegen
    # (z. B. Oberfläche und Abnahmetest). Gelöscht werden nur Dateien mit LocalAi-Namen in Ordnern mit Markierung.
    param([Parameter(Mandatory)]$Vm)
    if (-not (Test-LocalAiOwnedVm -Vm $Vm)) { throw "VM '$($Vm.Name)' stammt nicht von LocalAi – wird nicht gelöscht." }
    $disks = @(Get-VMHardDiskDrive -VM $Vm | Select-Object -ExpandProperty Path) + @(Get-VMDvdDrive -VM $Vm | Where-Object { $_.Path } | Select-Object -ExpandProperty Path)
    Stop-VM -VM $Vm -TurnOff -Force -ErrorAction SilentlyContinue
    Remove-VM -VM $Vm -Force
    $removed = 0
    foreach ($disk in $disks) {
        if (-not $disk -or -not (Test-Path -LiteralPath $disk)) { continue }
        $name = Split-Path -Leaf $disk
        $folder = Split-Path -Parent $disk
        # Nur eigene Dateien: Systemplatte, Seed (VHDX/ISO), Setup-ISO, Transfer- und Eingangs-Laufwerke – nie die Windows-ISO.
        if (($name -like '*-os.vhdx' -or $name -like '*-seed.vhdx' -or $name -like '*-seed.iso' -or $name -like '*-unattend.iso' -or
             $name -like 'transfer-*.vhdx' -or $name -like 'inbound-*.vhdx') -and (Test-Path -LiteralPath (Join-Path $folder $script:DirectoryMarker))) {
            Invoke-LocalAiRetry { Remove-Item -LiteralPath $disk -Force } | Out-Null
            $removed++
        }
    }
    return $removed
}

function Write-LocalAiRawImage {
    # Schreibt ein Rohabbild (z. B. debian-13-generic-amd64.raw) sektorweise auf einen eingebundenen Datenträger.
    # Der Datenträger wird dafür offline geschaltet (Windows bindet keine Partitionen ein, Rohschreiben ist erlaubt).
    # Blöcke aus lauter Nullen werden übersprungen – die frische dynamische VHDX liefert dort ohnehin Nullen.
    param([Parameter(Mandatory)][string]$ImagePath, [Parameter(Mandatory)][int]$DiskNumber)
    Set-Disk -Number $DiskNumber -IsOffline $true -ErrorAction SilentlyContinue
    Set-Disk -Number $DiskNumber -IsReadOnly $false
    Initialize-LocalAiRawWriter
    return [LocalAiRawWriter]::Copy($ImagePath, "\\.\PhysicalDrive$DiskNumber")
}

function Initialize-LocalAiRawWriter {
    # Kopierroutine für Rohabbilder (kompiliert, schnell genug für mehrere GB).
    if (-not ('LocalAiRawWriter' -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.IO;
public static class LocalAiRawWriter {
    public static long Copy(string image, string device) {
        const int Chunk = 4 * 1024 * 1024;
        byte[] buffer = new byte[Chunk];
        long written = 0, offset = 0;
        using (FileStream source = File.OpenRead(image))
        using (FileStream target = new FileStream(device, FileMode.Open, FileAccess.ReadWrite, FileShare.ReadWrite)) {
            while (true) {
                int read = 0, n;
                while (read < Chunk && (n = source.Read(buffer, read, Chunk - read)) > 0) { read += n; }
                if (read == 0) { break; }
                int length = (read + 511) / 512 * 512;
                if (length > read) { Array.Clear(buffer, read, length - read); }
                bool empty = true;
                for (int i = 0; i < length; i++) { if (buffer[i] != 0) { empty = false; break; } }
                if (!empty) { target.Position = offset; target.Write(buffer, 0, length); written += length; }
                offset += length;
                if (read < Chunk) { break; }
            }
            target.Flush();
        }
        return written;
    }
}
"@
    }
}

function Invoke-LocalAiRetry {
    param([Parameter(Mandatory)][scriptblock]$Action, [int]$Attempts = 20, [int]$DelayMs = 500)
    for ($i = 1; ; $i++) {
        try { return & $Action }
        catch {
            if ($i -ge $Attempts) { throw }
            Start-Sleep -Milliseconds $DelayMs
        }
    }
}

function Clear-LocalAiFile {
    # Überschreibt eine Datei mit Nullen und löscht sie (Aufbaumedien mit Sitzungsschlüssel bzw. Kennwort).
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Invoke-LocalAiRetry {
        $stream = [System.IO.File]::Open($Path, 'Open', 'Write', 'None')
        try {
            $zero = New-Object byte[] (1MB)
            $left = $stream.Length
            while ($left -gt 0) { $n = [int][Math]::Min($left, $zero.Length); $stream.Write($zero, 0, $n); $left -= $n }
            $stream.Flush($true)
        } finally { $stream.Dispose() }
        Remove-Item -LiteralPath $Path -Force
    } | Out-Null
}

function Send-LocalAiBootKeys {
    # Windows-ISO startet nur nach Tastendruck („Press any key to boot from CD“). Ohne Einbinden des Abbilds
    # (hostMount: false) lässt sich die Abfrage nicht aus der ISO entfernen.
    # Weg 1 (auf einem Firmenrechner erprobt): VM-Konsole (vmconnect) öffnen, ihren Verbindungsdialog mit Tab+Enter
    # bestätigen und dann $Seconds lang Enter an das Fenster senden. Braucht einen angemeldeten Desktop; das Fenster
    # bekommt kurz den Fokus. Weg 2 (Rückfall, ohne Fenster): Tastatur der VM über WMI (Msvm_Keyboard).
    param([Parameter(Mandatory)][string]$VmName, [ValidateRange(5, 120)][int]$Seconds = 5)
    $vm = Get-VM -Name $VmName -ErrorAction Stop
    if ($vm.State -ne 'Running') { throw "Die VM '$VmName' läuft nicht." }
    $vmConnectPath = Join-Path $env:WINDIR 'System32\vmconnect.exe'
    $activated = $false
    if (Test-Path -LiteralPath $vmConnectPath) {
        Write-Host "Öffne VM-Konsole für '$VmName' …"
        $vmConnect = Start-Process -FilePath $vmConnectPath -ArgumentList @('localhost', "`"$VmName`"") -PassThru
        $shell = New-Object -ComObject WScript.Shell
        try {
            Start-Sleep -Seconds 10 # Konsole und Verbindungsdialog aufbauen lassen
            if (-not $vmConnect.HasExited) {
                try {
                    $vmConnect.Refresh()
                    if ($vmConnect.MainWindowHandle -ne 0 -and $shell.AppActivate($vmConnect.Id)) {
                        $activated = $true
                        $shell.SendKeys('{TAB}'); Start-Sleep -Milliseconds 750
                        $shell.SendKeys('{ENTER}'); Start-Sleep -Milliseconds 250
                        Write-Host 'Verbindungsdialog bestätigt (Tab, Enter).'
                    }
                } catch { Write-Verbose "Tastatureingabe noch nicht möglich: $($_.Exception.Message)" }
            }
            $deadline = (Get-Date).AddSeconds($Seconds)
            while ($activated -and (Get-Date) -lt $deadline -and -not $vmConnect.HasExited) {
                try {
                    $vmConnect.Refresh()
                    if ($vmConnect.MainWindowHandle -ne 0 -and $shell.AppActivate($vmConnect.Id)) {
                        $shell.SendKeys('{ENTER}')
                        Write-Host 'Enter an die VM-Konsole gesendet.'
                        Start-Sleep -Milliseconds 750
                    }
                } catch { Write-Verbose "Tastatureingabe nicht möglich: $($_.Exception.Message)" }
                Start-Sleep -Milliseconds 500
            }
        } finally {
            try { if (-not $vmConnect.HasExited) { $vmConnect.CloseMainWindow() | Out-Null } } catch { }
            try { [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) | Out-Null } catch { }
        }
    }
    if (-not $activated) {
        Write-Host 'VM-Konsole nicht nutzbar – Enter über die Tastatur der VM (WMI).'
        $cim = Get-CimInstance -Namespace root\virtualization\v2 -ClassName Msvm_ComputerSystem -Filter "ElementName='$VmName'"
        $keyboard = Get-CimAssociatedInstance -InputObject $cim -ResultClassName Msvm_Keyboard
        $until = (Get-Date).AddSeconds([Math]::Max($Seconds, 40))
        while ((Get-Date) -lt $until) {
            try { Invoke-CimMethod -InputObject $keyboard -MethodName TypeKey -Arguments @{ keyCode = [uint32]13 } | Out-Null } catch { }
            Start-Sleep -Milliseconds 700
        }
    }
}

Export-ModuleMember -Function * -Variable IntegrationIds, VmNotes, DirectoryMarker
