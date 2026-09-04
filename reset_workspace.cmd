@echo off
setlocal
set "CHONGZU_RESET_ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CHONGZU_RESET_ROOT%scripts\reset_workspace.ps1" %*
exit /b %ERRORLEVEL%
