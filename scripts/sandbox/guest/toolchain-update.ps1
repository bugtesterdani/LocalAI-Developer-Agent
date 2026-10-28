#Requires -Version 5.1
<#
    Toolchain-Update (läuft als SYSTEM, vom Worker gestartet): installiert die übergebenen Toolchains in einer laufenden
    VM neu – ohne Windows neu aufzubauen. Gleicher Ablauf wie beim Aufbau (provision.ps1): Hashes im Gast prüfen,
    installieren, Prüfbefehl, Selbsttest, für Jobs bereitstellen. Eine vorhandene Installation wird vorher entfernt.

    -Names       Toolchain-Namen, durch Komma getrennt (Ordner unter -SourceRoot, je mit toolchain.json und pinned.json)
    -SourceRoot  entpackte Nutzlast des Jobs Sandbox.Toolchains
    -ResultFile  Ergebnis je Toolchain als JSON (name, version, installed, check, selfTest, seconds, error)
#>
param(
    [Parameter(Mandatory)][string]$Names,
    [Parameter(Mandatory)][string]$SourceRoot,
    [Parameter(Mandatory)][string]$ResultFile
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Import-Module (Join-Path $root 'LocalAiGuest.psm1') -Force
Set-LocalAiGuestRoot -Path $root

$results = @()
foreach ($name in ($Names -split ',' | Where-Object { $_ })) {
    if ($name -notmatch '^[a-z0-9][a-z0-9-]{0,63}$') {
        $results += [ordered]@{ name = $name; installed = $false; error = 'unzulässiger Name' }
        continue
    }
    Write-Host "Toolchain $name wird neu installiert …"
    $entry = $null
    try {
        # Alte Installation und Bereitstellung entfernen (ZIP-Archive lassen sich nicht über vorhandene Dateien entpacken).
        foreach ($old in @("C:\Toolchains\$name", (Join-Path $root "toolchains\$name"), (Join-Path $root "work\selftest-$name"))) {
            if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }
        }
        $entry = Install-Toolchain -Name $name -SourceRoot $SourceRoot
        if ($entry.installed) {
            try { Publish-ToolchainForJobs -Name $name -SourceRoot $SourceRoot -Target (Join-Path $root 'toolchains') }
            catch { $entry.error = "Bereitstellung für Jobs: $($_.Exception.Message -replace '[\r\n]+', ' ')"; $entry.installed = $false }
        }
    } catch {
        $entry = [ordered]@{ name = $name; installed = $false; error = ($_.Exception.Message -replace '[\r\n]+', ' ') }
    }
    Write-Host ("Toolchain {0}: installiert={1} {2}" -f $name, $entry.installed, $entry.error)
    $results += $entry
}

# Wie nach dem Aufbau: Toolchains für Werkzeugprozesse nur lesbar (SEC-21).
if (Test-Path -LiteralPath 'C:\Toolchains') {
    & icacls.exe 'C:\Toolchains' /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
}
ConvertTo-Json -InputObject @($results) -Depth 4 | Set-Content -LiteralPath $ResultFile -Encoding UTF8
exit 0
