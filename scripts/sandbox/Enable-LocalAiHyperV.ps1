#Requires -Version 5.1
<#
.SYNOPSIS
    Einmalige Einrichtung für die Offline-Sandbox (als Administrator ausführen).

.DESCRIPTION
    1. Aktiviert Hyper-V samt Verwaltungstools, falls noch nicht vorhanden (danach Neustart nötig).
    2. Nimmt den angemeldeten Benutzer in die Gruppe »Hyper-V-Administratoren« auf. Damit kann LocalAi
       die VM im laufenden Betrieb ohne Administratorrechte steuern (Copy-VMFile, Laufwerkswechsel, Status).
       Nur der Neuaufbau zu Sitzungsbeginn fragt per UAC nach Administratorrechten.
    Danach abmelden und wieder anmelden, damit die Gruppenmitgliedschaft gilt.
#>
$ErrorActionPreference = 'Stop'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Bitte in einer PowerShell »Als Administrator ausführen« starten.'
}

$feature = Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All
if ($feature.State -ne 'Enabled') {
    Write-Host 'Hyper-V wird aktiviert …'
    Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -All -NoRestart | Out-Null
    Write-Host 'Hyper-V aktiviert – bitte den PC neu starten und dieses Skript danach erneut ausführen.'
    exit 0
}
Write-Host 'Hyper-V ist aktiviert.'

$group = Get-LocalGroup -SID 'S-1-5-32-578'
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$members = @(Get-LocalGroupMember -Group $group | ForEach-Object { $_.Name })
if ($members -contains $user) {
    Write-Host "$user ist bereits Mitglied von »$($group.Name)«."
} else {
    Add-LocalGroupMember -Group $group -Member $user
    Write-Host "$user wurde in »$($group.Name)« aufgenommen. Bitte abmelden und wieder anmelden."
}
