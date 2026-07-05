# S3b - rerun of the two ERB dense-weight sweep steps that failed on
# 2026-07-02 (sweep expects a JSON LIST of {query, gold_ids}; ERB ships
# JSONL with uuid golds). erb_to_sweep_queries.py adapts per bed genome.
$ErrorActionPreference = 'Continue'
$repo = 'F:\Projects\helix-context'
$logs = "$repo\benchmarks\logs"
$ts = Get-Date -Format 'yyyy-MM-dd_HHmm'
$env:HELIX_OTEL_ENABLED = '1'
$env:HELIX_OTEL_ENDPOINT = 'localhost:4317'
cd $repo

function Set-Status {
    param([string]$stage, [string]$state)
    @{stage=$stage; state=$state; at=(Get-Date -Format o)} | ConvertTo-Json |
        Set-Content "$repo\benchmarks\logs\chain_status.json"
}

$weights = '0,1,2,3,4,6'

foreach ($bed in @(
    @('erb10k', 'genomes\bench\matrix\enterprise_rag_10k_batched.db'),
    @('erb50k', 'genomes\bench\matrix\enterprise_rag_50k_batched.db')
)) {
    $name = $bed[0]; $db = $bed[1]
    Set-Status 's3b_erb_sweep' "adapt-$name"
    $qfile = "$repo\benchmarks\results\erb_sweep_queries_$name.json"
    python scripts\bench_chain\erb_to_sweep_queries.py --genome $db `
        --out $qfile *> "$logs\s3b_adapt_${name}_$ts.log"
    if ($LASTEXITCODE -ne 0) {
        "adapter FAILED for ${name}: see s3b_adapt log" | Add-Content "$logs\s3b_summary_$ts.log"
        continue
    }
    Set-Status 's3b_erb_sweep' "sweep-$name"
    python benchmarks\sweep_dense_additive_weight.py `
        --genome $db --queries $qfile --weights $weights `
        --out benchmarks\results\sweep_w_${name}_realq_$ts.json `
        *> "$logs\s3b_${name}_$ts.log"
    "$name exit=$LASTEXITCODE" | Add-Content "$logs\s3b_summary_$ts.log"
}
Set-Status 's3b_erb_sweep' 'DONE'
