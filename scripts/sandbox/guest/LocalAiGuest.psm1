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

function Expand-Placeholders([string]$Text, [hashtable]$Map) {
    foreach ($key in $Map.Keys) { $Text = $Text.Replace('{' + $key + '}', [string]$Map[$key]) }
    return $Text
}

function Get-ToolchainBinary($Manifest, [string]$Name) {
    $binary = @($Manifest.binaries | Where-Object { $_.name -eq $Name }) | Select-Object -First 1
    if (-not $binary) { throw "Binary $Name nicht deklariert" }
    $path = Join-Path ([string]$Manifest.guestPath) (([string]$binary.path) -replace '/', '\')
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
        $environment = @{}
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

        if ($manifest.PSObject.Properties['postInstall'] -and $manifest.postInstall) {
            foreach ($command in @($manifest.postInstall)) {
                $run = Invoke-ToolchainCommand -Manifest $manifest -Command $command -Map $map -Environment $environment -WorkingDirectory $src -TimeoutMinutes $timeout
                if ($run.ExitCode -ne 0) { throw "Nachinstallation endete mit Exitcode $($run.ExitCode): $(Get-OutputTail $run.Output)" }
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

Export-ModuleMember -Function *
