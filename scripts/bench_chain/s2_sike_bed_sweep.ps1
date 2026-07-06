# Stage 2 - issue #221: fixed SIKE curated needle set swept across distractor beds.
# Beds: xl.db (own-code mixed) + enterprise_rag_10k_batched + enterprise_rag_50k_batched.
# Per bed: copy to genomes/bench/sike_beds/<name>.db (resume-safe: skip if present),
#          ingest the SIKE gold docs into the copy (bench_needle does NOT self-ingest),
#          start a blob-mode uvicorn on that copy, run the needle battery across a
#          local ollama ladder (auto-discovered) + one Claude Sonnet rung (cost-capped),
#          write results to benchmarks/results/sike_bedsweep_<bed>_<ts>.json.
# On a per-bed failure: log and continue to the next bed. Never exit on one step.
# Pure ASCII, PowerShell 5.1-safe.
$ErrorActionPreference = 'Continue'
$repo = 'F:\Projects\helix-context'
$logs = "$repo\benchmarks\logs"
$results = "$repo\benchmarks\results"
$bedsDir = "$repo\genomes\bench\sike_beds"
# Pause flag: create it (e.g. via sike_ctl.ps1 pause) to stop the sweep between
# needles/rungs with a checkpoint saved; delete it and relaunch to resume.
$pauseFlag = "$logs\sike_pause.flag"
$ts = Get-Date -Format 'yyyy-MM-dd_HHmm'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
New-Item -ItemType Directory -Force -Path $results | Out-Null
New-Item -ItemType Directory -Force -Path $bedsDir | Out-Null
$env:HELIX_OTEL_ENABLED = '1'
$env:HELIX_OTEL_ENDPOINT = 'localhost:4317'
$env:PYTHONHASHSEED = '0'
$port = 11437
$helixUrl = "http://127.0.0.1:$port"
cd $repo

function Set-Status {
    param([string]$stage, [string]$state)
    @{stage=$stage; state=$state; at=(Get-Date -Format o)} | ConvertTo-Json |
        Set-Content "$repo\benchmarks\logs\chain_status.json"
}

# ---- Server lifecycle helpers (mirror bench_orchestrator conventions) -------

function Wait-PortFree {
    param([int]$p, [int]$timeoutSec = 20)
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        $inUse = $false
        try {
            $c = New-Object System.Net.Sockets.TcpClient
            $iar = $c.BeginConnect('127.0.0.1', $p, $null, $null)
            if ($iar.AsyncWaitHandle.WaitOne(500)) {
                try { $c.EndConnect($iar); $inUse = $true } catch { $inUse = $false }
            }
            $c.Close()
        } catch { $inUse = $false }
        if (-not $inUse) { return $true }
        Start-Sleep -Milliseconds 300
    }
    return $false
}

function Wait-Healthy {
    param([string]$url, [int]$timeoutSec = 90)
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $h = Invoke-RestMethod -Uri "$url/health" -TimeoutSec 5 -ErrorAction Stop
            if ($h) { return $true }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    return $false
}

function Start-HelixOnBed {
    # Start uvicorn (blob mode) pointed at $bedPath via HELIX_GENOME_PATH.
    # Returns the process object, or $null on failure.
    param([string]$bedPath, [string]$srvLog)
    $env:HELIX_GENOME_PATH = $bedPath
    # 2026-07-03 fix: serve beds with the LEXICAL probe config (dense OFF).
    # Master's default has dense_embedding_enabled=true, which put BGE-M3
    # on the same 12GB GPU the ollama 26b/31b consumers were using -->
    # 30-60s/query --> the runner's httpx calls ALL hit ReadTimeout and
    # every needle scored zero delivery. SIKE needles are lexical-designed;
    # the CPU-only lexical config removes the contention entirely.
    $env:HELIX_CONFIG = "$repo\docs\benchmarks\helix_probe_lexical.toml"
    # 2026-07-05 fix: the proxy's default upstream_timeout (180s,
    # helix_context/config.py) fired mid-generation for the CPU-offloaded
    # gemma4 26b-a4b/31b/26b rungs -- ~25-30% of their needles came back
    # as httpx.ReadTimeout -> http_error rows at ~181s elapsed, deflating
    # coverage to 0.66-0.78. 600s gives slow local generations room; the
    # runner's client timeout is 660s so the server side still decides.
    $env:HELIX_SERVER_UPSTREAM_TIMEOUT = '600'
    # 2026-07-05 fix (gap A2): serve read-only so answering a needle never
    # persists a "User query: ..." echo gene back into the bed. Without this
    # the 1556 run self-contaminated its own corpus with 260-285 echoes that
    # ranked as perfect-lexical distractors. Gated in server/helpers.py.
    $env:HELIX_DISABLE_LEARN = '1'
    Remove-Item Env:\HELIX_USE_SHARDS -ErrorAction SilentlyContinue
    if (-not (Wait-PortFree -p $port -timeoutSec 20)) {
        "  port $port still occupied; cannot start server" | Add-Content $srvLog
        return $null
    }
    $srvArgs = @('-m', 'uvicorn', 'helix_context._asgi:app',
                 '--host', '127.0.0.1', '--port', "$port")
    $proc = Start-Process -FilePath 'python' -ArgumentList $srvArgs `
        -RedirectStandardOutput $srvLog -RedirectStandardError "$srvLog.err" `
        -WindowStyle Hidden -PassThru
    if (-not (Wait-Healthy -url $helixUrl -timeoutSec 90)) {
        "  server did not become healthy" | Add-Content $srvLog
        Stop-HelixTree -proc $proc
        return $null
    }
    return $proc
}

function Stop-HelixTree {
    param($proc)
    if ($null -eq $proc) { return }
    try { taskkill /T /F /PID $proc.Id 2>&1 | Out-Null } catch {}
    Start-Sleep -Seconds 1
    Wait-PortFree -p $port -timeoutSec 15 | Out-Null
}

# ---- Discover local ollama models (the local ladder rungs) ------------------

Set-Status 's2_sike_beds' 'discover-ollama'
# 2026-07-02 hang fix: `ollama list` blocked forever in the hidden shell
# (chain stalled 8.5h at this exact step). Discovery now runs in a job
# with a hard 30s timeout and uses the HTTP API (no console semantics);
# on timeout/empty the ladder degrades to the Sonnet rung only.
$ollamaModels = ''
$job = Start-Job -ScriptBlock {
    try {
        $r = Invoke-RestMethod -Uri 'http://localhost:11434/api/tags' -TimeoutSec 20
        ($r.models | ForEach-Object { $_.name }) -join ','
    } catch { '' }
}
if (Wait-Job $job -Timeout 30) {
    $ollamaModels = (Receive-Job $job)
} else {
    Stop-Job $job -ErrorAction SilentlyContinue
}
Remove-Job $job -Force -ErrorAction SilentlyContinue
if ($ollamaModels) {
    "ollama models discovered: $ollamaModels" | Add-Content "$logs\s2_summary_$ts.log"
} else {
    "ollama discovery timed out or empty - proceeding with Sonnet rung only" |
        Add-Content "$logs\s2_summary_$ts.log"
}
if (-not $ollamaModels) {
    "WARN: no ollama models discovered; local ladder will be empty (Claude rung still runs)" |
        Add-Content "$logs\s2_summary_$ts.log"
}

# ---- Bed table (source -> copy) --------------------------------------------

$beds = @(
    @{ name = 'xl';           src = "$repo\genomes\bench\matrix\xl_clean.db" },  # 2026-07-05: decontaminated (4676 worktree-dupe genes purged, gap A2)
    @{ name = 'enterprise_rag_10k'; src = "$repo\genomes\bench\matrix\enterprise_rag_10k_batched.db" },
    @{ name = 'enterprise_rag_50k'; src = "$repo\genomes\bench\matrix\enterprise_rag_50k_batched.db" }
)

foreach ($bed in $beds) {
    $name = $bed.name
    $src = $bed.src
    $copy = "$bedsDir\$name.db"
    Set-Status 's2_sike_beds' "bed:$name"
    "=== BED $name $(Get-Date -Format o) ===" | Add-Content "$logs\s2_summary_$ts.log"

    # 1. FORCE-FRESH copy (2026-07-03 fix). The resume-safe skip kept a
    # 132-gene stub db (auto-created by an earlier aborted attempt) as the
    # xl bed forever, and WAL-pending gold writes were invisible to the
    # server. Always delete + recopy, then verify below.
    try {
        if (-not (Test-Path $src)) {
            "  SOURCE MISSING: $src -- skipping bed $name" | Add-Content "$logs\s2_summary_$ts.log"
            continue
        }
        Remove-Item -Path $copy, "$copy-wal", "$copy-shm" -Force -ErrorAction SilentlyContinue
        "  fresh copy $src -> $copy" | Add-Content "$logs\s2_summary_$ts.log"
        Copy-Item -Path $src -Destination $copy -Force -ErrorAction Stop
        foreach ($side in @("$src-wal", "$src-shm")) {
            if (Test-Path $side) { Copy-Item -Path $side -Destination ($side -replace [regex]::Escape($src), $copy) -Force }
        }
    } catch {
        "  COPY FAILED for ${name}: $_ -- skipping" | Add-Content "$logs\s2_summary_$ts.log"
        continue
    }

    # 2. Ingest SIKE gold docs into the copy (bench_needle scores against them).
    $ingLog = "$logs\s2_${name}_ingest_$ts.log"
    python scripts\bench_chain\sike_bed_ingest.py --genome $copy --json `
        *> $ingLog
    $ingExit = $LASTEXITCODE
    "  gold-ingest exit=$ingExit (log: $ingLog)" | Add-Content "$logs\s2_summary_$ts.log"
    if ($ingExit -ne 0) {
        "  WARN: gold ingest returned $ingExit for $name; recall may read 0 -- continuing anyway" |
            Add-Content "$logs\s2_summary_$ts.log"
    }

    # 2b (2026-07-03): checkpoint the WAL so the server sees every gold,
    # then VERIFY the bed is a real corpus before serving. A bed with
    # fewer than 1000 genes means the copy/ingest went wrong -- skip it
    # loudly instead of producing all-zero results.
    $verify = python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1], timeout=60); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); n=c.execute('SELECT COUNT(*) FROM genes').fetchone()[0]; c.close(); print(n)" $copy
    "  bed verify: $verify genes in $copy" | Add-Content "$logs\s2_summary_$ts.log"
    if ([int]$verify -lt 1000) {
        "  BED INVALID ($verify genes) -- skipping $name" | Add-Content "$logs\s2_summary_$ts.log"
        continue
    }

    # 3. Start a blob-mode server on the bed copy.
    $srvLog = "$logs\s2_${name}_server_$ts.log"
    $proc = Start-HelixOnBed -bedPath $copy -srvLog $srvLog
    if ($null -eq $proc) {
        "  SERVER START FAILED for $name -- skipping bed" | Add-Content "$logs\s2_summary_$ts.log"
        continue
    }
    "  server pid=$($proc.Id) serving $copy" | Add-Content "$logs\s2_summary_$ts.log"

    # 4. Run the needle battery across the ollama ladder + Claude Sonnet rung.
    # STABLE out name (no $ts) so --resume finds the per-rung checkpoint on a
    # relaunch; the run timestamp lives inside the JSON. --pause-file lets a
    # pause stop cleanly between needles (runner exit 42).
    $outJson = "$results\sike_bedsweep_${name}.json"
    $runLog = "$logs\s2_${name}_run_$ts.log"
    $claudeArgs = @('scripts\bench_chain\s2_sike_bedsweep_run.py',
                    '--bed', $name,
                    '--helix-url', $helixUrl,
                    '--claude-model', 'sonnet',
                    '--claude-max-usd', '0.15',
                    '--resume',
                    '--pause-file', $pauseFlag,
                    '--out', $outJson)
    # The Claude Sonnet rung always runs (cost-capped). The local ollama
    # ladder is appended only when models were discovered; if none, the run
    # proceeds with the Claude rung alone.
    if ($ollamaModels) { $claudeArgs += @('--ollama-models', $ollamaModels) }

    python @claudeArgs *> $runLog
    $runExit = $LASTEXITCODE
    "  run exit=$runExit out=$outJson (log: $runLog)" | Add-Content "$logs\s2_summary_$ts.log"

    # Runner exit 42 == paused (checkpoint saved). Stop the whole sweep here;
    # deleting the pause flag and relaunching resumes from this bed's rungs.
    if ($runExit -eq 42) {
        "  bed $name PAUSED (exit 42). Delete $pauseFlag and relaunch to resume." |
            Add-Content "$logs\s2_summary_$ts.log"
        Stop-HelixTree -proc $proc
        Set-Status 's2_sike_beds' "PAUSED:$name"
        break
    }

    # 5. Tear down the server before the next bed.
    Stop-HelixTree -proc $proc
    "  server stopped for $name" | Add-Content "$logs\s2_summary_$ts.log"
}

Remove-Item Env:\HELIX_GENOME_PATH, Env:\HELIX_SERVER_UPSTREAM_TIMEOUT, Env:\HELIX_CONFIG -ErrorAction SilentlyContinue
Set-Status 's2_sike_beds' 'DONE'
"s2 complete $(Get-Date -Format o)" | Add-Content "$logs\s2_summary_$ts.log"
