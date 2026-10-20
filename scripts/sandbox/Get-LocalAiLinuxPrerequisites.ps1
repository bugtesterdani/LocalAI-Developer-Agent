#Requires -Version 5.1
<#
.SYNOPSIS
    Lädt alles, was die Linux-Sandbox-VM (M8) offline braucht, und schreibt prerequisites.json.

.DESCRIPTION
    Läuft auf dem Host mit Internet (nicht in der VM). Lädt:
      1. Debian 13 Cloud-Abbild (generic, amd64, Rohformat) samt Prüfung gegen SHA512SUMS
      2. Python für Linux (python-build-standalone, install_only_stripped) samt Prüfung gegen SHA256SUMS
      3. Wheels des Workers für Linux (pydantic und Abhängigkeiten, gleiche Versionen wie src/python/.venv)
      4. Debian-Paket hyperv-daemons (KVP-Datenaustausch und Copy-VMFile im Gast) samt Abhängigkeiten
      5. mit -WithQemu (M9, Betriebssystem-Debugging): QEMU, OVMF (UEFI) und binutils (objdump) samt Abhängigkeiten
    Ergebnis:
      <Ziel>\debian-13-generic-amd64.raw          → sandbox.hypervLinux.imagePath
      <Ziel>\linux-gast\prerequisites.json …      → sandbox.hypervLinux.prerequisites (Standard: neben dem Abbild)

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\sandbox\Get-LocalAiLinuxPrerequisites.ps1 -Target D:\LocalAi\linux
#>
param(
    [Parameter(Mandatory)][string]$Target,
    [string]$PythonMinor = '3.13',
    [string]$DebianImageUrl = 'https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.raw',
    [switch]$WithQemu,
    [switch]$SkipImage
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$prereq = Join-Path $Target 'linux-gast'
New-Item -ItemType Directory -Force -Path $prereq, (Join-Path $prereq 'wheels'), (Join-Path $prereq 'debs') | Out-Null

function Get-Sha([string]$Path, [string]$Algorithm = 'SHA256') { (Get-FileHash -LiteralPath $Path -Algorithm $Algorithm).Hash.ToLowerInvariant() }

function Save-Url([string]$Url, [string]$Path) {
    Write-Host "Lade $Url"
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Path
}

# ------------------------------------------------------------------ 1 Debian
if (-not $SkipImage) {
    $image = Join-Path $Target 'debian-13-generic-amd64.raw'
    $sums = Join-Path $Target 'SHA512SUMS'
    Save-Url (($DebianImageUrl -replace '/[^/]+$', '') + '/SHA512SUMS') $sums
    $line = Get-Content -LiteralPath $sums | Where-Object { $_ -match ' debian-13-generic-amd64\.raw$' } | Select-Object -First 1
    if (-not $line) { throw 'debian-13-generic-amd64.raw fehlt in SHA512SUMS' }
    $expected = ($line -split '\s+')[0].ToLowerInvariant()
    if (-not (Test-Path -LiteralPath $image) -or (Get-Sha $image 'SHA512') -ne $expected) {
        Save-Url $DebianImageUrl $image
        if ((Get-Sha $image 'SHA512') -ne $expected) { throw 'Debian-Abbild: SHA512 stimmt nicht' }
    }
    Write-Host "Debian-Abbild geprüft: $image"
}

# ------------------------------------------------------------------ 2 Python für Linux
$release = Invoke-RestMethod -UseBasicParsing -Uri 'https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest'
$pattern = '^cpython-' + [regex]::Escape($PythonMinor) + '\.[0-9]+\+[0-9]+-x86_64-unknown-linux-gnu-install_only_stripped\.tar\.gz$'
$asset = @($release.assets | Where-Object { $_.name -match $pattern }) | Select-Object -First 1
if (-not $asset) { throw "Kein python-build-standalone für Python $PythonMinor (x86_64-unknown-linux-gnu) im Release $($release.tag_name)" }
$sumAsset = @($release.assets | Where-Object { $_.name -eq 'SHA256SUMS' }) | Select-Object -First 1
$pythonFile = Join-Path $prereq $asset.name
if (-not (Test-Path -LiteralPath $pythonFile)) { Save-Url $asset.browser_download_url $pythonFile }
if ($sumAsset) {
    $sumFile = Join-Path $prereq 'python-SHA256SUMS'
    Save-Url $sumAsset.browser_download_url $sumFile
    $line = Get-Content -LiteralPath $sumFile | Where-Object { $_ -match [regex]::Escape($asset.name) + '$' } | Select-Object -First 1
    if ($line -and ($line -split '\s+')[0].ToLowerInvariant() -ne (Get-Sha $pythonFile)) { throw 'Python-Archiv: SHA256 stimmt nicht' }
    Remove-Item -LiteralPath $sumFile -Force
}
$pythonVersion = [regex]::Match($asset.name, '^cpython-([0-9.]+)\+').Groups[1].Value
Write-Host "Python für Linux: $($asset.name)"

# ------------------------------------------------------------------ 3 Wheels (Versionen wie in der Entwicklungsumgebung)
# Installierte Fassung: kein .venv und kein uv – mitgeliefertes Python unter runtime\python.
$runtimePython = Join-Path $repo 'runtime\python\python.exe'
$venv = Join-Path $repo 'src\python\.venv'
if (Test-Path -LiteralPath $venv) {
    $site = Get-ChildItem -LiteralPath $venv -Recurse -Directory -Filter 'site-packages' | Select-Object -First 1
} elseif (Test-Path -LiteralPath $runtimePython) {
    $site = Get-Item -LiteralPath (Join-Path $repo 'runtime\python\Lib\site-packages')
}
if (-not $site) { throw "src\python\.venv fehlt – zuerst 'uv sync --project src/python' ausführen" }
$pydantic = Get-ChildItem -LiteralPath $site.FullName -Directory -Filter 'pydantic-*.dist-info' | Select-Object -First 1
if (-not $pydantic) { throw "pydantic ist in $($site.FullName) nicht installiert" }
$useUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)
$pydanticVersion = ($pydantic.Name -replace '^pydantic-', '' -replace '\.dist-info$', '')
$wheelDir = Join-Path $prereq 'wheels'
Get-ChildItem -LiteralPath $wheelDir -Filter '*.whl' | Remove-Item -Force
$pipArgs = @('download', "pydantic==$pydanticVersion", '--dest', $wheelDir, '--only-binary=:all:',
    '--platform', 'manylinux2014_x86_64', '--platform', 'manylinux_2_17_x86_64', '--python-version', $PythonMinor, '--implementation', 'cp')
if ($useUv) { & uvx --from pip pip @pipArgs } else { & $runtimePython -m pip @pipArgs }
if ($LASTEXITCODE -ne 0) { throw "pip download fehlgeschlagen (Exitcode $LASTEXITCODE)" }

# ------------------------------------------------------------------ 4/5 Debian-Pakete (mit Abhängigkeiten, Hashprüfung)
$debDir = Join-Path $prereq 'debs'
Get-ChildItem -LiteralPath $debDir -Filter '*.deb' | Remove-Item -Force
$debPackages = @('hyperv-daemons')
if ($WithQemu) { $debPackages += @('qemu-system-x86', 'ovmf', 'binutils') }
Push-Location $repo
try {
    if ($useUv) { & uv run --no-sync --project src/python python tools/prereq/debian_closure.py --dest $debDir @debPackages }
    else { & $runtimePython tools/prereq/debian_closure.py --dest $debDir @debPackages }
    if ($LASTEXITCODE -ne 0) { throw "Debian-Pakete konnten nicht geladen werden (Exitcode $LASTEXITCODE)" }
} finally { Pop-Location }

# ------------------------------------------------------------------ prerequisites.json
$hashes = [ordered]@{}
$wheels = @(Get-ChildItem -LiteralPath $wheelDir -Filter '*.whl' | Sort-Object Name | ForEach-Object { 'wheels/' + $_.Name })
$debs = @(Get-ChildItem -LiteralPath $debDir -Filter '*.deb' | Sort-Object Name | ForEach-Object { 'debs/' + $_.Name })
foreach ($file in @($asset.name) + $wheels + $debs) { $hashes[$file] = Get-Sha (Join-Path $prereq ($file -replace '/', '\')) }
$manifest = [ordered]@{
    schemaVersion = 1
    python        = $asset.name
    pythonVersion = $pythonVersion
    wheels        = $wheels
    debs          = $debs
    sha256        = $hashes
    created       = (Get-Date).ToString('s')
}
$json = $manifest | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText((Join-Path $prereq 'prerequisites.json'), $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ''
Write-Host "Fertig. In config\localai.json unter sandbox.hypervLinux eintragen:"
Write-Host ("  ""imagePath"": ""{0}""" -f ((Join-Path $Target 'debian-13-generic-amd64.raw') -replace '\\', '\\'))
Write-Host ("  ""prerequisites"": ""{0}""" -f ($prereq -replace '\\', '\\'))
