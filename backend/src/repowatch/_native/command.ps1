# Dot-sourced by the native host. All repository execution stays in AppContainer.
function Get-NativeSha256([string]$path) {
    $stream=[IO.File]::OpenRead($path)
    $algorithm=[Security.Cryptography.SHA256]::Create()
    try {return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}
    finally {$algorithm.Dispose();$stream.Dispose()}
}
function Assert-NativePlainPath([string]$path) {
    $entry=Get-Item -LiteralPath $path -Force
    if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Reparse points are not allowed'}
    return $entry
}
function Assert-NativeRelativePath([string]$path) {
    if(-not $path -or $path -match '[^\x20-\x7e]|[\\:<>"|?*]' -or $path.StartsWith('/')){throw 'Unsupported native source path'}
    foreach($part in $path.Split('/')) {
        if(-not $part -or $part -in @('.','..','.git') -or $part.EndsWith('.') -or $part.EndsWith(' ') -or $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])\s*(\.|$)'){throw 'Unsafe native source component'}
    }
}
function Get-NativeFiles([string]$directory) {
    Assert-NativePlainPath $directory | Out-Null
    foreach($entry in @(Get-ChildItem -LiteralPath $directory -Force)) {
        if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Reparse point in source tree'}
        if($entry.PSIsContainer){Get-NativeFiles $entry.FullName}else{$entry}
    }
}
function Write-NativeAtomic([string]$path,$value) {
    $temporary=$path+'.'+[Guid]::NewGuid().ToString('N')+'.tmp'
    $value | ConvertTo-Json -Depth 20 -Compress | Set-Content -LiteralPath $temporary -Encoding UTF8
    if(Test-Path -LiteralPath $path){
        # Windows PowerShell coerces a null string argument to an empty backup path.
        $backup=$path+'.'+[Guid]::NewGuid().ToString('N')+'.old'
        [IO.File]::Replace($temporary,$path,$backup)
        [IO.File]::Delete($backup)
    }else{[IO.File]::Move($temporary,$path)}
}
function Remove-NativeTaskTree([string]$path) {
    # No process is alive at this point. Never recurse through repository-created links.
    $entry=Get-Item -LiteralPath $path -Force
    if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){
        if($entry.PSIsContainer){[IO.Directory]::Delete($path)}else{[IO.File]::Delete($path)}
        return
    }
    if($entry.PSIsContainer){
        foreach($child in @(Get-ChildItem -LiteralPath $path -Force)){Remove-NativeTaskTree $child.FullName}
        [IO.Directory]::Delete($path)
    }else{[IO.File]::Delete($path)}
}
function Invoke-NativeCheck($request,[string]$requestHash,$profile,[string]$root,[string]$generation,[DateTime]$sessionDeadline,$execution) {
    $id=$request.request_id
    $payload=$request.payload
    $keys=@($payload.PSObject.Properties.Name | Sort-Object)
    if(($keys -join ',') -ne 'argv,deadline_unix_ms,manifest_sha256,timeout_ms,workspace_id' -or $payload.workspace_id -notmatch '^repowatch-native-[a-f0-9]{32}$' -or $payload.manifest_sha256 -notmatch '^[a-f0-9]{64}$'){throw 'Invalid native check scope'}
    if($payload.timeout_ms -isnot [int] -and $payload.timeout_ms -isnot [long]){throw 'Integer timeout required'}
    if($payload.timeout_ms -lt 1 -or $payload.timeout_ms -gt 3600000){throw 'Finite timeout required'}
    if(($payload.deadline_unix_ms -isnot [long] -and $payload.deadline_unix_ms -isnot [int]) -or $payload.deadline_unix_ms -le 0){throw 'Integer deadline required'}
    $argv=@($payload.argv)
    if($argv.Count -lt 1 -or $argv.Count -gt 256){throw 'Explicit argv required'}
    $length=0
    foreach($argument in $argv){if($argument -isnot [string] -or $argument.Contains([string][char]0) -or $argument.Length -gt 16384){throw 'Invalid argv'};$length+=$argument.Length}
    if($length -gt 65536){throw 'Argument limit exceeded'}
    $tool=$profile.tools.PSObject.Properties[$argv[0]]
    if($null -eq $tool){throw 'Executable is not in the native toolchain'}
    $workspaceRoot=Join-Path $root ('workspaces/'+$payload.workspace_id)
    Assert-NativePlainPath $workspaceRoot | Out-Null
    $metadataPath=Join-Path $workspaceRoot 'workspace.json'
    Assert-NativePlainPath $metadataPath | Out-Null
    $metadata=Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
    if($metadata.id -ne $payload.workspace_id -or $metadata.kind -ne 'native' -or $metadata.image -ne $profile.runtime_id -or $metadata.ready -ne $true){throw 'Workspace identity mismatch'}
    $sourceRoot=Join-Path $workspaceRoot 'source'
    $files=@{}
    $total=0
    foreach($entry in @(Get-NativeFiles $sourceRoot)) {
        $relative=$entry.FullName.Substring($sourceRoot.Length+1).Replace('\','/')
        Assert-NativeRelativePath $relative
        if($files.ContainsKey($relative) -or $entry.Length -gt 16777216){throw 'Source collision or size limit'}
        $total+=$entry.Length
        if($total -gt 67108864){throw 'Snapshot size limit'}
        $files[$relative]=$entry
    }
    $modes=@{}
    foreach($entry in @($metadata.manifest)){$modes[$entry.path]=$entry.mode}
    [string[]]$paths=@($files.Keys)
    [Array]::Sort($paths,[StringComparer]::Ordinal)
    $manifestParts=@()
    foreach($relative in $paths) {
        $mode=if($modes.ContainsKey($relative)){$modes[$relative]}else{'100644'}
        if($mode -notin @('100644','100755')){throw 'Unsupported source object'}
        $hash=Get-NativeSha256 $files[$relative].FullName
        $manifestParts+='{"mode":"'+$mode+'","path":"'+$relative+'","sha256":"'+$hash+'","size":'+$files[$relative].Length+'}'
    }
    $manifestText='['+($manifestParts -join ',')+']'
    $algorithm=[Security.Cryptography.SHA256]::Create()
    try {$snapshotHash=([BitConverter]::ToString($algorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($manifestText)))).Replace('-','').ToLowerInvariant()}finally{$algorithm.Dispose()}
    if($snapshotHash -ne $payload.manifest_sha256){throw 'Frozen snapshot changed before native execution'}
    $runRoot=Join-Path $env:PUBLIC ('Documents/RepoWatchNativeTask-'+$id)
    if(Test-Path -LiteralPath $runRoot){throw 'Native task directory already exists; reconcile before retry'}
    New-Item -ItemType Directory -Path $runRoot | Out-Null
    $sandboxProfile=$null
    $safeCleanup=$false
    $record=[ordered]@{protocol='repowatch-native-v1';request_id=$id;request_sha256=$requestHash;generation=$generation;workspace_id=$payload.workspace_id;state='preparing'}
    $commandRecord=Join-Path $root ('responses/commands/'+$id+'.json')
    Write-NativeAtomic $commandRecord $record
    try {
        $sandboxProfile=[RepoWatchNativeKernel+Profile]::new('RepoWatch.Native.'+$id)
        $sid=[Security.Principal.SecurityIdentifier]::new($sandboxProfile.Sid)
        $actor=[Security.Principal.WindowsIdentity]::GetCurrent().User
        $inherit=[Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
        $acl=[Security.AccessControl.DirectorySecurity]::new();$acl.SetOwner($actor);$acl.SetAccessRuleProtection($true,$false)
        foreach($principal in @($actor,[Security.Principal.SecurityIdentifier]::new($profile.controller_sid),[Security.Principal.SecurityIdentifier]::new('S-1-5-18'),[Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($principal,'FullControl',$inherit,'None','Allow'))
        }
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'ReadAndExecute','None','None','Allow'))
        Set-Acl -LiteralPath $runRoot -AclObject $acl
        $copy=Join-Path $runRoot 'source';$runtime=Join-Path $runRoot 'runtime'
        New-Item -ItemType Directory -Path $copy,$runtime | Out-Null
        foreach($entry in @(@($copy,'Modify'),@($runtime,'ReadAndExecute'))) {
            $permissions=Get-Acl -LiteralPath $entry[0]
            $permissions.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,$entry[1],$inherit,'None','Allow'))
            Set-Acl -LiteralPath $entry[0] -AclObject $permissions
        }
        & icacls.exe $copy /setintegritylevel '(OI)(CI)L' | Out-Null
        if($LASTEXITCODE -ne 0){throw 'Low-integrity task directory setup failed'}
        foreach($relative in $paths){$destination=Join-Path $copy $relative;New-Item -ItemType Directory -Path (Split-Path $destination) -Force | Out-Null;Copy-Item -LiteralPath $files[$relative].FullName -Destination $destination}
        foreach($property in $profile.runtime_files.PSObject.Properties) {
            Assert-NativeRelativePath $property.Name
            $original=Join-Path (Join-Path $root 'runtime') $property.Name
            Assert-NativePlainPath $original | Out-Null
            if((Get-NativeSha256 $original) -ne $property.Value){throw 'Native runtime changed'}
            $destination=Join-Path $runtime $property.Name
            New-Item -ItemType Directory -Path (Split-Path $destination) -Force | Out-Null
            Copy-Item -LiteralPath $original -Destination $destination
        }
        $taskHome=Join-Path $copy '.repowatch-native-home'
        if(Test-Path -LiteralPath $taskHome){throw 'Reserved native home directory collides with source'}
        New-Item -ItemType Directory -Path $taskHome,(Join-Path $taskHome 'temp'),(Join-Path $taskHome 'local'),(Join-Path $taskHome 'roaming') | Out-Null
        $executable=Join-Path $runtime $tool.Value
        $nativeArgv=[string[]]@($executable)+[string[]]@($argv | Select-Object -Skip 1)
        $pathDirectories=@($profile.tools.PSObject.Properties | ForEach-Object {Split-Path (Join-Path $runtime $_.Value)} | Select-Object -Unique)
        $nativePath=($pathDirectories -join ';')+';'+$env:SystemRoot+'\System32'
        $environment="APPDATA=$taskHome\roaming`0ComSpec=$env:SystemRoot\System32\cmd.exe`0HOME=$taskHome`0LOCALAPPDATA=$taskHome\local`0PATH=$nativePath`0PYTHONDONTWRITEBYTECODE=1`0PYTHONHOME=$runtime\python`0PYTHONNOUSERSITE=1`0SystemRoot=$env:SystemRoot`0TEMP=$taskHome\temp`0TMP=$taskHome\temp`0USERPROFILE=$taskHome`0WINDIR=$env:SystemRoot`0`0"
        $remaining=[Math]::Min([long]$payload.timeout_ms,[Math]::Min([long]$payload.deadline_unix_ms-[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(),[long]($sessionDeadline-[DateTime]::UtcNow).TotalMilliseconds))
        if($remaining -le 0){throw 'Native check deadline expired before startup'}
        $options=[RepoWatchNativeKernel+Options]::new()
        $options.JobName='Local\RepoWatchNative-'+$id
        $options.ControllerSid=$profile.controller_sid
        $options.OutputPath=Join-Path $root ('responses/logs/'+$id+'.log')
        $options.ProgressPath=Join-Path $root ('responses/commands/'+$id+'.bound.json')
        $options.GatePath=Join-Path $root ('requests/'+$id+'.start')
        $options.CancelPath=Join-Path $root ('requests/'+$id+'.cancel')
        if(Test-Path -LiteralPath $options.CancelPath){throw 'Native check cancelled before process creation'}
        $execution.entered_kernel=$true
        $result=[RepoWatchNativeKernel]::Execute($sandboxProfile,$executable,[RepoWatchNativeKernel]::CommandLine($nativeArgv),$copy,$environment,[int]$remaining,$options)
        if($result.ActiveAfterStop -ne 0){throw 'Native job stop unverified'}
        $safeCleanup=$true
        $record.state='stopped'
        $record.exit_code=$result.ExitCode;$record.timed_out=$result.TimedOut;$record.control_stopped=$result.Cancelled
        $record.log_truncated=$result.LogTruncated;$record.log_file=$id+'.log'
        $algorithm=[Security.Cryptography.SHA256]::Create()
        try {$record.log_sha256=([BitConverter]::ToString($algorithm.ComputeHash($result.Output))).Replace('-','').ToLowerInvariant()}finally{$algorithm.Dispose()}
        $record.pid=$result.ProcessId;$record.created=$result.Created;$record.active_processes=0;$record.identity_verified=$true
    } finally {
        if($null -ne $sandboxProfile){$sandboxProfile.Dispose()}
        if($safeCleanup -or -not $execution.entered_kernel){
            try {Remove-NativeTaskTree $runRoot;$record.cleanup_pending=$false}
            catch {$record.cleanup_pending=$true}
        }else{$record.cleanup_pending=$true}
    }
    return $record
}
