#Requires -Version 5.1
<#
.SYNOPSIS
  Sperrt Edge0 den Weg ins Internet: ausgehende Verbindungen aller Programme im Edge0-Ordner werden blockiert,
  eingehend ist nur der Server-Port von den angegebenen Rechnern erlaubt (docs/edge0.md, Abschnitt 4.2).

.DESCRIPTION
  Legt Regeln der Windows-Firewall in der Gruppe "LocalAI Edge0" an:
   - je Programm (*.exe) unter -Edge0Dir eine Ausgangsregel "Blockieren" (alle Profile, alle Ziele). Sie gilt für
     Verbindungen, die das Programm selbst aufbaut; Antworten auf erlaubte eingehende Verbindungen (Orchestrator ->
     Edge0) sind nicht betroffen. Loopback (127.0.0.1) filtert Windows nicht.
   - Benutzt eine .venv im Ordner einen Python-Interpreter außerhalb des Ordners, wird auch dieser gesperrt (mit
     Warnung: die Sperre gilt dann für alle Programme, die diesen Interpreter nutzen).
   - mit -AllowFrom eine Eingangsregel "Zulassen" für edge0-infer.exe: nur TCP -Port, nur von diesen Adressen.
  Erneut ausführen ersetzt alle Regeln der Gruppe (z. B. nach einem Update von Edge0).

.PARAMETER Edge0Dir
  Ordner mit edge0-infer.exe (und ggf. edge0-strata, python). Mehrere Ordner möglich.
.PARAMETER AllowFrom
  IP-Adressen (oder Bereiche wie 192.168.178.0/24) der Rechner mit dem Orchestrator – nur bei Betrieb im LAN.
.PARAMETER Port
  Port von edge0-infer serve (Standard 8000).
.PARAMETER Check
  Nur prüfen: Regeln anzeigen, Programme ohne Ausgangssperre und fremde Eingangsregeln für edge0-infer.exe melden.
.PARAMETER Remove
  Alle Regeln der Gruppe entfernen.

.EXAMPLE
  .\scripts\edge0\firewall.ps1 -Edge0Dir D:\Edge0
.EXAMPLE
  .\scripts\edge0\firewall.ps1 -Edge0Dir D:\Edge0 -AllowFrom 192.168.178.50 -Port 8000
.EXAMPLE
  .\scripts\edge0\firewall.ps1 -Edge0Dir D:\Edge0 -Check
#>
[CmdletBinding(DefaultParameterSetName = 'Apply')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Apply')]
    [Parameter(Mandatory = $true, ParameterSetName = 'Check')]
    [string[]] $Edge0Dir,
    [Parameter(ParameterSetName = 'Apply')]
    [string[]] $AllowFrom = @(),
    [Parameter(ParameterSetName = 'Apply')]
    [ValidateRange(1, 65535)]
    [int] $Port = 8000,
    [Parameter(Mandatory = $true, ParameterSetName = 'Check')]
    [switch] $Check,
    [Parameter(Mandatory = $true, ParameterSetName = 'Remove')]
    [switch] $Remove
)
$ErrorActionPreference = 'Stop'
$Group = 'LocalAI Edge0'

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-Dirs([string[]] $dirs) {
    foreach ($dir in $dirs) {
        if (-not (Test-Path -LiteralPath $dir -PathType Container)) { throw "Ordner nicht gefunden: $dir" }
        (Resolve-Path -LiteralPath $dir).ProviderPath.TrimEnd('\')
    }
}

function Test-Inside([string] $path, [string[]] $roots) {
    foreach ($root in $roots) {
        if ($path.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

# Alle Programme, die gesperrt werden müssen: *.exe in den Ordnern plus Basis-Interpreter von .venv-Umgebungen.
function Get-Edge0Programs([string[]] $roots) {
    $programs = New-Object System.Collections.Generic.List[string]
    foreach ($root in $roots) {
        Get-ChildItem -LiteralPath $root -Recurse -File -Filter '*.exe' -ErrorAction SilentlyContinue |
            ForEach-Object { $programs.Add($_.FullName) }
        foreach ($cfg in Get-ChildItem -LiteralPath $root -Recurse -File -Filter 'pyvenv.cfg' -ErrorAction SilentlyContinue) {
            $line = Select-String -LiteralPath $cfg.FullName -Pattern '^\s*home\s*=\s*(.+?)\s*$' | Select-Object -First 1
            if (-not $line) { continue }
            $baseDir = $line.Matches[0].Groups[1].Value
            foreach ($name in 'python.exe', 'pythonw.exe') {
                $base = Join-Path $baseDir $name
                if (-not (Test-Path -LiteralPath $base)) { continue }
                $base = (Resolve-Path -LiteralPath $base).ProviderPath
                if (-not (Test-Inside $base $roots)) {
                    Write-Warning ((("{0} nutzt den Interpreter {1} außerhalb des Edge0-Ordners. Er wird mit gesperrt – " +
                        "damit hat JEDES Programm, das ihn nutzt, keinen Netzzugang mehr. Besser einen eigenen " +
                        "Interpreter im Edge0-Ordner verwenden (docs/edge0.md, Abschnitt 3.5).") -f $cfg.DirectoryName, $base))
                }
                $programs.Add($base)
            }
        }
    }
    $programs | Sort-Object -Unique
}

function Get-GroupRules {
    @(Get-NetFirewallRule -Group $Group -ErrorAction SilentlyContinue)
}

function Get-RuleProgram($rule) {
    ($rule | Get-NetFirewallApplicationFilter).Program
}

# Eingangsregeln anderer Herkunft für edge0-infer.exe (z. B. aus der Abfrage „Zugriff zulassen?“ beim ersten Start):
# Zulassen öffnet evtl. mehr als nötig, Blockieren sperrt den Orchestrator aus. Pfadvergleich ohne Groß-/Kleinschreibung.
function Get-ForeignRules([string[]] $programs) {
    $servers = @($programs | Where-Object { (Split-Path -Leaf $_) -ieq 'edge0-infer.exe' })
    if ($servers.Count -eq 0) { return }
    Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue |
        Where-Object { $p = $_.Program; @($servers | Where-Object { $_ -ieq $p }).Count -gt 0 } |
        Get-NetFirewallRule |
        Where-Object { $_.Group -ne $Group -and "$($_.Direction)" -eq 'Inbound' -and "$($_.Enabled)" -eq 'True' }
}

if ($Remove) {
    if (-not (Test-Admin)) { throw 'Bitte die PowerShell „Als Administrator ausführen“.' }
    $rules = @(Get-GroupRules)
    if ($rules.Count -gt 0) { $rules | Remove-NetFirewallRule }
    Write-Host "Entfernt: $($rules.Count) Regel(n) der Gruppe '$Group'."
    exit 0
}

$roots = @(Resolve-Dirs $Edge0Dir)
$programs = @(Get-Edge0Programs $roots)
if ($programs.Count -eq 0) { throw "Keine Programme (*.exe) unter $($roots -join ', ') gefunden." }

$off = @(Get-NetFirewallProfile | Where-Object { "$($_.Enabled)" -ne 'True' } | ForEach-Object { $_.Name })
if ($off.Count -gt 0) {
    Write-Warning ("Die Windows-Firewall ist für folgende Profile ausgeschaltet: {0}. Dort wirken die Regeln nicht." -f ($off -join ', '))
}

if ($Check) {
    $rules = @(Get-GroupRules)
    $blocked = @()
    foreach ($rule in $rules) {
        $program = Get-RuleProgram $rule
        if ("$($rule.Direction)" -eq 'Outbound' -and "$($rule.Action)" -eq 'Block' -and "$($rule.Enabled)" -eq 'True') {
            $blocked += "$program".ToLowerInvariant()
        }
        Write-Host ("{0,-8} {1,-6} {2,-5} {3}" -f $rule.Direction, $rule.Action, $rule.Enabled, $program)
    }
    $missing = @($programs | Where-Object { $blocked -notcontains $_.ToLowerInvariant() })
    $foreign = @(Get-ForeignRules $programs)
    foreach ($rule in $foreign) {
        Write-Warning ((("Fremde Eingangsregel für edge0-infer.exe: '{0}' ({1}). Prüfen und ggf. entfernen: " +
            "Remove-NetFirewallRule -Name '{2}'") -f $rule.DisplayName, $rule.Action, $rule.Name))
    }
    if ($missing.Count -gt 0) {
        Write-Warning ("Ohne Ausgangssperre:`n  " + ($missing -join "`n  ") + "`nfirewall.ps1 ohne -Check erneut ausführen.")
        exit 1
    }
    Write-Host "OK: alle $($programs.Count) Programm(e) ausgehend gesperrt."
    exit 0
}

if (-not (Test-Admin)) { throw 'Bitte die PowerShell „Als Administrator ausführen“.' }
Get-GroupRules | Remove-NetFirewallRule
$n = 0
foreach ($program in $programs) {
    $n++
    New-NetFirewallRule -DisplayName ("LocalAI Edge0 - ausgehend gesperrt - {0} ({1})" -f (Split-Path -Leaf $program), $n) `
        -Group $Group -Direction Outbound -Action Block -Program $program -Profile Any -Enabled True | Out-Null
    Write-Host "ausgehend gesperrt: $program"
}
if ($AllowFrom.Count -gt 0) {
    $server = $programs | Where-Object { (Split-Path -Leaf $_) -ieq 'edge0-infer.exe' } | Select-Object -First 1
    if (-not $server) { throw 'edge0-infer.exe nicht gefunden – keine Eingangsregel angelegt.' }
    New-NetFirewallRule -DisplayName "LocalAI Edge0 - eingehend TCP $Port" -Group $Group -Direction Inbound -Action Allow `
        -Program $server -Protocol TCP -LocalPort $Port -RemoteAddress $AllowFrom -Profile Any -Enabled True | Out-Null
    Write-Host ("eingehend erlaubt: TCP {0} zu {1}, nur von {2}" -f $Port, $server, ($AllowFrom -join ', '))
} else {
    Write-Host 'Keine Eingangsregel (nur Loopback). Für den Orchestrator auf einem anderen Rechner: -AllowFrom <IP>.'
}
foreach ($rule in @(Get-ForeignRules $programs)) {
    Write-Warning ((("Fremde Eingangsregel für edge0-infer.exe: '{0}' ({1}) – Zulassen öffnet evtl. mehr als nötig, " +
        "Blockieren sperrt den Orchestrator aus. Entfernen: Remove-NetFirewallRule -Name '{2}'") -f $rule.DisplayName, $rule.Action, $rule.Name))
}
Write-Host "Fertig: $n Programm(e) ausgehend gesperrt (Gruppe '$Group'). Prüfen: firewall.ps1 -Edge0Dir ... -Check"
