@echo off
rem Laeuft einmal als SYSTEM, nachdem Windows in der Sandbox-VM eingerichtet ist.
if not exist C:\LocalAi\logs mkdir C:\LocalAi\logs
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\LocalAi\provision.ps1 > C:\LocalAi\logs\setupcomplete.log 2>&1
