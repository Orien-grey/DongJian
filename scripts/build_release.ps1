param(
    [string]$Version = "",
    [string]$OutputRoot = "",
    [string]$ArtifactSuffix = "rc2"
)

$ErrorActionPreference = 'Stop'

function Resolve-ContainedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Base,
        [Parameter(Mandatory = $true)][string]$Candidate
    )

    $basePath = [System.IO.Path]::GetFullPath($Base).TrimEnd('\') + '\'
    $candidatePath = [System.IO.Path]::GetFullPath($Candidate)
    if (-not $candidatePath.StartsWith($basePath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside the release output root: $candidatePath"
    }
    return $candidatePath
}

function Copy-RequiredPath {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Required release input is missing: $Source"
    }
    $parent = Split-Path -Parent $Destination
    if ($parent) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    if ((Get-Item -LiteralPath $Source).PSIsContainer) {
        Copy-Item -LiteralPath $Source -Destination $Destination -Recurse -Force
    } else {
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
    }
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not $Version) {
    $Version = (Get-Content -LiteralPath (Join-Path $repoRoot 'VERSION') -Raw).Trim()
}
if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$') {
    throw "VERSION must be a simple semantic version, got '$Version'"
}
if ($ArtifactSuffix -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]*$') {
    throw "ArtifactSuffix must contain only letters, numbers, dots, and hyphens"
}

# A formal release build is source evidence: it must identify one exact clean
# Git tree before any output is assembled.  The product itself does not call
# Git at runtime; this check is a development/release-machine requirement.
$gitCommand = Get-Command git.exe -ErrorAction SilentlyContinue
if (-not $gitCommand) {
    throw 'Formal release build requires Git so the manifest can identify a clean source tree'
}
$gitStatusOutput = (& git -C $repoRoot status --porcelain --untracked-files=all 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect Git source state: $gitStatusOutput"
}
if ($gitStatusOutput) {
    throw "Release build requires a clean Git tree. Current status:`n$gitStatusOutput"
}
$gitCommit = (& git -C $repoRoot rev-parse HEAD 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $gitCommit -notmatch '^[0-9a-fA-F]{40}$') {
    throw "Unable to resolve a 40-character Git HEAD: $gitCommit"
}

$auditPython = Join-Path $repoRoot 'runtime\python\cpython-3.11.15-windows-x86_64-none\python.exe'
$auditScript = Join-Path $repoRoot 'scripts\audit_third_party.py'
if (-not (Test-Path -LiteralPath $auditPython -PathType Leaf)) {
    throw "Project standalone Python is required for the release audit: $auditPython"
}
if (-not (Test-Path -LiteralPath $auditScript -PathType Leaf)) {
    throw "Third-party audit script is missing: $auditScript"
}
& $auditPython -B $auditScript --repo-root $repoRoot --output-root $repoRoot --check
if ($LASTEXITCODE -ne 0) {
    throw 'Third-party audit outputs are stale or incomplete; rerun the local audit before building'
}

if ($OutputRoot) {
    $outputRootPath = [System.IO.Path]::GetFullPath($OutputRoot)
} else {
    $outputRootPath = Join-Path $repoRoot 'release'
}
$outputRootPath = [System.IO.Path]::GetFullPath($outputRootPath)
New-Item -ItemType Directory -Path $outputRootPath -Force | Out-Null
$bundleName = "ChongZu-$Version-$ArtifactSuffix-win-x64"
$bundlePath = Resolve-ContainedPath -Base $outputRootPath -Candidate (Join-Path $outputRootPath $bundleName)
$zipPath = Resolve-ContainedPath -Base $outputRootPath -Candidate (Join-Path $outputRootPath "$bundleName.zip")

foreach ($target in @($bundlePath, $zipPath)) {
    if (Test-Path -LiteralPath $target) {
        if ((Get-Item -LiteralPath $target).PSIsContainer) {
            Remove-Item -LiteralPath $target -Recurse -Force
        } else {
            Remove-Item -LiteralPath $target -Force
        }
    }
}

New-Item -ItemType Directory -Path $bundlePath -Force | Out-Null

# This list is deliberately explicit.  In particular, it does not copy the
# development workspace, tests, caches, uv, venv, node-dev, or repository
# metadata into a product bundle.
$rootFiles = @(
    'start.cmd',
    'stop.cmd',
    'chongzu.cmd',
    'doctor.cmd',
    '.env.example',
    'README.md',
    'VERSION',
    'THIRD_PARTY_NOTICES.txt',
    'third-party-components.json'
)
foreach ($name in $rootFiles) {
    Copy-RequiredPath -Source (Join-Path $repoRoot $name) -Destination (Join-Path $bundlePath $name)
}

$directoryCopies = @(
    @{ Source = 'src'; Destination = 'src' },
    @{ Source = 'frontend\dist'; Destination = 'frontend\dist' },
    @{ Source = 'runtime\python\cpython-3.11.15-windows-x86_64-none'; Destination = 'runtime\python\cpython-3.11.15-windows-x86_64-none' },
    @{ Source = 'runtime\packages'; Destination = 'runtime\packages' },
    @{ Source = 'runtime\models'; Destination = 'runtime\models' },
    @{ Source = 'config\llm.example.json'; Destination = 'config\llm.example.json' },
    @{ Source = 'scripts\env.ps1'; Destination = 'scripts\env.ps1' },
    @{ Source = 'scripts\chongzu.ps1'; Destination = 'scripts\chongzu.ps1' },
    @{ Source = 'scripts\doctor.ps1'; Destination = 'scripts\doctor.ps1' },
    @{ Source = 'docs'; Destination = 'docs' },
    @{ Source = 'licenses'; Destination = 'licenses' }
)
foreach ($item in $directoryCopies) {
    Copy-RequiredPath `
        -Source (Join-Path $repoRoot $item.Source) `
        -Destination (Join-Path $bundlePath $item.Destination)
}

# The portable product always starts with a directly editable, secret-free
# project configuration.  Never copy a developer's ignored config/llm.json.
Copy-RequiredPath `
    -Source (Join-Path $repoRoot 'config\llm.example.json') `
    -Destination (Join-Path $bundlePath 'config\llm.json')

if (-not (Test-Path -LiteralPath (Join-Path $bundlePath 'frontend\dist\index.html') -PathType Leaf)) {
    throw 'frontend/dist/index.html is required; run npm run build before packaging'
}

# Keep the empty runtime contract explicit without carrying development data.
$emptyDirectories = @(
    'models',
    'models\docling',
    'workspace',
    'workspace\artifacts',
    'workspace\artifacts\tables',
    'workspace\artifacts\sheets',
    'workspace\artifacts\cleaning',
    'workspace\artifacts\cleaning\tables',
    'workspace\artifacts\cleaning\text',
    'workspace\artifacts\analysis',
    'workspace\artifacts\reports',
    'workspace\input',
    'workspace\staging',
    'workspace\output',
    'workspace\quarantine',
    'workspace\state',
    'workspace\logs'
)
foreach ($relative in $emptyDirectories) {
    New-Item -ItemType Directory -Path (Join-Path $bundlePath $relative) -Force | Out-Null
}

# Runtime wheels can contain test data and bytecode from provisioning.  They
# are not needed by the standalone product and are excluded from the RC.
$developmentDirectories = Get-ChildItem -LiteralPath $bundlePath -Recurse -Force -Directory |
    Where-Object { $_.Name -in @('__pycache__', 'tests', 'test') } |
    Sort-Object { $_.FullName.Length } -Descending
foreach ($directory in $developmentDirectories) {
    Remove-Item -LiteralPath $directory.FullName -Recurse -Force
}
Get-ChildItem -LiteralPath $bundlePath -Recurse -Force -File |
    Where-Object { $_.Extension -in @('.pyc', '.pyo') } |
    Remove-Item -Force
$productionSitePackages = Join-Path $bundlePath 'runtime\python\cpython-3.11.15-windows-x86_64-none\Lib\site-packages'
if (Test-Path -LiteralPath $productionSitePackages) {
    # The standalone interpreter's bundled pip/setuptools are provisioning
    # tools, not product dependencies.  The product imports pinned wheels
    # exclusively from runtime/packages via PYTHONPATH.
    Remove-Item -LiteralPath $productionSitePackages -Recurse -Force
}
$productionProvisioningPaths = @(
    (Join-Path $bundlePath 'runtime\python\cpython-3.11.15-windows-x86_64-none\Scripts'),
    (Join-Path $bundlePath 'runtime\python\cpython-3.11.15-windows-x86_64-none\Lib\ensurepip'),
    (Join-Path $bundlePath 'runtime\python\cpython-3.11.15-windows-x86_64-none\Lib\venv')
)
foreach ($path in $productionProvisioningPaths) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}
$bundleArchiveFiles = Get-ChildItem -LiteralPath $bundlePath -Recurse -Force -File |
    Where-Object { $_.Extension -in @('.whl', '.zip', '.pyc', '.pyo') }
foreach ($file in $bundleArchiveFiles) {
    Remove-Item -LiteralPath $file.FullName -Force
}
$numpyBuildConfig = Join-Path $bundlePath 'runtime\packages\numpy\__config__.py'
if (Test-Path -LiteralPath $numpyBuildConfig) {
    # NumPy imports this optional diagnostics module during initialization.
    # Retain the module but remove upstream CI machine paths from its display
    # metadata; no numerical/runtime behavior is changed.
    $numpyConfigText = Get-Content -LiteralPath $numpyBuildConfig -Raw
    $numpyConfigText = $numpyConfigText -replace 'C:/Users/runneradmin/AppData/Local/Temp/[^\"]+', '<upstream-build-path-omitted>'
    $numpyConfigText = $numpyConfigText -replace 'C:\\Users\\runneradmin\\AppData\\Local\\Temp\\[^\"]+', '<upstream-build-path-omitted>'
    $numpyConfigText = $numpyConfigText -replace 'D:/a/numpy-release/numpy-release/\.openblas', '<upstream-build-path-omitted>'
    Set-Content -LiteralPath $numpyBuildConfig -Value $numpyConfigText -Encoding UTF8
}

$thirdPartyManifestPath = Join-Path $bundlePath 'third-party-components.json'
$thirdPartyManifest = Get-Content -LiteralPath $thirdPartyManifestPath -Raw | ConvertFrom-Json
$thirdPartySummary = $thirdPartyManifest.summary
$manifestPath = Join-Path $bundlePath 'release-manifest.json'
$manifest = [ordered]@{
    version = $Version
    artifact_name = $bundleName
    git_commit = $gitCommit
    source_tree_clean = $true
    built_at = [DateTime]::UtcNow.ToString('o')
    python_version = '3.11.15'
    platform = 'win-x64'
    frontend_build = [ordered]@{
        status = 'built'
        path = 'frontend/dist'
        runtime = 'self-contained static assets'
    }
    runtime_components = [ordered]@{
        python = 'runtime/python/cpython-3.11.15-windows-x86_64-none'
        packages = 'runtime/packages'
        models = 'runtime/models/ocr'
        license_notices = @('THIRD_PARTY_NOTICES.txt', 'third-party-components.json', 'licenses')
        provisioning_uv = 'excluded from release; provisioning-only'
        development_venv = 'excluded from release'
        development_node = 'excluded from release'
    }
    ai_configuration = [ordered]@{
        project_file = 'config/llm.json'
        example_file = 'config/llm.example.json'
        api_key_policy = 'never included in release manifest, logs, tasks, or registry'
    }
    llm_status = 'NOT_CONFIGURED'
    third_party_manifest = [ordered]@{
        path = 'third-party-components.json'
        component_count = [int]$thirdPartySummary.component_count
        distributed_runtime_component_count = [int]$thirdPartySummary.distributed_runtime_component_count
        build_only_component_count = [int]$thirdPartySummary.build_only_component_count
        license_identified_count = [int]$thirdPartySummary.license_identified_count
        review_required_count = [int]$thirdPartySummary.review_required_count
    }
    payload_checksums = [ordered]@{
        'VERSION' = (Get-FileHash -LiteralPath (Join-Path $bundlePath 'VERSION') -Algorithm SHA256).Hash.ToLowerInvariant()
        'frontend/dist/index.html' = (Get-FileHash -LiteralPath (Join-Path $bundlePath 'frontend\dist\index.html') -Algorithm SHA256).Hash.ToLowerInvariant()
        'runtime/models/ocr/manifest.json' = (Get-FileHash -LiteralPath (Join-Path $bundlePath 'runtime\models\ocr\manifest.json') -Algorithm SHA256).Hash.ToLowerInvariant()
        'THIRD_PARTY_NOTICES.txt' = (Get-FileHash -LiteralPath (Join-Path $bundlePath 'THIRD_PARTY_NOTICES.txt') -Algorithm SHA256).Hash.ToLowerInvariant()
        'third-party-components.json' = (Get-FileHash -LiteralPath $thirdPartyManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

$manifestJson = $manifest | ConvertTo-Json -Depth 12
$utf8NoBom = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList @($false)
[System.IO.File]::WriteAllText($manifestPath, $manifestJson + [Environment]::NewLine, $utf8NoBom)

# The allowlist is enforced after filtering as a release invariant.  These
# paths must never enter the product bundle even if a source tree happens to
# contain them.
$forbiddenBundlePaths = @(
    '.git',
    '.env',
    'node_modules',
    'cache',
    'tests',
    'runtime\venv',
    'runtime\uv',
    'runtime\node-dev',
    'workspace\state\registry.duckdb'
)
foreach ($relative in $forbiddenBundlePaths) {
    if (Test-Path -LiteralPath (Join-Path $bundlePath $relative)) {
        throw "Release allowlist violation: $relative"
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $bundlePath 'licenses') -PathType Container)) {
    throw 'Release allowlist violation: licenses directory is missing'
}

Compress-Archive -LiteralPath $bundlePath -DestinationPath $zipPath -CompressionLevel Optimal -Force
$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$zipPath.sha256.txt" -Value "$hash  $([System.IO.Path]::GetFileName($zipPath))" -Encoding ASCII

$bundleBytes = (Get-ChildItem -LiteralPath $bundlePath -Recurse -File | Measure-Object -Property Length -Sum).Sum
$zipBytes = (Get-Item -LiteralPath $zipPath).Length
Write-Output "Release bundle: $bundlePath"
Write-Output "Bundle bytes: $bundleBytes"
Write-Output "ZIP: $zipPath"
Write-Output "ZIP bytes: $zipBytes"
Write-Output "ZIP SHA-256: $hash"
