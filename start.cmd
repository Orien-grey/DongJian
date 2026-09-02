@echo off
setlocal
set "CHONGZU_LAUNCHER_ROOT=%~dp0"
set "CHONGZU_BROWSER_ARG="
if /I "%CHONGZU_NO_BROWSER%"=="1" set "CHONGZU_BROWSER_ARG=--no-browser"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "& { . '%CHONGZU_LAUNCHER_ROOT%scripts\env.ps1'; & $env:CHONGZU_PYTHON -m chongzu.api.lifecycle start --project-root $env:CHONGZU_PROJECT_ROOT %CHONGZU_BROWSER_ARG%; exit $LASTEXITCODE }"
exit /b %ERRORLEVEL%
