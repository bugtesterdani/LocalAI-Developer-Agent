BeforeAll {
    Import-Module (Join-Path $PSScriptRoot '..\src\Rechner.psm1') -Force
}

Describe 'Get-Summe' {
    It 'addiert zwei Zahlen' {
        Get-Summe -A 2 -B 3 | Should -Be 5
    }
    It 'kann negativ' {
        Get-Summe -A -2 -B 2 | Should -Be 0
    }
}

Describe 'Get-Produkt' {
    It 'multipliziert' {
        Get-Produkt -A 4 -B 5 | Should -Be 20
    }
}
