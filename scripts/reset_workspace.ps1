[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot ".." )).Path

# This command is intentionally narrower than a generic cleanup command.  It
# can only remove generated data rooted directly below this ChongZu project.
$required = @("src", "runtime", "models", "config", "workspace", "cache")
foreach ($name in $required) {
    $path = Join-Path $repoRoot $name
    if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        throw "Refusing to reset: expected project directory is missing: $path"
    }
}

$dataRoots = @(
    (Join-Path $repoRoot "workspace\artifacts"),
    (Join-Path $repoRoot "workspace\state"),
    (Join-Path $repoRoot "workspace\logs"),
    (Join-Path $repoRoot "workspace\output"),
    (Join-Path $repoRoot "workspace\quarantine"),
    (Join-Path $repoRoot "workspace\staging"),
    (Join-Path $repoRoot "cache")
)
$protectedRoots = @(
    (Join-Path $repoRoot "src"),
    (Join-Path $repoRoot "runtime"),
    (Join-Path $repoRoot "models"),
    (Join-Path $repoRoot "config"),
    (Join-Path $repoRoot "frontend"),
    (Join-Path $repoRoot "release"),
    (Join-Path $repoRoot "workspace\input")
)
# Runtime ownership files belong to the launcher/server lifecycle.  They are
# intentionally outside the generated-project-data contract and must survive
# an administrative reset while the product is running.
$runtimeControlFiles = @(
    (Join-Path $repoRoot "workspace\state\server.lock"),
    (Join-Path $repoRoot "workspace\state\server.start.lock"),
    (Join-Path $repoRoot "workspace\state\server.pid"),
    (Join-Path $repoRoot "workspace\logs\server.log")
)

function Test-ChildOf([string]$child, [string]$parent) {
    $childFull = [IO.Path]::GetFullPath($child).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $parentFull = [IO.Path]::GetFullPath($parent).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    return $childFull.StartsWith($parentFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}

function Test-RuntimeControlFile([string]$path) {
    foreach ($runtimeControl in $runtimeControlFiles) {
        if ([IO.Path]::GetFullPath($path).Equals(
                [IO.Path]::GetFullPath($runtimeControl),
                [StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

foreach ($root in $dataRoots) {
    $rootFull = [IO.Path]::GetFullPath($root)
    if (-not (Test-ChildOf $rootFull $repoRoot)) {
        throw "Refusing to reset data outside the project root: $rootFull"
    }
    foreach ($protected in $protectedRoots) {
        if ($rootFull.Equals([IO.Path]::GetFullPath($protected), [StringComparison]::OrdinalIgnoreCase) -or
            (Test-ChildOf $rootFull $protected)) {
            throw "Refusing to reset a protected project directory: $rootFull"
        }
    }
}

if (-not $Force) {
    Write-Host "WARNING: this removes ChongZu-generated data under:" -ForegroundColor Yellow
    $dataRoots | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
    Write-Host "It does not delete source directories, runtime, models, frontend, config, or release files." -ForegroundColor Yellow
    $confirmation = Read-Host "Type RESET to continue"
    if ($confirmation -cne "RESET") {
        Write-Host "Reset cancelled."
        exit 2
    }
}

# The live server cannot safely remove its own runtime handles.  Delegate the
# controlled STOP -> RESET -> RESTART lifecycle to the bundled interpreter.
$bundledPython = Join-Path $repoRoot "runtime\python\cpython-3.11.15-windows-x86_64-none\python.exe"
if (-not (Test-Path -LiteralPath $bundledPython -PathType Leaf)) {
    throw "Refusing to reset: bundled Python is missing: $bundledPython"
}
$env:PYTHONPATH = "$(Join-Path $repoRoot 'src')$([IO.Path]::PathSeparator)$(Join-Path $repoRoot 'runtime\packages')"
$env:PYTHONNOUSERSITE = "1"
& $bundledPython -m chongzu.api.reset_helper --project-root $repoRoot --request-id ("manual_" + [guid]::NewGuid().ToString("N"))
exit $LASTEXITCODE

foreach ($root in $dataRoots) {
    $rootFull = [IO.Path]::GetFullPath($root)
    $files = @(Get-ChildItem -LiteralPath $rootFull -Force -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne ".gitkeep" -and -not (Test-RuntimeControlFile $_.FullName) })
    foreach ($entry in $files) {
        if (-not (Test-ChildOf $entry.FullName $rootFull)) {
            throw "Refusing to remove path outside reset root: $($entry.FullName)"
        }
        if ($PSCmdlet.ShouldProcess($entry.FullName, "Remove generated ChongZu data")) {
            Remove-Item -LiteralPath $entry.FullName -Force
        }
    }

    # Remove now-empty generated directories deepest-first.  Directories that
    # contain a .gitkeep marker are retained, but their generated files were
    # already removed above.
    $directories = @(Get-ChildItem -LiteralPath $rootFull -Force -Recurse -Directory -ErrorAction SilentlyContinue |
        Sort-Object { $_.FullName.Length } -Descending)
    foreach ($entry in $directories) {
        if (-not (Test-ChildOf $entry.FullName $rootFull)) {
            throw "Refusing to remove path outside reset root: $($entry.FullName)"
        }
        if (@(Get-ChildItem -LiteralPath $entry.FullName -Force -Recurse -Filter ".gitkeep" -ErrorAction SilentlyContinue).Count -gt 0) {
            continue
        }
        if (@(Get-ChildItem -LiteralPath $entry.FullName -Force -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object { Test-RuntimeControlFile $_.FullName }).Count -gt 0) {
            continue
        }
        if ($PSCmdlet.ShouldProcess($entry.FullName, "Remove empty generated directory")) {
            Remove-Item -LiteralPath $entry.FullName -Recurse -Force
        }
    }
}

Write-Host "ChongZu generated workspace/cache data reset. Source directories were not touched."
