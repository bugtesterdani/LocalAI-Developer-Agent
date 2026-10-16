#Requires -Version 5.1
# Prüft die Sandbox-Skripte ohne Hyper-V: Syntax (auch unter Windows PowerShell 5.1), Kodierung, Antwortdatei.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$repo = Split-Path -Parent (Split-Path -Parent $root)
$failures = New-Object System.Collections.Generic.List[string]

# 1 – Syntax aller Skripte
$files = @(Get-ChildItem -LiteralPath (Join-Path $repo 'scripts') -Recurse -Include '*.ps1', '*.psm1' -File)
foreach ($file in $files) {
    $tokens = $null; $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors) | Out-Null
    foreach ($e in @($errors)) { $failures.Add("$($file.Name):$($e.Extent.StartLineNumber): $($e.Message)") }
    # Windows PowerShell 5.1 liest Dateien ohne BOM als ANSI: Umlaute brauchen UTF-8 mit BOM.
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    $hasBom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    if (-not $hasBom -and @($bytes | Where-Object { $_ -ge 0x80 }).Count -gt 0) { $failures.Add("$($file.Name): Nicht-ASCII ohne UTF-8-BOM") }
}

# 2 – Antwortdatei
Import-Module (Join-Path $root 'LocalAiSandbox.psm1') -Force
$xmlText = New-LocalAiUnattendXml -UiLanguage 'de-DE' -Password 'a<b>&"c''d' -ProductKey 'AAAAA-BBBBB-CCCCC-DDDDD-EEEEE'
$xml = [xml]$xmlText
$ns = New-Object System.Xml.XmlNamespaceManager($xml.NameTable)
$ns.AddNamespace('u', 'urn:schemas-microsoft-com:unattend')
function Expect([bool]$condition, [string]$message) { if (-not $condition) { $failures.Add($message) } }
Expect ($xml.SelectSingleNode('//u:UILanguage', $ns).InnerText -eq 'de-DE') 'UILanguage'
Expect ($xml.SelectSingleNode('//u:LocalAccount/u:Group', $ns).InnerText -eq 'Administratoren') 'Gruppe de-DE'
Expect ($xml.SelectSingleNode('//u:LocalAccount/u:Password/u:Value', $ns).InnerText -eq 'a<b>&"c''d') 'Kennwort escaped'
Expect ($xml.SelectSingleNode('//u:ProductKey', $ns).InnerText -eq 'AAAAA-BBBBB-CCCCC-DDDDD-EEEEE') 'ProductKey'
Expect ($xml.SelectSingleNode('//u:HideOnlineAccountScreens', $ns).InnerText -eq 'true') 'OOBE ohne Onlinekonto'
Expect ($xml.SelectSingleNode('//u:ProtectYourPC', $ns).InnerText -eq '3') 'Datenschutzoptionen aus'
$en = [xml](New-LocalAiUnattendXml -UiLanguage 'en-US' -Password 'x')
Expect ($null -eq $en.SelectSingleNode('//u:ProductKey', $ns)) 'kein ProductKey ohne Angabe'
Expect ($en.SelectSingleNode('//u:LocalAccount/u:Group', $ns).InnerText -eq 'Administrators') 'Gruppe en-US'
try { New-LocalAiUnattendXml -UiLanguage 'de"><x' -Password 'x' | Out-Null; $failures.Add('ungültige Sprache angenommen') } catch { }

# 3 – Hilfsfunktionen
Expect ((Get-LocalAiImageLanguage -ImageInfo ([pscustomobject]@{ Languages = @('en-US', 'de-DE (Default)'); DefaultLanguageIndex = 1 })) -eq 'de-DE') 'Sprache aus Abbild'
Expect ((Get-LocalAiImageLanguage -ImageInfo ([pscustomobject]@{ Languages = @() })) -eq 'en-US') 'Sprache Standard'
try { Assert-LocalAiVmName -VmName "x'; Remove-VM *"; $failures.Add('VM-Name nicht geprüft') } catch { }
$pw = New-LocalAiPassword
Expect ($pw.Length -ge 30 -and $pw -match '[0-9]' -and $pw -match '[A-Za-z]' -and $pw -match '!') 'Kennwort-Komplexität'

if ($failures.Count -gt 0) {
    $failures | ForEach-Object { Write-Output "FEHLER $_" }
    exit 1
}
Write-Output "OK $($files.Count) Skripte geprüft"
exit 0
