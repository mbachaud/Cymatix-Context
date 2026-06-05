<#
.SYNOPSIS
  Full semantic-wiring-arm A/B on the record: baseline (arm OFF, arm-code-present)
  then experiment (arm ON, weight 12 + broaden, the canonical arm), both on
  semantic-125 @k200, parallel fan-out (HELIX_SHARD_WORKERS, recall-identical
  per the byte-identity oracle). ASCII-only (PS 5.1 ANSI gotcha).

  Each arm: restart_bench_lane.ps1 (blocks to genes>0) -> recall harness.
  Score afterward with: python F:/tmp/score_semantic_arm.py \
    --baseline benchmarks/results/recall_sem125_off_*.json \
    --experiment benchmarks/results/recall_sem125_ON_*.json
#>
param([int]$ShardWorkers = 8, [int]$Kp = 200, [int]$MaxQ = 200)
$ErrorActionPreference = "Continue"
$WT     = "F:/Projects/helix-context/.claude/worktrees/vibrant-easley-73d68a"
$recipe = Join-Path $WT "benchmarks/restart_bench_lane.ps1"
$py     = "F:/tmp/bgem3_gpu_venv/Scripts/python.exe"
$bench  = Join-Path $WT "benchmarks/bench_enterprise_rag_recall.py"
$url    = "http://127.0.0.1:11439"

function Run-Arm($label, $armOn) {
  Write-Host "`n==================== ARM: $label (arm=$armOn) ===================="
  if ($armOn) { & $recipe -SemanticArm -ShardWorkers $ShardWorkers }
  else        { & $recipe -ShardWorkers $ShardWorkers }
  if ($LASTEXITCODE -ne 0) { Write-Host "RESTART FAILED for $label (exit $LASTEXITCODE) -- skipping bench"; return }
  Write-Host "--- benching $label (semantic-$MaxQ, k=$Kp) ---"
  & $py $bench --types semantic --max-questions $MaxQ --k $Kp --helix-url $url --label $label
  Write-Host "--- $label bench exit $LASTEXITCODE ---"
}

$t0 = Get-Date
Run-Arm "sem125_off" $false
Run-Arm "sem125_ON"  $true
Write-Host "`n==================== A/B COMPLETE ($([int]((Get-Date)-$t0).TotalMinutes) min) ===================="
Write-Host "results: $WT/benchmarks/results/recall_sem125_off_*.json + recall_sem125_ON_*.json"
Write-Host "score:   $py F:/tmp/score_semantic_arm.py --baseline `"$WT/benchmarks/results/recall_sem125_off_*.json`" --experiment `"$WT/benchmarks/results/recall_sem125_ON_*.json`""
