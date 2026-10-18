#Requires -Version 5.1
<#
.SYNOPSIS
    Legt die Toolchain-Ordner für M5 an (C++, PowerShell, Batch, HDevelop) und lädt bzw. sammelt die Dateien,
    die die Offline-VM braucht.

.DESCRIPTION
    Läuft EINMAL auf dem Host MIT Internet (nicht in der VM). Je gewählter Toolchain:

      batch        Vorlage kopieren – sonst nichts nötig (cmd.exe und Windows PowerShell sind Teil von Windows).
      powershell   Vorlage kopieren, PowerShell-ZIP von GitHub laden, Pester und PSScriptAnalyzer per Save-Module
                   nach modules\ speichern.
      powershell5  Vorlage kopieren, Pester und PSScriptAnalyzer nach modules\ (Windows PowerShell 5.1 ist im Gast).
      cpp          Vorlage kopieren, Visual-Studio-Build-Tools-Bootstrapper laden und ein Offline-Layout nur mit den
                   nötigen Komponenten nach layout\ erzeugen (einige GB, dauert).
      hdevelop     Vorlage kopieren, aus deiner HALCON-Installation halcon.zip packen (ohne examples\ und doc\),
                   Lizenzdateien nach license\ kopieren, VC++-Laufzeit (vc_redist.x64.exe) laden.

    Bestehende toolchain.json werden nur mit -Force ersetzt. Danach in der Oberfläche unter TOOLCHAINS prüfen und
    registrieren; die VM baut sich beim nächsten Start mit den neuen Toolchains auf.

.PARAMETER Target
    Toolchain-Ordner aus config\localai.json (toolchains.directory), z. B. D:\Agent\Toolchains.

.PARAMETER Toolchain
    Welche Toolchains: batch, powershell, powershell5, cpp, hdevelop (Standard: batch, powershell, cpp, hdevelop).

.PARAMETER HalconRoot
    HALCON-Installationsordner (Standard: $env:HALCONROOT).

.EXAMPLE
    .\scripts\toolchains\New-M5Toolchains.ps1 -Target D:\Agent\Toolchains
.EXAMPLE
    .\scripts\toolchains\New-M5Toolchains.ps1 -Target D:\Agent\Toolchains -Toolchain batch, powershell
#>
param(
    [Parameter(Mandatory = $true)][string]$Target,
    [ValidateSet('batch', 'powershell', 'powershell5', 'cpp', 'hdevelop')][string[]]$Toolchain = @('batch', 'powershell', 'cpp', 'hdevelop'),
    [string]$HalconRoot = $env:HALCONROOT,
    [string]$PowerShellVersion = '7.6.7',
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$templates = Join-Path $PSScriptRoot '..\..\toolchains'

function Copy-Template([string]$Name) {
    $dest = Join-Path $Target $Name
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $source = Join-Path $templates $Name
    foreach ($item in Get-ChildItem -LiteralPath $source -Force) {
        $to = Join-Path $dest $item.Name
        if ($item.Name -eq 'toolchain.json' -and (Test-Path -LiteralPath $to) -and -not $Force) {
            Write-Host "  $to bleibt (mit -Force ersetzen)"
            continue
        }
        Copy-Item -LiteralPath $item.FullName -Destination $dest -Recurse -Force
    }
    Write-Host "  Vorlage nach $dest kopiert"
    return $dest
}

function Get-File([string]$Url, [string]$Path) {
    if (Test-Path -LiteralPath $Path) { Write-Host "  vorhanden: $Path"; return }
    Write-Host "  lade $Url …"
    Invoke-WebRequest -Uri $Url -OutFile $Path -UseBasicParsing
}

function Save-Modules([string]$Dest) {
    $modules = Join-Path $Dest 'modules'
    New-Item -ItemType Directory -Force -Path $modules | Out-Null
    foreach ($name in 'Pester', 'PSScriptAnalyzer') {
        if (Get-ChildItem -LiteralPath (Join-Path $modules $name) -ErrorAction SilentlyContinue) { Write-Host "  vorhanden: $name"; continue }
        Write-Host "  Save-Module $name …"
        Save-Module -Name $name -Path $modules -Repository PSGallery -Force
    }
}

foreach ($name in $Toolchain) {
    Write-Host "== $name"
    $dest = Copy-Template $name
    switch ($name) {
        'batch' { }
        'powershell' {
            Get-File "https://github.com/PowerShell/PowerShell/releases/download/v$PowerShellVersion/PowerShell-$PowerShellVersion-win-x64.zip" (Join-Path $dest "PowerShell-$PowerShellVersion-win-x64.zip")
            Save-Modules $dest
        }
        'powershell5' { Save-Modules $dest }
        'cpp' {
            $bootstrapper = Join-Path $dest 'vs_buildtools_bootstrapper.exe'
            Get-File 'https://aka.ms/vs/17/release/vs_buildtools.exe' $bootstrapper
            $layout = Join-Path $dest 'layout'
            Write-Host "  Offline-Layout nach $layout (einige GB) …"
            $arguments = @('--layout', $layout, '--lang', 'en-US', '--passive', '--wait',
                '--add', 'Microsoft.VisualStudio.Workload.VCTools',
                '--add', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
                '--add', 'Microsoft.VisualStudio.Component.VC.CMake.Project',
                '--add', 'Microsoft.VisualStudio.Component.Windows11SDK.26100')
            $process = Start-Process -FilePath $bootstrapper -ArgumentList $arguments -Wait -PassThru
            if ($process.ExitCode -notin 0, 3010) { throw "Layout fehlgeschlagen (Exitcode $($process.ExitCode))" }
            if (-not (Test-Path -LiteralPath (Join-Path $layout 'vs_buildtools.exe'))) { throw "layout\vs_buildtools.exe fehlt nach dem Layout" }
            # Der Bootstrapper selbst gehört nicht in den Toolchain-Ordner (die VM installiert aus layout\).
            Remove-Item -LiteralPath $bootstrapper -Force
        }
        'hdevelop' {
            if (-not $HalconRoot -or -not (Test-Path -LiteralPath (Join-Path $HalconRoot 'bin'))) { throw "HALCON-Installation nicht gefunden: '$HalconRoot' (-HalconRoot angeben)" }
            $zip = Join-Path $dest 'halcon.zip'
            if (-not (Test-Path -LiteralPath $zip)) {
                Write-Host "  packe $HalconRoot nach $zip (ohne examples\, doc\, license\) …"
                Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
                $root = (Resolve-Path -LiteralPath $HalconRoot).Path.TrimEnd('\')
                $archive = [System.IO.Compression.ZipFile]::Open($zip, [System.IO.Compression.ZipArchiveMode]::Create)
                try {
                    Get-ChildItem -LiteralPath $root -Recurse -File | ForEach-Object {
                        $rel = $_.FullName.Substring($root.Length + 1)
                        if ($rel -match '^(examples|doc|license)\\') { return }
                        [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $_.FullName, ($rel -replace '\\', '/'), [System.IO.Compression.CompressionLevel]::Optimal)
                    }
                } finally {
                    $archive.Dispose()
                }
            }
            $license = Join-Path $dest 'license'
            New-Item -ItemType Directory -Force -Path $license | Out-Null
            Get-ChildItem -LiteralPath (Join-Path $HalconRoot 'license') -Filter 'license*.dat' -ErrorAction SilentlyContinue | Copy-Item -Destination $license -Force
            if (-not (Get-ChildItem -LiteralPath $license -Filter '*.dat')) {
                Write-Warning "Keine Lizenzdatei in $license – eine Lizenz, die ohne Netzwerkkarte/Dongle gilt, dort ablegen."
            }
            Get-File 'https://aka.ms/vs/17/release/vc_redist.x64.exe' (Join-Path $dest 'vc_redist.x64.exe')
        }
    }
}
Write-Host ''
Write-Host 'Fertig. Versionen in toolchain.json prüfen (version, install.file, install.check.expectOutput), dann in der Oberfläche'
Write-Host 'unter TOOLCHAINS prüfen und registrieren. Im Projekt "toolchains" wählen (z. B. config\m5-cpp-project.json).'
