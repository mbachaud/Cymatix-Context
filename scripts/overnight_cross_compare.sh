#!/usr/bin/env bash
# Overnight orchestrator — build bench genomes + cross-compare.
#
# Runs two phases sequentially:
#   1. scripts/build_bench_genomes.py   (~10min for cymatix, ~1-2h for organic)
#   2. scripts/cross_compare_genomes.py (~1-2min per genome + queries)
#
# Writes all output to:
#   overnight_logs/build_{cymatix,organic}.log
#   docs/FUTURE/genome_cross_compare_2026-04-15.md
#
# Intended to be launched detached and forgotten. Safe to re-run — each
# phase handles its own idempotency (deletes target genome before
# rebuild). Does NOT touch genome.db or the live server on :11437.

set -u  # unbound variable = fail
# Deliberately NOT set -e — we want phase failures logged but not fatal
# so that e.g. a successful "cymatix" build still gets compared even if
# "organic" bails halfway.

cd "$(dirname "$0")/.."  # repo root

LOGDIR="overnight_logs"
mkdir -p "$LOGDIR"

STAMP="$(date +%Y-%m-%d_%H%M)"
REPORT="docs/FUTURE/genome_cross_compare_${STAMP}.md"
OVERALL_LOG="$LOGDIR/overnight_${STAMP}.log"

log() {
    local msg="$1"
    local ts
    ts="$(date +'%H:%M:%S')"
    echo "[$ts] $msg" | tee -a "$OVERALL_LOG"
}

log "=== Overnight cross-compare run: $STAMP ==="
log "Writing final report to: $REPORT"
log ""

# ── Phase 1a: build the tight cymatix-only bench genome ─────────────
log "Phase 1a: build genome_bench_cymatix.db (tight, ~5-10 min)"
python scripts/build_bench_genomes.py --target cymatix \
    >> "$LOGDIR/build_cymatix_${STAMP}.log" 2>&1
RC_CYMATIX=$?
if [ $RC_CYMATIX -eq 0 ]; then
    log "  ✓ cymatix bench genome built"
else
    log "  ✗ cymatix bench build failed (rc=$RC_CYMATIX); see $LOGDIR/build_cymatix_${STAMP}.log"
fi

# ── Phase 1b: build the wide organic bench genome ────────────────
log "Phase 1b: build genome_bench_organic.db (wide, 1-2 hr)"
python scripts/build_bench_genomes.py --target organic \
    >> "$LOGDIR/build_organic_${STAMP}.log" 2>&1
RC_ORGANIC=$?
if [ $RC_ORGANIC -eq 0 ]; then
    log "  ✓ organic bench genome built"
else
    log "  ✗ organic bench build failed (rc=$RC_ORGANIC); see $LOGDIR/build_organic_${STAMP}.log"
fi

# ── Phase 2: cross-compare (skip missing targets) ─────────────────
log ""
log "Phase 2: cross-compare"

GENOMES="genome.db"
[ -f "genome_bench_cymatix.db" ]    && GENOMES="$GENOMES genome_bench_cymatix.db"
[ -f "genome_bench_organic.db" ]  && GENOMES="$GENOMES genome_bench_organic.db"

log "Comparing: $GENOMES"
python scripts/cross_compare_genomes.py $GENOMES --out "$REPORT" \
    >> "$LOGDIR/compare_${STAMP}.log" 2>&1
RC_CMP=$?
if [ $RC_CMP -eq 0 ]; then
    log "  ✓ cross-compare report: $REPORT"
else
    log "  ✗ cross-compare failed (rc=$RC_CMP); see $LOGDIR/compare_${STAMP}.log"
fi

# ── Summary ───────────────────────────────────────────────────────
log ""
log "=== DONE ==="
log "Report: $REPORT"
log "Phase exit codes: cymatix=$RC_CYMATIX, organic=$RC_ORGANIC, compare=$RC_CMP"
log ""
log "Listing produced .db files:"
ls -lh genome_bench_*.db 2>/dev/null | awk '{print "  " $5 "  " $NF}' | tee -a "$OVERALL_LOG"
