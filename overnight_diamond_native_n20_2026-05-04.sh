#!/usr/bin/env bash
# Native-stack regression bench (n=20) for the native-observability-sidecar PR.
# Date: 2026-05-04. Branch: feat/native-observability-sidecar. HEAD: e57f567.
#
# Spec gate (docs/specs/2026-05-04-native-observability-sidecar-design.md):
#   p95(native) - p95(docker baseline, same problem IDs) <= 5s
#
# Docker baseline: benchmarks/results/gpqa_on_diamond_2026-05-03.json
# Native output:   benchmarks/results/gpqa_native_n20_2026-05-04.json
#
# This is a SINGLE-cell ON-mode run. No abstain toggle (helix.toml stays at
# its current state), no helix.toml mutation. Native observability stack
# (otelcol/prom/tempo/loki/grafana) must already be running -- the user
# launches it via the helix tray. Helix server on :11437 will be stopped
# (taskkill) and respawned by this script for clean lifecycle.

set -u

cd "$(dirname "$0")"
mkdir -p overnight_logs benchmarks/results

LOG=overnight_logs/diamond_native_n20_2026-05-04.log
STATUS=overnight_logs/diamond_native_n20_2026-05-04.status
REPORT=overnight_logs/diamond_native_n20_2026-05-04_report.md

MODEL=gemma4:e4b
CLIENT_TIMEOUT=180
N_LIMIT=20
HELIX_PORT=11437
HEALTH_URL="http://127.0.0.1:${HELIX_PORT}/health"

CELL_OUT=benchmarks/results/gpqa_native_n20_2026-05-04.json
CELL_LOG=overnight_logs/helix_server_native_n20_2026-05-04.log
CELL_ERR=overnight_logs/helix_server_native_n20_2026-05-04.err
CELL_PID=overnight_logs/helix_server_native_n20_2026-05-04.pid

DOCKER_BASELINE=benchmarks/results/gpqa_on_diamond_2026-05-03.json

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG"; }
write_status() { echo "$1" > "$STATUS"; }

CURRENT_SERVER_PID=""

stop_server() {
  local pid="$1"
  local label="$2"
  if [ -z "$pid" ]; then return 0; fi
  if kill -0 "$pid" 2>/dev/null; then
    log "Stopping $label PID=$pid"
    taskkill //PID "$pid" //F >>"$LOG" 2>&1 || true
    local i=0
    while kill -0 "$pid" 2>/dev/null && [ $i -lt 10 ]; do
      sleep 1
      i=$((i+1))
    done
  else
    log "$label PID=$pid already gone"
  fi
}

cleanup() {
  local rc=$?
  if [ -n "$CURRENT_SERVER_PID" ]; then
    stop_server "$CURRENT_SERVER_PID" "(cleanup)"
    CURRENT_SERVER_PID=""
  fi
  if [ "$rc" -ne 0 ]; then
    write_status "EXITED rc=$rc at $(ts)"
  fi
}
trap 'cleanup' EXIT
trap 'log "Caught signal; cleaning up"; write_status "INTERRUPTED at $(ts)"; exit 130' INT TERM

start_server() {
  local out_log="$1" out_err="$2" pid_file="$3"
  log "Starting fresh helix server: log=$out_log err=$out_err pid=$pid_file"
  (
    py -3 -u -m uvicorn helix_context.server:app \
      --host 127.0.0.1 --port "$HELIX_PORT" \
      >"$out_log" 2>"$out_err" &
    echo $! > "$pid_file"
  )
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
    log "=== FAIL:  $name  (rc=$rc, ${dur}s) ==="
  fi
  return $rc
}

run_bench() {
  run_step "gpqa on (native, limit=${N_LIMIT})" \
    py -3 -u benchmarks/bench_aa_suite.py \
      --benchmark gpqa \
      --mode on \
      --model "$MODEL" \
      --limit "$N_LIMIT" \
      --timeout "$CLIENT_TIMEOUT" \
      --output "$CELL_OUT"
}

# -- Begin run -------------------------------------------------------

log "============================================================"
log "Native sidecar regression bench (n=${N_LIMIT}) starting"
log "Branch: $(git rev-parse --abbrev-ref HEAD)  HEAD: $(git rev-parse --short HEAD)"
log "Model: $MODEL    Client timeout: ${CLIENT_TIMEOUT}s    Limit: ${N_LIMIT}"
log "Native output:   $CELL_OUT"
log "Docker baseline: $DOCKER_BASELINE"
log "============================================================"

# Verify native observability stack is up.
log "Checking native observability sidecar ports..."
MISSING=""
for port in 4317 9090 3200 3100 3000; do
  owner=$(netstat -ano 2>/dev/null | grep -E "[: ]${port} " | grep LISTENING | head -1 | awk '{print $5}' || true)
  if [ -z "$owner" ]; then
    MISSING="$MISSING $port"
  else
    log "  port $port: PID=$owner"
  fi
done
if [ -n "$MISSING" ]; then
  log "ABORT: native observability ports not bound:$MISSING"
  write_status "ABORT native_stack at $(ts)"
  exit 1
fi
log "Native observability stack OK."

# Stop any existing helix server on :11437 (likely the tray-launched one).
PORT_OWNER=$(netstat -ano 2>/dev/null | grep "127.0.0.1:${HELIX_PORT} " | grep LISTENING | awk '{print $5}' | head -1 || true)
if [ -n "$PORT_OWNER" ]; then
  log "Port ${HELIX_PORT} held by PID=$PORT_OWNER; stopping"
  taskkill //PID "$PORT_OWNER" //F >>"$LOG" 2>&1 || true
  sleep 3
fi

# Start fresh helix server.
start_server "$CELL_LOG" "$CELL_ERR" "$CELL_PID" \
  || { log "ABORT: failed to start helix"; write_status "ABORT start at $(ts)"; exit 1; }

CELL_PID_VAL=$(cat "$CELL_PID")

wait_for_ready "$CELL_ERR" "$CELL_PID_VAL" \
  || { log "ABORT: helix not ready"; write_status "ABORT ready at $(ts)"; exit 1; }

# Run the bench.
run_bench
BENCH_RC=$?
log "Bench rc=$BENCH_RC"

stop_server "$CELL_PID_VAL" "bench helix"
CURRENT_SERVER_PID=""

# Build the report.
log ""
log "=== Generating native-vs-docker report ==="
write_status "RUNNING: report at $(ts)"

cat > "$REPORT" <<EOF
# GPQA Diamond native-stack regression (n=${N_LIMIT}) -- 2026-05-04

**Started:** $(head -1 "$LOG" | sed 's/^\[\(.*\)\].*/\1/')
**Completed:** $(ts)
**Model:** $MODEL
**Helix server:** http://127.0.0.1:${HELIX_PORT}
**Branch:** $(git rev-parse --abbrev-ref HEAD)  HEAD: $(git rev-parse --short HEAD)
**Spec:** [2026-05-04-native-observability-sidecar-design.md](../docs/specs/2026-05-04-native-observability-sidecar-design.md)
**Spec gate:** p95(native) - p95(docker, same IDs) <= 5s

- Native bench rc: ${BENCH_RC}

EOF

py -3 - "$CELL_OUT" "$DOCKER_BASELINE" >> "$REPORT" 2>&1 <<'PYEOF'
import sys, json

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

native_path, docker_path = sys.argv[1], sys.argv[2]
nat = json.load(open(native_path, encoding='utf-8'))
doc = json.load(open(docker_path, encoding='utf-8'))

def pct(L, p):
    if not L:
        return 0.0
    return L[max(0, min(len(L) - 1, int(len(L) * p)))]

def summarize(d, label):
    res = d.get('results', [])
    succ = [r for r in res if not r.get('error')]
    correct = sum(1 for r in succ if r.get('answer_correct'))
    lats = sorted(r.get('proxy_latency_s', 0) for r in succ if r.get('proxy_latency_s', 0) > 0)
    return {
        'label': label,
        'n_total': len(res),
        'n_succ': len(succ),
        'n_fail': len(res) - len(succ),
        'correct': correct,
        'p50': pct(lats, 0.5),
        'p90': pct(lats, 0.9),
        'p95': pct(lats, 0.95),
        'mx': max(lats) if lats else 0.0,
    }

s_nat = summarize(nat, 'native (n=20)')
s_doc_full = summarize(doc, 'docker (full n=198)')

# Apples-to-apples: filter docker baseline to the same problem IDs the native run hit.
nat_ids = {r['id'] for r in nat.get('results', [])}
doc_subset = {'results': [r for r in doc.get('results', []) if r['id'] in nat_ids]}
s_doc_subset = summarize(doc_subset, f'docker (same {len(nat_ids)} IDs)')

print("## Headline numbers")
print()
print("| run | n | completed | correct | p50 | p90 | p95 | max |")
print("|---|---|---|---|---|---|---|---|")
for s in (s_nat, s_doc_subset, s_doc_full):
    print(f"| {s['label']} | {s['n_total']} | {s['n_succ']} | {s['correct']} | {s['p50']:.1f}s | {s['p90']:.1f}s | {s['p95']:.1f}s | {s['mx']:.1f}s |")

print()
print("## Spec gate verdict")
print()
delta_p95 = s_nat['p95'] - s_doc_subset['p95']
print(f"- p95(native, n=20)              = **{s_nat['p95']:.2f}s**")
print(f"- p95(docker, same {len(nat_ids)} IDs) = **{s_doc_subset['p95']:.2f}s**")
print(f"- p95 delta (native - docker, same IDs) = **{delta_p95:+.2f}s**")
print()
print(f"- p95(docker, full n=198 baseline) = {s_doc_full['p95']:.2f}s (reference only)")
print()
gate_pass = delta_p95 <= 5.0
print(f"- **Spec gate (delta <= 5s): {'PASS' if gate_pass else 'FAIL'}**")

# Errors / timeouts breakdown
print()
print("## Error breakdown")
print()
def err_kinds(d):
    kinds = {}
    for r in d.get('results', []):
        e = r.get('error')
        if not e:
            continue
        if 'timed out' in e:
            kinds['timeout'] = kinds.get('timeout', 0) + 1
        elif '500' in e:
            kinds['proxy_500'] = kinds.get('proxy_500', 0) + 1
        else:
            kinds['other'] = kinds.get('other', 0) + 1
    return kinds

print(f"- native:                     {err_kinds(nat) or '-'}")
print(f"- docker (same {len(nat_ids)} IDs):  {err_kinds(doc_subset) or '-'}")
PYEOF

write_status "DONE at $(ts)"
log "Native regression bench COMPLETE -- report at $REPORT"
