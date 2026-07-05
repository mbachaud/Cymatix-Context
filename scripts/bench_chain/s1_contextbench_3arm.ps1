# Stage 1 — 3-arm ContextBench (council plan step 2 / run plan 2026-07-01)
# Arm A: BM25 foil = frozen baseline (0.484 packet line recall, stored)
# Arm B: master (cAST default-on) with the committed lexical probe config
# Arm C: ws3 worktree code (WS2+WS3, rebased on master) + symbol_graph=true
$ErrorActionPreference = 'Continue'
$repo = 'F:\Projects\helix-context'
$logs = "$repo\benchmarks\logs"
$ts = Get-Date -Format 'yyyy-MM-dd_HHmm'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$env:HELIX_OTEL_ENABLED = '1'
$env:HELIX_OTEL_ENDPOINT = 'localhost:4317'

function Set-Status($stage, $state) {
    @{stage=$stage; state=$state; at=(Get-Date -Format o)} | ConvertTo-Json |
        Set-Content "$repo\benchmarks\logs\chain_status.json"
}

Set-Status 's1_3arm' 'building-armC-config'
# Build arm-C toml: lexical probe + symbol knobs (python keeps TOML valid).
$py = @'
import io, tomllib
src = r"F:\Projects\helix-context\docs\benchmarks\helix_probe_lexical.toml"
dst = r"F:\Projects\helix-context\docs\benchmarks\helix_probe_symbol.toml"
t = io.open(src, encoding="utf-8").read()
def set_key(t, section, line):
    hdr = f"[{section}]"
    if hdr in t:
        return t.replace(hdr, hdr + "\n" + line, 1)
    return t + f"\n{hdr}\n{line}\n"
t = set_key(t, "ingestion", "symbol_graph = true")
t = set_key(t, "retrieval", "symbol_expansion_cap = 8")
io.open(dst, "w", encoding="utf-8").write(t)
tomllib.load(open(dst, "rb"))
print("arm-C toml ok")
'@
$py | Set-Content "$env:TEMP\mk_symbol_toml.py" -Encoding UTF8
python "$env:TEMP\mk_symbol_toml.py"
if ($LASTEXITCODE -ne 0) { Set-Status 's1_3arm' 'FAILED-armC-config'; exit 1 }

cd $repo
# ── Arm B: master code ───────────────────────────────────────────────
Set-Status 's1_3arm' 'armB-running'
Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
python benchmarks\cb_helix_pred.py --tag armB_cast_master_$ts `
    --config docs\benchmarks\helix_probe_lexical.toml --workers 3 `
    *> "$logs\s1_armB_$ts.log"
$armB_exit = $LASTEXITCODE

# ── Arm C: ws3 worktree code (WS2+WS3) + symbol config ──────────────
Set-Status 's1_3arm' 'armC-running'
$env:PYTHONPATH = 'F:\Projects\helix-context\.worktrees\ws3-pagerank'
python benchmarks\cb_helix_pred.py --tag armC_symbol_ws3_$ts `
    --config docs\benchmarks\helix_probe_symbol.toml --workers 3 `
    *> "$logs\s1_armC_$ts.log"
$armC_exit = $LASTEXITCODE
Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue

# ── AST-path assertion (run-plan gap #2): chunk_regex must be 0 ─────
Set-Status 's1_3arm' 'ast-assertion'
$regexHits = (Select-String -Path "$logs\s1_armB_$ts.log","$logs\s1_armC_$ts.log" `
    -Pattern 'chunking path: regex|fell back to regex' -ErrorAction SilentlyContinue |
    Measure-Object).Count
"AST assertion: chunk_regex log lines = $regexHits (must be 0 for code corpora)" |
    Add-Content "$logs\s1_summary_$ts.log"

# ── Score both arms with the cb-step0 venv ──────────────────────────
Set-Status 's1_3arm' 'scoring'
$scorer = 'F:\Projects\_venvs\cb-step0\Scripts\python.exe'
& $scorer benchmarks\cb_score_all.py *> "$logs\s1_score_$ts.log"

"armB_exit=$armB_exit armC_exit=$armC_exit" | Add-Content "$logs\s1_summary_$ts.log"
if ($armB_exit -eq 0 -and $armC_exit -eq 0) { Set-Status 's1_3arm' 'DONE' }
else { Set-Status 's1_3arm' "DONE-WITH-ERRORS(B=$armB_exit,C=$armC_exit)" }
