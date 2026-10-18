Set-StrictMode -Version Latest

function Get-Summe {
    <#
    .SYNOPSIS
    Summe von A und B.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][int]$A,
        [Parameter(Mandatory = $true)][int]$B
    )
    return $A + $B
}

function Get-Produkt {
    <#
    .SYNOPSIS
    Produkt von A und B.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][int]$A,
        [Parameter(Mandatory = $true)][int]$B
    )
    return $A * $B
}

Export-ModuleMember -Function Get-Summe, Get-Produkt
