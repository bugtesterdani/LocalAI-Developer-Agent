@echo off
rem Laeuft einmal als SYSTEM, nachdem Windows in der Sandbox-VM eingerichtet ist.
if not exist C:\LocalAi\logs mkdir C:\LocalAi\logs
rem Fortschritt fuer den Host (KVP): die Einrichtung von Windows ist durch, die Provisionierung beginnt.
reg.exe add "HKLM\SOFTWARE\Microsoft\Virtual Machine\Guest" /v LocalAiProgress /t REG_SZ /d setupcomplete /f > C:\LocalAi\logs\setupcomplete-reg.log 2>&1
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\LocalAi\provision.ps1 > C:\LocalAi\logs\setupcomplete.log 2>&1
