# Watcher (2026-07-03): waits for the SIKE+203 sequencer to finish its
# #203 sweeps, then reruns the FIXED S2 bed-sweep (lexical-config serving,
# no GPU contention with the ollama ladder). Final state: COMPLETE-FINAL.
$ErrorActionPreference = 'Continue'
$repo = 'F:\Projects\helix-context'
$logs = "$repo\benchmarks\logs"
$status = "$logs\chain_status.json"
"s2-after-203 watcher started $(Get-Date -Format o) pid=$PID" | Set-Content "$logs\s2after203_watcher.log"
$PID | Set-Content "$logs\s2after203.pid"

while ($true) {
    Start-Sleep -Seconds 120
    try { $s = Get-Content $status -Raw | ConvertFrom-Json } catch { continue }
    if ($s.state -like 'COMPLETE-SIKE-203*') { break }
}
"203 done; rerunning fixed S2 $(Get-Date -Format o)" | Add-Content "$logs\s2after203_watcher.log"
$run = @{stage='s2_rerun2'; state='starting'; at=(Get-Date -Format o)} | ConvertTo-Json
$run | Set-Content $status

& powershell -NoProfile -ExecutionPolicy Bypass -File "$repo\scripts\bench_chain\s2_sike_bed_sweep.ps1"
"s2 rerun2 exit=$LASTEXITCODE $(Get-Date -Format o)" | Add-Content "$logs\s2after203_watcher.log"

$done = @{stage='chain'; state='COMPLETE-FINAL (SIKE fixed + 203 n100; erb500k skipped)'; at=(Get-Date -Format o)} | ConvertTo-Json
$done | Set-Content $status
"watcher done $(Get-Date -Format o)" | Add-Content "$logs\s2after203_watcher.log"
