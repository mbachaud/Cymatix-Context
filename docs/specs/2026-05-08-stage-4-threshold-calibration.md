# Stage 4 — Calibrated Thresholds (per-classifier + margin-over-random)

Plan: helix-context retrieval-fix, Stage 4 of 6 (council 2026-05-08). Depends on Stages 2+3 (score scale stable post-RRF + 1024-dim dense).

## 1. Goals + non-goals

**Goals.** Replace two hand-picked constants with reproducible, data-derived calibrations: (a) the absolute ANN cosine cutoff in `genome.py:371`, and (b) the global confidence floors in `context_manager.py:946-989`. After Stages 2 (1024-dim restore) and 3 (RRF fusion), both are stale. Stage 4 produces a `scripts/calibrate_thresholds.py` artifact that re-derives both from a genome snapshot + bench JSON, persists provenance, and exposes it via `/health` and `/context`.

**Non-goals.** No new query classes, no LLM calls, no changes to retrieval signal mix (Stages 2/3), no `caller_model_class` (Stage 5), no know/miss block (Stage 6).

## 2. Surface area

| File | Lines | Change |
|---|---|---|
| `helix_context/genome.py` | 371 | Read `ann_threshold` from new `genome_calibration` table when mode=margin_over_random |
| `helix_context/genome.py` | ~452 (DDL) | New `CREATE TABLE genome_calibration` |
| `helix_context/context_manager.py` | 946-989 | Per-classifier floor lookup; pass `cls` from upstream classifier |
| `helix_context/context_manager.py` | 1094-1096 | `alpha = floors.foveated_alpha(cls)` |
| `helix_context/config.py` | TOML loader | New `[abstain]`, `[abstain.<cls>]`, extended `[retrieval]` keys |
| `helix.toml` | 250-257 | `ann_threshold_mode`, `ann_threshold_sigma_multiplier`; `[abstain]` block |
| `helix_context/server.py` | `/health`, `/context` handlers | Emit calibration provenance |
| `scripts/calibrate_thresholds.py` | NEW | CLI driver |
| `tests/test_calibration.py` | NEW | Unit + property tests |

## 3. Margin-over-random ANN threshold

**Algorithm (deterministic, seeded).**

```
INPUT: genome.db, dim (1024 post-Stage-2), N=10_000, seed=42
1. SELECT gene_id FROM genes WHERE embedding_dense_v2 IS NOT NULL
2. rng = numpy.random.default_rng(seed)
3. ids = rng.choice(all_ids, size=2*N, replace=True).reshape(N, 2)
4. For each (a, b) in ids where a != b:
     v_a, v_b = np.frombuffer(BLOB)  # already L2-normalized at index time
     cos[i] = float(np.dot(v_a, v_b))
5. mu, sigma = float(cos.mean()), float(cos.std(ddof=1))
6. threshold = mu + sigma_mult * sigma   # default sigma_mult=3.0
7. UPSERT INTO genome_calibration (key, value_json, computed_at) VALUES
     ('ann_threshold', json{'value':threshold,'mu':mu,'sigma':sigma,
       'N':N,'dim':dim,'sigma_mult':3.0,'seed':42}, now())
```

**DDL.**
```sql
CREATE TABLE IF NOT EXISTS genome_calibration (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  computed_at TEXT NOT NULL,    -- ISO8601 UTC
  source TEXT                   -- 'calibrate_thresholds.py vX'
);
```

Genome `query_genes()` reads `ann_threshold` once at first call (cache on `self`), invalidated on `set_replication_manager` rotation. If the row is missing AND `mode=margin_over_random`, log warning and fall back to the legacy absolute value from helix.toml.

## 4. Per-classifier confidence floors

**Inputs.** `located_n1000.json` rows carry `gene_id` (planted ground-truth) plus the response's `agent.score_top` and the candidate list. We re-classify each row's `query` through `classify_query` to populate `cls`, then split each cls into hits (`gene_id` appears in retrieved set) vs misses.

**Percentile choices.**
- `abstain_top` = **p85** of MISS scores per cls — score-below-this is dominated by failures (15% false-abstain budget).
- `focused_top` = **p25** of HIT scores per cls — 75% of true hits clear it.
- `tight_top` = **p60** of HIT scores per cls — top-confidence band only.

Reasoning: abstain at the upper tail of misses (cheap to be wrong: re-engages BROAD); tight at the lower-middle of hits (expensive to be wrong: drops 9 candidates). Asymmetric percentiles bias toward recall.

**Default-class fanout (from `query_classifier.py:130-179`).**

| cls | assembly_max_genes_cap | abstain_top | focused_top | tight_top | foveated_alpha |
|---|---|---|---|---|---|
| arithmetic | 2 | p85_miss | p25_hit | p60_hit | 1.6 (sharp — only 2 genes ever) |
| factual | 5 | p85_miss | p25_hit | p60_hit | 1.4 |
| procedural | 6 | p85_miss | p25_hit | p60_hit | 0.9 |
| multi_hop | 8 | p85_miss | p25_hit | p60_hit | 0.6 (flat — distribute budget) |
| default | (unset) | p85_miss | p25_hit | p60_hit | 1.0 |

Numeric values are **derived** at calibration time per genome+bench. The α column ships as defaults; the script also emits suggested α from observed mean compression-ratio per cls (out of scope to retune α automatically — manual review).

## 5. Calibration script

**CLI.**
```
scripts/calibrate_thresholds.py
  --genome PATH                   # required, sqlite path
  --bench PATH                    # located_n1000.json
  --output-toml PATH              # default: stdout
  --output-report PATH            # default: calibration_report.json
  --sigma-mult FLOAT              # default 3.0
  --random-pairs INT              # default 10000
  --seed INT                      # default 42
  --abstain-pct FLOAT             # default 85.0
  --focused-pct FLOAT             # default 25.0
  --tight-pct FLOAT               # default 60.0
  --write-db / --no-write-db      # default --write-db (UPSERT genome_calibration)
  --dry-run                       # print only, no DB write, exit 0
```

**stdout (TOML snippet).**
```toml
[retrieval]
ann_threshold_mode = "margin_over_random"
ann_threshold_sigma_multiplier = 3.0
# computed: mu=0.018 sigma=0.131 N=10000 dim=1024 -> 0.411

[abstain]
mode = "per_classifier"

[abstain.factual]
abstain_top = 0.42
focused_top = 0.71
tight_top = 1.18

[abstain.multi_hop]
abstain_top = 0.38
focused_top = 0.55
tight_top = 0.92
# ... arithmetic, procedural, default
```

**`calibration_report.json` schema.**
```json
{
  "$schema": "https://helix-context.dev/schemas/calibration_report.v1.json",
  "version": 1,
  "computed_at": "2026-05-08T14:32:00Z",
  "genome": {"path": "...", "sha256": "...", "gene_count": 18934, "dim": 1024},
  "bench": {"path": "...", "rows": 1000, "harness_version": 2},
  "ann": {
    "mu": 0.018, "sigma": 0.131, "N": 10000, "seed": 42,
    "sigma_mult": 3.0, "threshold": 0.411
  },
  "classifiers": {
    "factual": {
      "n_total": 412, "n_hit": 358, "n_miss": 54,
      "hit_score_p25": 0.71, "hit_score_p60": 1.18,
      "miss_score_p85": 0.42,
      "floors": {"abstain_top": 0.42, "focused_top": 0.71, "tight_top": 1.18}
    },
    "multi_hop": {"...": "..."}, "arithmetic": {"...": "..."},
    "procedural": {"...": "..."}, "default": {"...": "..."}
  },
  "warnings": ["arithmetic: n_total=8 < 30, floors marked low_confidence"]
}
```

Any cls with `n_total < 30` emits a warning and copies `default` floors instead.

## 6. helix.toml schema additions

```toml
[retrieval]
ann_threshold_mode = "absolute"            # "absolute" | "margin_over_random"  (default: absolute, back-compat)
ann_threshold_sigma_multiplier = 3.0       # only read when mode=margin_over_random
# ann_similarity_threshold stays as the absolute fallback / legacy value

[abstain]
mode = "global"                            # "global" | "per_classifier"  (default: global)
# Global mode: existing TIGHT_SCORE_FLOOR=5.0, FOCUSED_SCORE_FLOOR=2.5 remain hard-coded constants.

[abstain.factual]
abstain_top = 0.42
focused_top = 0.71
tight_top = 1.18
foveated_alpha = 1.4

[abstain.multi_hop]
abstain_top = 0.38
focused_top = 0.55
tight_top = 0.92
foveated_alpha = 0.6

[abstain.arithmetic]
foveated_alpha = 1.6
# ...
[abstain.procedural]
foveated_alpha = 0.9
[abstain.default]
foveated_alpha = 1.0
```

`mode="global"` preserves Stage-3 behavior exactly. Flipping to `"per_classifier"` requires every emitted `cls` to have a block; missing → loader raises `ConfigError`.

## 7. Per-classifier foveated_alpha

`HelixContextManager` already receives `ClassifierResult` upstream. At line 1094, replace `alpha=self.config.budget.foveated_alpha` with a lookup helper:

```python
def _alpha_for_cls(self, cls: str) -> float:
    if self.config.abstain.mode == "per_classifier":
        return self.config.abstain.per_class[cls].foveated_alpha
    return self.config.budget.foveated_alpha
```

Threaded through `_compute_foveated_caps(n, alpha=...)` unchanged. Window metadata records `foveated_alpha_source: "per_classifier:factual"` for telemetry.

## 8. Wire-through points

- **`context_manager.py:946-989`** — replace `FOCUSED_SCORE_FLOOR_FOR_ABSTAIN=2.5`, `TIGHT_SCORE_FLOOR=5.0`, `FOCUSED_SCORE_FLOOR=2.5` with `floors = self._floors_for(cls)`. The three checks become `top_score < floors.abstain_top`, `top_score >= floors.tight_top`, `top_score >= floors.focused_top`. Ratio gates (1.8, 3.0) untouched — they're scale-free.
- **`genome.py:371`** — `self._ann_threshold` initialised from helix.toml as today; `query_genes()` calls `self._effective_ann_threshold()` which checks `mode` and reads `genome_calibration` row. Cached after first read.
- **Classifier output already flows in** via the existing injection-router path; just persist `result.cls` onto the active query context for the budget block to read.

## 9. Provenance in /health and /context

**`/health`** adds:
```json
"calibration": {
  "ann_threshold": 0.411,
  "ann_threshold_mode": "margin_over_random",
  "ann_threshold_provenance": {"mu": 0.018, "sigma": 0.131, "N": 10000, "dim": 1024},
  "abstain_mode": "per_classifier",
  "computed_at": "2026-05-08T14:32:00Z",
  "calibration_age_days": 0
}
```

**`/context` response** — add to existing `agent` dict:
```json
"calibration_age_days": 0,
"calibration_stale": false        // true if age > 30
```

When stale, also add a `warnings: ["calibration_stale"]` entry. Loader logs WARNING at startup if age > 30 days.

## 10. Test plan

```
tests/test_calibration.py

test_calibrate_threshold_outputs_margin_over_random_value
  Build 50-gene fixture genome with dim=64 random unit vectors.
  Run calibrate(genome, N=1000). Assert |mu - 0| < 0.05, threshold = mu+3sigma,
  threshold < 1.0.

test_per_classifier_abstain_factual_tighter_than_multi_hop
  Synthesize bench JSON: factual rows hit at score 0.7, multi_hop hit at 0.4.
  Run calibrator. Assert floors.factual.tight_top > floors.multi_hop.tight_top.

test_calibration_report_jsonschema_validates
  Run calibrator on fixture, validate report against
  schemas/calibration_report.v1.json with jsonschema.validate(...)

test_global_mode_preserves_legacy_behavior
  ContextManager(config(abstain.mode="global")). Inject scored candidates with
  top_score=4.9, ratio=2.5. Assert tier="focused" (matches pre-Stage-4 5.0 floor).

test_property_random_genome_threshold_rejects_99pct (hypothesis)
  @given dim in [128, 1024], n_genes in [200, 5000]:
    Build genome of unit-Gaussian vectors. Calibrate.
    Sample 5000 fresh random pairs. Assert >= 0.99 fall below threshold.

test_floor_lookup_falls_back_to_default_when_cls_missing
test_calibration_age_warning_on_stale_db (mock now() to 31 days ahead)
test_genome_calibration_table_upsert_idempotent
```

Mock-only; no live Ollama. Add `tests/fixtures/calibration_bench_50.json` minimal.

## 11. Acceptance criteria

- `bench_needle_1000.py` retrieval@1 ≥ **82%** with Stages 1+2+3+4 stacked (vs current 13.8% at thr=0.30).
- Per-classifier abstain rate on factual queries ≤ **5%** (current implicit ~95% — 5.0 floor unreachable post-RRF).
- `mode="global"` regression diff vs pre-Stage-4 head ≤ **±1 row** out of 1000 on the same bench.
- `genome_calibration` UPSERT round-trips: `calibrate → reopen → /health` returns identical provenance.
- Property test passes ≥ 50 hypothesis examples.

## 12. Out of scope

- `caller_model_class` adaptive sizing (Stage 5).
- Know/miss negative-evidence block (Stage 6).
- Auto-recalibration cron / scheduled refresh.
- Per-classifier α auto-tuning beyond defaults.
