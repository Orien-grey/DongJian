param()

$ErrorActionPreference = 'Stop'
. (Join-Path -Path $PSScriptRoot -ChildPath 'env.ps1')

$pythonExe = $env:CHONGZU_PYTHON
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    Write-Error "Project standalone Python was not found: $pythonExe"
    exit 2
}

Push-Location -LiteralPath $env:CHONGZU_ROOT
try {
    # $args contains every argument passed after this script path.  Splatting
    # preserves spaces, Unicode, and the caller's argument boundaries on
    # Windows PowerShell 5.1.
    & $pythonExe -m chongzu @args
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $exitCode
