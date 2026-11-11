# Qualification-only bridge. Requests cannot supply commands, paths or scripts.
param([int]$SessionSeconds = 1800)
$ErrorActionPreference = 'Stop'
if ($SessionSeconds -lt 1 -or $SessionSeconds -gt 1800) { throw 'Session duration must be within 1..1800 seconds' }
$configurationPath = Join-Path $PSScriptRoot 'configuration.json'
$configuration = Get-Content -LiteralPath $configurationPath -Raw | ConvertFrom-Json
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if ($identity.User.Value -ne $configuration.expected_sid) { throw 'Unexpected session identity' }
if ([Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator identity refused' }
$inbox = Join-Path $PSScriptRoot 'requests'
$outbox = Join-Path $PSScriptRoot 'responses'
if (-not (Test-Path -LiteralPath $inbox -PathType Container) -or -not (Test-Path -LiteralPath $outbox -PathType Container)) { throw 'Prepared request/response directories are required' }
$ready = [ordered]@{ protocol='native-qualification-v1'; run_id=$configuration.run_id; identity=$identity.Name; sid=$identity.User.Value; process_id=$PID; created_at=[DateTime]::UtcNow.ToString('o'); native_admitted=$false }
$readyPath = Join-Path $outbox ('ready-' + $PID + '.json')
$ready | ConvertTo-Json | Set-Content -LiteralPath $readyPath -Encoding UTF8
$deadline = [DateTime]::UtcNow.AddSeconds($SessionSeconds)
Write-Output 'Qualification session ready (maximum 30 minutes). Keep this window open; Ctrl+C stops waiting.'
try {
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath (Join-Path $inbox 'stop.json')) { break }
        foreach ($requestFile in @(Get-ChildItem -LiteralPath $inbox -Filter 'request-*.json' -File | Sort-Object Name)) {
            if ([DateTime]::UtcNow -ge $deadline) { break }
            if ($requestFile.Name -notmatch '^request-([a-f0-9]{32})\.json$') { continue }
            $requestId = $Matches[1]
            $resultPath = Join-Path $outbox ('result-' + $requestId + '.json')
            if (Test-Path -LiteralPath $resultPath) { continue }
            $result = [ordered]@{ protocol='native-qualification-v1'; request_id=$requestId; identity=$identity.Name; sid=$identity.User.Value; observed_at=[DateTime]::UtcNow.ToString('o'); passed=$false; native_admitted=$false }
            try {
                if ($requestFile.Length -gt 2048) { throw 'Oversized request refused' }
                $request = Get-Content -LiteralPath $requestFile.FullName -Raw | ConvertFrom-Json
                $keys = @($request.PSObject.Properties.Name | Sort-Object)
                if (($keys -join ',') -ne 'operation,protocol,request_id' -or $request.request_id -cne $requestId -or $request.protocol -cne 'native-qualification-v1' -or $request.operation -cne 'qualify') { throw 'Only a fixed qualification request is permitted' }
                $start = [Diagnostics.ProcessStartInfo]::new()
                $start.FileName = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
                $start.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'isolation.ps1') + '" -ConfigurationPath "' + $configurationPath + '"'
                $start.UseShellExecute = $false
                $start.CreateNoWindow = $true
                $start.RedirectStandardOutput = $true
                $start.RedirectStandardError = $true
                $child = [Diagnostics.Process]::Start($start)
                try {
                    # Both streams drain concurrently; diagnostics cannot fill a pipe and stall cleanup.
                    $stdout = $child.StandardOutput.ReadToEndAsync()
                    $stderr = $child.StandardError.ReadToEndAsync()
                    $remaining = [Math]::Max(1,[Math]::Min(30000,[int]($deadline - [DateTime]::UtcNow).TotalMilliseconds))
                    if (-not $child.WaitForExit($remaining)) {
                        $child.Kill()
                        if (-not $child.WaitForExit(5000)) { throw 'Qualification helper stop unverified' }
                        throw 'Qualification helper exceeded its finite timeout'
                    }
                    if ($child.ExitCode -ne 0) { throw ('Qualification helper failed with exit code ' + $child.ExitCode) }
                    $proof = $stdout.Result | ConvertFrom-Json
                    if ($proof.kind -ne 'native-appcontainer-experiment' -or $proof.native_admitted -or -not $proof.passed -or $proof.identity -ine $identity.Name) { throw 'Invalid qualification proof' }
                    $result.proof = $proof
                    $result.passed = $true
                } finally {
                    try {
                        if (-not $child.HasExited) {
                            $child.Kill()
                            if (-not $child.WaitForExit(5000)) { throw 'Helper cleanup unverified' }
                        }
                    } finally { $child.Dispose() }
                }
            } catch {
                # Return a bounded diagnostic, never the full external process output.
                $result.error = $_.Exception.Message.Substring(0, [Math]::Min(300,$_.Exception.Message.Length))
            }
            $temporary = Join-Path $outbox ('result-' + $requestId + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
            $result | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $temporary -Encoding UTF8
            [IO.File]::Move($temporary,$resultPath)
            Write-Output ('Request ' + $requestId + ': passed=' + $result.passed)
        }
        Start-Sleep -Milliseconds 200
    }
} finally {
    $ready.stopped_at = [DateTime]::UtcNow.ToString('o')
    $ready | ConvertTo-Json | Set-Content -LiteralPath $readyPath -Encoding UTF8
}
