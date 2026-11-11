param([string]$OutputDirectory = (Join-Path $PSScriptRoot '../.cache/release'))
$ErrorActionPreference = 'Stop'
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$packageOutput = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $packageOutput -Force | Out-Null
Push-Location $repositoryRoot
try {
    pnpm --dir frontend build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
    uv lock --check --project backend --offline --cache-dir .cache/uv
    if ($LASTEXITCODE -ne 0) { throw 'Dependency lock mismatch' }
    uv build --project backend --wheel --offline --cache-dir .cache/uv --out-dir $packageOutput
    if ($LASTEXITCODE -ne 0) { throw 'Wheel build failed' }
    uv export --project backend --frozen --no-dev --no-hashes --no-header --no-emit-project --format requirements-txt --output-file (Join-Path $packageOutput 'requirements.lock') --cache-dir .cache/uv | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Dependency export failed' }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'install.ps1') -Destination (Join-Path $packageOutput 'install.ps1')
    Write-Output "Release files prepared: $packageOutput"
} finally { Pop-Location }
