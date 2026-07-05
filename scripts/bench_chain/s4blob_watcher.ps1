# Final-stage watcher (2026-07-02): waits until BOTH conditions hold, then
# runs the repointed S4 (ERB 500-q scored on the BLOB fixture F:\tmp\erb_blob.db):
#   1. chain_status.json state == COMPLETE-ALL(s2+s3b-rerun-done)
#      (i.e. the s2/s3b rerun watcher has finished and the rig is free)
#   2. claude auth works (probe succeeds) -- the first S4 attempt burned
#      74 min on 401s; this watcher self-resumes the moment Max re-auths.
$ErrorActionPreference = 'Continue'
$repo = 'F:\Projects\helix-context'
$logs = "$repo\benchmarks\logs"
$status = "$logs\chain_status.json"
"s4blob watcher started $(Get-Date -Format o) pid=$PID" | Set-Content "$logs\s4blob_watcher.log"
$PID | Set-Content "$logs\s4blob_watcher.pid"

function Test-ClaudeAuth {
    try {
        $probe = & claude -p --model sonnet --tools "" --max-budget-usd 0.02 `
            --output-format json -- "Reply with exactly: OK" 2>$null
        $aj = $probe | ConvertFrom-Json
        return (-not $aj.is_error)
    } catch { return $false }
}

$rigFree = $false
$authOk = $false
while (-not ($rigFree -and $authOk)) {
    Start-Sleep -Seconds 120
    if (-not $rigFree) {
        try {
            $s = Get-Content $status -Raw | ConvertFrom-Json
            if ($s.state -like 'COMPLETE-ALL*') { $rigFree = $true;
                "rig free at $(Get-Date -Format o)" | Add-Content "$logs\s4blob_watcher.log" }
        } catch {}
    }
    if ($rigFree -and -not $authOk) {
        $authOk = Test-ClaudeAuth
        if (-not $authOk) {
            $w = @{stage='s4_blob'; state='WAITING-claude-auth'; at=(Get-Date -Format o)} | ConvertTo-Json
            $w | Set-Content $status
        } else {
            "claude auth OK at $(Get-Date -Format o)" | Add-Content "$logs\s4blob_watcher.log"
        }
    }
}

"launching blob S4 $(Get-Date -Format o)" | Add-Content "$logs\s4blob_watcher.log"
& powershell -NoProfile -ExecutionPolicy Bypass -File "$repo\scripts\bench_chain\s4_erb500k_scored.ps1"
"s4 blob exit=$LASTEXITCODE $(Get-Date -Format o)" | Add-Content "$logs\s4blob_watcher.log"

$done = @{stage='chain'; state='COMPLETE-FINAL(all-stages+blob-s4)'; at=(Get-Date -Format o)} | ConvertTo-Json
$done | Set-Content $status
"s4blob watcher done $(Get-Date -Format o)" | Add-Content "$logs\s4blob_watcher.log"
