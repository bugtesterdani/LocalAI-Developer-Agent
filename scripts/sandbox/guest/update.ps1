#Requires -Version 5.1
<#
    Gast-Update (läuft als SYSTEM, vom Worker gestartet): idempotente Einrichtungsschritte nach dem Austausch von
    Worker-Code und Gastskripten in einer laufenden VM. Toolchains, Python-Laufzeit und Windows bleiben unverändert –
    ändert sich davon etwas, baut der Host die VM neu auf.
#>
$ErrorActionPreference = 'Stop'
$root = 'C:\LocalAi'
Import-Module (Join-Path $root 'LocalAiGuest.psm1') -Force
Set-LocalAiGuestRoot -Path $root
$config = Get-Content -LiteralPath (Join-Path $root 'guest-config.json') -Raw | ConvertFrom-Json
$problem = Set-SystemTempIntegrity -Config $config
if ($problem) { Write-Output $problem; exit 1 }
Set-WorkerCommand -Config $config
Write-Output 'Gast-Update eingerichtet'
exit 0
