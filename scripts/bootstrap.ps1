[CmdletBinding()]
param(
    [switch]$SkipPythonInstall
)

$ErrorActionPreference = 'Stop'
$scriptRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$projectRoot = (Resolve-Path -LiteralPath (Join-Path -Path $scriptRoot -ChildPath '..')).Path

. (Join-Path -Path $scriptRoot -ChildPath 'env.ps1')

if (-not (Test-Path -LiteralPath $env:CHONGZU_PROJECT_UV -PathType Leaf)) {
    throw "Project-local uv is missing: $env:CHONGZU_PROJECT_UV. Refusing to fall back to PATH uv."
}

$architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
if ($architecture -ne 'X64') {
    throw "Unsupported process architecture: $architecture; ChongZu requires Windows x64."
}

$pythonRuntime = $env:CHONGZU_RUNTIME_PYTHON
if (-not $SkipPythonInstall) {
    & $env:CHONGZU_PROJECT_UV python install 3.11.15 `
        --install-dir (Join-Path -Path $env:CHONGZU_RUNTIME_ROOT -ChildPath 'python') `
        --no-bin --no-registry
    if ($LASTEXITCODE -ne 0) {
        throw "Project-local CPython installation failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $pythonRuntime -PathType Leaf)) {
    throw "Pinned project CPython was not found: $pythonRuntime"
}

$venvRoot = Join-Path -Path $env:CHONGZU_RUNTIME_ROOT -ChildPath 'venv'
$venvPython = Join-Path -Path (Join-Path -Path $venvRoot -ChildPath 'Scripts') -ChildPath 'python.exe'
if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    Write-Output "Reusing existing project virtual environment: $venvPython"
} else {
    & $env:CHONGZU_PROJECT_UV venv $venvRoot --python $pythonRuntime --no-project
    if ($LASTEXITCODE -ne 0) {
        throw "Project virtual environment creation failed with exit code $LASTEXITCODE"
    }
}

& $env:CHONGZU_PROJECT_UV sync --locked --directory $env:CHONGZU_PROJECT_ROOT
if ($LASTEXITCODE -ne 0) {
    throw "Locked dependency synchronization failed with exit code $LASTEXITCODE"
}

$packagesRoot = $env:CHONGZU_PACKAGES
New-Item -ItemType Directory -Path $packagesRoot -Force | Out-Null
& $env:CHONGZU_PROJECT_UV pip install --target $packagesRoot --python $pythonRuntime --no-deps --only-binary=:all: --exact duckdb==1.5.5
if ($LASTEXITCODE -ne 0) {
    throw "Portable runtime package installation failed with exit code $LASTEXITCODE"
}

Write-Output "Bootstrap complete. Development venv Python: $env:CHONGZU_DEV_PYTHON"
Write-Output "Production standalone Python: $env:CHONGZU_PYTHON"
Write-Output "Production packages: $env:CHONGZU_PACKAGES (duckdb==1.5.5)"
Write-Output "No Java, Tika, Docling, Torch, OCR, or model artifacts are installed by this script."
