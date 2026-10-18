@echo off
rem Rechner fuer die Kommandozeile: rechne.cmd add|mul A B
setlocal
if "%~3"=="" goto :aufruf
if /i "%~1"=="add" goto :add
if /i "%~1"=="mul" goto :mul
goto :aufruf

:add
set /a ERGEBNIS=%~2 + %~3
echo %ERGEBNIS%
exit /b 0

:mul
set /a ERGEBNIS=%~2 * %~3
echo %ERGEBNIS%
exit /b 0

:aufruf
echo Aufruf: rechne.cmd add^|mul A B
exit /b 2
