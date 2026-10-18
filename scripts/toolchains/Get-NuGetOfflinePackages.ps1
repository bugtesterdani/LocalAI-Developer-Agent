#Requires -Version 5.1
<#
.SYNOPSIS
    Legt eine Offline-NuGet-Quelle für die dotnet-Toolchain an (Ordner packages\ im Toolchain-Ordner).

.DESCRIPTION
    Die Sandbox-VM hat kein Netzwerk. Restore, Build und Test holen Pakete deshalb nur aus dem Ordner
    packages\ der registrierten dotnet-Toolchain. Dieses Skript läuft EINMAL auf dem Host MIT Internet:
    es stellt ein kleines Vorlagenprojekt (Testframeworks) – und optional weitere Projekte – über nuget.org
    wieder her und kopiert alle dabei geladenen .nupkg-Dateien flach nach <Toolchain>\packages.

    Es wird nur 'dotnet restore' ausgeführt, kein Build und kein Projektcode. Danach die Toolchain in der
    Oberfläche erneut prüfen und registrieren (die Dateien gehen in den Hash ein) und die VM neu aufbauen.

.PARAMETER ToolchainDirectory
    Registrierter Toolchain-Ordner, z. B. D:\Agent\Toolchains\dotnet-9.0.306.

.PARAMETER Project
    Optionale weitere .csproj/.sln, deren Pakete ebenfalls offline verfügbar sein sollen.

.EXAMPLE
    .\scripts\toolchains\Get-NuGetOfflinePackages.ps1 -ToolchainDirectory D:\Agent\Toolchains\dotnet-9.0.306
#>
param(
    [Parameter(Mandatory)][string]$ToolchainDirectory,
    [string[]]$Project = @()
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath (Join-Path $ToolchainDirectory 'toolchain.json'))) {
    throw "Kein Toolchain-Ordner (toolchain.json fehlt): $ToolchainDirectory"
}
$dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
if (-not $dotnet) { throw 'dotnet wurde auf dem Host nicht gefunden (wird nur für dieses Herunterladen gebraucht).' }

$temp = Join-Path ([System.IO.Path]::GetTempPath()) ("localai-nuget-" + [guid]::NewGuid().ToString('N'))
$cache = Join-Path $temp 'cache'
New-Item -ItemType Directory -Force -Path $cache | Out-Null
try {
    $projects = @((Join-Path $PSScriptRoot 'nuget-template\Packages.csproj')) + $Project
    foreach ($p in $projects) {
        Write-Host "Pakete für $p werden geladen …"
        & $dotnet.Source restore $p --packages $cache --source https://api.nuget.org/v3/index.json --nologo
        if ($LASTEXITCODE -ne 0) { throw "dotnet restore für $p fehlgeschlagen (Exitcode $LASTEXITCODE)" }
    }
    $target = Join-Path $ToolchainDirectory 'packages'
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $count = 0
    Get-ChildItem -LiteralPath $cache -Recurse -Filter *.nupkg | ForEach-Object {
        $dest = Join-Path $target $_.Name
        if (-not (Test-Path -LiteralPath $dest)) { Copy-Item -LiteralPath $_.FullName -Destination $dest; $count++ }
    }
    Write-Host "$count Paket(e) nach $target kopiert."
    Write-Host 'Jetzt in der Oberfläche unter TOOLCHAINS erneut prüfen und registrieren, danach die VM neu aufbauen.'
} finally {
    Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
}
