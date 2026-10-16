#Requires -Version 5.1
# Löscht die Sandbox-VM samt ihren Festplatten (Sitzungsende). Entfernt nur, was LocalAi selbst angelegt hat.
param(
    [Parameter(Mandatory)][string]$VmName,
    [Parameter(Mandatory)][string]$VmDirectory
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LocalAiSandbox.psm1') -Force
Initialize-LocalAiConsole
try {
    Assert-LocalAiVmName -VmName $VmName
    $vm = Get-VM -Name $VmName -ErrorAction SilentlyContinue
    if ($vm) {
        if (-not (Test-LocalAiOwnedVm -Vm $vm)) { throw "VM '$VmName' stammt nicht von LocalAi – wird nicht gelöscht." }
        Stop-VM -VM $vm -TurnOff -Force -ErrorAction SilentlyContinue
        Remove-VM -VM $vm -Force
    }
    $removed = 0
    if (Test-Path -LiteralPath (Join-Path $VmDirectory $DirectoryMarker)) {
        Get-ChildItem -LiteralPath $VmDirectory -Filter '*.vhdx' -File | Where-Object {
            $_.Name -eq "$VmName-os.vhdx" -or $_.Name -like 'transfer-*.vhdx'
        } | ForEach-Object {
            $file = $_.FullName
            Invoke-LocalAiRetry { Remove-Item -LiteralPath $file -Force } | Out-Null
            $removed++
        }
        $config = Join-Path $VmDirectory $VmName
        if (Test-Path -LiteralPath $config) { Remove-Item -LiteralPath $config -Recurse -Force }
    }
    Write-LocalAiResult @{ removed = $removed }
} catch {
    Write-LocalAiLine ("ERROR " + ($_.Exception.Message -replace '[\r\n]+', ' '))
    exit 1
}
