# Funktionen der Gast-Provisionierung (Windows PowerShell 5.1 im Gast; testbar ohne VM).
$script:Root = 'C:\LocalAi'

function Set-LocalAiGuestRoot([string]$Path) { $script:Root = $Path }

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
    $key = 'HKLM:\SOFTWARE\Microsoft\Virtual Machine\Guest'
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    Set-ItemProperty -Path $key -Name 'LocalAiSignal' -Value 'report:0' -Type String
}

function ConvertTo-CommandLineArgument([string]$Value) {
    # Regeln von CommandLineToArgvW: jedes Argument kommt unverändert beim Programm an.
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $backslashes = 0
    foreach ($c in $Value.ToCharArray()) {
        if ($c -eq '\') { $backslashes++; continue }
        if ($c -eq '"') {
            [void]$sb.Append('\' * ($backslashes * 2 + 1)); [void]$sb.Append('"'); $backslashes = 0; continue
        }
        if ($backslashes -gt 0) { [void]$sb.Append('\' * $backslashes); $backslashes = 0 }
        [void]$sb.Append($c)
    }
    [void]$sb.Append('\' * ($backslashes * 2))
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Invoke-ToolchainProcess([string]$Exe, [string[]]$Arguments, [string]$WorkingDirectory, [int]$TimeoutMinutes, [hashtable]$Environment) {
    # Fester Programmaufruf mit Argumentliste – keine Shell dazwischen.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = (@($Arguments | ForEach-Object { ConvertTo-CommandLineArgument $_ }) -join ' ')
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    foreach ($key in $Environment.Keys) { $psi.EnvironmentVariables[$key] = [string]$Environment[$key] }
    $process = [System.Diagnostics.Process]::Start($psi)
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutMinutes * 60000)) {
        try { $process.Kill() } catch { }
        throw "Zeitüberschreitung nach $TimeoutMinutes Minuten: $Exe"
    }
    $process.WaitForExit()
    $output = $stdout.Result + $stderr.Result
    Add-Content -LiteralPath (Join-Path $script:Root 'logs\toolchains.log') -Value "> $Exe $($psi.Arguments)`r`n$output`r`n(Exitcode $($process.ExitCode))" -Encoding UTF8
    return [pscustomobject]@{ ExitCode = $process.ExitCode; Output = $output }
}

function Get-OutputTail([string]$Output, [int]$Max = 600) {
    # Letzte Zeilen mit Fehlern bevorzugt, damit der Grund im Bericht steht (Volltext: logs\toolchains.log).
    $lines = @(($Output -split "`r?`n") | Where-Object { $_.Trim() })
    $errors = @($lines | Where-Object { $_ -match 'error|Fehler|fail' })
    $pick = if ($errors.Count -gt 0) { $errors | Select-Object -Last 4 } else { $lines | Select-Object -Last 4 }
    $text = (($pick | ForEach-Object { $_.Trim() }) -join ' | ')
    if ($text.Length -gt $Max) { $text = $text.Substring($text.Length - $Max) }
    return $text
}

function Get-ToolchainProfileEnvironment {
    $profileDir = Join-Path $script:Root 'work\home'
    $roaming = Join-Path $profileDir 'AppData\Roaming'
    $local = Join-Path $profileDir 'AppData\Local'
    $temp = Join-Path $local 'Temp'
    foreach ($d in $roaming, $local, $temp) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
    return @{
        USERPROFILE     = $profileDir
        HOME            = $profileDir
        APPDATA         = $roaming
        LOCALAPPDATA    = $local
        TEMP            = $temp
        TMP             = $temp
        DOTNET_CLI_HOME = $profileDir
    }
}

function Expand-Placeholders([string]$Text, [hashtable]$Map) {
    foreach ($key in $Map.Keys) { $Text = $Text.Replace('{' + $key + '}', [string]$Map[$key]) }
    return $Text
}

$script:SystemBinaries = @{ 'cmd.exe' = 'cmd.exe'; 'windowspowershell\v1.0\powershell.exe' = 'WindowsPowerShell\v1.0\powershell.exe' }

function Test-SafeRelativePath([string]$Path) {
    $p = $Path -replace '/', '\'
    return ($p -and -not [System.IO.Path]::IsPathRooted($p) -and $p -notmatch '(^|\\)\.\.(\\|$)' -and $p -notmatch ':')
}

function Get-ToolchainBinary($Manifest, [string]$Name) {
    $binary = @($Manifest.binaries | Where-Object { $_.name -eq $Name }) | Select-Object -First 1
    if (-not $binary) { throw "Binary $Name nicht deklariert" }
    $raw = ([string]$binary.path) -replace '/', '\'
    if ($raw.ToLowerInvariant().StartsWith('{system}\')) {
        # Nur die erlaubten Windows-Programme (Batch, PowerShell 5.1); kein Hash, sie ändern sich mit Updates.
        $rest = $script:SystemBinaries[$raw.Substring(9).ToLowerInvariant()]
        if (-not $rest) { throw "Windows-Programm nicht erlaubt: $raw" }
        return (Join-Path $env:WINDIR "System32\$rest")
    }
    if (-not (Test-SafeRelativePath $raw)) { throw "Binary ${Name}: unzulässiger Pfad" }
    $path = Join-Path ([string]$Manifest.guestPath) $raw
    if (-not (Test-Path -LiteralPath $path)) { throw "Binary fehlt nach der Installation: $path" }
    if ($binary.PSObject.Properties['sha256'] -and $binary.sha256) {
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne [string]$binary.sha256) { throw "Hash von $Name weicht ab" }
    }
    return $path
}

function Invoke-ToolchainCommand($Manifest, $Command, [hashtable]$Map, [hashtable]$Environment, [string]$WorkingDirectory, [int]$TimeoutMinutes) {
    $exe = Get-ToolchainBinary -Manifest $Manifest -Name ([string]$Command.binary)
    $arguments = @($Command.arguments | ForEach-Object { Expand-Placeholders ([string]$_) $Map })
    return Invoke-ToolchainProcess -Exe $exe -Arguments $arguments -WorkingDirectory $WorkingDirectory -TimeoutMinutes $TimeoutMinutes -Environment $Environment
}

function Install-Toolchain([string]$Name, [string]$SourceRoot) {
    $result = [ordered]@{ name = $Name; version = ''; installed = $false; check = ''; selfTest = 'keiner'; seconds = 0; error = $null }
    $started = Get-Date
    try {
        $src = Join-Path $SourceRoot $Name
        $manifest = Get-Content -LiteralPath (Join-Path $src 'toolchain.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $pinned = Get-Content -LiteralPath (Join-Path $src 'pinned.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $result.version = [string]$manifest.version
        if ([string]$manifest.name -ne $Name) { throw 'Name in toolchain.json passt nicht' }
        $guest = [string]$manifest.guestPath
        if ($guest -ne "C:\Toolchains\$Name") { throw "guestPath unzulässig: $guest" }

        # Hashes im Gast erneut prüfen (zweite Prüfung nach dem Host, SEC-19).
        foreach ($entry in $pinned.files.PSObject.Properties) {
            $file = Join-Path $src ($entry.Name -replace '/', '\')
            $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($hash -ne [string]$entry.Value) { throw "Hash weicht ab: $($entry.Name)" }
        }

        $map = @{
            guest    = $guest
            source   = $src
            work     = (Join-Path $script:Root 'work')
            results  = (Join-Path $script:Root 'work\results')
            selftest = (Join-Path $script:Root "work\selftest-$Name")
        }
        # Eigenes Benutzerprofil für Werkzeugprozesse: Als SYSTEM in SetupComplete fehlen APPDATA,
        # LOCALAPPDATA usw. – NuGet bricht dann mit »Value cannot be null (Parameter 'path1')« ab.
        $environment = Get-ToolchainProfileEnvironment
        if ($manifest.PSObject.Properties['environment'] -and $manifest.environment) {
            foreach ($entry in $manifest.environment.PSObject.Properties) { $environment[$entry.Name] = Expand-Placeholders ([string]$entry.Value) $map }
        }
        $timeout = [int]$manifest.install.expectedMinutes * 3 + 5

        switch ([string]$manifest.install.kind) {
            'Archive' {
                New-Item -ItemType Directory -Force -Path $guest | Out-Null
                Add-Type -AssemblyName System.IO.Compression.FileSystem
                [System.IO.Compression.ZipFile]::ExtractToDirectory((Join-Path $src ([string]$manifest.install.file)), $guest)
            }
            'Installer' {
                $file = Join-Path $src ([string]$manifest.install.file)
                $arguments = @()
                if ($manifest.install.PSObject.Properties['arguments'] -and $manifest.install.arguments) {
                    $arguments = @($manifest.install.arguments | ForEach-Object { Expand-Placeholders ([string]$_) $map })
                }
                $exe = $file
                if ($file -like '*.msi') { $exe = Join-Path $env:WINDIR 'System32\msiexec.exe'; $arguments = @('/i', $file) + $arguments }
                $run = Invoke-ToolchainProcess -Exe $exe -Arguments $arguments -WorkingDirectory $src -TimeoutMinutes $timeout -Environment $environment
                if ($run.ExitCode -ne 0 -and $run.ExitCode -ne 3010) { throw "Installer endete mit Exitcode $($run.ExitCode): $(Get-OutputTail $run.Output)" }
            }
            'None' { }
            default { throw "Unbekannte Installationsart: $($manifest.install.kind)" }
        }

        # deploy: Dateien/Ordner aus dem Toolchain-Ordner nach guestPath (Runner-Skripte, Module, Lizenzdatei).
        if ($manifest.PSObject.Properties['deploy'] -and $manifest.deploy) {
            foreach ($entry in @($manifest.deploy)) {
                if (-not (Test-SafeRelativePath ([string]$entry.source)) -or -not (Test-SafeRelativePath ([string]$entry.target))) {
                    throw "deploy: unzulässiger Pfad ($($entry.source) -> $($entry.target))"
                }
                $from = Join-Path $src (([string]$entry.source) -replace '/', '\')
                $to = Join-Path $guest (([string]$entry.target) -replace '/', '\')
                $parent = Split-Path -Parent $to
                if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
                if (Test-Path -LiteralPath $from -PathType Container) {
                    New-Item -ItemType Directory -Force -Path $to | Out-Null
                    Get-ChildItem -LiteralPath $from -Force | Copy-Item -Destination $to -Recurse -Force
                } elseif (Test-Path -LiteralPath $from -PathType Leaf) {
                    Copy-Item -LiteralPath $from -Destination $to -Force
                } else {
                    throw "deploy: Quelle fehlt: $($entry.source)"
                }
            }
        }

        if ($manifest.PSObject.Properties['postInstall'] -and $manifest.postInstall) {
            foreach ($command in @($manifest.postInstall)) {
                $run = Invoke-ToolchainCommand -Manifest $manifest -Command $command -Map $map -Environment $environment -WorkingDirectory $src -TimeoutMinutes $timeout
                # 3010 = Neustart empfohlen, 1638 = neuere Version schon installiert (z. B. VC++-Laufzeit).
                if ($run.ExitCode -notin 0, 3010, 1638) { throw "Nachinstallation endete mit Exitcode $($run.ExitCode): $(Get-OutputTail $run.Output)" }
            }
        }

        $run = Invoke-ToolchainCommand -Manifest $manifest -Command $manifest.install.check -Map $map -Environment $environment -WorkingDirectory $src -TimeoutMinutes 10
        $result.check = ($run.Output.Trim() -split "`r?`n")[0]
        if ($run.ExitCode -ne 0) { throw "Prüfbefehl endete mit Exitcode $($run.ExitCode): $(Get-OutputTail $run.Output)" }
        if ($manifest.install.check.PSObject.Properties['expectOutput'] -and $manifest.install.check.expectOutput -and
            -not $run.Output.Contains([string]$manifest.install.check.expectOutput)) {
            throw "Prüfbefehl liefert nicht '$($manifest.install.check.expectOutput)'"
        }

        if ($manifest.PSObject.Properties['selfTest'] -and $manifest.selfTest) {
            $result.selfTest = 'fehlgeschlagen'
            $dir = $map.selftest
            if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
            Copy-Item -LiteralPath (Join-Path $src (([string]$manifest.selfTest.directory) -replace '/', '\')) -Destination $dir -Recurse
            $last = $null
            foreach ($step in @($manifest.selfTest.steps)) {
                $last = Invoke-ToolchainCommand -Manifest $manifest -Command $step -Map $map -Environment $environment -WorkingDirectory $dir -TimeoutMinutes 15
                if ($last.ExitCode -ne 0) { throw "Selbsttest: Schritt $($step.arguments -join ' ') endete mit Exitcode $($last.ExitCode): $(Get-OutputTail $last.Output)" }
            }
            if (-not $last.Output.Contains([string]$manifest.selfTest.expectOutput)) { throw "Selbsttest: Ausgabe '$($manifest.selfTest.expectOutput)' fehlt" }
            $result.selfTest = 'ok'
        }
        $result.installed = $true
    } catch {
        $result.error = ($_.Exception.Message -replace '[\r\n]+', ' ')
    }
    $result.seconds = [int]((Get-Date) - $started).TotalSeconds
    return $result
}

function Publish-ToolchainForJobs([string]$Name, [string]$SourceRoot, [string]$Target) {
    # toolchain.json und die Offline-Paketquelle (packageSource.path) nach C:\LocalAi\toolchains\<name>; der Worker
    # liest dort die erlaubten Aktionen. Ohne Paketquelle bleibt ein leerer Ordner (Restore nur aus dem Projekt selbst).
    $src = Join-Path $SourceRoot $Name
    $dest = Join-Path $Target $Name
    New-Item -ItemType Directory -Force -Path (Join-Path $dest 'packages') | Out-Null
    Copy-Item -LiteralPath (Join-Path $src 'toolchain.json') -Destination (Join-Path $dest 'toolchain.json') -Force
    $manifest = Get-Content -LiteralPath (Join-Path $src 'toolchain.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.PSObject.Properties['packageSource'] -and $manifest.packageSource -and $manifest.packageSource.PSObject.Properties['path'] -and $manifest.packageSource.path) {
        $packages = Join-Path $src (([string]$manifest.packageSource.path) -replace '/', '\')
        if (Test-Path -LiteralPath $packages -PathType Container) {
            Get-ChildItem -LiteralPath $packages -Force | Copy-Item -Destination (Join-Path $dest 'packages') -Recurse -Force
        }
    }
}

function Test-LowIntegrityTools($Config) {
    return -not ($Config.PSObject.Properties['lowIntegrityTools'] -and $Config.lowIntegrityTools -eq $false)
}

function Set-SystemTempIntegrity($Config) {
    # Werkzeugprozesse laufen mit SYSTEM-Token auf niedriger Integrität. Windows gibt SYSTEM-Prozessen als Temp-Ordner
    # fest C:\Windows\SystemTemp (GetTempPath2, TEMP/TMP werden ignoriert) – MSBuild/NuGet brauchen dort Schreibrecht.
    # Nur die Integritätsstufe wird gesenkt; der Worker räumt nach jedem Job auf, was der Job dort angelegt hat.
    $systemTemp = Join-Path $env:SystemRoot 'SystemTemp'
    if ((Test-LowIntegrityTools $Config) -and (Test-Path -LiteralPath $systemTemp)) {
        & icacls.exe $systemTemp /setintegritylevel '(OI)(CI)low' | Out-Null
        if ($LASTEXITCODE -ne 0) { return "icacls SystemTemp fehlgeschlagen ($LASTEXITCODE)" }
    }
    return $null
}

function Set-WorkerCommand($Config) {
    # worker.cmd startet den Worker; Exitcode 75 = nach einem Gast-Update mit neuem Code neu starten.
    $root = $script:Root
    $python = Join-Path $root 'python\python.exe'
    $size = [int64]$Config.transferDiskBytes
    # Toolchain-Aktionen nur mit installierten Toolchains; Werkzeugprozesse standardmäßig mit niedriger Integrität (SEC-21).
    $execArgs = "--toolchains $root\toolchains"
    if (-not (Test-LowIntegrityTools $Config)) { $execArgs += ' --no-low-integrity' }
    # Ohne Copy-VMFile (inbound: Disk): Jobs kommen auf einem vom Host angehängten Eingangs-Laufwerk.
    if ($Config.PSObject.Properties['inbound'] -and [string]$Config.inbound -eq 'Disk') { $execArgs += ' --inbound-disk auto' }
    $lines = @(
        '@echo off',
        ':start',
        "`"$python`" -I -m localai.worker --inbox $root\inbox --work $root\work --key-file $root\session.key --transfer-disk auto --transfer-size $size --require-ready $execArgs >> $root\logs\worker.log 2>&1",
        'if %ERRORLEVEL%==75 goto start',
        ''
    )
    [System.IO.File]::WriteAllText((Join-Path $root 'worker.cmd'), ($lines -join "`r`n"), [System.Text.Encoding]::ASCII)
}

Export-ModuleMember -Function *
