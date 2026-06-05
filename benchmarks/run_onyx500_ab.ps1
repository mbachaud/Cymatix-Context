<#
.SYNOPSIS
  Full onyx-500 A/B on v2 (enterprise_rag_onyx_full_2): BASELINE (stock pipeline)
  then FIXED (HELIX_QUESTION_DENSE + semantic-scoped broaden + dense_additive_weight 16),
  all 10 question types @k200, w8 parallel. ASCII-only.

  Per-type deltas after with: python F:/tmp/score_onyx500_bytype.py \
    --baseline benchmarks/results/recall_onyx500_baseline_*.json \
    --fixed    benchmarks/results/recall_onyx500_fixed_*.json
#>
param([int]$ShardWorkers = 8, [int]$Kp = 200, [int]$MaxQ = 600, [double]$Weight = 16)
$ErrorActionPreference = "Continue"
$WT     = "F:/Projects/helix-context/.claude/worktrees/vibrant-easley-73d68a"
$recipe = Join-Path $WT "benchmarks/restart_bench_lane.ps1"
$py     = "F:/tmp/bgem3_gpu_venv/Scripts/python.exe"
$bench  = Join-Path $WT "benchmarks/bench_enterprise_rag_recall.py"
$url    = "http://127.0.0.1:11439"
$TYPES  = "basic,semantic,intra_document_reasoning,project_related,constrained,conflicting_info,completeness,miscellaneous,info_not_found,high_level"

function Run-Arm($label, $fixed) {
  Write-Host "`n==================== ARM: $label (fixed=$fixed) ===================="
  if ($fixed) { & $recipe -QuestionDense -SemanticArm -DenseWeight $Weight -ShardWorkers $ShardWorkers }
  else        { & $recipe -ShardWorkers $ShardWorkers }
  if ($LASTEXITCODE -ne 0) { Write-Host "RESTART FAILED for $label (exit $LASTEXITCODE) -- skipping bench"; return }
  Write-Host "--- benching $label (all 10 types, max $MaxQ, k=$Kp) ---"
  & $py $bench --types $TYPES --max-questions $MaxQ --k $Kp --helix-url $url --label $label
  Write-Host "--- $label bench exit $LASTEXITCODE ---"
}

$t0 = Get-Date
Run-Arm "onyx500_baseline" $false
Run-Arm "onyx500_fixed"    $true
Write-Host "`n==================== ONYX-500 A/B COMPLETE ($([int]((Get-Date)-$t0).TotalMinutes) min) ===================="
Write-Host "score: $py F:/tmp/score_onyx500_bytype.py --baseline `"$WT/benchmarks/results/recall_onyx500_baseline_*.json`" --fixed `"$WT/benchmarks/results/recall_onyx500_fixed_*.json`""
