param([Parameter(Mandatory = $true)][string]$ConfigurationPath)
$ErrorActionPreference='Stop'
& (Join-Path $PSScriptRoot '../backend/src/repowatch/_native/crash_owner.ps1') -ConfigurationPath $ConfigurationPath
