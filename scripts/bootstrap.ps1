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
$runtimeRequirements = @(
    'duckdb==1.5.5'
    'polars==1.44.1'
    'python-calamine==0.8.2'
    'PyMuPDF==1.28.2'
    'img2table==2.0.0'
    'rapidocr==3.9.2'
    'onnxruntime==1.29.0'
    'omegaconf==2.0.6'
)

# uv's Windows target installer may leave a PE trampoline locked while a
# native wheel is being replaced.  Create a project-local staging venv with
# the already-pinned standalone CPython (not a host Python), then let the
# project-local uv install into it and publish only its site-packages payload.
# This avoids both target-directory lock races and uv's optional trampoline
# resource rewrite while retaining uv as the dependency installer.
$stagingRoot = Join-Path -Path $env:CHONGZU_CACHE_TEMP -ChildPath 'runtime-provision-staging'
if (Test-Path -LiteralPath $stagingRoot) {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force -ErrorAction SilentlyContinue
}
& $pythonRuntime -m venv $stagingRoot --without-pip
if ($LASTEXITCODE -ne 0) {
    throw "Portable staging environment creation failed with exit code $LASTEXITCODE"
}
$stagingPython = Join-Path -Path (Join-Path -Path $stagingRoot -ChildPath 'Scripts') -ChildPath 'python.exe'
& $env:CHONGZU_PROJECT_UV pip install `
    --python $stagingPython `
    --only-binary=:all: `
    --exact `
    @runtimeRequirements
if ($LASTEXITCODE -ne 0) {
    throw "Portable runtime wheel provisioning failed with exit code $LASTEXITCODE"
}
# RapidOCR declares the smaller opencv-python wheel while img2table declares
# opencv-contrib-python.  Both publish the same ``cv2`` import; publish the
# contrib payload last so the table candidate retains ximgproc and other
# contrib APIs instead of whichever wheel happened to be installed last.
& $env:CHONGZU_PROJECT_UV pip install `
    --python $stagingPython `
    --only-binary=:all: `
    --no-deps `
    --reinstall-package opencv-contrib-python `
    'opencv-contrib-python==5.0.0.93'
if ($LASTEXITCODE -ne 0) {
    throw "OpenCV contrib overlay provisioning failed with exit code $LASTEXITCODE"
}
$stagingSite = Join-Path -Path (Join-Path -Path $stagingRoot -ChildPath 'Lib') -ChildPath 'site-packages'
$skipPayload = @('_virtualenv.py', '_virtualenv.pth', '__pycache__', 'bin')
# Publish an exact package payload.  Stale native modules from an earlier
# candidate must not survive a refresh and shadow the newly locked wheels.
foreach ($existing in (Get-ChildItem -LiteralPath $packagesRoot -Force)) {
    if ($existing.Name -eq '.gitkeep') { continue }
    Remove-Item -LiteralPath $existing.FullName -Recurse -Force
}
foreach ($item in (Get-ChildItem -LiteralPath $stagingSite -Force)) {
    if ($skipPayload -contains $item.Name) { continue }
    $destination = Join-Path -Path $packagesRoot -ChildPath $item.Name
    if (Test-Path -LiteralPath $destination) {
        Remove-Item -LiteralPath $destination -Recurse -Force
    }
    Copy-Item -LiteralPath $item.FullName -Destination $packagesRoot -Recurse -Force
}

# RapidOCR's wheel also carries a copy of its default model payload.  The
# portable contract keeps one audited copy under runtime\models\ocr instead;
# remove the wheel-bundled duplicate so the delivery bundle does not contain
# two independent model roots.  The adapter always receives the explicit
# runtime model paths below.
$bundledRapidOcrModels = Join-Path -Path (Join-Path -Path $packagesRoot -ChildPath 'rapidocr') -ChildPath 'models'
if (Test-Path -LiteralPath $bundledRapidOcrModels) {
    Remove-Item -LiteralPath $bundledRapidOcrModels -Recurse -Force
}

$ocrModelRoot = Join-Path -Path $env:CHONGZU_RUNTIME_ROOT -ChildPath 'models\ocr'
New-Item -ItemType Directory -Path $ocrModelRoot -Force | Out-Null
$stagingModelRoot = Join-Path -Path $stagingSite -ChildPath 'rapidocr\models'
foreach ($model in (Get-ChildItem -LiteralPath $stagingModelRoot -Filter '*.onnx' -File)) {
    Copy-Item -LiteralPath $model.FullName -Destination (Join-Path $ocrModelRoot $model.Name) -Force
}
$modelEntries = @(
    Get-ChildItem -LiteralPath $ocrModelRoot -Filter '*.onnx' -File | Sort-Object Name | ForEach-Object {
        [ordered]@{
            name = $_.Name
            size_bytes = [int64]$_.Length
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
)
[ordered]@{
    contract_version = 'phase5a-ocr-models-v1'
    extractor = 'rapidocr'
    extractor_version = '3.9.2'
    files = $modelEntries
    runtime_download_disabled = $true
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $ocrModelRoot 'manifest.json') -Encoding utf8

# The staging environment is only a provisioning workspace.  Removing it
# keeps the portable payload unambiguous and avoids leaving a second model
# copy under the project cache after a successful bootstrap.
if (Test-Path -LiteralPath $stagingRoot) {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output "Bootstrap complete. Development venv Python: $env:CHONGZU_DEV_PYTHON"
Write-Output "Production standalone Python: $env:CHONGZU_PYTHON"
Write-Output "Production packages: $env:CHONGZU_PACKAGES ($($runtimeRequirements -join ', '))"
Write-Output "RapidOCR and ONNX Runtime are provisioned as a wheel-only offline OCR foundation; models are copied to $ocrModelRoot and runtime downloads are disabled. Java, Tika, Docling, Torch, and OCR cloud services remain excluded."
