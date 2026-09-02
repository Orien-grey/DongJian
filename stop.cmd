@echo off
setlocal
set "CHONGZU_LAUNCHER_ROOT=%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "& { . '%CHONGZU_LAUNCHER_ROOT%scripts\env.ps1'; & $env:CHONGZU_PYTHON -m chongzu.api.lifecycle stop --project-root $env:CHONGZU_PROJECT_ROOT; exit $LASTEXITCODE }"
exit /b %ERRORLEVEL%
