<#
  Test-Harness für Batch-Skripte (Toolchain "batch").

  Tests: alle Dateien *.test.cmd, *.test.bat, test_*.cmd, test_*.bat unter -Path. Jeder Test läuft mit
  cmd.exe /d /c im Projektordner (aktuelles Verzeichnis) und gilt als bestanden bei Exitcode 0. Liegt daneben
  <name>.expected.txt, muss die Ausgabe (ohne Leerzeichen am Zeilenende) genau dem Inhalt entsprechen.
  Ergebnis: JUnit-XML in -Results\batch.xml. Exitcode 0 = alle bestanden (oder keine Tests), 1 = Fehler.

  -Lint: statische Prüfung aller *.cmd/*.bat unter -Path (Sprungmarken von goto/call vorhanden, Klammern
  ausgeglichen). Exitcode 1 bei Fehlern.
#>
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$Results = '',
    [int]$TimeoutSeconds = 60,
    [switch]$Lint
)
$ErrorActionPreference = 'Stop'
$root = (Get-Location).Path
$base = (Resolve-Path -LiteralPath $Path).Path
$cmd = if ($env:SystemRoot) { Join-Path $env:SystemRoot 'System32\cmd.exe' } else { 'cmd.exe' }
try { $oem = [System.Text.Encoding]::GetEncoding([System.Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage) } catch { $oem = [System.Text.Encoding]::UTF8 }

function Get-RelativePath([string]$Full) {
    $prefix = $root.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if ($Full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { $Full = $Full.Substring($prefix.Length) }
    return ($Full -replace '\\', '/')
}

function Get-BatchFiles([string[]]$Patterns) {
    $items = if (Test-Path -LiteralPath $base -PathType Leaf) { @(Get-Item -LiteralPath $base) } else { @(Get-ChildItem -LiteralPath $base -Recurse -File) }
    return @($items | Where-Object { $name = $_.Name.ToLowerInvariant(); @($Patterns | Where-Object { $name -like $_ }).Count -gt 0 } | Sort-Object FullName)
}

function Invoke-Lint {
    $problems = 0
    foreach ($file in (Get-BatchFiles @('*.cmd', '*.bat'))) {
        $rel = Get-RelativePath $file.FullName
        $lines = [System.IO.File]::ReadAllLines($file.FullName, $oem)
        $labels = @{}
        foreach ($line in $lines) {
            if ($line -match '^\s*:([A-Za-z0-9_.\-]+)') { $labels[$Matches[1].ToLowerInvariant()] = $true }
        }
        $depth = 0
        for ($i = 0; $i -lt $lines.Length; $i++) {
            $line = $lines[$i]
            if ($line -match '^\s*(::|rem\b)' ) { continue }
            foreach ($m in [regex]::Matches($line, '(?i)\b(goto|call)\s+:?([A-Za-z0-9_.\-]+)')) {
                $verb = $m.Groups[1].Value.ToLowerInvariant()
                $target = $m.Groups[2].Value.ToLowerInvariant()
                $isLabelCall = $verb -eq 'goto' -or $line.Substring($m.Index) -match '(?i)^call\s+:'
                if ($isLabelCall -and $target -ne 'eof' -and -not $labels.ContainsKey($target)) {
                    Write-Output ("{0}:{1}: [Error] Sprungmarke :{2} fehlt ({3})" -f $rel, ($i + 1), $target, $verb)
                    $problems++
                }
            }
            $code = [regex]::Replace($line, '(?i)^\s*echo\b.*$', '')
            $code = [regex]::Replace($code, '"[^"]*"', '')
            $code = $code -replace '\^[()]', ''
            $depth += ([regex]::Matches($code, '\(').Count - [regex]::Matches($code, '\)').Count)
            if ($depth -lt 0) {
                Write-Output ("{0}:{1}: [Error] schließende Klammer ohne öffnende" -f $rel, ($i + 1))
                $problems++
                $depth = 0
            }
        }
        if ($depth -gt 0) {
            Write-Output ("{0}: [Error] {1} öffnende Klammer(n) nicht geschlossen" -f $rel, $depth)
            $problems++
        }
    }
    Write-Output ("Batch-Prüfung: {0} Fehler" -f $problems)
    if ($problems -gt 0) { exit 1 }
    exit 0
}

function Invoke-Test([System.IO.FileInfo]$File) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $cmd
    $psi.Arguments = '/d /c "' + $File.FullName + '"'
    $psi.WorkingDirectory = $root
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $true
    $psi.CreateNoWindow = $true
    $psi.StandardOutputEncoding = $oem
    $psi.StandardErrorEncoding = $oem
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $process = [System.Diagnostics.Process]::Start($psi)
    $process.StandardInput.Close()
    $out = $process.StandardOutput.ReadToEndAsync()
    $err = $process.StandardError.ReadToEndAsync()
    $timedOut = -not $process.WaitForExit($TimeoutSeconds * 1000)
    if ($timedOut) {
        & (Join-Path $env:SystemRoot 'System32\taskkill.exe') /T /F /PID $process.Id 2>&1 | Out-Null
        $process.WaitForExit()
    }
    $process.WaitForExit()
    return [pscustomobject]@{
        ExitCode = if ($timedOut) { -1 } else { $process.ExitCode }
        TimedOut = $timedOut
        Output   = ($out.Result + $err.Result)
        Seconds  = $watch.Elapsed.TotalSeconds
    }
}

function ConvertTo-Lines([string]$Text) {
    return @(($Text -replace "`r`n", "`n" -split "`n") | ForEach-Object { $_.TrimEnd() }) -join "`n"
}

if ($Lint) { Invoke-Lint }

if (-not $Results) { throw '-Results fehlt' }
New-Item -ItemType Directory -Force -Path $Results | Out-Null
$tests = Get-BatchFiles @('*.test.cmd', '*.test.bat', 'test_*.cmd', 'test_*.bat')
$xml = New-Object System.Xml.XmlDocument
$suites = $xml.AppendChild($xml.CreateElement('testsuites'))
$suite = $suites.AppendChild($xml.CreateElement('testsuite'))
$suite.SetAttribute('name', 'batch')
$failed = 0
foreach ($test in $tests) {
    $rel = Get-RelativePath $test.FullName
    $run = Invoke-Test $test
    $case = $suite.AppendChild($xml.CreateElement('testcase'))
    $case.SetAttribute('classname', ((Split-Path -Parent $rel) -replace '\\', '/'))
    $case.SetAttribute('name', $test.Name)
    $case.SetAttribute('time', ('{0:0.000}' -f $run.Seconds).Replace(',', '.'))
    $problem = $null
    if ($run.TimedOut) {
        $problem = "Zeitüberschreitung nach $TimeoutSeconds s"
    } elseif ($run.ExitCode -ne 0) {
        $problem = "Exitcode $($run.ExitCode)"
    } else {
        $stem = $test.FullName -replace '\.(cmd|bat)$', ''
        $expected = "$stem.expected.txt"
        if (Test-Path -LiteralPath $expected) {
            $want = ConvertTo-Lines ([System.IO.File]::ReadAllText($expected)).Trim()
            $got = ConvertTo-Lines $run.Output.Trim()
            if ($want -ne $got) { $problem = 'Ausgabe weicht von ' + (Split-Path -Leaf $expected) + " ab`nerwartet:`n$want`nerhalten:`n$got" }
        }
    }
    $tail = $run.Output
    if ($tail.Length -gt 4000) { $tail = $tail.Substring($tail.Length - 4000) }
    if ($problem) {
        $failed++
        $failure = $case.AppendChild($xml.CreateElement('failure'))
        $failure.SetAttribute('message', ($problem -split "`n")[0])
        $failure.InnerText = "$problem`n$tail"
        Write-Output "FEHLER  $rel – $(($problem -split "`n")[0])"
    } else {
        Write-Output "ok      $rel"
    }
    $sysout = $case.AppendChild($xml.CreateElement('system-out'))
    $sysout.InnerText = $tail
}
$suite.SetAttribute('tests', [string]$tests.Count)
$suite.SetAttribute('failures', [string]$failed)
$xml.Save((Join-Path $Results 'batch.xml'))
if ($tests.Count -eq 0) { Write-Output 'Keine Batch-Tests gefunden (*.test.cmd, test_*.cmd).'; exit 0 }
Write-Output ("Tests: {0}, bestanden {1}, fehlgeschlagen {2}" -f $tests.Count, ($tests.Count - $failed), $failed)
if ($failed -gt 0) { exit 1 }
exit 0
