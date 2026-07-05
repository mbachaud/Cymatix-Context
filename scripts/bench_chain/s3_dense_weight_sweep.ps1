# Stage 3 — issue #203: dense_additive_weight sweep with REAL ERB questions
# Arms: ERB-10K, ERB-50K (prose) + medium.db with SIKE-style content queries (code side)
$ErrorActionPreference = 'Continue'
$repo = 'F:\Projects\helix-context'
$logs = "$repo\benchmarks\logs"
$ts = Get-Date -Format 'yyyy-MM-dd_HHmm'
$env:HELIX_OTEL_ENABLED = '1'
$env:HELIX_OTEL_ENDPOINT = 'localhost:4317'
cd $repo

function Set-Status($stage, $state) {
    @{stage=$stage; state=$state; at=(Get-Date -Format o)} | ConvertTo-Json |
        Set-Content "$repo\benchmarks\logs\chain_status.json"
}

$questions = 'F:\Projects\EnterpriseRAG-Bench-main\questions.jsonl'
$weights = '0,1,2,3,4,6'

Set-Status 's3_203_sweep' 'erb10k'
python benchmarks\sweep_dense_additive_weight.py `
    --genome genomes\bench\matrix\enterprise_rag_10k_batched.db `
    --queries $questions --weights $weights `
    --out benchmarks\results\sweep_w_erb10k_realq_$ts.json `
    *> "$logs\s3_erb10k_$ts.log"
"erb10k exit=$LASTEXITCODE" | Add-Content "$logs\s3_summary_$ts.log"

Set-Status 's3_203_sweep' 'erb50k'
python benchmarks\sweep_dense_additive_weight.py `
    --genome genomes\bench\matrix\enterprise_rag_50k_batched.db `
    --queries $questions --weights $weights `
    --out benchmarks\results\sweep_w_erb50k_realq_$ts.json `
    *> "$logs\s3_erb50k_$ts.log"
"erb50k exit=$LASTEXITCODE" | Add-Content "$logs\s3_summary_$ts.log"

Set-Status 's3_203_sweep' 'code-arm-medium'
python benchmarks\sweep_dense_additive_weight.py `
    --genome genomes\bench\matrix\medium.db `
    --weights $weights `
    --out benchmarks\results\sweep_w_medium_code_$ts.json `
    *> "$logs\s3_medium_$ts.log"
"medium exit=$LASTEXITCODE" | Add-Content "$logs\s3_summary_$ts.log"

Set-Status 's3_203_sweep' 'DONE'
