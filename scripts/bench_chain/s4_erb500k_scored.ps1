# Stage 4 - issue #93 / G2 prose gate: ERB 500-question scored run against the
# sharded 500K fixture (genomes/bench/matrix-sharded/enterprise_rag_500k).
# Per question: /context/packet (know/miss recorded verbatim) -> Claude Sonnet
# answer -> Sonnet trinary judge (CORRECT/INCORRECT/ABSTAINED, reference-guided)
# -> 10% Opus second-opinion audit. Outputs a per-question JSONL + a summary
# JSON with correctness/hallucination/coverage/know-vs-judged agreement and the
# published baselines (BM25 68.8 / Vector 51.4 / Onyx+GPT-4 72.4).
# RESUMABLE: the python helper skips question ids already in the output jsonl,
# so a mid-run interruption is recovered by re-running (the chain may restart it).
# Pure ASCII, PowerShell 5.1-safe. Never exits on a single-step failure.
$ErrorActionPreference = 'Continue'
$repo = 'F:\Projects\helix-context'
$logs = "$repo\benchmarks\logs"
$results = "$repo\benchmarks\results"
$ts = Get-Date -Format 'yyyy-MM-dd_HHmm'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
New-Item -ItemType Directory -Force -Path $results | Out-Null
$env:HELIX_OTEL_ENABLED = '1'
$env:HELIX_OTEL_ENDPOINT = 'localhost:4317'
$env:PYTHONHASHSEED = '0'
$port = 11437
$helixUrl = "http://127.0.0.1:$port"
# 2026-07-02 repoint (Max): use the BLOB-mode ERB fixture instead of the
# sharded routing DB. F:\tmp\erb_blob.db = 829,131 genes over 499,997 ERB
# source docs, dense_v2 97.6% populated, finished 2026-06-16, WAL
# checkpointed clean. Blob mode = every retrieval feature live (the
# sharded path skips WS2/WS3 + several blob-only tiers).
$fixture = "F:\tmp\erb_blob.db"
$erbRoot = 'F:\Projects\EnterpriseRAG-Bench-main'
cd $repo

function Set-Status {
    param([string]$stage, [string]$state)
    @{stage=$stage; state=$state; at=(Get-Date -Format o)} | ConvertTo-Json |
        Set-Content "$repo\benchmarks\logs\chain_status.json"
}

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
    param([string]$url, [int]$timeoutSec = 120)
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $h = Invoke-RestMethod -Uri "$url/health" -TimeoutSec 5 -ErrorAction Stop
            if ($h) { return $true }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    return $false
}

function Stop-HelixTree {
    param($proc)
    if ($null -eq $proc) { return }
    try { taskkill /T /F /PID $proc.Id 2>&1 | Out-Null } catch {}
    Start-Sleep -Seconds 1
    Wait-PortFree -p $port -timeoutSec 15 | Out-Null
}

Set-Status 's4_erb500k' 'preflight'

# Preflight 0 (2026-07-02): claude auth probe. The first S4 attempt burned
# 74 minutes producing 500x judge_error rows because every claude -p call
# hit 401. Fail fast instead.
$authProbe = & claude -p --model sonnet --tools "" --max-budget-usd 0.02 `
    --output-format json -- "Reply with exactly: OK" 2>$null
$authOk = $false
try {
    $aj = $authProbe | ConvertFrom-Json
    if (-not $aj.is_error) { $authOk = $true }
} catch {}
if (-not $authOk) {
    "CLAUDE AUTH FAILED (401?) -- run 'claude login' or set a valid " +
    "ANTHROPIC_API_KEY, then rerun this stage" |
        Add-Content "$logs\s4_summary_$ts.log"
    Set-Status 's4_erb500k' 'SKIPPED-claude-auth'
    return
}

# Preflight: fixture + question set must exist. If not, log clearly and mark
# the stage skipped -- do not crash the chain.
if (-not (Test-Path $fixture)) {
    "FIXTURE MISSING: $fixture -- cannot run ERB 500K scored run" |
        Add-Content "$logs\s4_summary_$ts.log"
    Set-Status 's4_erb500k' 'SKIPPED-no-fixture'
    return
}
if (-not (Test-Path "$erbRoot\questions.jsonl")) {
    "QUESTIONS MISSING: $erbRoot\questions.jsonl -- cannot run" |
        Add-Content "$logs\s4_summary_$ts.log"
    Set-Status 's4_erb500k' 'SKIPPED-no-questions'
    return
}
"preflight ok: fixture + questions present" | Add-Content "$logs\s4_summary_$ts.log"

# Start uvicorn in BLOB mode pointed at erb_blob.db (2026-07-02 repoint).
# No HELIX_USE_SHARDS: a plain genome path loads the standard KnowledgeStore
# with the full blob-mode tier stack.
Set-Status 's4_erb500k' 'starting-server'
Remove-Item Env:\HELIX_USE_SHARDS -ErrorAction SilentlyContinue
$env:HELIX_GENOME_PATH = $fixture
$srvLog = "$logs\s4_server_$ts.log"
$proc = $null
if (Wait-PortFree -p $port -timeoutSec 20) {
    $srvArgs = @('-m', 'uvicorn', 'helix_context._asgi:app',
                 '--host', '127.0.0.1', '--port', "$port")
    $proc = Start-Process -FilePath 'python' -ArgumentList $srvArgs `
        -RedirectStandardOutput $srvLog -RedirectStandardError "$srvLog.err" `
        -WindowStyle Hidden -PassThru
    if (-not (Wait-Healthy -url $helixUrl -timeoutSec 120)) {
        "SERVER did not become healthy (sharded 500K) -- see $srvLog" |
            Add-Content "$logs\s4_summary_$ts.log"
        Stop-HelixTree -proc $proc
        Remove-Item Env:\HELIX_USE_SHARDS -ErrorAction SilentlyContinue
        Remove-Item Env:\HELIX_GENOME_PATH -ErrorAction SilentlyContinue
        Set-Status 's4_erb500k' 'FAILED-server-start'
        return
    }
} else {
    "PORT $port occupied; cannot start sharded server" |
        Add-Content "$logs\s4_summary_$ts.log"
    Set-Status 's4_erb500k' 'FAILED-port-busy'
    return
}
"server pid=$($proc.Id) serving BLOB ERB fixture (erb_blob.db, 829K genes)" |
    Add-Content "$logs\s4_summary_$ts.log"

# Run the scored harness (resumable). Fixed output paths per-run so a restart
# of THIS ps1 within the same minute resumes the same jsonl; a later chain run
# gets a new $ts. To make the run resumable ACROSS chain restarts on different
# minutes, we use a stable (non-timestamped) jsonl name so re-invocation always
# finds prior progress; the summary is timestamped.
Set-Status 's4_erb500k' 'scoring'
# 2026-07-02: blob-specific output name so the resume logic never skips
# rows from the failed sharded attempt (archived separately).
$outJsonl = "$results\erb500k_blob_scored.jsonl"  # stable name -> resume across restarts
$summaryJson = "$results\erb500k_blob_scored_summary_$ts.json"
$runLog = "$logs\s4_run_$ts.log"

python scripts\bench_chain\erb500k_scored.py `
    --erb-root $erbRoot `
    --helix-url $helixUrl `
    --out $outJsonl `
    --summary-out $summaryJson `
    --answer-model sonnet `
    --audit-model opus `
    --answer-max-usd 0.20 `
    --judge-max-usd 0.10 `
    --audit-max-usd 0.40 `
    --audit-fraction 0.10 `
    --max-genes 8 `
    *> $runLog
$runExit = $LASTEXITCODE
"scored-run exit=$runExit out=$outJsonl summary=$summaryJson (log: $runLog)" |
    Add-Content "$logs\s4_summary_$ts.log"

# Also drop a per-run timestamped copy of the jsonl so the run is archivable
# alongside its summary (the stable jsonl keeps accumulating for resume).
try {
    if (Test-Path $outJsonl) {
        Copy-Item -Path $outJsonl -Destination "$results\erb500k_blob_scored_$ts.jsonl" -Force
    }
} catch {
    "archive copy failed: $_" | Add-Content "$logs\s4_summary_$ts.log"
}

# Tear down the server.
Stop-HelixTree -proc $proc
Remove-Item Env:\HELIX_USE_SHARDS -ErrorAction SilentlyContinue
Remove-Item Env:\HELIX_GENOME_PATH -ErrorAction SilentlyContinue
"server stopped" | Add-Content "$logs\s4_summary_$ts.log"

if ($runExit -eq 0) { Set-Status 's4_erb500k' 'DONE' }
else { Set-Status 's4_erb500k' "DONE-WITH-ERRORS(exit=$runExit)" }
