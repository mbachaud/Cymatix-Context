# Bench chain 2026-07-01 - serial stages per goal-gates run discipline.
# S1 3-arm ContextBench -> S2 SIKE bed-sweep (#221) -> S3 dense sweep (#203)
# -> S4 ERB 500-q scored @ June-11 500K fixture (#93).
# Every stage logs to benchmarks/logs and updates chain_status.json.
$ErrorActionPreference = 'Continue'
$repo = 'F:\Projects\helix-context'
$chain = "$repo\scripts\bench_chain"
$logs = "$repo\benchmarks\logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
"chain started $(Get-Date -Format o) pid=$PID" | Set-Content "$logs\chain_runner.log"
$PID | Set-Content "$logs\chain.pid"

# Load annotation (method-notes discipline): record rig load at chain start.
try {
    $gpu = nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader
    $cpu = (Get-CimInstance Win32_Processor).LoadPercentage
    "load at start: GPU $gpu / CPU $cpu pct" | Add-Content "$logs\chain_runner.log"
} catch {}

function Run-Stage {
    param([string]$name, [string]$script)
    if (-not (Test-Path $script)) {
        "SKIP $name - $script not found $(Get-Date -Format o)" | Add-Content "$logs\chain_runner.log"
        return
    }
    "START $name $(Get-Date -Format o)" | Add-Content "$logs\chain_runner.log"
    & powershell -NoProfile -ExecutionPolicy Bypass -File $script
    "END $name exit=$LASTEXITCODE $(Get-Date -Format o)" | Add-Content "$logs\chain_runner.log"
}

Run-Stage -name 's1_3arm' -script "$chain\s1_contextbench_3arm.ps1"
Run-Stage -name 's2_sike_beds' -script "$chain\s2_sike_bed_sweep.ps1"
Run-Stage -name 's3_203_sweep' -script "$chain\s3_dense_weight_sweep.ps1"
Run-Stage -name 's4_erb500k' -script "$chain\s4_erb500k_scored.ps1"

"chain complete $(Get-Date -Format o)" | Add-Content "$logs\chain_runner.log"
$done = @{stage='chain'; state='COMPLETE'; at=(Get-Date -Format o)} | ConvertTo-Json
$done | Set-Content "$logs\chain_status.json"
