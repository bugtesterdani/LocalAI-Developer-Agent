@echo off
rem Ohne Argumente: Exitcode 2 erwartet
call scripts\rechne.cmd >nul
if errorlevel 2 exit /b 0
exit /b 1
