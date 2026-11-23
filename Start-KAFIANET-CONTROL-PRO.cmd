@echo off
setlocal
rem Launch through the windowless WScript host. This wrapper exits immediately.
set "VBS_LAUNCHER=%~dp0Launch-KAFIA-NET-CONTROL-PRO.vbs"
if not exist "%VBS_LAUNCHER%" (
    echo [KAFIA NET CONTROL PRO] The hidden launcher is missing.
    pause
    exit /b 2
)
start "" "%SystemRoot%\System32\wscript.exe" "%VBS_LAUNCHER%"
exit /b 0
