@echo off
setlocal
set "DONGJIAN_RESET_ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%DONGJIAN_RESET_ROOT%scripts\reset_workspace.ps1" %*
exit /b %ERRORLEVEL%
