# SIKE + #203 sequencer (2026-07-03). ERB-500q scored run SKIPPED per Max
# (resource requirements). Serial: S2' SIKE bed-sweep (fixed: force-fresh
# copies + checkpoint + gene-count gate) -> S3b' ERB dense-weight sweeps
# (n=100 seeded sample, weights 0,2,4,6 -- real queries run ~25s each, the
# full 470x6 grid would be ~23h/bed).
$ErrorActionPreference = 'Continue'
$repo = 'F:\Projects\helix-context'
$logs = "$repo\benchmarks\logs"
$ts = Get-Date -Format 'yyyy-MM-dd_HHmm'
$env:HELIX_OTEL_ENABLED = '1'
$env:HELIX_OTEL_ENDPOINT = 'localhost:4317'
cd $repo
"sike+203 sequencer started $(Get-Date -Format o) pid=$PID" | Set-Content "$logs\sike203_runner.log"
$PID | Set-Content "$logs\sike203.pid"

function Set-Status {
    param([string]$stage, [string]$state)
    @{stage=$stage; state=$state; at=(Get-Date -Format o)} | ConvertTo-Json |
        Set-Content "$repo\benchmarks\logs\chain_status.json"
}

# ---- Stage A: SIKE bed-sweep (repaired) ------------------------------------
"START s2_sike $(Get-Date -Format o)" | Add-Content "$logs\sike203_runner.log"
& powershell -NoProfile -ExecutionPolicy Bypass -File "$repo\scripts\bench_chain\s2_sike_bed_sweep.ps1"
"END s2_sike exit=$LASTEXITCODE $(Get-Date -Format o)" | Add-Content "$logs\sike203_runner.log"

# ---- Stage B: #203 sweeps, sampled -----------------------------------------
$weights = '0,2,4,6'
foreach ($bed in @(
    @('erb10k', 'genomes\bench\matrix\enterprise_rag_10k_batched.db'),
    @('erb50k', 'genomes\bench\matrix\enterprise_rag_50k_batched.db')
)) {
    $name = $bed[0]; $db = $bed[1]
    Set-Status 's3b_203' "adapt-$name"
    $qfull = "$repo\benchmarks\results\erb_sweep_queries_$name.json"
    $qsample = "$repo\benchmarks\results\erb_sweep_queries_${name}_n100.json"
    if (-not (Test-Path $qfull)) {
        python scripts\bench_chain\erb_to_sweep_queries.py --genome $db `
            --out $qfull *> "$logs\s3b_adapt_${name}_$ts.log"
        if ($LASTEXITCODE -ne 0) {
            "adapter FAILED for $name" | Add-Content "$logs\sike203_runner.log"
            continue
        }
    }
    python -c "import json,random,sys; qs=json.load(open(sys.argv[1],encoding='utf-8')); random.Random(0).shuffle(qs); json.dump(qs[:100], open(sys.argv[2],'w',encoding='utf-8'))" $qfull $qsample
    Set-Status 's3b_203' "sweep-$name-n100"
    "START sweep $name $(Get-Date -Format o)" | Add-Content "$logs\sike203_runner.log"
    python benchmarks\sweep_dense_additive_weight.py `
        --genome $db --queries $qsample --weights $weights `
        --out benchmarks\results\sweep_w_${name}_realq_n100_$ts.json `
        *> "$logs\s3b_${name}_n100_$ts.log"
    "END sweep $name exit=$LASTEXITCODE $(Get-Date -Format o)" | Add-Content "$logs\sike203_runner.log"
}

Set-Status 'chain' 'COMPLETE-SIKE-203 (erb500k skipped per Max)'
"sequencer complete $(Get-Date -Format o)" | Add-Content "$logs\sike203_runner.log"
