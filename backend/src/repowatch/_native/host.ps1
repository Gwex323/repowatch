param([switch]$AllowWorkspaceExecution,[int]$SessionSeconds=3600)
$ErrorActionPreference='Stop'
$env:PSModulePath=Join-Path $PSHOME 'Modules'
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Utility/Microsoft.PowerShell.Utility.psd1')
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1')
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Management/Microsoft.PowerShell.Management.psd1')
if(-not $AllowWorkspaceExecution){throw 'Native workspace execution must be explicitly enabled'}
if($SessionSeconds -lt 30 -or $SessionSeconds -gt 14400){throw 'Native host lifetime must be within 30..14400 seconds'}
if([IntPtr]::Size -ne 8){throw 'The first supported native host requires 64-bit Windows PowerShell'}
$root=Split-Path $PSScriptRoot
$nativeProfile=Get-Content -LiteralPath (Join-Path $root 'profile.json') -Raw | ConvertFrom-Json
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
if($nativeProfile.protocol -ne 'repowatch-native-v1' -or $identity.User.Value -ne $nativeProfile.execution_sid -or $identity.User.Value -eq $nativeProfile.controller_sid){throw 'Native host execution identity mismatch'}
if([Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Elevated native host refused'}
function Assert-NativeAcl([string]$path,[bool]$allowExecutionWrite=$false) {
    $security=Get-Acl -LiteralPath $path
    $owner=$security.GetOwner([Security.Principal.SecurityIdentifier]).Value
    $owners=if($allowExecutionWrite){@($nativeProfile.controller_sid,$nativeProfile.execution_sid)}else{@($nativeProfile.controller_sid)}
    if($owner -notin $owners){throw 'Native host resource owner mismatch'}
    $allowed=@($nativeProfile.controller_sid,$nativeProfile.execution_sid,'S-1-5-18','S-1-5-32-544')
    foreach($rule in $security.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
        if($rule.AccessControlType -ne 'Allow'){continue}
        $actor=$rule.IdentityReference.Value
        if($actor -notin $allowed){throw 'Native host resource has a broad or untrusted access grant'}
        if(-not $allowExecutionWrite -and $actor -eq $nativeProfile.execution_sid -and ([long]$rule.FileSystemRights -band 0x500D0116)){throw 'Execution account can modify trusted host resources'}
    }
}
Assert-NativeAcl $root
Assert-NativeAcl $PSScriptRoot
Assert-NativeAcl (Join-Path $root 'profile.json')
foreach($name in @('requests','workspaces','runtime')){Assert-NativeAcl (Join-Path $root $name)}
Assert-NativeAcl (Join-Path $root 'responses') $true
$required=@('capture_qualification.ps1','command.ps1','crash_owner.ps1','host.ps1','kernel.cs','qualification.ps1','security_probe.cs')
if((@($nativeProfile.assets.PSObject.Properties.Name | Sort-Object) -join ',') -cne ($required -join ',')){throw 'Native host resource manifest is incomplete'}
foreach($property in $nativeProfile.assets.PSObject.Properties) {
    if([IO.Path]::GetFileName($property.Name) -ne $property.Name){throw 'Invalid host resource name'}
    $path=Join-Path $PSScriptRoot $property.Name
    Assert-NativeAcl $path
    if((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $property.Value){throw 'Native host resource hash mismatch'}
}
Add-Type -Path (Join-Path $PSScriptRoot 'kernel.cs')
. (Join-Path $PSScriptRoot 'command.ps1')
$mutex=[Threading.Mutex]::new($false,('Local\RepoWatchNativeHost-'+$nativeProfile.profile_id))
$acquired=$false
$ready=$null
try {
    $acquired=$mutex.WaitOne(0)
    if(-not $acquired){throw 'This native host profile already has an active owner'}
    $qualificationPath=Join-Path $root ('responses/qualification-'+[Guid]::NewGuid().ToString('N')+'.json')
    @{expected_sid=$nativeProfile.execution_sid;control_url=$nativeProfile.control_url} | ConvertTo-Json | Set-Content -LiteralPath $qualificationPath -Encoding UTF8
    $proofText=& (Join-Path $PSScriptRoot 'qualification.ps1') -ConfigurationPath $qualificationPath
    $proof=($proofText -join [Environment]::NewLine) | ConvertFrom-Json
    if(-not $proof.passed -or $proof.control_api_status_before -ne 401 -or $proof.control_api_status_after -ne 401 -or $proof.owner_crash.active_members_after -ne 0 -or $proof.security_canaries.credential_error -ne 5){throw 'Native host isolation qualification failed'}
    $captureText=& (Join-Path $PSScriptRoot 'capture_qualification.ps1') -ControllerSid $nativeProfile.controller_sid
    $capture=($captureText -join [Environment]::NewLine) | ConvertFrom-Json
    if(-not $capture.passed){throw 'Native host capture/startup-gate qualification failed'}
    $generation=[Guid]::NewGuid().ToString('N')
    $profileHash=Get-NativeSha256 (Join-Path $root 'profile.json')
    $ready=[ordered]@{protocol='repowatch-native-v1';generation=$generation;profile_sha256=$profileHash;pid=$PID;created=[RepoWatchNativeKernel]::CurrentCreated();stopped=$false;
        qualifications=@('appcontainer_token','workspace_scope','source_read_only','credential_canary','environment_allowlist','control_api_boundary','owner_death_job_cleanup','bounded_timeout')}
    $readyPath=Join-Path $root 'responses/ready.json'
    if(Test-Path -LiteralPath $readyPath){
        $old=Get-Content -LiteralPath $readyPath -Raw | ConvertFrom-Json
        if(-not $old.stopped -and (Get-Process -Id $old.pid -ErrorAction SilentlyContinue)){throw 'Previous native host identity needs reconciliation'}
        Remove-Item -LiteralPath $readyPath
    }
    Write-NativeAtomic $readyPath $ready
    Write-Output 'Native workspace host ready. Repository commands require a current controller startup gate.'
    $deadline=[DateTime]::UtcNow.AddSeconds($SessionSeconds)
    while([DateTime]::UtcNow -lt $deadline -and -not(Test-Path -LiteralPath (Join-Path $root 'requests/shutdown'))) {
        foreach($requestFile in @(Get-ChildItem -LiteralPath (Join-Path $root 'requests') -Filter '*.json' -File | Sort-Object Name)) {
            if([DateTime]::UtcNow -ge $deadline){break}
            if($requestFile.BaseName -notmatch '^[a-f0-9]{32}$'){continue}
            $id=$requestFile.BaseName
            $resultPath=Join-Path $root ('responses/'+$id+'.json')
            if(Test-Path -LiteralPath $resultPath){continue}
            $execution=@{entered_kernel=$false}
            $requestHash=Get-NativeSha256 $requestFile.FullName
            $record=[ordered]@{protocol='repowatch-native-v1';request_id=$id;request_sha256=$requestHash;generation=$generation;state='unverified';identity_verified=$false}
            try {
                if($requestFile.Length -gt 65536){throw 'Native request size limit'}
                $request=Get-Content -LiteralPath $requestFile.FullName -Raw | ConvertFrom-Json
                $keys=@($request.PSObject.Properties.Name | Sort-Object)
                if(($keys -join ',') -ne 'generation,operation,payload,profile_sha256,protocol,request_id' -or $request.request_id -cne $id -or $request.protocol -cne 'repowatch-native-v1' -or $request.generation -cne $generation -or $request.profile_sha256 -cne $profileHash){throw 'Native request binding mismatch'}
                if($request.operation -ne 'check'){throw 'Only admitted workspace check requests are accepted'}
                $record=Invoke-NativeCheck $request $requestHash $nativeProfile $root $generation $deadline $execution
            } catch {
                if(-not $execution.entered_kernel){
                    $record.state='stopped';$record.active_processes=0;$record.identity_verified=$true;$record.exit_code=125
                    $record.preparation_report=@{reason='native_environment_unavailable';details=$_.Exception.Message.Substring(0,[Math]::Min(300,$_.Exception.Message.Length))}
                    $record.log_file=$id+'.log';$record.log_truncated=$false
                    $text='REPOWATCH_ENVIRONMENT_UNAVAILABLE: '+($record.preparation_report | ConvertTo-Json -Compress)
                    [IO.File]::WriteAllText((Join-Path $root ('responses/logs/'+$id+'.log')),$text,[Text.Encoding]::UTF8)
                    $record.log_sha256=Get-NativeSha256 (Join-Path $root ('responses/logs/'+$id+'.log'))
                } else {
                    $record.error='Native execution completion is unverified; host stops accepting work'
                }
            }
            $commandPath=Join-Path $root ('responses/commands/'+$id+'.json')
            if(Test-Path -LiteralPath $commandPath){Write-NativeAtomic $commandPath $record}
            Write-NativeAtomic $resultPath $record
            if($record.state -ne 'stopped'){throw 'Native execution state requires reconciliation'}
        }
        Start-Sleep -Milliseconds 100
    }
} finally {
    if($null -ne $ready){
        $ready.stopped=$true
        Write-NativeAtomic (Join-Path $root 'responses/ready.json') $ready
    }
    if($acquired){$mutex.ReleaseMutex()}
    $mutex.Dispose()
}
