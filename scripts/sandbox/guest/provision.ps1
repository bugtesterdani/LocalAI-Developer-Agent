#Requires -Version 5.1
<#
    Provisionierung im Gast (läuft als SYSTEM über SetupComplete.cmd, ohne Netzwerk).
    1. Antwortdatei mit Kennwort entfernen, Rechte auf C:\LocalAi setzen
    2. Netzwerkprüfungen (alle müssen scheitern)
    3. Toolchains installieren (M2c)
    4. Worker als geplante Aufgabe (SYSTEM) einrichten und starten
    5. Bericht über das Transfer-Laufwerk an den Host
#>
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = 'C:\LocalAi'
foreach ($d in 'logs', 'inbox', 'work') { New-Item -ItemType Directory -Force -Path (Join-Path $root $d) | Out-Null }
try { Start-Transcript -LiteralPath (Join-Path $root 'logs\provision.log') -Append | Out-Null } catch { }

$started = Get-Date
$errors = New-Object System.Collections.Generic.List[string]
$report = [ordered]@{
    schemaVersion   = 1
    computerName    = $env:COMPUTERNAME
    osVersion       = [Environment]::OSVersion.VersionString
    networkAdapters = -1
    probes          = @()
    worker          = [ordered]@{ running = $false; python = '' }
    toolchains      = @()
    errors          = @()
    durationSeconds = 0
}

function Test-TcpProbe([string]$name, [string]$address, [int]$port) {
    $ip = [System.Net.IPAddress]::Parse($address)
    $client = New-Object System.Net.Sockets.TcpClient($ip.AddressFamily)
    try {
        $task = $client.ConnectAsync($ip, $port)
        $done = $task.Wait(3000)
        return [ordered]@{ name = $name; target = "$address port $port"; reachable = [bool]($done -and $client.Connected) }
    } catch {
        return [ordered]@{ name = $name; target = "$address port $port"; reachable = $false }
    } finally {
        $client.Dispose()
    }
}

function Get-Probes {
    $probes = @()
    try {
        $null = [System.Net.Dns]::GetHostAddresses('www.microsoft.com')
        $probes += [ordered]@{ name = 'dns'; target = 'www.microsoft.com'; reachable = $true }
    } catch {
        $probes += [ordered]@{ name = 'dns'; target = 'www.microsoft.com'; reachable = $false }
    }
    try {
        $null = Invoke-WebRequest -UseBasicParsing -Uri 'http://www.msftconnecttest.com/connecttest.txt' -TimeoutSec 5
        $probes += [ordered]@{ name = 'http'; target = 'www.msftconnecttest.com'; reachable = $true }
    } catch {
        $probes += [ordered]@{ name = 'http'; target = 'www.msftconnecttest.com'; reachable = $false }
    }
    $probes += Test-TcpProbe 'tcp-ipv4-https' '1.1.1.1' 443
    $probes += Test-TcpProbe 'tcp-ipv4-dns' '8.8.8.8' 53
    $probes += Test-TcpProbe 'tcp-ipv6-https' '2606:4700:4700::1111' 443
    $probes += Test-TcpProbe 'tcp-hyperv-default-switch' '172.17.0.1' 445
    return ,$probes
}

function Send-ReportWithoutPython([byte[]]$Payload, [int64]$Size) {
    # Gleiches Containerformat wie localai.worker.transfer (Art 3 = Bericht).
    $disks = @(Get-Disk | Where-Object { $_.Size -eq $Size -and $_.PartitionStyle -eq 'RAW' })
    if ($disks.Count -ne 1) { throw "Transfer-Laufwerk nicht eindeutig ($($disks.Count))" }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $hash = $sha.ComputeHash($Payload) } finally { $sha.Dispose() }
    $header = New-Object byte[] 4096
    [System.Text.Encoding]::ASCII.GetBytes('LAIXFER1').CopyTo($header, 0)
    [BitConverter]::GetBytes([uint32]1).CopyTo($header, 8)
    [BitConverter]::GetBytes([uint32]3).CopyTo($header, 12)
    [BitConverter]::GetBytes([uint64]0).CopyTo($header, 16)
    [BitConverter]::GetBytes([uint64]$Payload.Length).CopyTo($header, 24)
    $hash.CopyTo($header, 32)
    $body = New-Object byte[] ([math]::Ceiling($Payload.Length / 4096) * 4096)
    $Payload.CopyTo($body, 0)
    $stream = New-Object System.IO.FileStream("\\.\PhysicalDrive$($disks[0].Number)", [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::ReadWrite)
    try {
        $first = New-Object byte[] 4096
        $null = $stream.Read($first, 0, 4096)
        if (@($first | Where-Object { $_ -ne 0 }).Count -gt 0) { throw 'Transfer-Laufwerk ist nicht leer' }
        $stream.Position = 4096
        $stream.Write($body, 0, $body.Length)
        $stream.Flush($true)
        $stream.Position = 0
        $stream.Write($header, 0, 4096)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
    $key = 'HKLM:\SOFTWARE\Microsoft\Virtual Machine\Auto'
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    Set-ItemProperty -Path $key -Name 'LocalAiSignal' -Value 'report:0' -Type String
}

$size = 0
try {
    $config = Get-Content -LiteralPath (Join-Path $root 'guest-config.json') -Raw | ConvertFrom-Json
    $size = [int64]$config.transferDiskBytes

    # 1 – Kennwort im Klartext nicht liegen lassen; Worker-Laufzeit nur für SYSTEM/Administratoren schreibbar (SEC-21).
    Remove-Item -LiteralPath 'C:\Windows\Panther\unattend.xml' -Force -ErrorAction SilentlyContinue
    & icacls.exe $root /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
    if ($LASTEXITCODE -ne 0) { $errors.Add("icacls fehlgeschlagen ($LASTEXITCODE)") }

    # 2 – Netzwerk
    $report.networkAdapters = @(Get-CimInstance -ClassName Win32_NetworkAdapter -Filter 'PhysicalAdapter = TRUE').Count
    $report.probes = Get-Probes

    # 3 – Toolchains folgen mit M2c.

    # 4 – Worker
    $python = Join-Path $root 'python\python.exe'
    $version = & $python -I -c "import sys, pydantic, localai.worker; print(sys.version.split()[0])"
    if ($LASTEXITCODE -ne 0) { throw "Python oder Worker im Gast nicht lauffähig (Exitcode $LASTEXITCODE)" }
    $report.worker.python = ([string]$version).Trim()
    $cmd = "@echo off`r`n`"$python`" -I -m localai.worker --inbox $root\inbox --work $root\work --key-file $root\session.key --transfer-disk auto --transfer-size $size --require-ready >> $root\logs\worker.log 2>&1`r`n"
    [System.IO.File]::WriteAllText((Join-Path $root 'worker.cmd'), $cmd, [System.Text.Encoding]::ASCII)
    $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$root\worker.cmd`"" -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName 'LocalAiWorker' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName 'LocalAiWorker'
    Start-Sleep -Seconds 5
    $report.worker.running = ((Get-ScheduledTask -TaskName 'LocalAiWorker').State -eq 'Running')
} catch {
    $errors.Add(($_.Exception.Message -replace '[\r\n]+', ' '))
}

# 5 – Bericht
$report.errors = @($errors)
$report.durationSeconds = [int]((Get-Date) - $started).TotalSeconds
$json = $report | ConvertTo-Json -Depth 6
$reportFile = Join-Path $root 'report.json'
[System.IO.File]::WriteAllText($reportFile, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host $json
& (Join-Path $root 'python\python.exe') -I -m localai.worker --report $reportFile --transfer-disk auto --transfer-size $size
Write-Host "Bericht übergeben (Exitcode $LASTEXITCODE)"
if ($LASTEXITCODE -ne 0) {
    # Ohne lauffähiges Python trotzdem melden, damit der Host nicht bis zur Zeitgrenze wartet.
    try { Send-ReportWithoutPython -Payload ([System.Text.Encoding]::UTF8.GetBytes($json)) -Size $size; Write-Host 'Bericht ohne Python übergeben' }
    catch { Write-Host "Bericht konnte nicht übergeben werden: $($_.Exception.Message)" }
}
try { Stop-Transcript | Out-Null } catch { }
