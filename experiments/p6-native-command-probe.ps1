param([Parameter(Mandatory=$true)][string]$Root,[Parameter(Mandatory=$true)][string]$KernelRoot)
$ErrorActionPreference='Stop'
$env:PSModulePath=Join-Path $PSHOME 'Modules'
Add-Type -Path (Join-Path $KernelRoot 'kernel.cs')
. (Join-Path $KernelRoot 'command.ps1')
$profile=Get-Content -LiteralPath (Join-Path $Root 'profile.json') -Raw | ConvertFrom-Json
$requestPath=Join-Path $Root 'probe-request.json'
$request=Get-Content -LiteralPath $requestPath -Raw | ConvertFrom-Json
$execution=@{entered_kernel=$false}
$result=Invoke-NativeCheck $request (Get-NativeSha256 $requestPath) $profile $Root $request.generation ([DateTime]::UtcNow.AddMinutes(2)) $execution
$result | ConvertTo-Json -Depth 10 -Compress
