#Requires -Version 5.1
<#
    Provisionierung im Gast (läuft als SYSTEM über SetupComplete.cmd, ohne Netzwerk).
    1. Antwortdatei mit Kennwort entfernen, Rechte auf C:\LocalAi setzen
    2. Netzwerkprüfungen (alle müssen scheitern)
    3. Toolchains installieren, prüfen und mit Hallo-Welt testen
    4. Worker als geplante Aufgabe (SYSTEM) einrichten und starten
    5. Bericht über das Transfer-Laufwerk an den Host
#>
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = 'C:\LocalAi'

function Set-Progress([string]$Token) {
    # Fortschritt für den Host (KVP, festes Format). Darf die Provisionierung nie abbrechen.
    try {
        $key = 'HKLM:\SOFTWARE\Microsoft\Virtual Machine\Guest'
        if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
        Set-ItemProperty -Path $key -Name 'LocalAiProgress' -Value $Token -Type String
    } catch { }
}
Set-Progress 'start'
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

Import-Module (Join-Path $root 'LocalAiGuest.psm1') -Force
Set-LocalAiGuestRoot -Path $root

$size = 0
try {
    $config = Get-Content -LiteralPath (Join-Path $root 'guest-config.json') -Raw | ConvertFrom-Json
    $size = [int64]$config.transferDiskBytes

    # 1 – Kennwort im Klartext nicht liegen lassen; Worker-Laufzeit nur für SYSTEM/Administratoren schreibbar (SEC-21).
    Remove-Item -LiteralPath 'C:\Windows\Panther\unattend.xml' -Force -ErrorAction SilentlyContinue
    & icacls.exe $root /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
    if ($LASTEXITCODE -ne 0) { $errors.Add("icacls fehlgeschlagen ($LASTEXITCODE)") }

    # 2 – Netzwerk
    Set-Progress 'network'
    $report.networkAdapters = @(Get-CimInstance -ClassName Win32_NetworkAdapter -Filter 'PhysicalAdapter = TRUE').Count
    $report.probes = Get-Probes

    Set-Progress 'toolchains'
    # 3 – Toolchains (nur die für das Projekt gewählten und registrierten), danach nur noch lesbar (SEC-21).
    $sourceRoot = Join-Path $root 'toolchain-src'
    $toolchains = @()
    foreach ($name in @($config.toolchains)) {
        if (-not $name) { continue }
        Write-Host "Toolchain $name wird installiert …"
        $entry = Install-Toolchain -Name ([string]$name) -SourceRoot $sourceRoot
        Write-Host ("Toolchain {0}: installiert={1} Selbsttest={2} {3}" -f $entry.name, $entry.installed, $entry.selfTest, $entry.error)
        if ($entry.installed) {
            # Manifest und Offline-Paketquelle für Toolchain-Aktionen behalten (nur lesbar für Jobs).
            try { Publish-ToolchainForJobs -Name ([string]$name) -SourceRoot $sourceRoot -Target (Join-Path $root 'toolchains') }
            catch { $entry.error = "Bereitstellung für Jobs: $($_.Exception.Message -replace '[\r\n]+', ' ')"; $entry.installed = $false }
        }
        $toolchains += $entry
        if ($entry.error) { $errors.Add("Toolchain $($entry.name): $($entry.error)") }
    }
    $report.toolchains = $toolchains
    if (Test-Path -LiteralPath 'C:\Toolchains') {
        & icacls.exe 'C:\Toolchains' /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
        if ($LASTEXITCODE -ne 0) { $errors.Add("icacls C:\Toolchains fehlgeschlagen ($LASTEXITCODE)") }
    }
    if (Test-Path -LiteralPath $sourceRoot) { Remove-Item -LiteralPath $sourceRoot -Recurse -Force -ErrorAction SilentlyContinue }
    # Werkzeugprozesse mit niedriger Integrität brauchen C:\Windows\SystemTemp (siehe Set-SystemTempIntegrity).
    $problem = Set-SystemTempIntegrity -Config $config
    if ($problem) { $errors.Add($problem) }

    # 4 – Worker
    Set-Progress 'worker'
    $python = Join-Path $root 'python\python.exe'
    $version = & $python -I -c "import sys, pydantic, localai.worker; print(sys.version.split()[0])"
    if ($LASTEXITCODE -ne 0) { throw "Python oder Worker im Gast nicht lauffähig (Exitcode $LASTEXITCODE)" }
    $report.worker.python = ([string]$version).Trim()
    Set-WorkerCommand -Config $config
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
Set-Progress 'report'
$report.errors = @($errors)
$report.durationSeconds = [int]((Get-Date) - $started).TotalSeconds
$json = $report | ConvertTo-Json -Depth 6
$reportFile = Join-Path $root 'report.json'
[System.IO.File]::WriteAllText($reportFile, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host $json
# Ausgabe des Workers (auch Fehlermeldungen) ins Protokoll – die Aufzeichnung erfasst sie bei Programmen sonst nicht.
$reportOut = Join-Path $root 'logs\report-stdout.log'
$reportErr = Join-Path $root 'logs\report-stderr.log'
$exitCode = 1
try {
    $proc = Start-Process -FilePath (Join-Path $root 'python\python.exe') -NoNewWindow -Wait -PassThru `
        -ArgumentList @('-I', '-m', 'localai.worker', '--report', "`"$reportFile`"", '--transfer-disk', 'auto', '--transfer-size', "$size") `
        -RedirectStandardOutput $reportOut -RedirectStandardError $reportErr
    $exitCode = $proc.ExitCode
} catch { Write-Host "Worker für den Bericht nicht startbar: $($_.Exception.Message)" }
foreach ($f in $reportOut, $reportErr) { if (Test-Path -LiteralPath $f) { Get-Content -LiteralPath $f | ForEach-Object { Write-Host $_ } } }
Write-Host "Bericht übergeben (Exitcode $exitCode)"
if ($exitCode -eq 0) { Set-Progress 'report-sent' }
if ($exitCode -ne 0) {
    # Ohne lauffähiges Python trotzdem melden, damit der Host nicht bis zur Zeitgrenze wartet.
    try { Send-ReportWithoutPython -Payload ([System.Text.Encoding]::UTF8.GetBytes($json)) -Size $size; Write-Host 'Bericht ohne Python übergeben'; Set-Progress 'report-sent-ps' }
    catch { Write-Host "Bericht konnte nicht übergeben werden: $($_.Exception.Message)"; Set-Progress 'report-failed' }
}
try { Stop-Transcript | Out-Null } catch { }
