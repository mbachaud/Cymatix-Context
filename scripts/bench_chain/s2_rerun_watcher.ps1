# Waits for the main chain to COMPLETE, then re-runs the repaired S2
# (SIKE bed-sweep) that was killed after the 2026-07-02 ollama-list hang.
# Serial discipline: never runs S2 while S3/S4 own the rig.
$ErrorActionPreference = 'Continue'
$repo = 'F:\Projects\helix-context'
$logs = "$repo\benchmarks\logs"
$status = "$logs\chain_status.json"
"s2 rerun watcher started $(Get-Date -Format o) pid=$PID" | Set-Content "$logs\s2_watcher.log"
$PID | Set-Content "$logs\s2_watcher.pid"

while ($true) {
    Start-Sleep -Seconds 60
    if (-not (Test-Path $status)) { continue }
    try { $s = Get-Content $status -Raw | ConvertFrom-Json } catch { continue }
    if ($s.state -eq 'COMPLETE') { break }
}

"chain COMPLETE detected; starting repaired S2 $(Get-Date -Format o)" | Add-Content "$logs\s2_watcher.log"
$run = @{stage='s2_rerun'; state='starting'; at=(Get-Date -Format o)} | ConvertTo-Json
$run | Set-Content $status

& powershell -NoProfile -ExecutionPolicy Bypass -File "$repo\scripts\bench_chain\s2_sike_bed_sweep.ps1"
"s2 rerun exit=$LASTEXITCODE $(Get-Date -Format o)" | Add-Content "$logs\s2_watcher.log"

& powershell -NoProfile -ExecutionPolicy Bypass -File "$repo\scripts\bench_chain\s3b_erb_sweep_rerun.ps1"
"s3b erb sweep rerun exit=$LASTEXITCODE $(Get-Date -Format o)" | Add-Content "$logs\s2_watcher.log"

$done = @{stage='chain'; state='COMPLETE-ALL(s2+s3b-rerun-done)'; at=(Get-Date -Format o)} | ConvertTo-Json
$done | Set-Content $status
"watcher done $(Get-Date -Format o)" | Add-Content "$logs\s2_watcher.log"
