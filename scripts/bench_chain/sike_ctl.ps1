# sike_ctl.ps1 - pause / resume / status / launch control for the SIKE bedsweep.
#
#   .\sike_ctl.ps1 pause     # ask the running sweep to stop cleanly (checkpoint saved)
#   .\sike_ctl.ps1 resume    # clear the pause flag (then run 'launch' to continue)
#   .\sike_ctl.ps1 status    # show chain state + per-bed rung progress
#   .\sike_ctl.ps1 launch    # start the sweep detached (auto-resumes any checkpoints)
#
# The sweep checkpoints after every consumer rung, so pausing (or a hard kill)
# loses at most the single in-flight rung; 'launch' with --resume in the runner
# picks up from the next unfinished rung. Pure ASCII, PowerShell 5.1-safe.
param([Parameter(Position = 0)][ValidateSet('pause', 'resume', 'status', 'launch')][string]$cmd = 'status')

$repo = 'F:\Projects\helix-context'
$logs = "$repo\benchmarks\logs"
$results = "$repo\benchmarks\results"
$pauseFlag = "$logs\sike_pause.flag"
$sweep = "$repo\scripts\bench_chain\s2_sike_bed_sweep.ps1"
$beds = @('xl', 'enterprise_rag_10k', 'enterprise_rag_50k')

switch ($cmd) {
    'pause' {
        New-Item -ItemType File -Force -Path $pauseFlag | Out-Null
        "PAUSE requested. The sweep exits cleanly between needles/rungs (may take" | Write-Output
        "up to one needle on the current rung). Checkpoints are in $results." | Write-Output
        "Resume with:  .\sike_ctl.ps1 resume  ;  .\sike_ctl.ps1 launch" | Write-Output
    }
    'resume' {
        if (Test-Path $pauseFlag) { Remove-Item $pauseFlag -Force; "pause flag cleared." | Write-Output }
        else { "no pause flag set." | Write-Output }
        "Now run:  .\sike_ctl.ps1 launch   (it will --resume every checkpoint)." | Write-Output
    }
    'status' {
        "paused: $([bool](Test-Path $pauseFlag))" | Write-Output
        $cs = "$logs\chain_status.json"
        if (Test-Path $cs) { "chain_status: " + ((Get-Content $cs -Raw | ConvertFrom-Json).state) | Write-Output }
        foreach ($b in $beds) {
            $j = "$results\sike_bedsweep_$b.json"
            if (Test-Path $j) {
                try {
                    $d = Get-Content $j -Raw | ConvertFrom-Json
                    $done = @($d.progress.rungs_done).Count
                    $gd = $d.retrieval.gold_delivered_rate
                    $flag = if ($d.complete) { 'COMPLETE' } else { 'partial ' }
                    "  {0,-20} {1}  rungs_done={2,-2}  gold_delivered_rate={3}" -f $b, $flag, $done, $gd | Write-Output
                }
                catch { "  {0,-20} (unreadable checkpoint)" -f $b | Write-Output }
            }
            else { "  {0,-20} not started" -f $b | Write-Output }
        }
    }
    'launch' {
        if (Test-Path $pauseFlag) {
            "REFUSING to launch: pause flag is still set. Run 'resume' first." | Write-Output; return
        }
        Start-Process -FilePath 'powershell' -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $sweep -WindowStyle Hidden
        "sweep launched detached. Watch with:  .\sike_ctl.ps1 status" | Write-Output
    }
}
