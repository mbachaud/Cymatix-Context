#!/usr/bin/env bash
# Fresh GPQA Diamond pass — 2026-05-05.
# e4b (off + on) -> qwen3:8b (off + on), full n=198 each.
# Last full e4b run was 2026-05-03; this is a re-baseline before the
# qwen3:8b head-to-head.
#
# Runtime expectation: ~8-10 hours total based on the 2026-05-01 e4b
# numbers (off=129min). qwen3:8b is roughly comparable.
#
# helix.toml is NOT modified; default upstream_timeout used.
# Helix server expected to already be running on :11437.

set -u

cd "$(dirname "$0")"
mkdir -p overnight_logs benchmarks/results

DATE=2026-05-05
LOG=overnight_logs/diamond_${DATE}_e4b_qwen3.log
STATUS=overnight_logs/diamond_${DATE}_e4b_qwen3.status
REPORT=overnight_logs/diamond_${DATE}_e4b_qwen3_report.md

CLIENT_TIMEOUT=180
HELIX_URL=http://127.0.0.1:11437

E4B_MODEL=gemma4:e4b
QWEN_MODEL=qwen3:8b

E4B_OFF=benchmarks/results/gpqa_off_diamond_${DATE}_e4b.json
E4B_ON=benchmarks/results/gpqa_on_diamond_${DATE}_e4b.json
QWEN_OFF=benchmarks/results/gpqa_off_diamond_${DATE}_qwen3-8b.json
QWEN_ON=benchmarks/results/gpqa_on_diamond_${DATE}_qwen3-8b.json

# Prior e4b baseline (2026-05-03) for delta comparison.
E4B_OFF_PRIOR=benchmarks/results/gpqa_off_diamond_2026-05-03.json
E4B_ON_PRIOR=benchmarks/results/gpqa_on_diamond_2026-05-03.json

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG"; }
write_status() { echo "$1" > "$STATUS"; }

trap 'log "Caught signal; bailing"; write_status "INTERRUPTED at $(ts)"; exit 130' INT TERM

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
    log "=== DONE:  $name  (rc=$rc, ${dur}s = $((dur/60))min) ==="
  else
    log "=== FAIL:  $name  (rc=$rc, ${dur}s) — continuing ==="
  fi
  return $rc
}

run_bench() {
  local model="$1"; local mode="$2"; local out="$3"
  run_step "gpqa $mode model=$model (full diamond)" \
    py -3 -u benchmarks/bench_aa_suite.py \
      --benchmark gpqa \
      --mode "$mode" \
      --model "$model" \
      --timeout "$CLIENT_TIMEOUT" \
      --output "$out"
}

# -- Begin run -------------------------------------------------------

log "============================================================"
log "Fresh diamond pass starting (e4b -> qwen3:8b, n=198 each)"
log "Branch: $(git rev-parse --abbrev-ref HEAD)  HEAD: $(git rev-parse --short HEAD)"
log "Models: $E4B_MODEL then $QWEN_MODEL"
log "Client timeout: ${CLIENT_TIMEOUT}s"
log "Outputs:"
log "  $E4B_OFF"
log "  $E4B_ON"
log "  $QWEN_OFF"
log "  $QWEN_ON"
log "============================================================"

log "Helix server health:"
curl -s -m 5 "$HELIX_URL/health" >> "$LOG" 2>&1 || true
echo "" >> "$LOG"

# Phase 1: e4b
run_bench "$E4B_MODEL" off "$E4B_OFF"
run_bench "$E4B_MODEL" on  "$E4B_ON"

# Phase 2: qwen3:8b (only after e4b finishes)
run_bench "$QWEN_MODEL" off "$QWEN_OFF"
run_bench "$QWEN_MODEL" on  "$QWEN_ON"

# -- Aggregate report -----------------------------------------------

log ""
log "=== Generating 4-way report ==="
write_status "RUNNING: report at $(ts)"

cat > "$REPORT" <<EOF
# GPQA Diamond fresh pass — ${DATE}

**Started:** $(head -1 "$LOG" | sed 's/^\[\(.*\)\].*/\1/')
**Completed:** $(ts)
**Models:** \`$E4B_MODEL\` then \`$QWEN_MODEL\`
**Helix server:** $HELIX_URL
**Branch:** $(git rev-parse --abbrev-ref HEAD)  HEAD: $(git rev-parse --short HEAD)
**Client timeout:** ${CLIENT_TIMEOUT}s
**Mode definitions:**
- \`off\`: background injected directly into prompt (no helix retrieval).
- \`on\`: helix \`/context\` queried first; LLM then answers from question alone.

EOF

py -3 - "$E4B_OFF" "$E4B_ON" "$QWEN_OFF" "$QWEN_ON" "$E4B_OFF_PRIOR" "$E4B_ON_PRIOR" >> "$REPORT" 2>&1 <<'PYEOF'
import json, sys, os

def load(p):
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)

def pct(L, p):
    if not L:
        return 0.0
    return L[max(0, min(len(L) - 1, int(len(L) * p)))]

def stats(d, label):
    if d is None:
        return {"label": label, "missing": True}
    res = d.get("results", [])
    succ = [r for r in res if not r.get("error")]
    fail = [r for r in res if r.get("error")]
    correct = sum(1 for r in succ if r.get("answer_correct"))
    found = sum(1 for r in succ if r.get("found_in_context"))
    lats = sorted(r.get("proxy_latency_s", 0) for r in succ if r.get("proxy_latency_s", 0) > 0)
    err_kinds = {}
    for r in fail:
        e = r.get("error") or ""
        kind = "timeout" if "timed out" in e else ("proxy_500" if "500" in e else "other")
        err_kinds[kind] = err_kinds.get(kind, 0) + 1
    return {
        "label": label,
        "missing": False,
        "n": len(res),
        "succ": len(succ),
        "fail": len(fail),
        "correct": correct,
        "found": found,
        "p50": pct(lats, 0.5),
        "p90": pct(lats, 0.9),
        "p95": pct(lats, 0.95),
        "mx": max(lats) if lats else 0.0,
        "err_kinds": err_kinds,
    }

paths = sys.argv[1:7]
labels = ["e4b OFF (fresh)", "e4b ON (fresh)", "qwen3:8b OFF", "qwen3:8b ON",
          "e4b OFF (2026-05-03)", "e4b ON (2026-05-03)"]
runs = [stats(load(p), lab) for p, lab in zip(paths, labels)]

print("## Headline numbers")
print()
print("| run | n | completed | correct | accuracy (of completed) | found_in_ctx | errors |")
print("|---|---|---|---|---|---|---|")
for s in runs:
    if s.get("missing"):
        print(f"| {s['label']} | — | — | — | MISSING | — | — |")
        continue
    acc = (s["correct"] / s["succ"] * 100) if s["succ"] else 0
    eb = ", ".join(f"{k}={v}" for k, v in s["err_kinds"].items()) or "—"
    print(f"| {s['label']} | {s['n']} | {s['succ']} | {s['correct']} | {acc:.1f}% | {s['found']} | {eb} |")

print()
print("## Latency on successes")
print()
print("| run | p50 | p90 | p95 | max |")
print("|---|---|---|---|---|")
for s in runs:
    if s.get("missing"):
        continue
    print(f"| {s['label']} | {s['p50']:.1f}s | {s['p90']:.1f}s | {s['p95']:.1f}s | {s['mx']:.1f}s |")

# Apples-to-apples deltas where both runs of a pair completed.
def pair_delta(off, on, label):
    if off is None or on is None:
        return
    on_by_id = {r["id"]: r for r in on.get("results", [])}
    off_by_id = {r["id"]: r for r in off.get("results", [])}
    both_ok = [pid for pid in on_by_id
               if not on_by_id[pid].get("error") and not off_by_id.get(pid, {}).get("error")]
    if not both_ok:
        return
    oc = sum(1 for pid in both_ok if off_by_id[pid].get("answer_correct"))
    nc = sum(1 for pid in both_ok if on_by_id[pid].get("answer_correct"))
    print(f"### {label}")
    print()
    print(f"- n_both_completed = **{len(both_ok)}**")
    print(f"- OFF correct: {oc}/{len(both_ok)} = {100*oc/len(both_ok):.1f}%")
    print(f"- ON  correct: {nc}/{len(both_ok)} = {100*nc/len(both_ok):.1f}%")
    print(f"- **Delta: {100*nc/len(both_ok) - 100*oc/len(both_ok):+.1f}pp**")
    print()

print()
print("## Apples-to-apples (only problems where BOTH off+on modes completed)")
print()
e4b_off, e4b_on, qwen_off, qwen_on, e4b_off_prior, e4b_on_prior = [load(p) for p in paths]
pair_delta(e4b_off, e4b_on, "e4b (fresh)")
pair_delta(qwen_off, qwen_on, "qwen3:8b")
pair_delta(e4b_off_prior, e4b_on_prior, "e4b (2026-05-03 reference)")
PYEOF

write_status "DONE at $(ts)"
log "Diamond fresh pass COMPLETE — report at $REPORT"
