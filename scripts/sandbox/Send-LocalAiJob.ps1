#Requires -Version 5.1
# Host -> VM: kopiert die Dateien eines signierten Jobs mit Copy-VMFile (Gastdienstschnittstelle) in den Eingang
# des Gasts – in der übergebenen Reihenfolge (Nutzlast, Job, zuletzt .ready). Ausgabe: JSON {nics, copied}.
param(
    [Parameter(Mandatory)][string]$VmName,
    [Parameter(Mandatory)][string]$GuestDirectory,
    [Parameter(Mandatory)][string]$FileList
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Import-Module (Join-Path $PSScriptRoot 'LocalAiSandbox.psm1') -Force
Initialize-LocalAiConsole
try {
    Assert-LocalAiVmName -VmName $VmName
    # Windows-Gast: C:\LocalAi\inbox; Linux-Gast (M8): /var/lib/localai/inbox (hv_fcopy im Gast).
    $linux = $GuestDirectory -match '^/[a-z0-9/_-]+$' -and $GuestDirectory -notmatch '//|/\.'
    if (-not $linux -and $GuestDirectory -notmatch '^[A-Za-z]:\\[A-Za-z0-9\\]+$') { throw "Ungültiger Gastordner: $GuestDirectory" }
    $separator = if ($linux) { '/' } else { '\' }
    $nics = Get-LocalAiNicCount -VmName $VmName
    if ($nics -ne 0) {
        Write-LocalAiResult @{ nics = $nics; copied = 0 }
        exit 0
    }
    $copied = 0
    foreach ($file in $FileList.Split('|')) {
        $name = Split-Path -Leaf $file
        if ($name -notmatch '^[0-9]{12}-[0-9a-f-]{36}\.(payload\.zip|job\.json|ready)$') { throw "Unerwarteter Dateiname: $name" }
        Copy-VMFile -Name $VmName -SourcePath $file -DestinationPath ($GuestDirectory + $separator + $name) -FileSource Host -CreateFullPath -Force
        $copied++
    }
    Write-LocalAiResult @{ nics = $nics; copied = $copied }
} catch {
    Write-LocalAiLine ("ERROR " + ($_.Exception.Message -replace '[\r\n]+', ' '))
    exit 1
}
