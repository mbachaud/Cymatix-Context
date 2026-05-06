#!/usr/bin/env bash
# Overnight benchmark orchestrator — kicked off 2026-04-29 evening.
# Captures real-dataset GPQA + SciCode (off vs on) plus the existing
# mock A/B suite (IFBench, AA-Omniscience, CritPt). Logs everything
# with timestamps to overnight_logs/overnight_2026-04-29.log.

set -u  # treat unset vars as errors; intentionally NOT -e so a single
        # benchmark failure does not nuke the whole run.

cd "$(dirname "$0")"
mkdir -p overnight_logs benchmarks/results

LOG=overnight_logs/overnight_2026-04-29.log
STATUS=overnight_logs/overnight_2026-04-29.status
REPORT=overnight_logs/overnight_2026-04-29_report.md

PYBIN=py
PYARGS="-3"
MODEL=gemma4:e4b
GPQA_LIMIT=100
SCICODE_LIMIT=100   # SciCode dataset has fewer; this is an upper bound.

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG"; }

write_status() {
  echo "$1" > "$STATUS"
}

run_step() {
  local name="$1"; shift
  log "=== START: $name ==="
  write_status "RUNNING: $name (started $(ts))"
  local t0=$(date +%s)
  "$@" >>"$LOG" 2>&1
  local rc=$?
  local t1=$(date +%s)
  local dur=$((t1 - t0))
  if [ $rc -eq 0 ]; then
    log "=== DONE:  $name  (rc=$rc, ${dur}s) ==="
  else
    log "=== FAIL:  $name  (rc=$rc, ${dur}s) — continuing ==="
  fi
  return $rc
}

run_bench() {
  local benchmark="$1"
  local mode="$2"
  local out="$3"
  local extra=("${@:4}")
  run_step "${benchmark} ${mode}" \
    $PYBIN $PYARGS benchmarks/bench_aa_suite.py \
      --benchmark "$benchmark" \
      --mode "$mode" \
      --model "$MODEL" \
      --output "$out" \
      "${extra[@]}"
}

compare() {
  local off="$1"
  local on="$2"
  local label="$3"
  echo "" >> "$REPORT"
  echo "## $label" >> "$REPORT"
  echo "" >> "$REPORT"
  echo "\`\`\`" >> "$REPORT"
  $PYBIN $PYARGS benchmarks/compare_ab.py "$off" "$on" >> "$REPORT" 2>&1 \
    || echo "compare_ab.py failed" >> "$REPORT"
  echo "\`\`\`" >> "$REPORT"
}

# -- Begin run -------------------------------------------------------

log "Overnight benchmark run starting"
log "Model: $MODEL    GPQA limit: $GPQA_LIMIT    SciCode limit: $SCICODE_LIMIT"
log "Helix server health:"
curl -s -m 5 http://127.0.0.1:11437/health >> "$LOG" 2>&1 || true
echo "" >> "$LOG"

write_status "RUNNING: setup"

# -- Real-dataset benchmarks -----------------------------------------

# GPQA off / on (HuggingFace dataset; real)
run_bench gpqa off    benchmarks/results/gpqa_off_2026-04-29.json    --limit "$GPQA_LIMIT"
run_bench gpqa on     benchmarks/results/gpqa_on_2026-04-29.json     --limit "$GPQA_LIMIT"

# SciCode off / on (HuggingFace dataset; real)
run_bench scicode off benchmarks/results/scicode_off_2026-04-29.json --limit "$SCICODE_LIMIT"
run_bench scicode on  benchmarks/results/scicode_on_2026-04-29.json  --limit "$SCICODE_LIMIT"

# -- Mock-dataset A/B suite (small, but completes the picture) -------

run_bench ifbench         off benchmarks/results/ifbench_off_2026-04-29.json
run_bench ifbench         on  benchmarks/results/ifbench_on_2026-04-29.json
run_bench aa-omniscience  off benchmarks/results/omn_off_2026-04-29.json
run_bench aa-omniscience  on  benchmarks/results/omn_on_2026-04-29.json
run_bench critpt          off benchmarks/results/critpt_off_2026-04-29.json
run_bench critpt          on  benchmarks/results/critpt_on_2026-04-29.json

# -- Aggregate report -------------------------------------------------

write_status "RUNNING: report generation"
log "=== Generating aggregated report ==="

cat > "$REPORT" <<EOF
# Overnight Benchmark Report — 2026-04-29

**Started:** $(head -1 "$LOG" | sed 's/^\[\(.*\)\].*/\1/')
**Completed:** $(ts)
**Model:** $MODEL
**Helix server:** http://127.0.0.1:11437 (17,090 genes at start)
**Branch:** master ($(git rev-parse --short HEAD))
**WIP in working tree:** accel.py expand_query_terms + lexical_rescue + chunk_fetch + relevance_window (gemini's uncommitted work)
EOF

compare benchmarks/results/gpqa_off_2026-04-29.json    benchmarks/results/gpqa_on_2026-04-29.json    "GPQA Diamond (real, n≤$GPQA_LIMIT)"
compare benchmarks/results/scicode_off_2026-04-29.json benchmarks/results/scicode_on_2026-04-29.json "SciCode (real, n≤$SCICODE_LIMIT)"
compare benchmarks/results/ifbench_off_2026-04-29.json benchmarks/results/ifbench_on_2026-04-29.json "IFBench (mock, smoke)"
compare benchmarks/results/omn_off_2026-04-29.json     benchmarks/results/omn_on_2026-04-29.json     "AA-Omniscience (mock, smoke)"
compare benchmarks/results/critpt_off_2026-04-29.json  benchmarks/results/critpt_on_2026-04-29.json  "CritPt (mock, smoke)"

echo "" >> "$REPORT"
echo "## Per-benchmark error counts" >> "$REPORT"
echo "" >> "$REPORT"
echo "\`\`\`" >> "$REPORT"
for f in benchmarks/results/*_2026-04-29.json; do
  $PYBIN $PYARGS -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    res = d.get('results', [])
    n = len(res)
    correct = sum(1 for r in res if r.get('answer_correct'))
    errs = sum(1 for r in res if r.get('error'))
    print(f'{sys.argv[1]:60s} n={n:4d} correct={correct:4d} errors={errs:4d}')
except Exception as e:
    print(f'{sys.argv[1]}: PARSE ERROR {e}')
" "$f" >> "$REPORT" 2>&1
done
echo "\`\`\`" >> "$REPORT"

write_status "DONE at $(ts)"
log "Overnight benchmark run COMPLETE — report at $REPORT"
