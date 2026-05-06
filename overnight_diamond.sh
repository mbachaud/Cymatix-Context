#!/usr/bin/env bash
# Full GPQA Diamond overnight — kicked off 2026-05-01.
# Runs n=198 (full diamond) off + on with bumped timeouts on both
# layers (httpx client 180s, helix upstream 240s in helix.toml).
# Auto-reverts helix.toml at end.

set -u

cd "$(dirname "$0")"
mkdir -p overnight_logs benchmarks/results

LOG=overnight_logs/diamond_2026-05-01.log
STATUS=overnight_logs/diamond_2026-05-01.status
REPORT=overnight_logs/diamond_2026-05-01_report.md
HELIX_TOML=helix.toml
HELIX_TOML_BACKUP=helix.toml.bench-backup

MODEL=gemma4:e4b
CLIENT_TIMEOUT=180

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG"; }
write_status() { echo "$1" > "$STATUS"; }

# Save current helix.toml so we can restore it at end (the upstream_timeout=240
# bump is for this run only — not committed).
cp -p "$HELIX_TOML" "$HELIX_TOML_BACKUP"

revert_config() {
  if [ -f "$HELIX_TOML_BACKUP" ]; then
    log "Reverting $HELIX_TOML to pre-run state"
    cp -p "$HELIX_TOML_BACKUP" "$HELIX_TOML"
    rm "$HELIX_TOML_BACKUP"
  fi
}
# Ensure config is reverted even on Ctrl-C / kill.
trap 'revert_config; write_status "INTERRUPTED at $(ts)"' INT TERM

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
  local mode="$1"
  local out="$2"
  run_step "gpqa $mode (full diamond)" \
    py -3 -u benchmarks/bench_aa_suite.py \
      --benchmark gpqa \
      --mode "$mode" \
      --model "$MODEL" \
      --timeout "$CLIENT_TIMEOUT" \
      --output "$out"
}

# -- Begin run -------------------------------------------------------

log "Diamond overnight run starting"
log "Model: $MODEL    Client timeout: ${CLIENT_TIMEOUT}s    Helix upstream_timeout: 240s (helix.toml)"
log "Helix server health:"
curl -s -m 5 http://127.0.0.1:11437/health >> "$LOG" 2>&1 || true
echo "" >> "$LOG"
log "WIP in working tree: gemini's accel.py expand_query_terms + lexical_rescue + chunk_fetch + relevance_window"
log "Branch: $(git rev-parse --abbrev-ref HEAD)  HEAD: $(git rev-parse --short HEAD)"

# -- Full Diamond off / on -------------------------------------------

run_bench off benchmarks/results/gpqa_off_diamond_2026-05-01.json
run_bench on  benchmarks/results/gpqa_on_diamond_2026-05-01.json

# -- Aggregate report ------------------------------------------------

write_status "RUNNING: report generation"
log "=== Generating aggregated report ==="

OFF=benchmarks/results/gpqa_off_diamond_2026-05-01.json
ON=benchmarks/results/gpqa_on_diamond_2026-05-01.json

cat > "$REPORT" <<EOF
# GPQA Diamond Overnight Report — 2026-05-01

**Started:** $(head -1 "$LOG" | sed 's/^\[\(.*\)\].*/\1/')
**Completed:** $(ts)
**Model:** $MODEL
**Helix server:** http://127.0.0.1:11437
**Branch:** $(git rev-parse --abbrev-ref HEAD)  HEAD: $(git rev-parse --short HEAD)
**Timeouts:** httpx client = ${CLIENT_TIMEOUT}s, helix upstream = 240s
**WIP:** gemini's accel.py expand_query_terms + lexical_rescue + chunk_fetch + relevance_window

## Headline numbers

EOF

py -3 <<'PYEOF' >> "$REPORT" 2>&1
import json, sys
off = json.load(open("benchmarks/results/gpqa_off_diamond_2026-05-01.json"))
on  = json.load(open("benchmarks/results/gpqa_on_diamond_2026-05-01.json"))

def stats(d, label):
    res = d['results']
    n = len(res)
    succ = [r for r in res if not r.get('error')]
    fail = [r for r in res if r.get('error')]
    correct = sum(1 for r in succ if r.get('answer_correct'))
    proxy_succ = sorted(r['proxy_latency_s'] for r in succ if r['proxy_latency_s'] > 0)
    err_kinds = {}
    for r in fail:
        e = r.get('error') or ''
        kind = 'timeout' if 'timed out' in e else ('proxy_500' if '500' in e else 'other')
        err_kinds[kind] = err_kinds.get(kind, 0) + 1
    def pct(L, p):
        if not L: return 0
        return L[max(0, min(len(L)-1, int(len(L)*p)))]
    return {
        'n': n, 'succ': len(succ), 'fail': len(fail),
        'correct': correct,
        'p50': pct(proxy_succ, 0.5),
        'p90': pct(proxy_succ, 0.9),
        'p95': pct(proxy_succ, 0.95),
        'mx':  max(proxy_succ) if proxy_succ else 0,
        'err_kinds': err_kinds,
    }

so = stats(off, 'OFF'); sn = stats(on, 'ON')

print(f"| | n | completed | correct | accuracy (of completed) | errors | error breakdown |")
print(f"|---|---|---|---|---|---|---|")
for label, s in [('OFF', so), ('ON', sn)]:
    acc = (s['correct']/s['succ']*100) if s['succ'] else 0
    eb = ', '.join(f'{k}={v}' for k,v in s['err_kinds'].items()) or '—'
    print(f"| {label} | {s['n']} | {s['succ']} | {s['correct']} | {acc:.1f}% | {s['fail']} | {eb} |")

# Apples-to-apples on the intersection
on_by_id  = {r['id']: r for r in on['results']}
off_by_id = {r['id']: r for r in off['results']}
both_ok = [pid for pid in on_by_id
           if not on_by_id[pid].get('error') and not off_by_id.get(pid, {}).get('error')]
oc = sum(1 for pid in both_ok if off_by_id[pid].get('answer_correct'))
nc = sum(1 for pid in both_ok if on_by_id[pid].get('answer_correct'))
print()
print("## Apples-to-apples (only problems where BOTH modes completed)")
print()
print(f"- n_both_completed = **{len(both_ok)}**")
print(f"- OFF correct: {oc}/{len(both_ok)} = {100*oc/max(len(both_ok),1):.1f}%")
print(f"- ON  correct: {nc}/{len(both_ok)} = {100*nc/max(len(both_ok),1):.1f}%")
delta_pp = 100*nc/max(len(both_ok),1) - 100*oc/max(len(both_ok),1)
print(f"- **Delta: {delta_pp:+.1f}pp**")

# Latency story
print()
print("## Latency on successes")
print()
print(f"| | p50 | p90 | p95 | max |")
print(f"|---|---|---|---|---|")
for label, s in [('OFF', so), ('ON', sn)]:
    print(f"| {label} | {s['p50']:.1f}s | {s['p90']:.1f}s | {s['p95']:.1f}s | {s['mx']:.1f}s |")
PYEOF

echo "" >> "$REPORT"
echo "## Raw compare_ab" >> "$REPORT"
echo "" >> "$REPORT"
echo "\`\`\`" >> "$REPORT"
py -3 benchmarks/compare_ab.py "$OFF" "$ON" >> "$REPORT" 2>&1 || echo "(compare_ab.py failed)" >> "$REPORT"
echo "\`\`\`" >> "$REPORT"

# Restore helix.toml
revert_config

write_status "DONE at $(ts)"
log "Diamond overnight run COMPLETE — report at $REPORT"
log "helix.toml has been reverted to pre-run state. Server still has 240s upstream_timeout in memory; restart helix to pick up the reverted config."
