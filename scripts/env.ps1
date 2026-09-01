# ChongZu project-local process environment (Windows PowerShell 5.1).
# Dot-source this file; it changes only the current PowerShell process.

$scriptRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$projectRoot = (Resolve-Path -LiteralPath (Join-Path -Path $scriptRoot -ChildPath '..')).Path

if (-not (Test-Path -LiteralPath (Join-Path -Path $projectRoot -ChildPath 'runtime') -PathType Container) -or
    -not (Test-Path -LiteralPath (Join-Path -Path $projectRoot -ChildPath 'src') -PathType Container)) {
    throw "Unable to locate ChongZu project root from $PSScriptRoot"
}

$runtimeRoot = Join-Path -Path $projectRoot -ChildPath 'runtime'
$pythonRuntimeRoot = Join-Path -Path $runtimeRoot -ChildPath 'python'
$pythonRuntimeDir = Join-Path -Path $pythonRuntimeRoot -ChildPath 'cpython-3.11.15-windows-x86_64-none'
$pythonExe = Join-Path -Path $pythonRuntimeDir -ChildPath 'python.exe'
$packagesRoot = Join-Path -Path $runtimeRoot -ChildPath 'packages'
$uvExe = Join-Path -Path (Join-Path -Path $runtimeRoot -ChildPath 'uv') -ChildPath 'uv.exe'
$venvRoot = Join-Path -Path $runtimeRoot -ChildPath 'venv'
$venvPython = Join-Path -Path (Join-Path -Path $venvRoot -ChildPath 'Scripts') -ChildPath 'python.exe'

$cacheRoot = Join-Path -Path $projectRoot -ChildPath 'cache'
$uvCache = Join-Path -Path $cacheRoot -ChildPath 'uv'
$pipCache = Join-Path -Path $cacheRoot -ChildPath 'pip'
$huggingfaceHome = Join-Path -Path $cacheRoot -ChildPath 'huggingface'
$huggingfaceHub = Join-Path -Path $huggingfaceHome -ChildPath 'hub'
$tempRoot = Join-Path -Path $cacheRoot -ChildPath 'temp'
$bytecodeCache = Join-Path -Path $tempRoot -ChildPath 'pycache'
$pipConfig = Join-Path -Path $pipCache -ChildPath 'pip.ini'

# These are all project-local directories. Directory creation is intentionally
# limited to paths below the project root and affects no global configuration.
foreach ($directory in @($uvCache, $pipCache, $huggingfaceHome, $huggingfaceHub, $tempRoot, $bytecodeCache)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
New-Item -ItemType Directory -Path $packagesRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $pipConfig -PathType Leaf)) {
    New-Item -ItemType File -Path $pipConfig -Force | Out-Null
}

$env:CHONGZU_PROJECT_ROOT = $projectRoot
$env:CHONGZU_ROOT = $projectRoot
$env:CHONGZU_RUNTIME_ROOT = $runtimeRoot
# Production commands always use the standalone interpreter plus the target
# package directory.  The venv remains available only for development tools
# such as pytest and uv sync.
$env:CHONGZU_PYTHON = $pythonExe
$env:CHONGZU_RUNTIME_PYTHON = $pythonExe
$env:CHONGZU_PACKAGES = $packagesRoot
$env:CHONGZU_SRC = Join-Path -Path $projectRoot -ChildPath 'src'
$env:CHONGZU_DEV_PYTHON = $venvPython
$env:CHONGZU_PROJECT_PYTHON = $pythonExe
$env:CHONGZU_PROJECT_UV = $uvExe

$env:UV_CACHE_DIR = $uvCache
$env:UV_PYTHON_INSTALL_DIR = $pythonRuntimeRoot
$env:UV_PYTHON = $pythonExe
$env:UV_MANAGED_PYTHON = '1'
$env:UV_PYTHON_DOWNLOADS = 'never'
$env:UV_PROJECT_ENVIRONMENT = $venvRoot
$env:UV_NO_CONFIG = '1'
$env:UV_NO_PROGRESS = '1'
$env:UV_LINK_MODE = 'copy'

$env:PIP_CACHE_DIR = $pipCache
$env:PIP_CONFIG_FILE = $pipConfig
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PIP_NO_INPUT = '1'

$env:HF_HOME = $huggingfaceHome
$env:HUGGINGFACE_HUB_CACHE = $huggingfaceHub
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONPYCACHEPREFIX = $bytecodeCache
# A caller may have an activated venv or a host PYTHONHOME.  Clear those
# process-local hints so the explicit standalone executable is authoritative;
# no user/system setting is changed.
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
$pathSeparator = [System.IO.Path]::PathSeparator
$env:PYTHONPATH = "$(Join-Path -Path $projectRoot -ChildPath 'src')$pathSeparator$packagesRoot"
$env:TMP = $tempRoot
$env:TEMP = $tempRoot
