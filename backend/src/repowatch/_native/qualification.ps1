param([Parameter(Mandatory = $true)][string]$ConfigurationPath)
$ErrorActionPreference = 'Stop'
$configuration = Get-Content -LiteralPath $ConfigurationPath -Raw | ConvertFrom-Json
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if ($identity.User.Value -ne $configuration.expected_sid) { throw 'Unexpected execution identity' }
if ([Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run the experiment under an ordinary, unelevated identity'
}
if (-not ('RepoWatchNativeKernel' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'kernel.cs') }
$runId = [Guid]::NewGuid().ToString('N')
$directory = Join-Path $env:PUBLIC ('Documents/RepoWatchAppContainerProbe-' + $runId)
$workspace = Join-Path $directory 'workspace'
$sourceDirectory = Join-Path $directory 'read-only-source'
$profile = $null
$listener = $null
$credentialTarget = 'RepoWatch.NativeCanary.' + $runId
$credentialCreated = $false
$environmentCanary = 'REPOWATCH_NATIVE_CANARY_' + $runId
function Assert-UnauthenticatedControlApi($url) {
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3 -MaximumRedirection 0
        throw 'Unauthenticated control API unexpectedly succeeded'
    } catch [Net.WebException] {
        if ($null -eq $_.Exception.Response -or [int]$_.Exception.Response.StatusCode -ne 401) { throw }
        $_.Exception.Response.Close()
        return 401
    }
}
$report = [ordered]@{
    kind='native-appcontainer-experiment'; run_id=$runId
    identity=$identity.Name; observed_at=[DateTime]::UtcNow.ToString('o')
    scope='Native AppContainer qualification: file boundaries, fake credential/environment/loopback canaries and bounded timeout; no product backend'
    passed=$false; native_admitted=$false
    pending=@('product_native_adapter','real_control_api_boundary','crash_and_descendant_recovery','full_product_sample')
}
try {
    $profile = [RepoWatchNativeKernel+Profile]::new('RepoWatch.Probe.' + $runId)
    $packageSid = [Security.Principal.SecurityIdentifier]::new($profile.Sid)
    New-Item -ItemType Directory -Path $directory | Out-Null
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($identity.User)
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $none = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    foreach ($sid in @($identity.User, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'), [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl',$inherit,$none,$allow))
    }
    # Traversal of the experiment root does not grant access to its marker files.
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($packageSid,'ReadAndExecute','None',$none,$allow))
    Set-Acl -LiteralPath $directory -AclObject $acl
    New-Item -ItemType Directory -Path $workspace,$sourceDirectory | Out-Null
    foreach ($entry in @(@($workspace,'Modify'),@($sourceDirectory,'ReadAndExecute'))) {
        $childAcl = Get-Acl -LiteralPath $entry[0]
        $childAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($packageSid,$entry[1],$inherit,$none,$allow))
        Set-Acl -LiteralPath $entry[0] -AclObject $childAcl
    }
    & icacls.exe $workspace /setintegritylevel '(OI)(CI)L' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot set the owned workspace low-integrity label' }
    $outside = Join-Path $directory 'outside-marker.txt'
    $source = Join-Path $sourceDirectory 'source-marker.txt'
    [IO.File]::WriteAllText($outside,'Harmless same-user marker outside the allowed workspace.')
    [IO.File]::WriteAllText($source,'Harmless read-only source marker.')
    # The parent can access both markers; missing/unreadable fixtures cannot count as isolation.
    $outsideHash = (Get-FileHash -LiteralPath $outside).Hash
    $sourceHash = (Get-FileHash -LiteralPath $source).Hash
    $baseline = [IO.File]::Open($outside,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite)
    $baseline.Dispose()
    $batch = @"
@echo off
setlocal
set /a probe_result=2+3 >nul
if errorlevel 1 exit /b 89
echo %probe_result% > result.txt
if errorlevel 1 exit /b 90
copy /y "$outside" outside-read.txt >nul 2>&1
if not errorlevel 1 exit /b 91
copy /y result.txt "$outside" >nul 2>&1
if not errorlevel 1 exit /b 92
copy /y "$source" source-read.txt >nul 2>&1
if errorlevel 1 exit /b 93
copy /y result.txt "$source" >nul 2>&1
if not errorlevel 1 exit /b 94
exit /b 0
"@
    $batchPath = Join-Path $workspace 'probe.cmd'
    [IO.File]::WriteAllText($batchPath,$batch,[Text.Encoding]::ASCII)
    $executable = Join-Path $env:SystemRoot 'System32/cmd.exe'
    $localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    $userProfile = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    $environmentBlock = "ComSpec=$executable`0LOCALAPPDATA=$localAppData`0PATH=$env:SystemRoot\System32`0SystemRoot=$env:SystemRoot`0TEMP=$workspace`0TMP=$workspace`0USERPROFILE=$userProfile`0WINDIR=$env:SystemRoot`0`0"
    $code = [RepoWatchNativeKernel]::Run($profile,$executable,('"' + $executable + '" /d /c probe.cmd'),$workspace,$environmentBlock,10000)
    if ($code -ne 0) { throw ('AppContainer child failed, exit code ' + $code) }
    if ([IO.File]::ReadAllText((Join-Path $workspace 'result.txt')).Trim() -ne '5') { throw 'Native arithmetic sample failed' }
    if ([IO.File]::ReadAllText((Join-Path $workspace 'source-read.txt')).Trim() -ne [IO.File]::ReadAllText($source)) { throw 'Read-only source was not actually read' }
    if ((Get-FileHash -LiteralPath $outside).Hash -ne $outsideHash -or (Get-FileHash -LiteralPath $source).Hash -ne $sourceHash) { throw 'A protected marker changed' }
    $securityExecutable = Join-Path $sourceDirectory 'SecurityProbe.exe'
    Add-Type -Path (Join-Path $PSScriptRoot 'security_probe.cs') -OutputAssembly $securityExecutable -OutputType ConsoleApplication
    & cmdkey.exe ('/generic:' + $credentialTarget) '/user:RepoWatchNativeCanary' '/pass:non-secret-native-canary' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the unique fake credential' }
    $credentialCreated = $true
    [Environment]::SetEnvironmentVariable($environmentCanary,'non-secret-native-canary','Process')
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $securityReport = Join-Path $workspace 'security-report.json'
    & $securityExecutable 'baseline' $credentialTarget $port $environmentCanary $securityReport
    if ($LASTEXITCODE -ne 0) { throw 'Ordinary parent could not access the fake credential, environment or loopback listener' }
    $baselineClient = $listener.AcceptTcpClient()
    $baselineClient.Dispose()
    $securityCommand = '"' + $securityExecutable + '" isolated ' + $credentialTarget + ' ' + $port + ' ' + $environmentCanary + ' "' + $securityReport + '"'
    $controlUri = $null
    if ($configuration.control_url) {
        $controlUri = [Uri]::new($configuration.control_url)
        if ($controlUri.Scheme -ne 'http' -or $controlUri.Host -ne '127.0.0.1' -or $controlUri.AbsolutePath -ne '/api/v1/state' -or $controlUri.Query -or $controlUri.UserInfo) { throw 'Controlled loopback API URL required' }
        $report.control_api_status_before = Assert-UnauthenticatedControlApi $controlUri.AbsoluteUri
        $securityCommand += ' ' + $controlUri.Port
    }
    $securityCode = [RepoWatchNativeKernel]::Run($profile,$securityExecutable,$securityCommand,$workspace,$environmentBlock,10000)
    if ($securityCode -ne 0) { throw ('Isolated security canaries failed: ' + $securityCode) }
    $securityResult = Get-Content -LiteralPath $securityReport -Raw | ConvertFrom-Json
    if (-not $securityResult.passed) { throw 'Security report did not pass' }
    if ($listener.Pending()) { throw 'The isolated child reached the canary listener' }
    & $securityExecutable 'baseline' $credentialTarget $port $environmentCanary $securityReport
    if ($LASTEXITCODE -ne 0) { throw 'Ordinary parent baseline failed after the isolated attempt' }
    $baselineClient = $listener.AcceptTcpClient()
    $baselineClient.Dispose()
    $report.security_canaries = $securityResult
    $report.isolated_loopback_connection_observed = $false
    $report.ordinary_parent_before_after_baselines = $true
    if ($null -ne $controlUri) {
        $report.control_api_status_after = Assert-UnauthenticatedControlApi $controlUri.AbsoluteUri
        $report.pending = @($report.pending | Where-Object { $_ -ne 'real_control_api_boundary' })
    }
    $timedOut = $false
    try {
        [RepoWatchNativeKernel]::Run($profile,$executable,('"' + $executable + '" /d /c "for /l %i in (1,1,2147483647) do @rem waiting"'),$workspace,$environmentBlock,100) | Out-Null
    } catch {
        $cause = $_.Exception
        while ($null -ne $cause.InnerException) { $cause = $cause.InnerException }
        if ($cause -isnot [TimeoutException]) { throw }
        $timedOut = $true
    }
    if (-not $timedOut) { throw 'Long-running child was not bounded by its timeout' }
    $stillRunning = $false
    try {
        $process = [Diagnostics.Process]::GetProcessById([RepoWatchNativeKernel]::LastProcessId)
        try { $stillRunning = (-not $process.HasExited -and $process.StartTime.ToUniversalTime().ToFileTimeUtc() -eq [RepoWatchNativeKernel]::LastCreationTime) }
        finally { $process.Dispose() }
    } catch [ArgumentException] { }
    if ($stillRunning) { throw 'Timed-out child identity still exists' }
    if ([RepoWatchNativeKernel]::LastJobActiveAfterStop -ne 0) { throw 'Timed-out job still has active members' }
    $report.timed_out_child_stopped = $true
    $crashBatchPath = Join-Path $workspace 'crash-tree.cmd'
    $crashBatch = @'
@echo off
start "" /b "%ComSpec%" /d /c "for /l %%i in (1,1,2147483647) do @rem descendant"
if errorlevel 1 exit /b 101
echo started > crash-tree-started.txt
for /l %%i in (1,1,2147483647) do @rem root
'@
    [IO.File]::WriteAllText($crashBatchPath,$crashBatch,[Text.Encoding]::ASCII)
    $jobName = 'Local\RepoWatchProbe-' + $runId
    $progressPath = Join-Path $directory 'crash-child-identity.json'
    $ownerConfigurationPath = Join-Path $directory 'crash-owner-configuration.json'
    @{ profile_name=$profile.Name; executable=$executable; command=('"' + $executable + '" /d /c crash-tree.cmd'); workspace=$workspace; environment=$environmentBlock; progress_path=$progressPath; job_name=$jobName } | ConvertTo-Json | Set-Content -LiteralPath $ownerConfigurationPath -Encoding UTF8
    $ownerStart = [Diagnostics.ProcessStartInfo]::new()
    $ownerStart.FileName = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
    $ownerStart.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'crash_owner.ps1') + '" -ConfigurationPath "' + $ownerConfigurationPath + '"'
    $ownerStart.UseShellExecute=$false
    $ownerStart.CreateNoWindow=$true
    $ownerStart.RedirectStandardOutput=$true
    $ownerStart.RedirectStandardError=$true
    $ownerProcess = [Diagnostics.Process]::Start($ownerStart)
    try {
        $ownerOut = $ownerProcess.StandardOutput.ReadToEndAsync()
        $ownerErr = $ownerProcess.StandardError.ReadToEndAsync()
        $startDeadline = [DateTime]::UtcNow.AddSeconds(8)
        $startedMarker = Join-Path $workspace 'crash-tree-started.txt'
        while ((-not (Test-Path -LiteralPath $progressPath) -or -not (Test-Path -LiteralPath $startedMarker)) -and [DateTime]::UtcNow -lt $startDeadline -and -not $ownerProcess.HasExited) { Start-Sleep -Milliseconds 20 }
        if (-not (Test-Path -LiteralPath $progressPath) -or -not (Test-Path -LiteralPath $startedMarker) -or $ownerProcess.HasExited) { throw 'Owner-crash fixture did not reach its active process-tree baseline' }
        $counts = [RepoWatchNativeKernel]::JobCounts($jobName)
        if ($null -eq $counts -or $counts[0] -lt 2 -or $counts[1] -lt 2) { throw 'Owner-crash fixture did not create an active descendant' }
        $childIdentity = Get-Content -LiteralPath $progressPath -Raw | ConvertFrom-Json
        $ownerProcess.Kill()
        if (-not $ownerProcess.WaitForExit(5000)) { throw 'Job owner termination unverified' }
        $stopDeadline = [DateTime]::UtcNow.AddSeconds(5)
        do {
            $remainingCounts = [RepoWatchNativeKernel]::JobCounts($jobName)
            if ($null -eq $remainingCounts -or $remainingCounts[0] -eq 0) { break }
            Start-Sleep -Milliseconds 20
        } while ([DateTime]::UtcNow -lt $stopDeadline)
        if ($null -ne $remainingCounts -and $remainingCounts[0] -ne 0) { throw 'Descendants survived abrupt job-owner death' }
        $sameChildAlive=$false
        try {
            $childProcess=[Diagnostics.Process]::GetProcessById($childIdentity.pid)
            try { $sameChildAlive=(-not $childProcess.HasExited -and $childProcess.StartTime.ToUniversalTime().ToFileTimeUtc() -eq $childIdentity.created) }
            finally { $childProcess.Dispose() }
        } catch [ArgumentException] { }
        if ($sameChildAlive) { throw 'Recorded native child survived job-owner death' }
        $report.owner_crash = @{ active_members_before=$counts[0]; total_members_before=$counts[1]; active_members_after=0; recorded_child_stopped=$true }
        $report.pending = @($report.pending | Where-Object { $_ -ne 'crash_and_descendant_recovery' })
    } finally {
        try { if (-not $ownerProcess.HasExited) { $ownerProcess.Kill(); $ownerProcess.WaitForExit(5000) | Out-Null } }
        finally { $ownerProcess.Dispose() }
    }
    $report.checks=@('actual_appcontainer_token','no_inherited_handles','explicit_environment','suspended_startup_job_gate','native_result_5','workspace_writable','same_user_outside_read_denied','same_user_outside_write_denied','source_read_allowed','source_write_denied','protected_markers_unchanged')
    $report.checks += @('ordinary_parent_canary_baseline','fake_credential_unreadable','parent_environment_not_inherited','loopback_canary_not_reached_with_live_before_after_baselines','timeout_child_stopped_with_creation_time_check')
    $report.checks += @('abrupt_job_owner_death_stops_native_root_and_descendants')
    if ($null -ne $controlUri) { $report.checks += @('real_control_api_unauthenticated_401_before_and_after','appcontainer_cannot_connect_to_live_control_api') }
    $report.passed=$true
} finally {
    if ($null -ne $listener) { $listener.Stop() }
    [Environment]::SetEnvironmentVariable($environmentCanary,$null,'Process')
    $credentialCleanupFailed = $false
    if ($credentialCreated) {
        & cmdkey.exe ('/delete:' + $credentialTarget) | Out-Null
        $credentialCleanupFailed = ($LASTEXITCODE -ne 0)
    }
    try {
        if ($null -ne $profile) { $profile.Dispose() }
    } catch {
        $report.passed=$false
        throw
    } finally {
        if ($credentialCleanupFailed) { $report.passed=$false }
        if (Test-Path -LiteralPath $directory) {
            $report | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $directory 'report.json') -Encoding UTF8
        }
    }
    if ($credentialCleanupFailed) { throw 'Fake credential cleanup failed' }
}
$report | ConvertTo-Json -Depth 4
