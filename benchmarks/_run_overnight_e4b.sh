#!/usr/bin/env bash
# Overnight benchmark suite — gemma4:e4b vs current Helix.
#
# Estimated wall time: ~8.5h (comfortable inside 9h).
#
# What runs (in order):
#   A. bench_needle_1000   N=1000  (core NIAH, live genome snapshot)   ~5.25h
#   B. bench_dimensional_lock N=50 (multi-axis retrieval curve)         ~1.1h
#   C. bench_aa_suite gpqa diamond ON  N=100 (Helix-augmented)          ~1.1h
#   D. bench_aa_suite gpqa diamond OFF N=100 (baseline)                 ~1.1h
#   E. bench_multi_needle_50  (retrieval-only, fast)                    ~5m
#   F. bench_rag_vs_sike_tokens (token budget, no model)                ~5m
#
# Usage:
#   cd F:/Projects/helix-context
#   bash benchmarks/_run_overnight_e4b.sh 2>&1 | tee benchmarks/logs/overnight_$(date +%Y-%m-%d_%H%M).log
#
# Preconditions (checked at start):
#   - Helix running on :11437
#   - Ollama running with gemma4:e4b available
#   - Python env has httpx, datasets (for gpqa HF load)

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

MODEL="gemma4:e4b"
HELIX_URL="http://127.0.0.1:11437"
OLLAMA_URL="http://localhost:11434"
TS=$(date +%Y-%m-%d_%H%M)
OUT_DIR="benchmarks/results/overnight_e4b_${TS}"
LOG_DIR="benchmarks/logs"

mkdir -p "$OUT_DIR" "$LOG_DIR"

SUITE_START=$(date +%s)
PASS_COUNT=0
FAIL_COUNT=0
declare -A BENCH_ELAPSED
declare -A BENCH_STATUS

# ── Colour helpers ────────────────────────────────────────────────────────────
_ts()  { date +%H:%M:%S; }
log()  { echo "[$(date +%H:%M:%S)] $*"; }
ok()   { echo "[$(date +%H:%M:%S)] OK    $*"; }
warn() { echo "[$(date +%H:%M:%S)] WARN  $*"; }
fail() { echo "[$(date +%H:%M:%S)] FAIL  $*"; }

# ── Preflight ─────────────────────────────────────────────────────────────────
log "=== Preflight checks ==="

if ! curl -sf "${HELIX_URL}/health" >/dev/null 2>&1; then
    fail "Helix not responding at ${HELIX_URL} — aborting"; exit 1
fi
GENOME_GENES=$(curl -sf "${HELIX_URL}/stats" | python -c "import json,sys; print(json.load(sys.stdin)['total_genes'])")
ok "Helix UP — ${GENOME_GENES} genes"

if ! curl -sf "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; then
    fail "Ollama not responding at ${OLLAMA_URL} — aborting"; exit 1
fi
ok "Ollama UP"

log "Pre-warming ${MODEL}..."
curl -sf -X POST "${OLLAMA_URL}/api/generate" \
    -H "Content-Type: application/json" \
    -d "{\"model\": \"${MODEL}\", \"prompt\": \"hi\", \"stream\": false, \"options\": {\"num_predict\": 1}}" \
    >/dev/null && ok "${MODEL} warm" || warn "Pre-warm non-200 (may still load)"

echo ""
log "Output dir:  ${OUT_DIR}"
log "Branch:      $(git branch --show-current 2>/dev/null || echo 'unknown')"
log "Suite start: $(date)"
log "Est. finish: $(date -d "+8 hours 30 minutes" 2>/dev/null || echo '~8.5h from now')"
echo ""

# ── Per-bench runner ──────────────────────────────────────────────────────────
_run() {
    local label="$1"; shift
    local bench_log="${OUT_DIR}/${label}.log"
    local t0; t0=$(date +%s)
    log "─── ${label} ───"
    # Run command, tee stdout+stderr to log file
    if "$@" 2>&1 | tee "$bench_log"; then
        local elapsed=$(( $(date +%s) - t0 ))
        ok "${label} finished in $((elapsed/60))m$((elapsed%60))s"
        BENCH_ELAPSED["$label"]=$elapsed
        BENCH_STATUS["$label"]="PASS"
        PASS_COUNT=$(( PASS_COUNT + 1 ))
    else
        local elapsed=$(( $(date +%s) - t0 ))
        fail "${label} FAILED after $((elapsed/60))m$((elapsed%60))s — see ${bench_log}"
        BENCH_ELAPSED["$label"]=$elapsed
        BENCH_STATUS["$label"]="FAIL"
        FAIL_COUNT=$(( FAIL_COUNT + 1 ))
    fi
    echo ""
    sleep 3   # VRAM settle between benches
}

LIVE_GENOME="F:/Projects/helix-context/genomes/main/genome.db"

# ── A. bench_needle_1000 N=1000 ───────────────────────────────────────────────
_run "A_needle_1000" \
    env HELIX_MODEL="$MODEL" \
        GENOME_DB="$LIVE_GENOME" \
        N=1000 \
        SEED=42 \
        PYTHONIOENCODING=utf-8 \
        OUTPUT="${OUT_DIR}/needle_1000_e4b_${TS}.json" \
    python benchmarks/bench_needle_1000.py

# ── B. bench_dimensional_lock N=50 ───────────────────────────────────────────
_run "B_dimensional_lock_n50" \
    env HELIX_MODEL="$MODEL" \
        GENOME_DB="$LIVE_GENOME" \
        N=50 \
        SEED=42 \
        PYTHONIOENCODING=utf-8 \
        OUTPUT="${OUT_DIR}/dimensional_lock_n50_e4b_${TS}.json" \
    python benchmarks/bench_dimensional_lock.py

# ── C. GPQA diamond ON (Helix-augmented) ─────────────────────────────────────
_run "C_gpqa_on_n100" \
    python benchmarks/bench_aa_suite.py \
        --benchmark gpqa \
        --mode on \
        --model "$MODEL" \
        --limit 100 \
        --output "${OUT_DIR}/gpqa_on_diamond_e4b_n100_${TS}.json"

# ── D. GPQA diamond OFF (baseline) ───────────────────────────────────────────
_run "D_gpqa_off_n100" \
    python benchmarks/bench_aa_suite.py \
        --benchmark gpqa \
        --mode off \
        --model "$MODEL" \
        --limit 100 \
        --output "${OUT_DIR}/gpqa_off_diamond_e4b_n100_${TS}.json"

# ── E. bench_multi_needle_50 (retrieval only, no model) ──────────────────────
_run "E_multi_needle_50" \
    python -m benchmarks.bench_multi_needle_50

# Copy auto-named output into our directory
LATEST_MN=$(ls -t benchmarks/results/multi_needle_50_*.json 2>/dev/null | head -1)
[ -n "$LATEST_MN" ] && cp "$LATEST_MN" "${OUT_DIR}/multi_needle_50_${TS}.json"

# ── F. bench_rag_vs_sike_tokens (token budget, no model calls) ───────────────
_run "F_rag_vs_sike_n200" \
    env N=200 \
        PYTHONIOENCODING=utf-8 \
        OUTPUT="${OUT_DIR}/rag_vs_sike_n200_${TS}.json" \
    python benchmarks/bench_rag_vs_sike_tokens.py

# ── Summary report ────────────────────────────────────────────────────────────
SUITE_ELAPSED=$(( $(date +%s) - SUITE_START ))

{
echo "============================================================"
echo " Overnight Bench — gemma4:e4b — ${TS}"
echo " Total elapsed: $((SUITE_ELAPSED/60))m$((SUITE_ELAPSED%60))s"
echo " Passed: ${PASS_COUNT}  Failed: ${FAIL_COUNT}"
echo " Branch: $(git branch --show-current 2>/dev/null)"
echo " Genome: ${GENOME_GENES} genes  Helix: ${HELIX_URL}"
echo "============================================================"
echo ""
echo "Bench breakdown:"
for label in A_needle_1000 B_dimensional_lock_n50 C_gpqa_on_n100 D_gpqa_off_n100 E_multi_needle_50 F_rag_vs_sike_n200; do
    status="${BENCH_STATUS[$label]:-skipped}"
    elapsed="${BENCH_ELAPSED[$label]:-0}"
    if [ "$status" = "skipped" ]; then
        printf "  %-35s  skipped\n" "$label"
    elif [ "$status" = "FAIL" ]; then
        printf "  %-35s  FAIL  (%dm%ds)\n" "$label" $(( elapsed/60 )) $(( elapsed%60 ))
    else
        printf "  %-35s  ok    (%dm%ds)\n" "$label" $(( elapsed/60 )) $(( elapsed%60 ))
    fi
done
echo ""
echo "Output files:"
ls -1 "${OUT_DIR}/"*.json 2>/dev/null | while read -r f; do
    sz=$(wc -c < "$f" 2>/dev/null || echo 0)
    printf "  %7d bytes  %s\n" "$sz" "$(basename "$f")"
done
echo ""
echo "Suite end: $(date)"
} | tee "${OUT_DIR}/suite_summary_${TS}.txt"

# ── Key metric extraction ─────────────────────────────────────────────────────
echo ""
log "=== Key metrics ==="
python - <<PYEOF 2>/dev/null || true
import json, glob, os

d = "${OUT_DIR}"

def load_first(pat):
    hits = sorted(glob.glob(os.path.join(d, pat)))
    return json.load(open(hits[-1])) if hits else None

n1k = load_first("needle_1000_*.json")
if n1k:
    s = n1k.get("summary", {})
    print(f"  needle-1000  retr={s.get('retrieval_rate',0)*100:.1f}%  ans={s.get('answer_accuracy_rate',0)*100:.1f}%  n={s.get('n','?')}")

dl = load_first("dimensional_lock_*.json")
if dl:
    curve = dl.get("retrieval_by_axis_count", dl.get("axis_recall_curve", {}))
    if curve:
        print(f"  dim-lock curve: {curve}")

gon = load_first("gpqa_on_*.json")
goff = load_first("gpqa_off_*.json")
if gon:
    so = gon.get("summary", {})
    print(f"  gpqa ON   ans={so.get('answer_accuracy_rate',0)*100:.1f}%  n={so.get('n','?')}")
if goff:
    sf = goff.get("summary", {})
    print(f"  gpqa OFF  ans={sf.get('answer_accuracy_rate',0)*100:.1f}%  n={sf.get('n','?')}")
if gon and goff:
    delta = (gon.get("summary",{}).get("answer_accuracy_rate",0) - goff.get("summary",{}).get("answer_accuracy_rate",0)) * 100
    print(f"  gpqa delta (on - off): {delta:+.1f}pp")
PYEOF

if [ "$FAIL_COUNT" -gt 0 ]; then
    warn "${FAIL_COUNT} bench(es) FAILED — check logs in ${OUT_DIR}/"
    exit 1
fi
ok "All benchmarks complete. Results in ${OUT_DIR}/"
