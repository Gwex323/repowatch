param([Parameter(Mandatory=$true)][string]$Root,[Parameter(Mandatory=$true)][string]$ExecutionSid)
$ErrorActionPreference='Stop'
$env:PSModulePath=Join-Path $PSHOME 'Modules'
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1')
$actor=[Security.Principal.WindowsIdentity]::GetCurrent().User
$runner=[Security.Principal.SecurityIdentifier]::new($ExecutionSid)
if($actor.Value -eq $runner.Value){throw 'A dedicated execution account is required'}
$null=$runner.Translate([Security.Principal.NTAccount])
if(Test-Path -LiteralPath $Root){throw 'A native host profile must use a new directory'}
$inherit=[Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
$acl=[Security.AccessControl.DirectorySecurity]::new()
$acl.SetOwner($actor);$acl.SetAccessRuleProtection($true,$false)
foreach($identity in @($actor,[Security.Principal.SecurityIdentifier]::new('S-1-5-18'),[Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))){
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($identity,'FullControl',$inherit,'None','Allow'))
}
$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($runner,'ReadAndExecute',$inherit,'None','Allow'))
# Create with the restricted ACL before copying code or runtime files.
$directory=[IO.DirectoryInfo]::new($Root)
$directory.Create($acl)
foreach($name in @('host','requests','responses','workspaces','runtime')){New-Item -ItemType Directory -Path (Join-Path $Root $name) | Out-Null}
$response=Join-Path $Root 'responses'
$writable=Get-Acl -LiteralPath $response
$writable.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($runner,'Modify',$inherit,'None','Allow'))
Set-Acl -LiteralPath $response -AclObject $writable
foreach($name in @('commands','logs')){New-Item -ItemType Directory -Path (Join-Path $response $name) | Out-Null}
