param(
    [Parameter(Mandatory = $true)][string]$ConfigurationPath
)
$ErrorActionPreference = 'Stop'
$configuration = Get-Content -LiteralPath $ConfigurationPath -Raw | ConvertFrom-Json
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$report = [ordered]@{
    kind = 'native-environment-preflight'
    run_id = $configuration.run_id
    observed_at = [DateTime]::UtcNow.ToString('o')
    identity = $identity.Name
    sid = $identity.User.Value
    identity_matches = ($identity.User.Value -eq $configuration.expected_sid)
    administrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    control_canary_read = 'not_run'
    control_canary_write = 'not_run'
    native_admitted = $false
    pending = @('native_adapter', 'workspace_role_permissions', 'credential_boundary', 'control_api_boundary', 'process_tree_lifecycle', 'positive_sample')
}
if (-not $report.identity_matches -or $report.administrator) {
    $report | ConvertTo-Json -Depth 4
    exit 2
}
foreach ($mode in @('Read', 'Write')) {
    $stream = $null
    try {
        # Open an existing harmless marker without truncating or changing its bytes.
        $access = [Enum]::Parse([IO.FileAccess], $mode)
        $stream = [IO.File]::Open(
            $configuration.control_canary,
            [IO.FileMode]::Open,
            $access,
            [IO.FileShare]::ReadWrite
        )
        $report['control_canary_' + $mode.ToLowerInvariant()] = 'allowed'
    } catch [UnauthorizedAccessException] {
        $report['control_canary_' + $mode.ToLowerInvariant()] = 'denied'
    } catch [System.Management.Automation.MethodInvocationException] {
        if ($_.Exception.InnerException -is [UnauthorizedAccessException]) {
            $report['control_canary_' + $mode.ToLowerInvariant()] = 'denied'
        } else {
            # Missing files and other failures do not prove access denial.
            $report['control_canary_' + $mode.ToLowerInvariant()] = 'unverified'
        }
    } catch {
        $report['control_canary_' + $mode.ToLowerInvariant()] = 'unverified'
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
}
$report.control_directory_probe_passed = (
    $report.control_canary_read -eq 'denied' -and
    $report.control_canary_write -eq 'denied'
)
$report | ConvertTo-Json -Depth 4
if (-not $report.control_directory_probe_passed) { exit 3 }
