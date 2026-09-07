@echo off
setlocal
set "DONGJIAN_LAUNCHER_ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%DONGJIAN_LAUNCHER_ROOT%scripts\doctor.ps1" %*
exit /b %ERRORLEVEL%
