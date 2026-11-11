# Trusted, temporary job owner for the owner-crash qualification experiment.
param([Parameter(Mandatory = $true)][string]$ConfigurationPath)
$ErrorActionPreference='Stop'
$configuration=Get-Content -LiteralPath $ConfigurationPath -Raw | ConvertFrom-Json
Add-Type -Path (Join-Path $PSScriptRoot 'kernel.cs')
$profile=[RepoWatchNativeKernel+Profile]::OpenExisting($configuration.profile_name)
try {
    [RepoWatchNativeKernel]::RunOwned(
        $profile,$configuration.executable,$configuration.command,$configuration.workspace,
        $configuration.environment,20000,$configuration.progress_path,$configuration.job_name
    ) | Out-Null
} finally { $profile.Dispose() }
