# Gemeinsame Funktionen der Sandbox-Skripte (Windows PowerShell 5.1 und PowerShell 7).
Set-StrictMode -Version 3.0

# Integrationsdienste über ihre festen IDs (die Namen sind je nach Windows-Sprache übersetzt).
$script:IntegrationIds = @{
    GuestServiceInterface = '6C09BB55-D683-4DA0-8931-C9BF705F6480'
    KeyValuePairExchange  = '2A34B1C2-FD73-4043-8A5B-DD2159BC743F'
    Heartbeat             = '84EAAE65-2F2E-45F5-9BB5-0E857DC8EB47'
    Shutdown              = '9F8233AC-BE49-4C79-8EE3-E7E1985B2077'
    TimeSynchronization   = '2497F4DE-E9FA-4204-80E4-4B75C46419C0'
    Vss                   = '5CED1297-4598-4915-A5FC-AD21BB4D02A4'
}

$script:VmNotes = 'LocalAi-Sandbox: automatisch erzeugt, wird bei jedem Sitzungsstart neu aufgebaut und danach geloescht.'
$script:DirectoryMarker = '.localai-vm'

function Initialize-LocalAiConsole {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
}

function Write-LocalAiResult {
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Result)
    [Console]::Out.WriteLine(($Result | ConvertTo-Json -Compress -Depth 6))
}

function Write-LocalAiLine {
    param([Parameter(Mandatory)][string]$Line)
    [Console]::Out.WriteLine($Line)
    [Console]::Out.Flush()
}

function Add-LocalAiStatus {
    param([Parameter(Mandatory)][string]$StatusFile, [Parameter(Mandatory)][string]$Line)
    # Ohne BOM anhängen: der Broker liest die Datei fortlaufend.
    [System.IO.File]::AppendAllText($StatusFile, $Line + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
}

function Assert-LocalAiVmName {
    param([Parameter(Mandatory)][string]$VmName)
    if ($VmName -notmatch '^[A-Za-z0-9-]{1,40}$') {
        throw "Ungültiger VM-Name: $VmName"
    }
}

function Get-LocalAiNicCount {
    param([Parameter(Mandatory)][string]$VmName)
    return @(Get-VMNetworkAdapter -VMName $VmName -ErrorAction Stop).Count
}

function Test-LocalAiOwnedVm {
    param([Parameter(Mandatory)]$Vm)
    return ([string]$Vm.Notes).StartsWith('LocalAi-Sandbox:')
}

function Get-LocalAiIntegrationService {
    param([Parameter(Mandatory)]$Vm, [Parameter(Mandatory)][string]$Name)
    $id = $script:IntegrationIds[$Name]
    return Get-VMIntegrationService -VM $Vm | Where-Object { ([string]$_.Id).ToUpperInvariant().EndsWith($id) }
}

function Set-LocalAiIntegrationServices {
    param([Parameter(Mandatory)]$Vm)
    foreach ($name in 'GuestServiceInterface', 'KeyValuePairExchange', 'Heartbeat', 'Shutdown', 'TimeSynchronization') {
        $service = Get-LocalAiIntegrationService -Vm $Vm -Name $name
        if (-not $service) { throw "Integrationsdienst $name nicht gefunden" }
        Enable-VMIntegrationService -VMIntegrationService $service
    }
    $vss = Get-LocalAiIntegrationService -Vm $Vm -Name 'Vss'
    if ($vss) { Disable-VMIntegrationService -VMIntegrationService $vss }
}

function Get-LocalAiAdminGroup {
    param([Parameter(Mandatory)][string]$UiLanguage)
    switch -Regex ($UiLanguage) {
        '^de' { return 'Administratoren' }
        '^fr' { return 'Administrateurs' }
        '^(es|pt)' { return 'Administradores' }
        default { return 'Administrators' }
    }
}

function New-LocalAiPassword {
    $bytes = New-Object byte[] 24
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return 'Lai-' + [Convert]::ToBase64String($bytes).Replace('+', 'x').Replace('/', 'y').Replace('=', '') + '9!'
}

function New-LocalAiUnattendXml {
    param(
        [Parameter(Mandatory)][string]$UiLanguage,
        [Parameter(Mandatory)][string]$Password,
        [string]$ProductKey,
        [string]$AdminGroup,
        [string]$ComputerName = 'LOCALAI-SBX'
    )
    if ($UiLanguage -notmatch '^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$') { throw "Ungültige Sprache: $UiLanguage" }
    if (-not $AdminGroup) { $AdminGroup = Get-LocalAiAdminGroup -UiLanguage $UiLanguage }
    $e = { param($s) [System.Security.SecurityElement]::Escape($s) }
    $common = 'processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"'
    $key = ''
    if ($ProductKey) { $key = "      <ProductKey>$(& $e $ProductKey)</ProductKey>`r`n" }
    return @"
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" $common>
      <ComputerName>$(& $e $ComputerName)</ComputerName>
$key      <TimeZone>W. Europe Standard Time</TimeZone>
    </component>
    <component name="Microsoft-Windows-Deployment" $common>
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Path>reg.exe add HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE /v BypassNRO /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" $common>
      <InputLocale>de-DE</InputLocale>
      <SystemLocale>de-DE</SystemLocale>
      <UILanguage>$(& $e $UiLanguage)</UILanguage>
      <UserLocale>de-DE</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" $common>
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <ProtectYourPC>3</ProtectYourPC>
        <SkipMachineOOBE>true</SkipMachineOOBE>
        <SkipUserOOBE>true</SkipUserOOBE>
      </OOBE>
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>localai</Name>
            <DisplayName>LocalAi</DisplayName>
            <Group>$(& $e $AdminGroup)</Group>
            <Password>
              <Value>$(& $e $Password)</Value>
              <PlainText>true</PlainText>
            </Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
      <TimeZone>W. Europe Standard Time</TimeZone>
    </component>
  </settings>
</unattend>
"@
}

function Get-LocalAiImageLanguage {
    param([Parameter(Mandatory)]$ImageInfo)
    $languages = @($ImageInfo.Languages)
    if ($languages.Count -eq 0) { return 'en-US' }
    $index = 0
    if ($ImageInfo.PSObject.Properties['DefaultLanguageIndex'] -and $ImageInfo.DefaultLanguageIndex -lt $languages.Count) {
        $index = [int]$ImageInfo.DefaultLanguageIndex
    }
    return ([string]$languages[$index]).Split(' ')[0]
}

function Invoke-LocalAiRetry {
    param([Parameter(Mandatory)][scriptblock]$Action, [int]$Attempts = 20, [int]$DelayMs = 500)
    for ($i = 1; ; $i++) {
        try { return & $Action }
        catch {
            if ($i -ge $Attempts) { throw }
            Start-Sleep -Milliseconds $DelayMs
        }
    }
}

Export-ModuleMember -Function * -Variable IntegrationIds, VmNotes, DirectoryMarker
