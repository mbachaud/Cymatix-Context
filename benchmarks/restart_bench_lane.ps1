<#
.SYNOPSIS
  Restart the helix BENCH-lane daemon on a dedicated port (default 11439),
  picking up the latest worktree code. Leaves the dev lane (tray/MCP on
  11437) untouched -- the kill step is scoped to THIS port only.

.DESCRIPTION
  Self-contained: port-scoped kill -> detached uvicorn (Start-Process, so it
  survives this shell) -> poll /health until genes>0. Adds, over a bare
  uvicorn launch:
    - the bench venv interpreter (full GPU encode stack, helix -> worktree),
    - per-lane OTel (service.name=helix-bench, helix.lane=bench) so this lane
      is splittable from helix-dev in Prometheus/Tempo/Grafana,
    - a -SemanticArm toggle for the wiring A/B (default OFF = byte-identical
      baseline; see docs/prds/2026-06-02-semantic-wiring-arm.md).

  "Pick up changes for testing" == just run this again. A fresh uvicorn
  re-imports helix_context from the worktree, so any code edit lands on
  restart. Toggle -SemanticArm to flip the experiment arm between runs.

  NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 as ANSI
  when there is no BOM, so box-drawing chars / em-dashes break the parser.

.EXAMPLE
  # Move/boot the bench lane to 11439 (baseline, arm off)
  powershell -ExecutionPolicy Bypass -File benchmarks\restart_bench_lane.ps1

.EXAMPLE
  # Same daemon, experiment arm ON (picks up code changes + flips the flag)
  powershell -ExecutionPolicy Bypass -File benchmarks\restart_bench_lane.ps1 -SemanticArm

.EXAMPLE
  # Then bench it (recall harness is port-flexible via --helix-url):
  F:\tmp\bgem3_gpu_venv\Scripts\python.exe benchmarks\bench_enterprise_rag_recall.py `
      --types semantic --max-questions 200 --k 200 `
      --helix-url http://127.0.0.1:11439 --label sem125_baseline
#>
param(
  [int]$Port = 11439,
  [string]$Config = "F:/tmp/helix_splade_on_mg10_rerank.toml",
  [string]$Fixture = "F:/Projects/helix-context/genomes/bench/matrix-sharded/enterprise_rag_onyx_full_2/main.genome.db",
  [switch]$SemanticArm,
  [double]$DenseWeight = 0,   # >0 overrides semantic_dense_additive_weight (diagnostic sweep)
  [switch]$BroadenOff,        # keep dense-weight arm but disable broaden routing
  [switch]$QuestionDense,     # HELIX_QUESTION_DENSE=1: sharded dense recall encodes the QUESTION not the tag-bag
  [int]$ShardWorkers = 0,     # >0 sets HELIX_SHARD_WORKERS (parallel fan-out; recall-identical per oracle)
  [switch]$NoOtel,
  [int]$TimeoutSec = 600
)

$ErrorActionPreference = "Stop"
$Py        = "F:/tmp/bgem3_gpu_venv/Scripts/python.exe"
$Worktree  = "F:/Projects/helix-context/.claude/worktrees/vibrant-easley-73d68a"
$Log       = "F:/tmp/helix_bench_$Port.log"
$ErrLog    = "F:/tmp/helix_bench_$Port.err.log"
$PidFile   = "F:/tmp/helix_bench_$Port.pid"
$HealthUrl = "http://127.0.0.1:$Port/health"

if (-not (Test-Path $Py))      { Write-Host "ERROR: bench venv python not found: $Py" -ForegroundColor Red; exit 2 }
if (-not (Test-Path $Fixture)) { Write-Host "ERROR: fixture not found: $Fixture" -ForegroundColor Red; exit 2 }
if (-not (Test-Path $Config))  { Write-Host "ERROR: config not found: $Config" -ForegroundColor Red; exit 2 }

# 1) Port-scoped kill: ONLY the uvicorn bound to THIS port. The dev lane on
#    11437 (and any other port) is left running. The trailing (\s|$) guard
#    stops 11439 from matching 114390.
$portGuard = "--port $Port(\s|`$)"
$victims = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'uvicorn helix_context._asgi' -and $_.CommandLine -match $portGuard }
if ($victims) {
  $victims | ForEach-Object { Write-Host "  killing stale pid $($_.ProcessId) on port $Port"; Stop-Process -Id $_.ProcessId -Force }
  Start-Sleep -Seconds 2
}

# 2) Environment
$env:HELIX_CONFIG          = $Config
$env:HELIX_GENOME_PATH     = $Fixture
$env:HELIX_USE_SHARDS      = "1"
$env:HF_HUB_OFFLINE        = "1"
$env:TRANSFORMERS_OFFLINE  = "1"
# per-lane OTel (harmless if the collector is down -- exporter just drops)
if (-not $NoOtel) {
  $env:HELIX_OTEL_ENABLED  = "1"
  $env:HELIX_OTEL_ENDPOINT = "localhost:4317"
  $env:HELIX_OTEL_INSECURE = "1"
}
$env:HELIX_OTEL_SERVICE_NAME = "helix-bench"
$env:HELIX_LANE              = "bench"
$env:HELIX_OTEL_INSTANCE_ID  = "127.0.0.1:$Port"
# semantic-wiring arm flag (default OFF = baseline)
if ($SemanticArm) { $env:HELIX_SEMANTIC_ARM = "1"; $armState = "ON" }
else { Remove-Item Env:HELIX_SEMANTIC_ARM -ErrorAction SilentlyContinue; $armState = "off" }
# Diagnostic sweep overrides (only meaningful with -SemanticArm).
if ($DenseWeight -gt 0) { $env:HELIX_SEMANTIC_DENSE_WEIGHT = "$DenseWeight" }
else { Remove-Item Env:HELIX_SEMANTIC_DENSE_WEIGHT -ErrorAction SilentlyContinue }
if ($BroadenOff) { $env:HELIX_SEMANTIC_BROADEN = "0" }
else { Remove-Item Env:HELIX_SEMANTIC_BROADEN -ErrorAction SilentlyContinue }
if ($ShardWorkers -gt 0) { $env:HELIX_SHARD_WORKERS = "$ShardWorkers" }
else { Remove-Item Env:HELIX_SHARD_WORKERS -ErrorAction SilentlyContinue }
if ($QuestionDense) { $env:HELIX_QUESTION_DENSE = "1" }
else { Remove-Item Env:HELIX_QUESTION_DENSE -ErrorAction SilentlyContinue }
$wState = "config"; if ($DenseWeight -gt 0) { $wState = "$DenseWeight" }
$bState = "on"; if ($BroadenOff) { $bState = "OFF" }
$swState = "default"; if ($ShardWorkers -gt 0) { $swState = "$ShardWorkers" }
$qdState = "off"; if ($QuestionDense) { $qdState = "ON" }

$otelState = "on"; if ($NoOtel) { $otelState = "off" }
Write-Host "=== restart bench lane port $Port ===" -ForegroundColor Cyan
Write-Host "  interpreter  = $Py"
Write-Host "  fixture      = $Fixture"
Write-Host "  config       = $Config"
Write-Host "  SEMANTIC_ARM = $armState    dense_w = $wState    broaden = $bState    question_dense = $qdState    shard_workers = $swState    OTel = $otelState (lane=helix-bench)"
Write-Host "  log          = $Log"
Write-Host "  (dev lane on 11437 NOT touched -- kill is port-scoped)"

# 3) Detached launch (survives this shell via Start-Process)
$p = Start-Process -FilePath $Py `
  -ArgumentList @("-m","uvicorn","helix_context._asgi:app","--host","127.0.0.1","--port","$Port") `
  -WorkingDirectory $Worktree -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput $Log -RedirectStandardError $ErrLog
$p.Id | Out-File -FilePath $PidFile -Encoding ascii
Write-Host "  launched pid = $($p.Id)" -ForegroundColor Green

# 4) Poll /health until genes>0. /health always returns HTTP 200; status may
#    be 'degraded' when the chat upstream (Ollama) is down -- that does NOT
#    affect retrieval (/fingerprint, /context).
#
#    NOTE: we do NOT early-bail on $p.HasExited. The uv venv python.exe is a
#    trampoline that re-execs to the base interpreter and then exits (racy
#    timing), so $p (the launcher stub) exiting does NOT mean the server died
#    -- the real uvicorn runs as a detached grandchild. Bailing on the stub's
#    exit (a) gave false "exited early" errors and (b) made THIS script return
#    in seconds, so the harness reaped the still-booting server. Poll the port
#    instead; a genuine crash just falls through to the timeout + log pointer.
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$ready = $false
Write-Host "  waiting up to $TimeoutSec s for genes>0 (poll-based; stub exit is normal) ..."
while ((Get-Date) -lt $deadline) {
  try {
    $h = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 30
    if ($h.genes -gt 0) {
      Write-Host ("OK  genes={0}  status={1}  ribosome={2}" -f $h.genes,$h.status,$h.ribosome_backend) -ForegroundColor Green
      $ready = $true; break
    }
  } catch { Start-Sleep -Seconds 3 }
}

if (-not $ready) { Write-Host "ERROR: never reached genes>0 within $TimeoutSec s (see $Log / $ErrLog)" -ForegroundColor Red; exit 2 }

Write-Host ""
Write-Host "bench lane UP -> http://127.0.0.1:$Port  (arm=$armState)" -ForegroundColor Green
Write-Host "bench it:" -ForegroundColor Green
Write-Host "  $Py benchmarks\bench_enterprise_rag_recall.py --types semantic --max-questions 200 --k 200 --helix-url http://127.0.0.1:$Port --label sem125_$armState"
exit 0
