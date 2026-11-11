param(
    [Parameter(Mandatory = $true)][string]$InstallDirectory,
    [string]$PackageDirectory = $PSScriptRoot,
    [string]$CacheDirectory,
    [switch]$Offline
)
$ErrorActionPreference = 'Stop'
$installTarget = [IO.Path]::GetFullPath($InstallDirectory)
$packageRoot = (Resolve-Path -LiteralPath $PackageDirectory).Path
if (Test-Path -LiteralPath $installTarget) { throw 'Choose a new install directory; existing installations are preserved.' }
$wheels = @(Get-ChildItem -LiteralPath $packageRoot -Filter 'repowatch-*.whl' -File)
if ($wheels.Count -ne 1) { throw 'Package directory must contain exactly one RepoWatch wheel.' }
$constraints = Join-Path $packageRoot 'requirements.lock'
if (-not (Test-Path -LiteralPath $constraints)) { throw 'Locked dependency list is missing.' }
uv venv --python 3.12 $installTarget
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
$pythonExecutable = Join-Path $installTarget 'Scripts/python.exe'
$installArguments = @('pip', 'install', '--python', $pythonExecutable, '--requirement', $constraints, $wheels[0].FullName)
if ($Offline) { $installArguments += '--offline' }
if ($CacheDirectory) { $installArguments += @('--cache-dir', [IO.Path]::GetFullPath($CacheDirectory)) }
& uv @installArguments
if ($LASTEXITCODE -ne 0) { throw 'Installation failed; preserved directory contains diagnostics and may be inspected.' }
& $pythonExecutable -m repowatch.operations status
if ($LASTEXITCODE -ne 0) { throw 'Installed application verification failed.' }
Write-Output "Installed. Initialize an explicit data directory with: $pythonExecutable -m repowatch.operations initialize --data-dir <absolute-local-data-directory>"
