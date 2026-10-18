# Gemeinsame Hilfen der Runner: Module aus dem Toolchain-Ordner (..\Modules) und der Offline-Paketquelle laden.
# Läuft unter Windows PowerShell 5.1 und PowerShell 7.

function Import-LocalAiModule([string]$Name) {
    $roots = @(
        (Join-Path $PSScriptRoot '..\Modules'),
        $env:LOCALAI_PACKAGES
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) }
    foreach ($root in $roots) {
        $manifest = Get-ChildItem -LiteralPath (Join-Path $root $Name) -Recurse -Filter "$Name.psd1" -ErrorAction SilentlyContinue |
            Sort-Object { try { [version]$_.Directory.Name } catch { [version]'0.0' } } -Descending |
            Select-Object -First 1
        if ($manifest) {
            Import-Module -Name $manifest.FullName -Force -ErrorAction Stop
            return (Get-Module -Name $Name | Select-Object -First 1)
        }
    }
    return $null
}

function Add-LocalAiPackagePath {
    # Projektmodule aus der Offline-Paketquelle ({packages}) für Import-Module verfügbar machen.
    if ($env:LOCALAI_PACKAGES -and (Test-Path -LiteralPath $env:LOCALAI_PACKAGES -PathType Container)) {
        $env:PSModulePath = $env:LOCALAI_PACKAGES + [System.IO.Path]::PathSeparator + $env:PSModulePath
    }
}
