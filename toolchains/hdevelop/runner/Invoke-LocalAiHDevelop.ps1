<#
  HDevelop über HDevEngine (Toolchain "hdevelop", Windows PowerShell 5.1 / .NET Framework).

  -Mode Info   HALCON-Version und Lizenz prüfen (Prüfbefehl beim VM-Aufbau).
  -Mode Run    Programm -Program ausführen, danach die Kontrollvariablen -Print ausgeben.
  -Mode Lint   Lade-Test: alle .hdev/.hdvp/.hdpl unter -Path laden, verwendete Prozeduren müssen auflösbar sein.
  -Mode Test   Lade-Test wie Lint, dann Tests ausführen und JUnit-XML nach -Results\hdevelop.xml schreiben.

  Tests: Programme *_test.hdev oder test_*.hdev. Jede lokale Prozedur test_* ohne Parameter ist ein Testfall;
  ein Testprogramm ohne solche Prozeduren läuft als ein Testfall (main). Ein Test schlägt fehl, wenn HDevEngine
  einen Fehler meldet – im Test z. B. mit  throw ('Erwartet 3, erhalten ' + Wert)  auslösen.
  Prozedurpfad: alle Ordner unter -Path mit .hdvp/.hdpl sowie %HALCONROOT%\procedures.

  Exitcode: 0 = ok, 1 = Fehler/fehlgeschlagene Tests, 2 = HALCON/Lizenz nicht nutzbar.
#>
param(
    [ValidateSet('Info', 'Run', 'Lint', 'Test')][string]$Mode = 'Test',
    [string]$Path = '.',
    [string]$Results = '',
    [string]$Program = '',
    [string[]]$Print = @()
)
$ErrorActionPreference = 'Stop'

function Initialize-Halcon {
    $root = $env:HALCONROOT
    # Meldungen hier mit Write-Host: die Ausgabe dieser Funktion ist ihr Rückgabewert (Version).
    if (-not $root -or -not (Test-Path -LiteralPath $root)) { Write-Host "HALCONROOT fehlt oder existiert nicht: '$root'"; exit 2 }
    $arch = if ($env:HALCONARCH) { $env:HALCONARCH } else { 'x64-win64' }
    $bin = Join-Path $root "bin\$arch"
    # halcondotnet lädt halcon.dll/hdevenginecpp.dll über PATH (nur für diesen Prozess).
    $env:PATH = "$bin;$env:PATH"
    $candidates = @($env:LOCALAI_HALCON_DOTNET, (Join-Path $root 'bin\dotnet35\halcondotnet.dll'),
                    (Join-Path $root 'bin\dotnet20\halcondotnet.dll'), (Join-Path $bin 'halcondotnet.dll')) |
                  Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    if (-not $candidates) { Write-Host "halcondotnet.dll nicht gefunden unter $root\bin"; exit 2 }
    [void][System.Reflection.Assembly]::LoadFrom(@($candidates)[0])
    try {
        $version = $null
        [HalconDotNet.HOperatorSet]::GetSystem('version', [ref]$version)
        return ((Get-TupleStrings $version) -join ' ')
    } catch {
        $message = Get-ErrorText $_.Exception
        Write-Host "HALCON nicht nutzbar: $message"
        if ($message -match '(?i)licen|2003|2042') {
            Write-Host 'Hinweis: Die VM hat keine Netzwerkkarte und keinen USB-Dongle – die Lizenz muss ohne MAC-Adresse/Dongle gelten (z. B. Evaluierungslizenz) und unter %HALCONROOT%\license liegen.'
        }
        exit 2
    }
}

function Get-ErrorText($Exception) {
    $e = $Exception
    while ($e.InnerException -and ($e -is [System.Management.Automation.MethodInvocationException] -or $e -is [System.Reflection.TargetInvocationException])) { $e = $e.InnerException }
    $parts = @($e.Message.Trim())
    foreach ($name in 'ProcedureName', 'LineNumber', 'LineText', 'HalconError') {
        $prop = $e.PSObject.Properties[$name]
        if ($prop -and $null -ne $prop.Value -and [string]$prop.Value -ne '' -and [string]$prop.Value -ne '0' -and -not $parts[0].Contains([string]$prop.Value)) {
            $parts += "$name=$($prop.Value)"
        }
    }
    return ($parts -join ' · ')
}

function Get-Relative([string]$Full) {
    $prefix = (Get-Location).Path.TrimEnd('\') + '\'
    if ($Full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { $Full = $Full.Substring($prefix.Length) }
    return ($Full -replace '\\', '/')
}

function Get-TupleStrings($Tuple) {
    # HTuple → Zeichenketten; Elemente über .O (Objektwert), damit Zahlen und Texte gleich behandelt werden.
    if ($null -eq $Tuple) { return @() }
    $list = @()
    for ($i = 0; $i -lt $Tuple.Length; $i++) { $list += [string]($Tuple[$i].O) }
    return $list
}

function New-Engine([string]$Base) {
    $engine = New-Object HalconDotNet.HDevEngine
    $dirs = @()
    if (Test-Path -LiteralPath $Base -PathType Container) {
        $dirs += @(Get-ChildItem -LiteralPath $Base -Recurse -File -Include '*.hdvp', '*.hdpl' | ForEach-Object { $_.DirectoryName } | Sort-Object -Unique)
    }
    $std = Join-Path $env:HALCONROOT 'procedures'
    if (Test-Path -LiteralPath $std) { $dirs += $std }
    if ($dirs.Count -gt 0) { $engine.SetProcedurePath(($dirs -join ';')) }
    return $engine
}

function Test-Load([string]$Base) {
    # Lädt jede Datei; Ergebnis: Liste von @{ File; Problem }.
    $results = @()
    $files = if (Test-Path -LiteralPath $Base -PathType Leaf) { @(Get-Item -LiteralPath $Base) } else {
        @(Get-ChildItem -LiteralPath $Base -Recurse -File -Include '*.hdev', '*.hdvp', '*.hdpl' | Sort-Object FullName) }
    foreach ($file in $files) {
        $problem = $null
        try {
            switch ($file.Extension.ToLowerInvariant()) {
                '.hdev' {
                    $program = New-Object HalconDotNet.HDevProgram($file.FullName)
                    $local = Get-TupleStrings $program.GetLocalProcedureNames()
                    foreach ($name in (Get-TupleStrings $program.GetUsedProcedureNames())) {
                        if ($local -contains $name) { continue }
                        try { $p = New-Object HalconDotNet.HDevProcedure($name); $p.Dispose() } catch { throw "Prozedur $name nicht auflösbar: $(Get-ErrorText $_.Exception)" }
                    }
                    $program.Dispose()
                }
                default {
                    $name = [System.IO.Path]::GetFileNameWithoutExtension($file.Name)
                    if ($file.Extension -ieq '.hdvp') { $p = New-Object HalconDotNet.HDevProcedure($name); $p.Dispose() }
                }
            }
        } catch {
            $problem = if ($_.Exception.Message -like 'Prozedur *') { $_.Exception.Message } else { Get-ErrorText $_.Exception }
        }
        $results += [pscustomobject]@{ File = $file.FullName; Problem = $problem }
    }
    return $results
}

function Invoke-Tests([string]$Base) {
    $cases = @()
    $tests = @(Get-ChildItem -LiteralPath $Base -Recurse -File -Include '*_test.hdev', 'test_*.hdev' | Sort-Object FullName)
    foreach ($file in $tests) {
        $rel = Get-Relative $file.FullName
        try {
            $program = New-Object HalconDotNet.HDevProgram($file.FullName)
        } catch {
            $cases += [pscustomobject]@{ Class = $rel; Name = 'laden'; Problem = (Get-ErrorText $_.Exception); Seconds = 0; Skipped = $false }
            continue
        }
        $names = @(Get-TupleStrings $program.GetLocalProcedureNames() | Where-Object { $_ -like 'test_*' } | Sort-Object)
        if ($names.Count -eq 0) {
            $watch = [System.Diagnostics.Stopwatch]::StartNew()
            $problem = $null
            try { $call = $program.Execute(); $call.Dispose() } catch { $problem = Get-ErrorText $_.Exception }
            $cases += [pscustomobject]@{ Class = $rel; Name = 'main'; Problem = $problem; Seconds = $watch.Elapsed.TotalSeconds; Skipped = $false }
        }
        foreach ($name in $names) {
            $watch = [System.Diagnostics.Stopwatch]::StartNew()
            $problem = $null
            $skipped = $false
            try {
                $proc = New-Object HalconDotNet.HDevProcedure($program, $name)
                $inputs = $proc.GetInputCtrlParamCount() + $proc.GetInputIconicParamCount()
                if ($inputs -gt 0) {
                    $skipped = $true
                } else {
                    $call = New-Object HalconDotNet.HDevProcedureCall($proc)
                    $call.Execute()
                    $call.Dispose()
                }
                $proc.Dispose()
            } catch {
                $problem = Get-ErrorText $_.Exception
            }
            $cases += [pscustomobject]@{ Class = $rel; Name = $name; Problem = $problem; Seconds = $watch.Elapsed.TotalSeconds; Skipped = $skipped }
        }
        $program.Dispose()
    }
    return $cases
}

function Write-JUnit($Cases, [string]$File) {
    $xml = New-Object System.Xml.XmlDocument
    $suites = $xml.AppendChild($xml.CreateElement('testsuites'))
    $suite = $suites.AppendChild($xml.CreateElement('testsuite'))
    $suite.SetAttribute('name', 'hdevelop')
    $suite.SetAttribute('tests', [string]@($Cases).Count)
    $suite.SetAttribute('failures', [string]@($Cases | Where-Object { $_.Problem }).Count)
    foreach ($c in $Cases) {
        $case = $suite.AppendChild($xml.CreateElement('testcase'))
        $case.SetAttribute('classname', $c.Class)
        $case.SetAttribute('name', $c.Name)
        $case.SetAttribute('time', ('{0:0.000}' -f $c.Seconds).Replace(',', '.'))
        if ($c.Problem) {
            $failure = $case.AppendChild($xml.CreateElement('failure'))
            $failure.SetAttribute('message', $c.Problem)
            $failure.InnerText = $c.Problem
        } elseif ($c.Skipped) {
            [void]$case.AppendChild($xml.CreateElement('skipped'))
        }
    }
    $xml.Save($File)
}

$version = Initialize-Halcon
Write-Output "HALCON $version (HDevEngine)"
switch ($Mode) {
    'Info' { exit 0 }
    'Run' {
        if (-not $Program) { Write-Output '-Program fehlt'; exit 1 }
        $base = Split-Path -Parent (Resolve-Path -LiteralPath $Program).Path
        $engine = New-Engine $base
        try {
            $prog = New-Object HalconDotNet.HDevProgram((Resolve-Path -LiteralPath $Program).Path)
            $call = $prog.Execute()
            foreach ($name in $Print) { Write-Output ("{0} = {1}" -f $name, ((Get-TupleStrings $call.GetCtrlVarTuple($name)) -join ', ')) }
            $call.Dispose(); $prog.Dispose()
        } catch {
            Write-Output "Fehler: $(Get-ErrorText $_.Exception)"
            exit 1
        }
        exit 0
    }
    default {
        $base = (Resolve-Path -LiteralPath $Path).Path
        $engine = New-Engine $base
        $loaded = @(Test-Load $base)
        $loadFailures = @($loaded | Where-Object { $_.Problem })
        foreach ($l in $loaded) {
            if ($l.Problem) { Write-Output ("{0}: [Error] {1}" -f (Get-Relative $l.File), $l.Problem) }
        }
        Write-Output ("Lade-Test: {0} Datei(en), {1} Fehler" -f $loaded.Count, $loadFailures.Count)
        if ($Mode -eq 'Lint') { if ($loadFailures.Count -gt 0) { exit 1 } else { exit 0 } }
        if (-not $Results) { Write-Output '-Results fehlt'; exit 1 }
        New-Item -ItemType Directory -Force -Path $Results | Out-Null
        $cases = @()
        foreach ($l in $loadFailures) { $cases += [pscustomobject]@{ Class = (Get-Relative $l.File); Name = 'laden'; Problem = $l.Problem; Seconds = 0; Skipped = $false } }
        $cases += @(Invoke-Tests $base)
        foreach ($c in $cases) {
            $state = if ($c.Problem) { 'FEHLER ' } elseif ($c.Skipped) { 'übersprungen (hat Parameter)' } else { 'ok     ' }
            Write-Output ("{0} {1} :: {2}{3}" -f $state, $c.Class, $c.Name, $(if ($c.Problem) { " – $($c.Problem)" } else { '' }))
        }
        Write-JUnit $cases (Join-Path $Results 'hdevelop.xml')
        $failed = @($cases | Where-Object { $_.Problem }).Count
        Write-Output ("Tests: {0}, fehlgeschlagen {1}" -f $cases.Count, $failed)
        if ($failed -gt 0) { exit 1 }
        exit 0
    }
}
