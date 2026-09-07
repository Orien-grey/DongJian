@echo off
setlocal
set "DONGJIAN_LAUNCHER_ROOT=%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "& { . '%DONGJIAN_LAUNCHER_ROOT%scripts\env.ps1'; & $env:DONGJIAN_PYTHON -m dongjian.api.lifecycle stop --project-root $env:DONGJIAN_PROJECT_ROOT; exit $LASTEXITCODE }"
exit /b %ERRORLEVEL%
