#!/usr/bin/env bash
# Full GPQA Diamond overnight — ABSTAIN tier in-run control bench (spec §9, take 2).
# Date: 2026-05-03b. Branch: feat/abstain-tier. HEAD: 24dd0cf.
#
# Today's morning OFF/ON bench showed +14.7pp accuracy with abstain ON, but the
# OFF run was a "no helix" baseline, not an abstain-disabled control on the same
# gene state. This script produces the missing control by running TWO ON cells
# back-to-back on the same gene state:
#   Cell 1: ON with abstain DISABLED (helix.toml abstain_enabled = false)
#   Cell 2: ON with abstain ENABLED  (helix.toml abstain_enabled = true)
# Then a head-to-head report compares them.
#
# Today's no-helix OFF baseline is benchmarks/results/gpqa_off_diamond_2026-05-03.json
# (available for reference, not the comparison axis here).
#
# Spec: docs/specs/2026-05-02-abstain-tier-design.md

set -u

cd "$(dirname "$0")"
mkdir -p overnight_logs benchmarks/results

LOG=overnight_logs/diamond_2026-05-03b.log
STATUS=overnight_logs/diamond_2026-05-03b.status
REPORT=overnight_logs/diamond_2026-05-03b_report.md
HELIX_TOML=helix.toml
HELIX_TOML_BACKUP=helix.toml.bench-backup

MODEL=gemma4:e4b
CLIENT_TIMEOUT=180
HELIX_PORT=11437
HEALTH_URL="http://127.0.0.1:${HELIX_PORT}/health"
CONTEXT_URL="http://127.0.0.1:${HELIX_PORT}/context"

CELL_A_OUT=benchmarks/results/gpqa_on_disabled_2026-05-03b.json
CELL_B_OUT=benchmarks/results/gpqa_on_enabled_2026-05-03b.json

CELL_A_LOG=overnight_logs/helix_server_2026-05-03b_disabled.log
CELL_A_ERR=overnight_logs/helix_server_2026-05-03b_disabled.err
CELL_A_PID=overnight_logs/helix_server_2026-05-03b_disabled.pid
CELL_A_SMOKE=overnight_logs/abstain_smoke_2026-05-03b_disabled.json

CELL_B_LOG=overnight_logs/helix_server_2026-05-03b_enabled.log
CELL_B_ERR=overnight_logs/helix_server_2026-05-03b_enabled.err
CELL_B_PID=overnight_logs/helix_server_2026-05-03b_enabled.pid
CELL_B_SMOKE=overnight_logs/abstain_smoke_2026-05-03b_enabled.json

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG"; }
write_status() { echo "$1" > "$STATUS"; }

# Save current helix.toml so we can restore it at end.
cp -p "$HELIX_TOML" "$HELIX_TOML_BACKUP"

# Track the currently-spawned bench server PID so we can clean it up on exit.
CURRENT_SERVER_PID=""

stop_server() {
  local pid="$1"
  local label="$2"
  if [ -z "$pid" ]; then return 0; fi
  if kill -0 "$pid" 2>/dev/null; then
    log "Stopping $label server PID=$pid"
    taskkill //PID "$pid" //F >>"$LOG" 2>&1 || true
    # Give Windows a moment to release the port.
    local i=0
    while kill -0 "$pid" 2>/dev/null && [ $i -lt 10 ]; do
      sleep 1
      i=$((i+1))
    done
  else
    log "$label server PID=$pid already gone"
  fi
}

revert_config() {
  if [ -f "$HELIX_TOML_BACKUP" ]; then
    log "Reverting $HELIX_TOML to pre-run state"
    cp -p "$HELIX_TOML_BACKUP" "$HELIX_TOML"
    rm "$HELIX_TOML_BACKUP"
  fi
}

cleanup() {
  local rc=$?
  if [ -n "$CURRENT_SERVER_PID" ]; then
    stop_server "$CURRENT_SERVER_PID" "(cleanup)"
    CURRENT_SERVER_PID=""
  fi
  revert_config
  if [ "$rc" -ne 0 ]; then
    write_status "EXITED rc=$rc at $(ts)"
  fi
}
# Cleanup on any exit, including success — revert + kill any spawned server.
trap 'cleanup' EXIT
trap 'log "Caught signal; cleaning up"; write_status "INTERRUPTED at $(ts)"; exit 130' INT TERM

# Set abstain_enabled to a given value (true|false) inside the [budget] block.
# Uses sed to rewrite the abstain_enabled = ... line directly. The current
# helix.toml has exactly one such line, on line 68; we match by key name to
# stay robust to small line shifts.
set_abstain() {
  local val="$1"
  if ! grep -q '^abstain_enabled' "$HELIX_TOML"; then
    log "ERROR: no 'abstain_enabled' line in $HELIX_TOML — bailing"
    return 1
  fi
  # Use a temp file so sed -i works portably on Windows.
  sed -E "s/^(abstain_enabled[[:space:]]*=[[:space:]]*)(true|false)/\1${val}/" \
    "$HELIX_TOML" > "${HELIX_TOML}.tmp" && mv "${HELIX_TOML}.tmp" "$HELIX_TOML"
  local now
  now=$(grep '^abstain_enabled' "$HELIX_TOML" | head -1)
  log "helix.toml now: $now"
  if ! echo "$now" | grep -q "= ${val}"; then
    log "ERROR: failed to set abstain_enabled=${val} (got: $now)"
    return 1
  fi
}

# Spawn a fresh helix server in the background. Args: log err pid_file
start_server() {
  local out_log="$1" out_err="$2" pid_file="$3"
  log "Starting fresh helix server: log=$out_log err=$out_err pid=$pid_file"
  # Subshell + & detaches from this script so it survives until we kill it.
  (
    py -3 -u -m uvicorn helix_context.server:app \
      --host 127.0.0.1 --port "$HELIX_PORT" \
      >"$out_log" 2>"$out_err" &
    echo $! > "$pid_file"
  )
  # Re-read the pid the subshell wrote.
  local pid
  pid=$(cat "$pid_file" 2>/dev/null || echo "")
  if [ -z "$pid" ]; then
    log "ERROR: failed to capture spawned PID"
    return 1
  fi
  log "Spawned helix server PID=$pid"
  CURRENT_SERVER_PID="$pid"
  return 0
}

# Wait for /health 200 + the "Application startup complete." + "Uvicorn running"
# marker in the err log. Up to 90s. Returns 0 on ready.
wait_for_ready() {
  local err_log="$1"
  local pid="$2"
  local deadline=$(( $(date +%s) + 90 ))
  log "Waiting up to 90s for /health 200 + Uvicorn ready markers..."
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      log "ERROR: helix server PID=$pid died during warmup; tail of err:"
      tail -40 "$err_log" >>"$LOG" 2>&1 || true
      return 1
    fi
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "$HEALTH_URL" 2>/dev/null || echo "000")
    if [ "$code" = "200" ] \
       && grep -q "Application startup complete" "$err_log" 2>/dev/null \
       && grep -q "Uvicorn running on http://127.0.0.1:${HELIX_PORT}" "$err_log" 2>/dev/null; then
      log "Helix server ready (health=200, uvicorn marker seen)"
      return 0
    fi
    sleep 2
  done
  log "ERROR: helix server did not become ready within 90s"
  log "Tail of err log:"
  tail -60 "$err_log" >>"$LOG" 2>&1 || true
  return 1
}

# POST a noise query; expect a particular budget_tier ("abstain" or "broad").
# Args: out_file expected_tier
smoke_test() {
  local out_file="$1" expected="$2"
  local body='{"query": "qzqx9k7m banana fluxcapacitor", "include_cold": false}'
  log "Smoke test: POST $CONTEXT_URL expecting budget_tier=$expected"
  curl -s -m 30 -H 'Content-Type: application/json' -d "$body" "$CONTEXT_URL" > "$out_file" 2>>"$LOG"
  if [ ! -s "$out_file" ]; then
    log "ERROR: smoke response empty (saved to $out_file)"
    return 1
  fi
  log "Smoke response saved to $out_file"
  # Extract budget_tier without depending on jq.
  local tier
  tier=$(py -3 -c "import json,sys
d=json.load(open(sys.argv[1]))
# Response is a list of one Pydantic record per spec.
if isinstance(d, list) and d:
    d = d[0]
print(d.get('agent', {}).get('budget_tier', 'MISSING'))
" "$out_file" 2>>"$LOG" || echo "PARSE_ERR")
  log "Smoke budget_tier=$tier (expected=$expected)"
  if [ "$tier" != "$expected" ]; then
    log "ERROR: smoke mismatch — expected $expected got $tier"
    return 1
  fi
  return 0
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
    log "=== DONE:  $name  (rc=$rc, ${dur}s = $((dur/60))min) ==="
  else
    log "=== FAIL:  $name  (rc=$rc, ${dur}s) — continuing ==="
  fi
  return $rc
}

run_bench() {
  local mode="$1" out="$2" tag="$3"
  run_step "gpqa $mode (full diamond, $tag)" \
    py -3 -u benchmarks/bench_aa_suite.py \
      --benchmark gpqa \
      --mode "$mode" \
      --model "$MODEL" \
      --timeout "$CLIENT_TIMEOUT" \
      --output "$out"
}

# -- Begin run -------------------------------------------------------

log "============================================================"
log "Diamond ABSTAIN in-run control bench starting"
log "Branch: $(git rev-parse --abbrev-ref HEAD)  HEAD: $(git rev-parse --short HEAD)"
log "Model: $MODEL    Client timeout: ${CLIENT_TIMEOUT}s"
log "Cell A: ON, abstain DISABLED -> $CELL_A_OUT"
log "Cell B: ON, abstain ENABLED  -> $CELL_B_OUT"
log "============================================================"

# -- Stop the currently running helix server (PID from this morning's run).
if [ -f overnight_logs/helix_server_2026-05-03.pid ]; then
  EXISTING_PID=$(cat overnight_logs/helix_server_2026-05-03.pid 2>/dev/null || true)
  if [ -n "$EXISTING_PID" ]; then
    log "Existing helix PID from morning run: $EXISTING_PID"
    if kill -0 "$EXISTING_PID" 2>/dev/null; then
      log "Killing existing helix server PID=$EXISTING_PID"
      taskkill //PID "$EXISTING_PID" //F >>"$LOG" 2>&1 || true
      sleep 3
    else
      log "PID $EXISTING_PID already gone"
    fi
  fi
fi

# Belt-and-braces: anything still bound to 11437?
PORT_OWNER=$(netstat -ano 2>/dev/null | grep "127.0.0.1:${HELIX_PORT} " | grep LISTENING | awk '{print $5}' | head -1 || true)
if [ -n "$PORT_OWNER" ]; then
  log "Port ${HELIX_PORT} still owned by PID=$PORT_OWNER; force-killing"
  taskkill //PID "$PORT_OWNER" //F >>"$LOG" 2>&1 || true
  sleep 3
fi

# =========================================================================
# Cell A: ON, abstain DISABLED
# =========================================================================
log ""
log ">>>>>>>>>>>> Cell A: ON, abstain DISABLED <<<<<<<<<<<<"
write_status "RUNNING: cell A (ON, abstain disabled) at $(ts)"

set_abstain "false" || { log "ABORT: could not set abstain_enabled=false"; write_status "ABORT cell A toml at $(ts)"; exit 1; }

start_server "$CELL_A_LOG" "$CELL_A_ERR" "$CELL_A_PID" \
  || { log "ABORT: failed to start Cell A server"; write_status "ABORT cell A start at $(ts)"; exit 1; }

CELL_A_PID_VAL=$(cat "$CELL_A_PID")

wait_for_ready "$CELL_A_ERR" "$CELL_A_PID_VAL" \
  || { log "ABORT: Cell A server not ready"; write_status "ABORT cell A ready at $(ts)"; exit 1; }

# Smoke: with abstain disabled, the noise query must NOT abstain — should fall
# through to broad (legacy behavior).
if ! smoke_test "$CELL_A_SMOKE" "broad"; then
  log "ABORT: Cell A smoke failed — abstain should be OFF but tier mismatch"
  write_status "ABORT cell A smoke at $(ts)"
  exit 1
fi

run_bench on "$CELL_A_OUT" "abstain-disabled"
CELL_A_RC=$?
log "Cell A bench rc=$CELL_A_RC"

stop_server "$CELL_A_PID_VAL" "Cell A"
CURRENT_SERVER_PID=""

# =========================================================================
# Cell B: ON, abstain ENABLED
# =========================================================================
log ""
log ">>>>>>>>>>>> Cell B: ON, abstain ENABLED <<<<<<<<<<<<"
write_status "RUNNING: cell B (ON, abstain enabled) at $(ts)"

set_abstain "true" || { log "ABORT: could not set abstain_enabled=true"; write_status "ABORT cell B toml at $(ts)"; exit 1; }

start_server "$CELL_B_LOG" "$CELL_B_ERR" "$CELL_B_PID" \
  || { log "ABORT: failed to start Cell B server"; write_status "ABORT cell B start at $(ts)"; exit 1; }

CELL_B_PID_VAL=$(cat "$CELL_B_PID")

wait_for_ready "$CELL_B_ERR" "$CELL_B_PID_VAL" \
  || { log "ABORT: Cell B server not ready"; write_status "ABORT cell B ready at $(ts)"; exit 1; }

# Smoke: with abstain enabled, the noise query SHOULD abstain.
if ! smoke_test "$CELL_B_SMOKE" "abstain"; then
  log "WARN: Cell B smoke did not return abstain — proceeding but flagging"
  # Don't abort: this might be a regression worth measuring, log will surface it.
fi

run_bench on "$CELL_B_OUT" "abstain-enabled"
CELL_B_RC=$?
log "Cell B bench rc=$CELL_B_RC"

stop_server "$CELL_B_PID_VAL" "Cell B"
CURRENT_SERVER_PID=""

# =========================================================================
# Report
# =========================================================================
log ""
log "=== Generating head-to-head report ==="
write_status "RUNNING: report generation at $(ts)"

cat > "$REPORT" <<EOF
# GPQA Diamond ABSTAIN in-run control — 2026-05-03b

**Started:** $(head -1 "$LOG" | sed 's/^\[\(.*\)\].*/\1/')
**Completed:** $(ts)
**Model:** $MODEL
**Helix server:** http://127.0.0.1:${HELIX_PORT}
**Branch:** $(git rev-parse --abbrev-ref HEAD)  HEAD: $(git rev-parse --short HEAD)
**Timeouts:** httpx client = ${CLIENT_TIMEOUT}s
**Spec:** [2026-05-02-abstain-tier-design.md](../docs/specs/2026-05-02-abstain-tier-design.md)

This run produces the in-run abstain-disabled control that the morning OFF/ON
bench could not provide. Both cells use ON-mode helix retrieval on the same
gene state; the only difference is the \`abstain_enabled\` toggle in
\`[budget]\`. Cell A = abstain DISABLED. Cell B = abstain ENABLED.

- Cell A bench rc: ${CELL_A_RC}
- Cell B bench rc: ${CELL_B_RC}

## Headline numbers

EOF

py -3 - "$CELL_A_OUT" "$CELL_B_OUT" >> "$REPORT" 2>&1 <<'PYEOF'
import sys, json

# Force ASCII-safe stdout — last night's heredoc crashed on a Unicode minus.
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

a_path, b_path = sys.argv[1], sys.argv[2]
a = json.load(open(a_path, encoding='utf-8'))
b = json.load(open(b_path, encoding='utf-8'))

def stats(d):
    res = d.get('results', [])
    n = len(res)
    succ = [r for r in res if not r.get('error')]
    fail = [r for r in res if r.get('error')]
    correct = sum(1 for r in succ if r.get('answer_correct'))
    proxy_succ = sorted(r.get('proxy_latency_s', 0) for r in succ if r.get('proxy_latency_s', 0) > 0)
    err_kinds = {}
    for r in fail:
        e = r.get('error') or ''
        kind = 'timeout' if 'timed out' in e else ('proxy_500' if '500' in e else 'other')
        err_kinds[kind] = err_kinds.get(kind, 0) + 1
    def pct(L, p):
        if not L:
            return 0
        return L[max(0, min(len(L) - 1, int(len(L) * p)))]
    return {
        'n': n, 'succ': len(succ), 'fail': len(fail),
        'correct': correct,
        'p50': pct(proxy_succ, 0.5),
        'p90': pct(proxy_succ, 0.9),
        'p95': pct(proxy_succ, 0.95),
        'mx':  max(proxy_succ) if proxy_succ else 0,
        'err_kinds': err_kinds,
    }

sa = stats(a)
sb = stats(b)

print("| cell | n | completed | correct | accuracy (of completed) | errors | error breakdown |")
print("|---|---|---|---|---|---|---|")
for label, s in [('A: ON abstain-disabled', sa), ('B: ON abstain-enabled', sb)]:
    acc = (s['correct'] / s['succ'] * 100) if s['succ'] else 0
    eb = ', '.join(f"{k}={v}" for k, v in s['err_kinds'].items()) or '-'
    print(f"| {label} | {s['n']} | {s['succ']} | {s['correct']} | {acc:.1f}% | {s['fail']} | {eb} |")

# Apples-to-apples on the intersection (only problems where BOTH cells completed).
a_by_id = {r['id']: r for r in a.get('results', [])}
b_by_id = {r['id']: r for r in b.get('results', [])}
both_ok = [pid for pid in a_by_id
           if not a_by_id[pid].get('error') and not b_by_id.get(pid, {}).get('error')]
ac = sum(1 for pid in both_ok if a_by_id[pid].get('answer_correct'))
bc = sum(1 for pid in both_ok if b_by_id[pid].get('answer_correct'))
print()
print("## Apples-to-apples (only problems where BOTH cells completed)")
print()
print(f"- n_both_completed = **{len(both_ok)}**")
print(f"- Cell A (abstain-disabled) correct: {ac}/{len(both_ok)} = {100*ac/max(len(both_ok),1):.1f}%")
print(f"- Cell B (abstain-enabled)  correct: {bc}/{len(both_ok)} = {100*bc/max(len(both_ok),1):.1f}%")
delta_pp = 100 * bc / max(len(both_ok), 1) - 100 * ac / max(len(both_ok), 1)
print(f"- **Accuracy delta (B - A): {delta_pp:+.1f}pp**")

# ----- Latency tables (full, fic=False, fic=True) ----------------------------
def lat_table_for(filter_fn, title):
    print()
    print(f"## Latency on successes ({title})")
    print()
    a_lat = sorted(r.get('proxy_latency_s', 0) for r in a.get('results', [])
                   if not r.get('error') and filter_fn(r) and r.get('proxy_latency_s', 0) > 0)
    b_lat = sorted(r.get('proxy_latency_s', 0) for r in b.get('results', [])
                   if not r.get('error') and filter_fn(r) and r.get('proxy_latency_s', 0) > 0)
    def pct(L, p):
        if not L:
            return 0
        return L[max(0, min(len(L) - 1, int(len(L) * p)))]
    def row(label, L):
        return (label, len(L),
                pct(L, 0.5), pct(L, 0.9), pct(L, 0.95),
                max(L) if L else 0)
    print("| cell | n | p50 | p90 | p95 | max |")
    print("|---|---|---|---|---|---|")
    for label, lat in [('A: ON abstain-disabled', a_lat), ('B: ON abstain-enabled', b_lat)]:
        _, n, p50, p90, p95, mx = row(label, lat)
        print(f"| {label} | {n} | {p50:.1f}s | {p90:.1f}s | {p95:.1f}s | {mx:.1f}s |")

lat_table_for(lambda r: True, "all completed")
lat_table_for(lambda r: r.get('found_in_context') is False, "fic=False (genome miss)")
lat_table_for(lambda r: r.get('found_in_context') is True,  "fic=True  (genome hit)")

# ----- Stratified by found_in_context ---------------------------------------
def stratified(results_list, fic_value):
    succ = [r for r in results_list
            if not r.get('error')
            and r.get('found_in_context') is fic_value
            and r.get('proxy_latency_s', 0) > 0]
    lat = sorted(r.get('proxy_latency_s', 0) for r in succ)
    correct = sum(1 for r in succ if r.get('answer_correct'))
    def pct(L, p):
        if not L:
            return 0
        return L[max(0, min(len(L) - 1, int(len(L) * p)))]
    timeouts = sum(1 for r in results_list
                   if (r.get('error') or '').find('timed out') >= 0
                   and r.get('found_in_context') is fic_value)
    return {
        'n': len(succ),
        'correct': correct,
        'acc': (100 * correct / len(succ)) if succ else 0.0,
        'p50': pct(lat, 0.5),
        'p90': pct(lat, 0.9),
        'p95': pct(lat, 0.95),
        'mx': max(lat) if lat else 0,
        'timeouts': timeouts,
    }

print()
print("## Stratified by `found_in_context`")
print()
strat_pairs = []
for fic_val, label in [(False, 'fic=False (genome miss - ABSTAIN target)'),
                       (True,  'fic=True  (genome hit  - must stay accurate)')]:
    sa_strat = stratified(a.get('results', []), fic_val)
    sb_strat = stratified(b.get('results', []), fic_val)
    strat_pairs.append((label, fic_val, sa_strat, sb_strat))
    print()
    print(f"### {label}")
    print()
    print("| cell | n | correct | accuracy | p50 | p90 | p95 | max | timeouts |")
    print("|---|---|---|---|---|---|---|---|---|")
    print(f"| A: abstain-disabled | {sa_strat['n']} | {sa_strat['correct']} | {sa_strat['acc']:.1f}% | {sa_strat['p50']:.1f}s | {sa_strat['p90']:.1f}s | {sa_strat['p95']:.1f}s | {sa_strat['mx']:.1f}s | {sa_strat['timeouts']} |")
    print(f"| B: abstain-enabled  | {sb_strat['n']} | {sb_strat['correct']} | {sb_strat['acc']:.1f}% | {sb_strat['p50']:.1f}s | {sb_strat['p90']:.1f}s | {sb_strat['p95']:.1f}s | {sb_strat['mx']:.1f}s | {sb_strat['timeouts']} |")
    p95_delta = sb_strat['p95'] - sa_strat['p95']
    acc_delta = sb_strat['acc'] - sa_strat['acc']
    to_delta = sb_strat['timeouts'] - sa_strat['timeouts']
    print(f"- p95 delta (B - A): **{p95_delta:+.1f}s**")
    print(f"- accuracy delta (B - A): **{acc_delta:+.1f}pp**")
    print(f"- timeout delta (B - A): **{to_delta:+d}**")

# ----- Headline: Does the abstain gate help? --------------------------------
print()
print("## Headline: Does the abstain gate help?")
print()

# Overall deltas (apples-to-apples, lat over A intersection-completed).
overall_acc_delta = 100 * bc / max(len(both_ok), 1) - 100 * ac / max(len(both_ok), 1)
overall_p95_delta = sb['p95'] - sa['p95']
overall_to_delta = sb['fail'] - sa['fail']

print(f"- **Accuracy delta (B - A, apples-to-apples):** {overall_acc_delta:+.1f}pp")
print(f"- **p95 latency delta (B - A, all successes):** {overall_p95_delta:+.1f}s")
print(f"- **Total errors delta (B - A):** {overall_to_delta:+d}")
print()
print("### Per stratum")
print()
print("| stratum | accuracy delta (B-A) | p95 delta (B-A) | timeout delta (B-A) |")
print("|---|---|---|---|")
for label, fic_val, sa_strat, sb_strat in strat_pairs:
    print(f"| {label} | {sb_strat['acc'] - sa_strat['acc']:+.1f}pp | {sb_strat['p95'] - sa_strat['p95']:+.1f}s | {sb_strat['timeouts'] - sa_strat['timeouts']:+d} |")
print()
print("Interpretation guide:")
print("- The abstain gate is a **win** if fic=False shows lower p95 / fewer timeouts at unchanged accuracy.")
print("- A regression on fic=True accuracy means the gate is firing on hits (false-positive abstains).")
print("- Net accuracy can go up if abstain prevents the small model from being misled by junk context on misses.")
PYEOF

echo "" >> "$REPORT"
echo "## Raw compare_ab" >> "$REPORT"
echo "" >> "$REPORT"
echo "\`\`\`" >> "$REPORT"
py -3 benchmarks/compare_ab.py "$CELL_A_OUT" "$CELL_B_OUT" >> "$REPORT" 2>&1 || echo "(compare_ab.py failed)" >> "$REPORT"
echo "\`\`\`" >> "$REPORT"

# Restore helix.toml (also handled by EXIT trap, but explicit here for clarity).
revert_config

write_status "DONE at $(ts)"
log "Diamond ABSTAIN control bench COMPLETE — report at $REPORT"
log "helix.toml has been reverted to pre-run state."
