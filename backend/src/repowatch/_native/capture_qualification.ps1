param([Parameter(Mandatory = $true)][string]$ControllerSid)
$ErrorActionPreference='Stop'
if(-not ('RepoWatchNativeKernel' -as [type])){Add-Type -Path (Join-Path $PSScriptRoot 'kernel.cs')}
$runId=[Guid]::NewGuid().ToString('N')
$directory=Join-Path $env:PUBLIC ('Documents/RepoWatchNativeCaptureProbe-' + $runId)
$workspace=Join-Path $directory 'workspace'
$sandboxProfile=[RepoWatchNativeKernel+Profile]::new('RepoWatch.Capture.'+$runId)
try {
    $sid=[Security.Principal.SecurityIdentifier]::new($sandboxProfile.Sid)
    $controller=[Security.Principal.WindowsIdentity]::GetCurrent().User
    New-Item -ItemType Directory -Path $directory,$workspace | Out-Null
    $inherit=[Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $acl=[Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($controller);$acl.SetAccessRuleProtection($true,$false)
    foreach($actor in @($controller,[Security.Principal.SecurityIdentifier]::new('S-1-5-18'),[Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($actor,'FullControl',$inherit,'None','Allow'))
    }
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'ReadAndExecute','None','None','Allow'))
    Set-Acl -LiteralPath $directory -AclObject $acl
    $workAcl=Get-Acl -LiteralPath $workspace
    $workAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'Modify',$inherit,'None','Allow'))
    Set-Acl -LiteralPath $workspace -AclObject $workAcl
    & icacls.exe $workspace /setintegritylevel '(OI)(CI)L' | Out-Null
    if($LASTEXITCODE -ne 0){throw 'Low integrity workspace setup failed'}
    $program=Join-Path $workspace 'ArgProbe.exe'
    Add-Type -TypeDefinition 'using System;using System.IO;using System.Text;public class ArgProbe{public static void Main(string[] args){Console.OutputEncoding=new UTF8Encoding(false);if(args.Length==2 && args[0]=="--marker")File.WriteAllText(args[1],"executed");Console.WriteLine(String.Join("|",args));Console.Error.WriteLine("stderr");}}' -OutputAssembly $program -OutputType ConsoleApplication
    $environmentBlock="LOCALAPPDATA=$env:LOCALAPPDATA`0SystemRoot=$env:SystemRoot`0TEMP=$workspace`0TMP=$workspace`0USERPROFILE=$env:USERPROFILE`0WINDIR=$env:SystemRoot`0`0"
    $options=[RepoWatchNativeKernel+Options]::new()
    $options.JobName='Local\RepoWatchNative-'+[Guid]::NewGuid().ToString('N')
    $options.ControllerSid=$ControllerSid
    $options.OutputPath=Join-Path $directory 'capture.log'
    $command=[RepoWatchNativeKernel]::CommandLine([string[]]@($program,'space value','quote"value','ending\',''))
    $result=[RepoWatchNativeKernel]::Execute($sandboxProfile,$program,$command,$workspace,$environmentBlock,10000,$options)
    $output=[Text.Encoding]::UTF8.GetString($result.Output)
    if($result.ExitCode -ne 0 -or $result.ActiveAfterStop -ne 0 -or $result.LogTruncated -or -not $output.Contains('space value|quote"value|ending\|') -or -not $output.Contains('stderr')) {throw 'Captured output or argument quoting failed'}
    $marker=Join-Path $workspace 'must-not-execute.txt'
    $options.JobName='Local\RepoWatchNative-'+[Guid]::NewGuid().ToString('N')
    $options.OutputPath=Join-Path $directory 'gate.log'
    $options.GatePath=Join-Path $directory 'gate-not-present'
    $command=[RepoWatchNativeKernel]::CommandLine([string[]]@($program,'--marker',$marker))
    $blocked=[RepoWatchNativeKernel]::Execute($sandboxProfile,$program,$command,$workspace,$environmentBlock,100,$options)
    if(-not $blocked.TimedOut -or $blocked.ActiveAfterStop -ne 0 -or (Test-Path -LiteralPath $marker)){throw 'Missing startup gate did not refuse execution'}
    $options.JobName='Local\RepoWatchNative-'+[Guid]::NewGuid().ToString('N')
    $options.OutputPath=Join-Path $directory 'cancel.log'
    New-Item -ItemType File -Path $options.GatePath | Out-Null
    $options.CancelPath=Join-Path $directory 'cancel-present'
    New-Item -ItemType File -Path $options.CancelPath | Out-Null
    $cancelled=[RepoWatchNativeKernel]::Execute($sandboxProfile,$program,$command,$workspace,$environmentBlock,10000,$options)
    if(-not $cancelled.Cancelled -or $cancelled.ActiveAfterStop -ne 0 -or (Test-Path -LiteralPath $marker)){throw 'Pre-cancelled job crossed its startup gate'}
    $report=@{passed=$true;native_admitted=$false;checks=@('explicit_stdio_capture','windows_argument_quoting','job_resource_configuration','missing_gate_prevents_execution','cancel_before_gate_prevents_execution','all_job_members_stopped')}
    $report | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $directory 'report.json') -Encoding UTF8
    $report | ConvertTo-Json -Compress
} finally {$sandboxProfile.Dispose()}
