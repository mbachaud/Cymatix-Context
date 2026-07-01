<#
.SYNOPSIS
    RepoBench-R Step-1 turnkey spin-up -- run from the repo root.

.DESCRIPTION
    One-command bring-up of the RepoBench-R retrieval benchmark against the
    frozen helix063 venv.  Covers python_cff and python_cfr configs, both
    difficulty levels (easy + hard), n=200 examples/level.

    Pipeline:
      1. Preflight  -- verify helix063 python.exe; install huggingface_hub +
                      rank-bm25 if missing; probe a free debug port (logged,
                      not bound -- arms are in-process, no daemon started).
      2. Write toml  -- emit helix_probe_lexical.toml into F:/tmp/cb_helix_probe/
                       so the Helix arms are lexical-only (dense/splade/
                       ribosome all OFF).
      3. Foils       -- benchmarks/repobench_r.py for each config (random /
                       Jaccard-overlap / BM25Okapi foils; writes per-example
                       JSON dumps the Helix arms need).
      4. Global arm  -- benchmarks/repobench_r_helix_global.py for each config
                       (one shared genome; B-mode pool-rank + C-mode global-
                       rank; also runs matched global-BM25 foil).
      5. Per-example arm -- benchmarks/repobench_r_helix.py for each config
                       (per-example fresh genome; expect lower acc@k than
                       global because the ~5-17 snippet pool degrades IDF).
      6. Summary     -- print result file locations and the head-to-head metric
                       (helix_B_acc@k vs bm25_B_acc@k from the global arm).

    NOTE -- No uvicorn daemon is started.  Both Helix arms use an in-process
    HelixContextManager via HELIX_CONFIG + HELIX_GENOME_PATH env vars.
    RepoBench-R is LLM-free and GPU-free on the lexical config; env vars
    HELIX_BFM_SPLADE=0 and HELIX_BFM_DENSE_BACKFILL=0 are set as a kill-
    switch against the #176 WDDM-spill livelock (multi-CUDA-context on <=12 GB
    cards) in case any background worker tries to fire dense/SPLADE.

.PARAMETER N
    Max examples per difficulty level (default: 200; 0 = full dataset).

.PARAMETER Configs
    Comma-separated dataset configs to run (default: "python_cff,python_cfr").

.PARAMETER Levels
    Comma-separated difficulty levels (default: "easy,hard").

.PARAMETER Workers
    Parallel workers for the per-example arm (default: 1).
    Only safe to raise when running the helix063 python.exe directly (not via
    uv or any wrapper), because ProcessPoolExecutor trampoline causes deadlock
    under wrapper envs.

.PARAMETER HelixPython
    Path to the helix063 venv python.exe
    (default: F:/Projects/_venvs/helix063/Scripts/python.exe).

.PARAMETER GenomeBase
    Base directory for bench genome scratch dirs
    (default: F:/tmp/repobench_r_genomes).

.PARAMETER ProbeTomlDir
    Directory where helix_probe_lexical.toml is written
    (default: F:/tmp/cb_helix_probe).

.EXAMPLE
    # Standard run -- from the repo root:
    powershell -ExecutionPolicy Bypass -File scripts\run_repobench_r.ps1

    # Quick smoke test -- 50 examples/level, CFF only:
    powershell -ExecutionPolicy Bypass -File scripts\run_repobench_r.ps1 `
        -N 50 -Configs python_cff

    # Full dataset, both configs, 4 parallel workers:
    powershell -ExecutionPolicy Bypass -File scripts\run_repobench_r.ps1 `
        -N 0 -Workers 4
#>

[CmdletBinding()]
param(
    [int]   $N            = 200,
    [string]$Configs      = "python_cff,python_cfr",
    [string]$Levels       = "easy,hard",
    [int]   $Workers      = 1,
    [string]$HelixPython  = "F:/Projects/_venvs/helix063/Scripts/python.exe",
    [string]$GenomeBase   = "F:/tmp/repobench_r_genomes",
    [string]$ProbeTomlDir = "F:/tmp/cb_helix_probe"
)

Set-StrictMode -Version Latest
# Native python calls (foils/arms) emit stderr (warnings, tracebacks) that under
# 'Stop' get promoted to terminating errors mid-pipeline -- killing the run before
# the explicit $LASTEXITCODE / Require-Exit0 checks fire. This script's error
# handling is exit-code-based by design, so use 'Continue' and rely on those checks.
$ErrorActionPreference = "Continue"

# ?? helpers ??????????????????????????????????????????????????????????????????

function Write-Step {
    param([string]$Msg)
    Write-Host "`n==> $Msg" -ForegroundColor Cyan
}
function Write-OK   { param([string]$Msg); Write-Host "  [OK]   $Msg" -ForegroundColor Green }
function Write-Warn { param([string]$Msg); Write-Host "  [WARN] $Msg" -ForegroundColor Yellow }
function Write-Fail {
    param([string]$Msg)
    Write-Host "`n[FAIL] $Msg" -ForegroundColor Red
    exit 1
}

function Require-Exit0 {
    param([string]$Label)
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "$Label exited with code $LASTEXITCODE"
    }
}

# ?? resolve paths ?????????????????????????????????????????????????????????????
# $PSScriptRoot is the scripts/ directory; parent is the repo root.
$RepoRoot   = Split-Path $PSScriptRoot -Parent
$BenchDir   = Join-Path $RepoRoot "benchmarks"
$ResultsDir = Join-Path $BenchDir "results"

Write-Host ""
Write-Host "RepoBench-R Step-1 -- turnkey spin-up" -ForegroundColor White
Write-Host "======================================" -ForegroundColor White
Write-Host "Repo root   : $RepoRoot"
Write-Host "Bench dir   : $BenchDir"
Write-Host "Results     : $ResultsDir"
Write-Host "N/level     : $N   (0 = full dataset)"
Write-Host "Configs     : $Configs"
Write-Host "Levels      : $Levels"
Write-Host "Workers     : $Workers"
Write-Host "Helix venv  : $HelixPython"
Write-Host "Genome base : $GenomeBase"
Write-Host ""


# =============================================================================
# STEP 1: PREFLIGHT
# =============================================================================
Write-Step "STEP 1: PREFLIGHT"

# 1a. helix063 python.exe
if (-not (Test-Path $HelixPython)) {
    Write-Fail (
        "helix063 python.exe not found at: $HelixPython`n" +
        "  Create the venv and install the package:`n" +
        "    python -m venv F:/Projects/_venvs/helix063`n" +
        "    F:/Projects/_venvs/helix063/Scripts/pip install -e $RepoRoot"
    )
}
Write-OK "helix063 python.exe found"

# 1b. helix_context importable
& $HelixPython -c "import helix_context" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Fail (
        "helix_context not importable in helix063 venv.`n" +
        "  Run: & '$HelixPython' -m pip install -e '$RepoRoot'"
    )
}
Write-OK "helix_context importable in helix063 venv"

# 1c. huggingface_hub -- used by repobench_r.py to download the dataset
& $HelixPython -c "import huggingface_hub" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warn "huggingface_hub not installed -- installing now..."
    & $HelixPython -m pip install --quiet huggingface_hub
    Require-Exit0 "pip install huggingface_hub"
    Write-OK "huggingface_hub installed"
} else {
    Write-OK "huggingface_hub present"
}

# 1d. rank-bm25 -- used by repobench_r.py for the BM25Okapi foil
& $HelixPython -c "import rank_bm25" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warn "rank-bm25 not installed -- installing now..."
    & $HelixPython -m pip install --quiet rank-bm25
    Require-Exit0 "pip install rank-bm25"
    Write-OK "rank-bm25 installed"
} else {
    Write-OK "rank-bm25 present"
}

# 1e. Free-port probe -- logged for human reference only; no daemon is started.
#     RepoBench-R arms are in-process; port is useful if someone wants to
#     start a debug helix-server manually alongside the bench run.
#     Avoids the dev server on :11437 and the strategy-doc bench lane :11439.
function Get-FreePort {
    $candidates = 11440..11460
    foreach ($p in $candidates) {
        try {
            $l = [System.Net.Sockets.TcpListener]::new(
                [System.Net.IPAddress]::Loopback, $p)
            $l.Start()
            $l.Stop()
            return $p
        } catch {
            # port in use, try next
        }
    }
    return $null
}
$FreePort = Get-FreePort
if ($FreePort) {
    Write-OK "Free debug port (informational, not bound): $FreePort"
} else {
    Write-Warn "No free port found in 11440-11460 -- arms still run in-process fine."
}

# 1f. Create output dirs
New-Item -ItemType Directory -Force -Path $ResultsDir   | Out-Null
New-Item -ItemType Directory -Force -Path $ProbeTomlDir | Out-Null
New-Item -ItemType Directory -Force -Path $GenomeBase   | Out-Null
$LogDir = Join-Path $ResultsDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir       | Out-Null
Write-OK "Output + log dirs ready"


# =============================================================================
# STEP 2: WRITE LEXICAL-PROBE helix.toml
# =============================================================================
Write-Step "STEP 2: WRITE helix_probe_lexical.toml"

$ProbeTomlPath = Join-Path $ProbeTomlDir "helix_probe.toml"

# Genome path used by the in-process arms -- each arm overrides HELIX_GENOME_PATH
# per run anyway, but [genome].path is the fallback the config loader reads.
# Use forward slashes inside the TOML string (TOML paths are strings, not PS paths).
$GenomeBaseFwd = $GenomeBase.Replace('\', '/')

$ProbeTomlContent = @"
# helix_probe_lexical.toml
# Auto-generated by scripts/run_repobench_r.ps1
# Lexical-only probe: dense / splade / ribosome / rerank all OFF.
# Both Helix arms (repobench_r_helix.py, repobench_r_helix_global.py) read this.

[ribosome]
enabled = false
backend = "none"
query_expansion_enabled = false
query_decomposition_enabled = false

[hardware]
device = "cpu"

[budget]
expression_tokens = 7000
max_genes_per_turn = 12
decoder_mode = "none"
legibility_enabled = false
session_delivery_enabled = false

[session]
synthetic_session_enabled = false

[genome]
path = "$GenomeBaseFwd/probe/genome.db"
compact_interval = 0

[server]
host = "127.0.0.1"
port = 11437
upstream = "http://localhost:11434"

[ingestion]
backend = "cpu"
splade_enabled = false
dense_embed_on_ingest = false
rerank_enabled = false
entity_graph = false

[retrieval]
dense_embedding_enabled = false
sr_enabled = false
ray_trace_theta = false
seeded_edges_enabled = false
filename_anchor_enabled = true
filename_anchor_weight = 4.0
bm25_shortlist_enabled = true
bm25_shortlist_size = 50
bm25_prefilter_enabled = false
entity_graph_retrieval_enabled = false
fusion_mode = "additive"

[context]
cold_tier_enabled = false

[cymatics]
enabled = false

[know]
confidence_floor = 0.0

[abstain]
enabled = false
"@

# NOTE: Windows PowerShell 5.1 '-Encoding UTF8' prepends a BOM, which Helix's TOML
# parser rejects ("Invalid statement at line 1, col 1") -> falls back to defaults
# (dense ingest ON) -> global-genome ingest fails. TOML here is ASCII, so write ASCII (no BOM).
$ProbeTomlContent | Set-Content -Path $ProbeTomlPath -Encoding Ascii -NoNewline
Write-OK "Probe TOML written -> $ProbeTomlPath"


# =============================================================================
# ENVIRONMENT -- set once for all child processes
# =============================================================================
# Kill-switches: prevents SPLADE + BGE-M3 dense-backfill workers from spinning
# up additional CUDA contexts on the 12 GB card (issue #176 WDDM-spill livelock).
# Safe even when dense is already OFF in the TOML (belt-and-suspenders).
$env:HELIX_BFM_SPLADE         = "0"
$env:HELIX_BFM_DENSE_BACKFILL = "0"
$env:HELIX_CONFIG             = $ProbeTomlPath
$env:PYTHONUNBUFFERED         = "1"

# Run timestamp for log file names (all arms share a single timestamp prefix).
$RunTs = Get-Date -Format "yyyyMMddTHHmmss"

# Parse config list once.
$ConfigList = ($Configs -split ",") | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }


# =============================================================================
# STEP 3: FOILS -- random / Jaccard-overlap / BM25Okapi
# =============================================================================
Write-Step "STEP 3: FOILS (repobench_r.py)"

Write-Host "  Dataset: tianyang/repobench-r (HuggingFace, CC-BY-4.0)"
Write-Host "  First run downloads gzipped pickle (~50-100 MB total for python configs)."
Write-Host "  Subsequent runs use the HF content-addressed cache."

foreach ($cfg in $ConfigList) {
    Write-Host ""
    Write-Host "  [foils] config=$cfg  n=$N  levels=$Levels" -ForegroundColor White
    $logFile = Join-Path $LogDir "foils_${cfg}_${RunTs}.log"

    # repobench_r.py CLI: --config --n --levels [--out]
    & $HelixPython -u `
        (Join-Path $BenchDir "repobench_r.py") `
        --config $cfg `
        --n      $N `
        --levels $Levels `
        2>&1 | Tee-Object -FilePath $logFile

    if ($LASTEXITCODE -ne 0) {
        Write-Fail "repobench_r.py failed for config=$cfg  (log: $logFile)"
    }
    Write-OK "Foils done for $cfg  (log: $(Split-Path $logFile -Leaf))"
}


# =============================================================================
# STEP 4: HELIX GLOBAL-GENOME ARM
# =============================================================================
Write-Step "STEP 4: HELIX GLOBAL-GENOME ARM (repobench_r_helix_global.py)"

Write-Host "  One shared genome over the deduped union of all candidate snippets."
Write-Host "  Scores B-mode (pool-rank, comparable to foils) and C-mode (global-rank)."
Write-Host "  Also runs matched global-BM25 foil (floored IDF -- correct foil at scale)."

foreach ($cfg in $ConfigList) {
    # Fresh genome dir per config so configs don't share state.
    $genomeDir = Join-Path $GenomeBase "global_${cfg}"
    Write-Host ""
    Write-Host "  [global] config=$cfg  genome-dir=$genomeDir" -ForegroundColor White
    $logFile = Join-Path $LogDir "global_${cfg}_${RunTs}.log"

    # repobench_r_helix_global.py CLI: --config --levels [--limit] --helix-config --genome-dir [--out]
    & $HelixPython -u `
        (Join-Path $BenchDir "repobench_r_helix_global.py") `
        --config       $cfg `
        --levels       $Levels `
        --helix-config $ProbeTomlPath `
        --genome-dir   $genomeDir `
        2>&1 | Tee-Object -FilePath $logFile

    if ($LASTEXITCODE -ne 0) {
        Write-Fail "repobench_r_helix_global.py failed for config=$cfg  (log: $logFile)"
    }
    Write-OK "Global arm done for $cfg  (log: $(Split-Path $logFile -Leaf))"
}


# =============================================================================
# STEP 5: HELIX PER-EXAMPLE ARM
# =============================================================================
Write-Step "STEP 5: HELIX PER-EXAMPLE ARM (repobench_r_helix.py)"

Write-Host "  Per-example fresh genome (~5-17 snippet corpus per query)."
Write-Host "  Expect lower acc@k than global arm -- IDF degrades at tiny corpus size."
Write-Host "  Results still useful: shows Helix floor vs BM25 at equal corpus scale."

foreach ($cfg in $ConfigList) {
    $genomeRoot = Join-Path $GenomeBase "per_example_${cfg}"
    Write-Host ""
    Write-Host "  [per-ex] config=$cfg  genome-root=$genomeRoot  workers=$Workers" -ForegroundColor White
    $logFile = Join-Path $LogDir "per_example_${cfg}_${RunTs}.log"

    # repobench_r_helix.py CLI: --config --levels [--limit] [--workers] --helix-config --genome-root [--out]
    & $HelixPython -u `
        (Join-Path $BenchDir "repobench_r_helix.py") `
        --config       $cfg `
        --levels       $Levels `
        --helix-config $ProbeTomlPath `
        --genome-root  $genomeRoot `
        --workers      $Workers `
        2>&1 | Tee-Object -FilePath $logFile

    if ($LASTEXITCODE -ne 0) {
        Write-Fail "repobench_r_helix.py failed for config=$cfg  (log: $logFile)"
    }
    Write-OK "Per-example arm done for $cfg  (log: $(Split-Path $logFile -Leaf))"
}


# =============================================================================
# STEP 6: SUMMARY
# =============================================================================
Write-Step "STEP 6: SUMMARY"

Write-Host ""
Write-Host "Results dir : $ResultsDir" -ForegroundColor White
Write-Host "Logs dir    : $LogDir"     -ForegroundColor White
Write-Host "Probe TOML  : $ProbeTomlPath" -ForegroundColor White
Write-Host ""

# Find result JSONs written this run.
$cutoff = (Get-Date).AddMinutes(-120)
$resultFiles = Get-ChildItem -Path $ResultsDir -Filter "*.json" -File -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -gt $cutoff } |
    Sort-Object LastWriteTime -Descending

if ($resultFiles) {
    Write-Host "Result files written this run:" -ForegroundColor Cyan
    foreach ($f in $resultFiles) {
        Write-Host "  $($f.FullName)"
    }
} else {
    Write-Warn "No result files found in the last 2 hours -- check logs."
}

Write-Host ""
Write-Host "Head-to-head metric (primary -- read from global arm JSON):" -ForegroundColor Cyan
Write-Host "  B-mode keys  : helix_B_acc@1  vs  bm25_B_acc@1  (pool-rank, comparable to foils)"
Write-Host "  C-mode keys  : helix_C_recall@k  (global-rank, realistic agent scenario)"
Write-Host "  Easy level   : acc@1, acc@3"
Write-Host "  Hard level   : acc@1, acc@3, acc@5"
Write-Host ""
Write-Host "Quick parse -- run this after the script completes:" -ForegroundColor DarkCyan
Write-Host ("  `$f = (Get-ChildItem '$ResultsDir' -Filter '*_global_*.json' | " +
            "Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName")
Write-Host ("  & '$HelixPython' -c " +
            '"import json,sys; d=json.load(open(sys.argv[1])); ' +
            '[print(k, json.dumps(v, indent=2)) for k,v in d[chr(108)+chr(101)+chr(118)+chr(101)+chr(108)+chr(115)].items()]" $f')
Write-Host ""
Write-Host "Expected ranges for python_cff n=200 (see runbook Table 5):" -ForegroundColor DarkCyan
Write-Host "  easy  helix_B_acc@1 ~0.35-0.45   bm25_B_acc@1 ~0.40-0.50"
Write-Host "  hard  helix_B_acc@1 ~0.25-0.35   bm25_B_acc@1 ~0.30-0.40"
Write-Host "  (Helix lexical expected near-parity with BM25 on this benchmark)"
Write-Host ""
Write-Host "Done. RepoBench-R Step-1 complete." -ForegroundColor Green
