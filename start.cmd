@echo off
setlocal
set "DONGJIAN_LAUNCHER_ROOT=%~dp0"
set "DONGJIAN_BROWSER_ARG="
if /I "%DONGJIAN_NO_BROWSER%"=="1" set "DONGJIAN_BROWSER_ARG=--no-browser"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "& { . '%DONGJIAN_LAUNCHER_ROOT%scripts\env.ps1'; & $env:DONGJIAN_PYTHON -m dongjian.api.lifecycle start --project-root $env:DONGJIAN_PROJECT_ROOT %DONGJIAN_BROWSER_ARG%; exit $LASTEXITCODE }"
exit /b %ERRORLEVEL%
