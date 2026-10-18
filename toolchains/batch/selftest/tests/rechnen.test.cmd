@echo off
set /a ERGEBNIS=1+2
if not "%ERGEBNIS%"=="3" exit /b 1
exit /b 0
