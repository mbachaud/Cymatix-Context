# ERB 580-char rate probe: build the first N files of the enterprise_rag_500k
# discovery order at chunk_chars 580 and 4000 with the witness wrapper
# (ingest_c=6 under the ERB equivalence receipt), then record genes, bytes
# and wall per arm. Sequential arms; each arm has its own output dir.
param(
    [int]$MaxFiles = 50000,
    [string]$Out = "F:\tmp\grain_erb_slice_2026-10-15",
    # Lean writes: no PKI rows, no COVER auto-link edges, no prefix-redundant
    # indexes (all query-inert at shipped defaults; verify_lean.py gate).
    [switch]$Lean,
    # Explicit interpreter: a Windows update can put the WindowsApps alias
    # ahead of the real python on PATH (rc 9009, 2026-10-16).
    [string]$Py = "C:\Users\max\AppData\Local\Python\pythoncore-3.14-64\python.exe"
)
if ($Lean) {
    $env:CYMATIX_BFM_PKI_WRITE = "0"
    $env:CYMATIX_BFM_ENTITY_AUTOLINK = "0"
    $env:CYMATIX_LEAN_INDEXES = "1"
} else {
    Remove-Item Env:CYMATIX_BFM_PKI_WRITE, Env:CYMATIX_BFM_ENTITY_AUTOLINK, Env:CYMATIX_LEAN_INDEXES -ErrorAction SilentlyContinue
}
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path "$PSScriptRoot\..\..\..").Path
$gate = "F:\Projects\cymatix-receipts\mirror\benchmarks\dogfood\receipts\ingest_equivalence_erb.json"
New-Item -ItemType Directory -Force $Out | Out-Null
$pidFile = Join-Path $Out "runner.pid"
if (Test-Path $pidFile) {
    $old = [int](Get-Content $pidFile)
    if (Get-Process -Id $old -ErrorAction SilentlyContinue) { throw "runner $old still alive" }
}
$PID | Set-Content $pidFile
foreach ($grain in 580, 4000) {
    $dir = Join-Path $Out "c$grain"
    $db = Join-Path $dir "erb_slice_c$grain.db"
    if (Test-Path $db) { "skip c$grain (exists)" | Add-Content (Join-Path $Out "rate.log"); continue }
    New-Item -ItemType Directory -Force $dir | Out-Null
    $env:CYMATIX_BFM_CHUNK_CHARS = "$grain"
    $env:PYTHONHASHSEED = "0"
    $env:PYTHONUTF8 = "1"
    Remove-Item Env:CYMATIX_ENCODER_URL, Env:CYMATIX_CONFIG -ErrorAction SilentlyContinue
    $t0 = Get-Date
    # PS 5.1: under "Stop", python's stderr logging becomes a terminating
    # NativeCommandError; the exit code is checked explicitly instead.
    $ErrorActionPreference = "Continue"
    & $Py "$repo\scripts\build_erb_blob_v09x.py" --out $db --out-dir $dir --workers 6 `
        --equivalence-receipt $gate --max-files $MaxFiles `
        --notes "grain ladder ERB rate probe c$grain first $MaxFiles files" *> (Join-Path $dir "build.log")
    $rc = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($rc -ne 0) { "c$grain build rc=$rc" | Add-Content (Join-Path $Out "rate.log"); continue }
    $wall = ((Get-Date) - $t0).TotalSeconds
    $genes = & $Py -c "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);print(c.execute('select count(*) from genes').fetchone()[0])" $db
    $bytes = (Get-Item $db).Length
    [pscustomobject]@{ grain = $grain; rc = $rc; files = $MaxFiles; genes = [int]$genes; bytes = $bytes;
        wall_s = [math]::Round($wall); genes_per_s = [math]::Round($genes / $wall, 1) } |
        ConvertTo-Json -Compress | Add-Content (Join-Path $Out "rate.jsonl")
}
"DONE" | Add-Content (Join-Path $Out "rate.log")
