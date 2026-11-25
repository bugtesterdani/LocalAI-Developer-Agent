#Requires -Version 5.1
<#
.SYNOPSIS
  Startet edge0-infer serve offline für LocalAI (docs/edge0.md, Abschnitt 4.3).

.DESCRIPTION
  Vor dem Start:
   - prüft, ob edge0-infer.exe eine Ausgangssperre der Windows-Firewall hat (firewall.ps1); ohne Sperre kein Start
     (überspringbar mit -SkipFirewallCheck),
   - schaltet Downloads und Telemetrie bekannter Bibliotheken ab (HF_HUB_OFFLINE, TRANSFORMERS_OFFLINE,
     HF_HUB_DISABLE_TELEMETRY, UV_OFFLINE, DO_NOT_TRACK) und lenkt HTTP(S)-Proxys auf eine tote lokale Adresse,
     Loopback ausgenommen – will doch etwas nachladen, scheitert es sofort lokal,
   - erzeugt bei Zugriff aus dem LAN ein zufälliges Token, falls die Token-Datei fehlt.

.PARAMETER Edge0Dir
  Ordner mit bin\edge0-infer.exe (oder edge0-infer.exe direkt darin).
.PARAMETER Model
  Modellordner (enthält model.json).
.PARAMETER Device
  cpu, vulkan, cuda oder cuda:N (145B/120B). Standard cpu.
.PARAMETER Context
  Kontextlänge; mindestens so groß wie das größte contextTokens der Edge0-Profile. 0 = Vorgabe des Servers.
.PARAMETER Port
  Standard 8000.
.PARAMETER ListenAddress
  127.0.0.1 (Standard, nur dieser Rechner) oder 0.0.0.0 bzw. eine LAN-IP für den Orchestrator auf einem anderen
  Rechner – dann ist -ApiKeyFile Pflicht.
.PARAMETER ApiKeyFile
  Token-Datei für Zugriffe außerhalb von Loopback; fehlt sie, wird ein zufälliges Token erzeugt.
.PARAMETER Alias
  Modellname für die Clients (Standard: aus model.json, z. B. edge0_35b).
.PARAMETER StrataDir
  Nur 145B/120B: Ordner edge0-strata.
.PARAMETER StrataPython
  Nur 145B/120B: Python der edge0-strata-Umgebung (.venv\Scripts\python.exe).
.PARAMETER ExtraArgs
  Weitere Argumente für "edge0-infer serve".

.EXAMPLE
  .\scripts\edge0\start.ps1 -Edge0Dir D:\Edge0 -Model D:\Edge0\modelle\edge0-35b -Device vulkan -Context 32768
.EXAMPLE
  .\scripts\edge0\start.ps1 -Edge0Dir D:\Edge0 -Model D:\Edge0\modelle\edge0-35b -Device vulkan -Context 32768 -ListenAddress 0.0.0.0 -ApiKeyFile D:\Edge0\edge0.key
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Edge0Dir,
    [Parameter(Mandatory = $true)] [string] $Model,
    [ValidatePattern('^(cpu|vulkan|cuda(:\d+)?)$')] [string] $Device = 'cpu',
    [ValidateRange(0, 1048576)] [int] $Context = 0,
    [ValidateRange(1, 65535)] [int] $Port = 8000,
    [string] $ListenAddress = '127.0.0.1',
    [string] $ApiKeyFile = '',
    [string] $Alias = '',
    [string] $StrataDir = '',
    [string] $StrataPython = '',
    [switch] $SkipFirewallCheck,
    [string[]] $ExtraArgs = @()
)
$ErrorActionPreference = 'Stop'

$exe = @((Join-Path $Edge0Dir 'bin\edge0-infer.exe'), (Join-Path $Edge0Dir 'edge0-infer.exe')) |
    Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $exe) {
    $exe = Get-ChildItem -LiteralPath $Edge0Dir -Recurse -File -Filter 'edge0-infer.exe' -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $exe) { throw "edge0-infer.exe nicht gefunden unter $Edge0Dir" }
$exe = (Resolve-Path -LiteralPath $exe).ProviderPath
if (-not (Test-Path -LiteralPath (Join-Path $Model 'model.json'))) { throw "Kein Modellordner (model.json fehlt): $Model" }

if (-not $SkipFirewallCheck) {
    # Regeln aus firewall.ps1 (Gruppe "LocalAI Edge0"); Pfadvergleich ohne Groß-/Kleinschreibung.
    $block = @(Get-NetFirewallRule -Group 'LocalAI Edge0' -ErrorAction SilentlyContinue |
        Where-Object { "$($_.Direction)" -eq 'Outbound' -and "$($_.Action)" -eq 'Block' -and "$($_.Enabled)" -eq 'True' } |
        Where-Object { ($_ | Get-NetFirewallApplicationFilter).Program -ieq $exe })
    if ($block.Count -eq 0) {
        throw ((("{0} hat keine Ausgangssperre in der Windows-Firewall. Zuerst als Administrator ausführen: " +
            "scripts\edge0\firewall.ps1 -Edge0Dir {1}") -f $exe, $Edge0Dir))
    }
}

$loopback = $ListenAddress -in @('127.0.0.1', 'localhost', '::1')
if (-not $loopback) {
    if (-not $ApiKeyFile) { throw 'Zugriff aus dem LAN nur mit Token: -ApiKeyFile <Pfad> angeben.' }
    if (-not (Test-Path -LiteralPath $ApiKeyFile)) {
        $bytes = New-Object byte[] 32
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
        Set-Content -LiteralPath $ApiKeyFile -Value $token -NoNewline -Encoding Ascii
        Write-Host "Neues Token in $ApiKeyFile – diese Datei auf den Orchestrator-Rechner kopieren (edge0.apiKeyFile)."
    }
}

# Offline: Downloads/Telemetrie aus, Proxy ins Leere (Loopback ausgenommen, edge0-infer spricht strata über Loopback an).
$env:HF_HUB_OFFLINE = '1'
$env:HF_DATASETS_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:HF_HUB_DISABLE_TELEMETRY = '1'
$env:UV_OFFLINE = '1'
$env:DO_NOT_TRACK = '1'
$env:HTTP_PROXY = 'http://127.0.0.1:9'
$env:HTTPS_PROXY = 'http://127.0.0.1:9'
$env:ALL_PROXY = 'http://127.0.0.1:9'
$env:NO_PROXY = 'localhost,127.0.0.1,::1'
if ($Device -like 'cuda*' -and $env:CUDA_PATH) {
    $env:PATH = (Join-Path $env:CUDA_PATH 'bin') + ';' + $env:PATH  # cublas64_13.dll für strata.exe
}

$serveArgs = @('serve', '--model', $Model, '--device', $Device, '--host', $ListenAddress, '--port', "$Port")
if ($Context -gt 0) { $serveArgs += @('--context', "$Context") }
if ($Alias) { $serveArgs += @('--alias', $Alias) }
if (-not $loopback) { $serveArgs += @('--api-key-file', $ApiKeyFile) }
if ($StrataDir) { $serveArgs += @('--strata-dir', $StrataDir) }
if ($StrataPython) { $serveArgs += @('--strata-python', $StrataPython) }
$serveArgs += $ExtraArgs

Write-Host ("Starte: {0} {1}" -f $exe, ($serveArgs -join ' '))
Write-Host 'Der Server lauscht erst, wenn das Modell geladen ist. Beenden mit Strg+C.'
& $exe @serveArgs
exit $LASTEXITCODE
