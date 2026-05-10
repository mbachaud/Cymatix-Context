#!/usr/bin/env bash
# Stage 1 launcher — N=1000 needle bench, LOCATED axis (4-axis locator query).
#
# This is the new headline configuration: build_query_located() emits a
# `What is the {key} value in {project}/{module}/{filename}?` query that
# mirrors dim-lock variant 4 (DEWEY=0). Target retrieval@1 >= 0.55 BEFORE
# Stages 2-4 are applied (sanity that bench redesign alone surfaces the
# locator-bearing recall hidden by the bare-key form).
#
# Snapshot: genome-bench-2026-05-08.db (18,934 embedded genes).
# Estimated wall time: ~50-90 min at ASK_PROXY=1, ~25-40 min at ASK_PROXY=0.
#
# Usage:
#   bash benchmarks/_run_n1000_located.sh \
#     2>&1 | tee benchmarks/logs/n1000_located_$(date +%Y-%m-%d_%H%M).log
#
# Preconditions:
#   - Helix bench server running on $HELIX_URL (default http://127.0.0.1:11437)
#   - Snapshot DB present at $GENOME_DB
#   - Optional: HELIX_MODEL set (default qwen3:4b); ASK_PROXY=0 to skip /chat
#
# Environment overrides:
#   HELIX_URL       /context endpoint (default http://127.0.0.1:11437)
#   HELIX_MODEL     downstream model (default qwen3:4b)
#   GENOME_DB       absolute path to snapshot DB
#   N               needle count (default 1000)
#   SEED            random seed (default 42)
#   ASK_PROXY       1 = full pipeline, 0 = retrieval-only

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SNAPSHOT_DEFAULT="$REPO_ROOT/genome-bench-2026-05-08.db"
TS=$(date +%Y-%m-%d_%H%M)
OUT_DIR="benchmarks/results/n1000_located_${TS}"
LOG_DIR="benchmarks/logs"

mkdir -p "$OUT_DIR" "$LOG_DIR"

log()  { echo "[$(date +%H:%M:%S)] $*"; }
ok()   { echo "[$(date +%H:%M:%S)] OK    $*"; }
fail() { echo "[$(date +%H:%M:%S)] FAIL  $*"; }

GENOME_DB_RESOLVED="${GENOME_DB:-$SNAPSHOT_DEFAULT}"
HELIX_URL_RESOLVED="${HELIX_URL:-http://127.0.0.1:11437}"

if [ ! -f "$GENOME_DB_RESOLVED" ]; then
    fail "Snapshot DB not found at $GENOME_DB_RESOLVED"
    exit 1
fi
ok "Snapshot DB: $GENOME_DB_RESOLVED"

if ! curl -sf "${HELIX_URL_RESOLVED}/health" >/dev/null 2>&1; then
    fail "Helix not responding at ${HELIX_URL_RESOLVED}"
    exit 1
fi
ok "Helix UP at ${HELIX_URL_RESOLVED}"

log "=== N=${N:-1000} needle bench — LOCATED axis ==="
log "Output:  ${OUT_DIR}/needle_1000_located_${TS}.json"

env HELIX_URL="$HELIX_URL_RESOLVED" \
    HELIX_MODEL="${HELIX_MODEL:-qwen3:4b}" \
    GENOME_DB="$GENOME_DB_RESOLVED" \
    N="${N:-1000}" \
    SEED="${SEED:-42}" \
    ASK_PROXY="${ASK_PROXY:-1}" \
    PYTHONIOENCODING=utf-8 \
    OUTPUT="${OUT_DIR}/needle_1000_located_${TS}.json" \
  python benchmarks/bench_needle_1000.py --axis located

EXIT=$?
if [ $EXIT -eq 0 ]; then
    ok "Located bench finished. Results: ${OUT_DIR}/"
else
    fail "Located bench exited with code $EXIT"
fi
exit $EXIT
