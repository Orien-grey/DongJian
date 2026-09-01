@echo off
setlocal
set "CHONGZU_LAUNCHER_ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CHONGZU_LAUNCHER_ROOT%scripts\doctor.ps1" %*
exit /b %ERRORLEVEL%
