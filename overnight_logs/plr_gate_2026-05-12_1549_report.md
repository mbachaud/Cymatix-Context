# PLR gate (#74): `[plr] enabled = true`

**Verdict: PASS** on all three gates:

| Gate                                | Threshold       | Result    | Status |
|-------------------------------------|-----------------|-----------|--------|
| Off-side clean (no PLR on baseline) | `0/N`           | `0/50`    | PASS   |
| On-side presence (packets w/ items) | `>= 90%`        | `100%`    | PASS   |
| p95 latency delta                   | `< +50 ms`      | `-389 ms` | PASS   |

PLR-on actually **improves** p95 by 389 ms in this run (likely seed-noise within the small N=50 sample — the point is "no degradation," and we got better-than-flat).

## Method

- **Harness**: new `benchmarks/bench_plr_smoke.py` (shipped with this PR). 50 retrieval-only queries via `/context/packet`. Corpus harvested by reusing `bench_needle_1000.harvest_needles` + `build_query_blind` so the question shape matches the existing #73 BROAD bench — every query has a real KV needle behind it.
- **Helix server**: editable install from `bench/plr-gate` worktree, `127.0.0.1:11437`, ribosome=disabled, CUDA on RTX 3080 Ti.
- **N**: 50 queries per side
- **Seed**: 42 (same harvester seed both sides)
- **PLR artifact**: `training/models/stacked_plr.joblib` (committed in this PR — pre-trained query-quality head, schema v1, label_set `t07`, training AUC 0.6314 > 0.55 §C2 gate). Sidecar `.sha256` also committed.
- **Wall**: 62-64 s per side, ~2 min total bench time.

## Numbers

### PLR off (baseline)

| Metric                | Value         |
|-----------------------|---------------|
| n                     | 50            |
| p50 ms                | 1164.81       |
| p95 ms                | 2612.82       |
| plr_present rate      | 0.0% (0/50)   |
| with_items count      | 50/50         |
| ok count (HTTP 200)   | 50/50         |
| wall s                | 64.33         |

### PLR on (candidate)

| Metric                | Value         |
|-----------------------|---------------|
| n                     | 50            |
| p50 ms                | 1185.98       |
| p95 ms                | 2223.99       |
| plr_present rate      | **100.0% (50/50)** |
| with_items count      | 50/50         |
| ok count (HTTP 200)   | 50/50         |
| wall s                | 62.17         |

### Deltas

| Metric          | Delta              |
|-----------------|--------------------|
| p50             | +21.17 ms          |
| p95             | **-388.83 ms** (gate <+50 ms) |
| presence (with_items) | **+100 pp** (gate >=90 pp) |
| off-side leakage | **0/50** (gate =0)|

### Sample PLR block

```json
{
  "prob_B":      0.9123,
  "logit":       2.3423,
  "score_A":     0.0877,
  "high_risk":   true,
  "artifact_label_set": "t07"
}
```

(`prob_B` = predicted log-odds of re-query within 60s under cos(q_t, q_{t+1}) filter; `high_risk` = `prob_B > config.plr.high_risk_threshold`, currently 0.5.)

## Why the smoke bench is HTTP-only

`_compute_plr_confidence` lives at `helix_context/server.py:453` and is **only** called from the `/context/packet` HTTP endpoint at `server.py:1634`. The in-tree `benchmarks/bench_packet.py` calls `build_context_packet` directly — it bypasses the route handler, so PLR is never exercised by it. This bench hits the endpoint over real HTTP so the `live_cfg.plr.enabled` gate and the PLR closure both fire.

## Provenance

- Branch: `bench/plr-gate` @ `6146b56` (prior blocker-report commit) + this commit
- Snapshot DB: `genome-bench-2026-05-08-frozen.db` sha256 `AEAAF3AB8FDF9E6078BEFCEECA7A11F91F74EA8B20F9EA167292B7C3476B37C7`
- Server-loaded gene count: 18,936
- Bench output JSONs:
  - `overnight_logs/plr_smoke_off_2026-05-12_1549.json`
  - `overnight_logs/plr_smoke_on_2026-05-12_1549.json`
- PLR artifact (committed under `training/models/`): copy of `stacked_plr.joblib` from main repo; same sha256 as documented in the trainer-side `.sha256` sidecar.

## `helix.toml` diff

```diff
--- a/helix.toml
+++ b/helix.toml
@@ -317,7 +317,7 @@ pki_weight = 1.0                        # path-key-index tier, RRF participant
 # Train a fresh artifact with:
 #   python scripts/pwpc/sprint3.py <windowed_export.json> \
 #       --save-model training/models/stacked_plr.joblib --save-label-set best
-enabled = false                         # Dark by default; bench before flipping
+enabled = true                          # Bench-gated 2026-05-12 (#74): plr_confidence on /context/packet; p95 delta < 50ms; presence >= 90% of packets-with-items
 model_path = "training/models/stacked_plr.joblib"
 expected_sha256 = ""                    # Empty = trust the .sha256 sidecar (written by the trainer)
 high_risk_threshold = 0.5               # `prob_B > this` surfaces a coarse "likely-to-re-query" boolean
```
