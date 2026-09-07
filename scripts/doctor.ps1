[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$scriptRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$projectRoot = (Resolve-Path -LiteralPath (Join-Path -Path $scriptRoot -ChildPath '..')).Path

. (Join-Path -Path $scriptRoot -ChildPath 'env.ps1')

$pythonExe = $env:DONGJIAN_PYTHON
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    Write-Error "Project standalone Python was not found: $pythonExe"
    exit 2
}

Push-Location -LiteralPath $projectRoot
try {
    & $pythonExe -m dongjian doctor
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $exitCode
