# Multi-Valid-Gold Needle Matching

**Status:** shipped 2026-05-14 in `feat/bench-multi-valid-gold` (PR forthcoming).
**Last updated:** 2026-05-28 — added §"EnterpriseRAG-Bench gold-path matching" for the Layer 3 multi-gold pattern.
**Scope:** `benchmarks/bench_claude_matrix.py` (the 10-needle matrix
runner) + `benchmarks/bench_needle.py` (the legacy 10-needle bench).
**Out of scope:** the 1000-needle bench (`bench_needle_1000.py` uses a
different per-needle schema with a single `source` string), the
multi-needle group benches (`bench_multi_needle*.py` use
`gold_source_groups` with stricter all-of semantics), and any retrieval
code. **The Layer 3 EnterpriseRAG-Bench harness uses a different multi-gold
mechanism with the same spirit — see §"EnterpriseRAG-Bench gold-path matching" below.**

## Why

The `retr_hit` (gold-source-in-citations) metric on the 10-needle
matrix was reporting 2/10 on `medium-sharded` vs 3-4/10 on `medium-blob`.
Inspection showed the gap was partly **labeling artifact**, not a
retrieval regression: several needles had a single `gold_source` entry
that wasn't the only — or even the best — file that answered the query.

Concrete example: `helix_port` asks "What port does the Helix proxy
listen on?" with `gold_source = ["helix-context/helix.toml"]`. The
literal value `11437` is also documented in `README.md`, `CLAUDE.md`,
`docs/SETUP.md`, `docs/TROUBLESHOOTING.md`, and `docs/api/endpoints.md`.
A retrieval run that surfaces `TROUBLESHOOTING.md` (a perfectly valid
answer source for the agent) gets scored as a miss under
single-`gold_source` even though the agent will give the right answer.

Multi-valid-gold makes `retr_hit` reflect what it claims to: "did the
retriever find a document that legitimately answers the query".

## How hit detection works

`gold_source` is a **list of case-insensitive path substrings**. A
needle counts as `gold_delivered=True` when ANY entry in the list
matches a delivered citation source. The match rule (lifted from
`bench_claude_matrix.retrieval_probe` and `bench_needle.check_gold_delivery`):

```python
for src in delivered_sources:                       # agent.citations[].source
    norm = src.replace("\\", "/").lower()
    if any(gs.replace("\\", "/").lower() in norm    # substring containment
           for gs in gold_sources):
        return True                                  # ANY-match short-circuit
```

Notes:

* Forward-slash + lowercase normalization, so Windows mixed-case paths
  match lowercase gold entries (regression test
  `test_match_is_case_insensitive`).
* Substring match in EITHER direction in `bench_needle_1000.py`'s
  `_gold_source_in_citations`, single direction in the 10-needle
  harness — both are exercised by the tests.
* Directory substrings work (`helix-context/docs` matches any file
  under that directory), but be cautious: an over-broad substring
  can match files that don't actually answer the question.

The hit-detection code was **already ANY-match across the list** before
this PR (introduced in #105 / PR `bench-claude-matrix`). The change here
is purely the curated data: more legitimate sources per needle.

## Curation policy

1. **Conservative inclusion.** A file counts only if an honest reader
   would consider it a valid source for the answer — not "mentions the
   topic", not "uses the keyword". For `bookkeeper_monetary` the
   accepted sources are `CLAUDE.md` (states the policy), `GAPS.md` (the
   decision rationale), and `storage/db.py` (the implementation with
   `to_decimal()`). The module docstrings in `journal.py`, `categorizer.py`,
   etc. that say "All monetary values use Decimal" are valid in
   principle but duplicative — included files cover them via the
   ANY-match semantics on the principal sources.

2. **Audit-trail preservation.** The historically labeled `gold_source`
   stays first in the list. Older JSONL captures and analyses that
   pivoted on the first entry remain comparable.

3. **No answer-key files.** `docs/benchmarks/BENCHMARKS.md` contains
   the bench needles AND their expected answers as a meta-spec. It is
   NEVER a valid gold source — adding it would make retrieval succeed
   whenever the answer-key doc is retrieved (circular). Enforced by
   `tests/test_bench_needles.py::test_bench_answer_key_doc_never_in_gold_source`.

4. **No test fixtures.** Files like `tests/test_integration.py` that
   embed needle facts as test data are not valid sources for the
   bench either.

## Per-needle table

### Original 10 needles (2026-05-14 expansion)

| Needle | Expected | Curated valid sources | Notes |
|---|---|---|---|
| `helix_port` | `11437` | `helix.toml`, `README.md`, `CLAUDE.md`, `docs/SETUP.md`, `docs/TROUBLESHOOTING.md`, `docs/api/endpoints.md` | Six legitimate sources — port is mentioned in setup, troubleshooting, the API reference, and the README quickstart. |
| `scorerift_threshold` | `0.15` | `two-brain-audit/README.md`, `docs/QUICKSTART.md`, `src/scorerift/reconciler.py`, `src/scorerift/engine.py` | README & QUICKSTART state the rule in prose; the two Python modules carry the `divergence_threshold: float = 0.15` default. |
| `biged_skills_count` | `125` (accepts `129`) | `Education/CLAUDE.md`, `FRAMEWORK_BLUEPRINT.md`, `ROADMAP.md`, `fleet/CLAUDE.md`, `fleet/knowledge/wiki/overview.md`, `fleet/knowledge/wiki/architecture.md` | Skills count is duplicated across project-status banners in 6 prominent docs. Count drifts between 125 and 129 across versions (accept list covers both). |
| `bookkeeper_monetary` | `Decimal` | `BookKeeper/CLAUDE.md`, `docs/planning/GAPS.md`, `bookkeeper/storage/db.py` | CLAUDE.md states the policy; GAPS.md has the 2026-03-24 decision rationale; `storage/db.py` defines the `to_decimal()` / `to_db_amount()` helpers. |
| `helix_pipeline_steps` | `6` (accepts `six`) | `helix-context/CLAUDE.md`, `README.md` | **Stale-needle warning:** current CLAUDE.md and README describe a 7-stage pipeline (classify + freshness gate were added). The "6" answer survives only in archived plans. retr_hit can still succeed by retrieving CLAUDE.md or README, but the answer-score will likely be 0 because the agent reads the current "7". |
| `biged_rust_binary_size` | `11` (`11 MB`) | `Education/biged-rs/README.md`, `DEPLOYMENT.md`, `fleet/knowledge/wiki/architecture.md` | All three explicitly state "11 MB release binary" with the build command. |
| `genome_compression_target` | `5x` | `helix-context/README.md`, `helix-context/docs`, `BENCHMARK_NOTES.md` | **Stale-label warning:** the original gold included `helix-context/README.md`, but README does NOT contain `5x` today — the only canonical claim lives in `BENCHMARK_NOTES.md` ("Targets: 5x, 7x, 10x"). The `helix-context/docs` substring also covers `docs/archive/research/RESEARCH.md`. README is kept for audit-trail but is effectively dead weight. |
| `scorerift_preset_dimensions` | `8` | `two-brain-audit/README.md`, `docs/ARCHITECTURE.md`, `src/scorerift/presets/python_project.py` | README and ARCHITECTURE state "8 dimensions" in tables; the preset module's docstring confirms it. |
| `helix_ribosome_budget` | `3000` | `helix-context/helix.toml`, `README.md`, `docs/config-reference.md` | **Stale-label warning:** `helix-context/README.md` does NOT contain `3000` — the canonical sources are `helix.toml` (the live config default) and `docs/config-reference.md` (the documented default for the `[budget]` section). README kept for audit-trail. |
| `biged_default_model` | `qwen3` (`qwen3:4b`) | `Education/CLAUDE.md`, `FRAMEWORK_BLUEPRINT.md`, `OPERATIONS.md`, `fleet/fleet.toml` | `fleet.toml` is the authoritative config (`conductor_model = "qwen3:4b"`); the three Markdown docs all state the same value in their fleet-model tables. |

### N=50 expansion (2026-05-15 PR feat/bench-needles-50)

Why grow the set: with N=10, the per-fixture correctness metric has ~1.15-stddev
noise — too noisy to detect single-knob tuning effects (dense recall, classifier
cap, expression_tokens probes all came back below the floor). N=50 cuts the SE
roughly in half (1/sqrt(5)) and adds shard coverage proportional to corpus size.

Coverage per shard (gold_source[0] root): BookKeeper × 7, CosmicTasha × 5,
two-brain-audit × 5, MaxExpressKit × 3, Education × 16, helix-context × 14.

Every new needle was verified against the medium-sharded fixture genes table
(`scripts/verify_facts.py`-style sqlite probe) before landing — the
`helix_ribosome_budget` / `genome_compression_target` stale-label class of bug
is not present in the new set.

| Needle | Expected | Curated valid sources | Notes |
|---|---|---|---|
| `bookkeeper_dashboard_port` | `8080` | `BookKeeper/bookkeeper.toml`, `README.md`, `docs/TENANCY_QUICKSTART.md` | `[api] port = 8080` in toml; README quickstart cites `http://127.0.0.1:8080`. |
| `bookkeeper_1099_threshold` | `600` | `BookKeeper/bookkeeper.toml`, `README.md`, `exports/tax_engine.py` | toml: `w9_required_threshold = 600`; tax_engine docstring states `>= $600`. |
| `bookkeeper_ocr_confidence` | `0.85` | `BookKeeper/bookkeeper.toml`, `README.md` | toml `[ingestion.ocr] confidence_threshold = 0.85`. |
| `bookkeeper_version` | `0.13.0` | `BookKeeper/pyproject.toml`, `bookkeeper/cli.py`, `CLAUDE.md`, `README.md` | pyproject `version = "0.13.0b"`; CLI `__version__`; CLAUDE phase ladder. |
| `bookkeeper_test_count` | `507` | `BookKeeper/README.md` | Sole authoritative claim ("507 tests passing"). |
| `bookkeeper_backup_interval` | `1200` | `BookKeeper/bookkeeper.toml` | `[backup] interval_secs = 1200  # 20 minutes`. |
| `cosmictasha_template_count` | `14` | `CosmicTasha/README.md`, `docs/superpowers/plans/2026-04-07-auth-biged-templates.md`, `web/src/lib/compliance-kb/soc2/templates/index.ts` | README repo-tour table; plan doc; the templates index module. |
| `cosmictasha_default_model` | `qwen3:8b` | `CosmicTasha/README.md`, `.github/workflows/ci.yml` | README env-var table and CI matrix both pin `qwen3:8b`. |
| `cosmictasha_biged_port` | `5555` | `CosmicTasha/README.md`, `docker-compose.yml`, `.github/workflows/ci.yml` | All three set `BIGED_URL=https://localhost:5555`. |
| `cosmictasha_auth_library` | `Lucia` | `CosmicTasha/README.md`, `.planning/03-architecture-decisions.md` | README architecture bullet; ADR-006 captures the decision rationale. |
| `cosmictasha_postgres_version` | `16` | `CosmicTasha/README.md`, `docker-compose.yml`, `.planning/01-infrastructure-hosting.md` | README architecture diagram; compose image tag `postgres:16-alpine`. |
| `scorerift_defense_layers` | `6` | `two-brain-audit/README.md` | "Six defense layers" + 6-row table. |
| `scorerift_confidence_floor` | `50%` | `two-brain-audit/README.md` | "auto-confidence is above 50%" in the divergence-detection paragraph. |
| `scorerift_pkg_version` | `2.0.0` | `two-brain-audit/pyproject.toml` | `version = "2.0.0"` — note v1.0.0 plan docs exist but pyproject is authoritative. |
| `mek_version` | `0.1.3` | `MaxExpressKit/package.json`, `pyproject.toml`, `marketplace.json` | All three pin `0.1.3`; marketplace.json is the install entry. |
| `mek_min_python` | `3.11` | `MaxExpressKit/pyproject.toml` | `requires-python = ">=3.11"`. |
| `mek_source_apps` | `three` | `MaxExpressKit/README.md`, `docs/superpowers/specs/2026-05-10-mek-design.md` | "distilled from three full apps (CosmicTasha, ScoreRift, BookKeeper)". |
| `biged_dashboard_port` | `5555` | `Education/README.md`, `DEVELOPMENT.md` | "Real-time web UI at localhost:5555"; DEVELOPMENT health-check curl. |
| `biged_ram_ceiling` | `97` | `Education/fleet/fleet.toml`, `CLAUDE.md`, `FRAMEWORK_BLUEPRINT.md` | toml `ram_ceiling_pct = 97`; CLAUDE & blueprint state same. |
| `biged_max_workers` | `14` | `Education/fleet/fleet.toml`, `biged-rs/tests/contract_test.rs` | toml `max_workers = 14`; Rust contract test pins the same value. |
| `biged_core_agents` | `4` | `Education/README.md`, `ROADMAP.md`, `docs/flowcharts/boot_sequence.txt` | README feature bullet; ROADMAP dynamic-scaling section; ASCII boot diagram. |
| `biged_audit_dimensions` | `12` | `Education/CLAUDE.md`, `ROADMAP.md`, `docs/superpowers/plans/ray_trace_plan.md` | CLAUDE docs-pointer table; ROADMAP audit-coverage check; ray_trace plan's BigEd graph. |
| `biged_db_tables` | `34` | `Education/CLAUDE.md`, `AUDIT_TRACKER.md`, `AUDIT_TRACKER_COMPLIANCE_REPORT.md` | Fleet-status banner duplicated across the three. |
| `biged_thermal_target` | `75` | `Education/fleet/fleet.toml`, `FRAMEWORK_BLUEPRINT.md` | toml `cooldown_target_c = 75`; blueprint thermal section. |
| `biged_vision_model` | `llava` | `Education/fleet/fleet.toml`, `biged-rs/tests/contract_test.rs` | toml `vision_model = "llava"`; Rust contract test pins same. |
| `biged_rust_msrv` | `1.76` | `Education/biged-rs/Cargo.toml`, `README.md` | workspace `rust-version = "1.76"`; README requirements. |
| `biged_rust_web_framework` | `axum` | `Education/biged-rs/Cargo.toml`, `README.md`, `DEPLOYMENT.md`, `FRAMEWORK_BLUEPRINT.md` | Cargo workspace dep, crate description, deployment doc, top-level blueprint. |
| `biged_complex_model` | `gemma4:31b` | `Education/fleet/fleet.toml`, `FRAMEWORK_BLUEPRINT.md`, `SESSION_HANDOFF.md` | toml `complex = "gemma4:31b"`; blueprint model-tier table. |
| `biged_smoke_tests` | `51/52` | `Education/CLAUDE.md`, `CONTRIBUTING.md`, `CROSS_PLATFORM.md` | Fleet-status banner; release checklist; cross-platform parity table. |
| `biged_idle_timeout` | `10` | `Education/fleet/fleet.toml`, `biged-rs/fleet.toml`, `biged-rs/crates/biged-core/src/config.rs` | toml `idle_timeout_secs = 10`; Rust config default. |
| `helix_expression_budget` | `7000` | `helix-context/helix.toml`, `overnight_logs/broad_tighten_2026-05-12_1422_report.md` | toml `[budget] expression_tokens = 7000`; report contrasts pre/post tighten. |
| `helix_max_genes_per_turn` | `12` | `helix-context/helix.toml`, `docs/config-reference.md` | toml `max_genes_per_turn = 12`; config reference example. |
| `helix_rrf_k` | `60` | `helix-context/helix.toml`, `docs/config-reference.md`, `docs/specs/2026-05-08-stage-3-rrf-fusion.md` | toml `rrf_k = 60  # Cormack 2009 default`; spec cites the same paper. |
| `helix_cold_start_threshold` | `10` | `helix-context/helix.toml`, `docs/config-reference.md`, `CLAUDE.md` | toml `cold_start_threshold = 10`; config reference duplicate; CLAUDE table entry. |
| `helix_session_window` | `300` | `helix-context/helix.toml`, `docs/config-reference.md`, `CLAUDE.md` | toml `synthetic_session_window_s = 300  # 5 min`. |
| `helix_headroom_port` | `8787` | `helix-context/helix.toml`, `docs/config-reference.md` | `[headroom] port = 8787`; config-reference Headroom section. |
| `helix_calibration_staleness` | `30` | `helix-context/helix.toml`, `docs/specs/2026-05-08-stage-6-know-miss-blocks.md` | `[know] stale_after_days = 30`; Stage-4 spec §9. |
| `helix_dense_encoder` | `BGE-M3` | `helix-context/CLAUDE.md`, `README.md`, `helix.toml` | CLAUDE pipeline description; README how-it-works; toml retrieval section comment. |
| `helix_filename_anchor` | `4.0` | `helix-context/helix.toml`, `docs/config-reference.md` | `[retrieval] filename_anchor_weight = 4.0`; config-reference duplicate. |
| `helix_subpackages_count` | `16` | `helix-context/CLAUDE.md`, `docs/superpowers/plans/2026-05-13-readme-v3-plan.md` | CLAUDE post-PR#90 package-structure table heading. |

### Headline expansion

`helix_port` is the clearest case: **1 → 6 valid sources**. Any
retrieval that surfaces the proxy port via the user-facing docs
(`SETUP.md`, `TROUBLESHOOTING.md`, `endpoints.md`, etc.) — i.e., exactly
what an agent answering this query would want to see — now counts as a
hit. Under strict single-gold the same delivery scored as a miss.

## Adding a new needle

1. Add the needle dict to BOTH `benchmarks/bench_claude_matrix.py` and
   `benchmarks/bench_needle.py`. The harness-sync regression test
   (`test_matrix_and_needle_needles_have_matching_gold`) will fail
   loudly if they drift.

2. Curation workflow per needle:
   1. Read the query and the expected answer.
   2. Grep the medium-fixture source roots (`F:/Projects/BookKeeper`,
      `F:/Projects/CosmicTasha`, `F:/Projects/two-brain-audit`,
      `F:/Projects/MaxExpressKit`, `F:/Projects/Education`,
      `F:/Projects/helix-context`) for the literal answer.
   3. For each hit file, decide:
      - Does this file *answer* the query (state the fact in prose,
        config, or code defaults)? ✅ include.
      - Does it merely mention the topic, or contain the value as a
        URL component / metadata header / test fixture? ❌ skip.
   4. Keep the historically labeled gold first in the list.
   5. Never add `docs/benchmarks/BENCHMARKS.md` (the answer key).
   6. Never add test files that embed the fact as fixture data.

3. The accept-substring list (`accept`) governs the agent-answer score
   and is independent of `gold_source` (which governs the retrieval
   score). They should be aligned but don't need to be byte-identical.

## Backward compatibility

Existing JSONL files were already list-shaped — `gold_source: [single]`
remains valid input to all analysis scripts. Verified call-sites:

* `benchmarks/bench_claude_matrix.py:retrieval_probe` — ANY-match list
  iteration since #105.
* `benchmarks/bench_needle.py:check_gold_delivery` — ANY-match list
  iteration since #105.
* `scripts/diagnose_query_extraction.py` — iterates the list with no
  size assumption.
* `scripts/diagnose_file_grain.py` — iterates the list with no size
  assumption.

The 1000-needle bench (`bench_needle_1000.py`) uses a separate per-needle
schema (`source` as a single string) and is **unaffected** by this
change. The same is true for the multi-needle group benches.

## EnterpriseRAG-Bench gold-path matching (added 2026-05-28)

Layer 3 of [`BENCHMARKS.md`](BENCHMARKS.md) uses a separate harness
(`benchmarks/score_enterprise_rag_onyx.py`,
`benchmarks/ablate_dense_prefilter.py`) with a different per-question
schema, but the **same "ANY-match against a gold list" spirit** as the
matrix bench.

### Schema

Each EnterpriseRAG-Bench question (from upstream `questions.jsonl` +
`uuid_index.json`) ships with:

```jsonc
{
  "id": "qst_0001",
  "question": "What is the deployment process for the new auth service?",
  "type": "basic",                              // or "semantic" / "intra_document_reasoning"
  "expected_doc_ids": [                         // gold-path list — ANY-match
    "9f3c1b...",                                // dsid → resolved to a path under sources/
    "a4d822..."
  ]
}
```

The dsid → relative-path resolution happens via
`generated_data/uuid_index.json` (the upstream's dsid → file map). At
score time, `expected_doc_ids` becomes a list of relative paths under
`generated_data/sources/`.

### Match rule

The orchestrator computes a hit when ANY entry in `expected_doc_ids`
matches a delivered citation's source path, using
`benchmarks/ablate_dense_prefilter._rel_after_sources` to normalize both
sides before comparison:

```python
gold_rels = {_rel_after_sources(p) for p in n["gold_paths"]}
gold_rels = {r for r in gold_rels if r}
# ...
for fp in fps:                                      # /fingerprint response
    rel = _rel_after_sources(fp.get("source", "")) or ""
    if rel in gold_rels:
        hit_rank = fp["rank"]
        break
```

Same ANY-match short-circuit as the matrix bench, just with **path
normalization that strips the variable prefix above `sources/`** rather
than substring containment. This handles the case where Helix delivers a
citation with an absolute Windows path (`F:\Projects\EnterpriseRAG-Bench-main\generated_data\sources\slack\incidents\msg-123.md`)
but the gold list ships relative paths (`slack/incidents/msg-123.md`) —
both normalize to `slack/incidents/msg-123.md` for the match.

### Prefix-tolerant gold matching (PR #148, 2026-05-26)

A subtle gold-match bug was found and fixed in this layer's first pass
(closes `#137 partial`): when a citation source lacks the `sources/`
segment in its path (e.g., a synthetic test fixture or a hand-built
shard), `_rel_after_sources` returned `None` rather than the path
as-given. Downstream this caused false `gold=False` at delivery time
even when the file content was correct.

**Fix:** `_rel_after_sources` now falls back to the raw normalized path
when no `sources/` segment is found, so prefix-mismatched citations
still get matched against the gold list when the trailing path
components agree.

Audit verification: d1 untainted, 0 flips on the existing fixtures,
recall sweep clean after the fix. See PR #148 commit message for the
exact diff.

### Why the EnterpriseRAG-Bench harness can't reuse the matrix-bench code

Two structural differences:

1. **List-of-dsids vs list-of-substrings.** EnterpriseRAG-Bench gold is
   a list of opaque document IDs that must be resolved through
   `uuid_index.json` to relative paths — there's no natural substring
   match (`gmail/jonas_weber/msg-9024.md` is not a substring of itself
   under absolute paths). The matrix bench's substring-containment rule
   wouldn't work without further normalization.
2. **No author curation on which gold paths are "best".** The
   matrix-bench curation policy (above) emphasized hand-picking one
   canonical path per needle. EnterpriseRAG-Bench's gold lists come from
   the upstream benchmark generator — every listed dsid is by definition
   a valid answer, and the helix orchestrator treats them as equal.

The end result is the same retrieval-quality signal — *"did the system
surface a document that actually answers the question?"* — but
mechanically implemented in different code with different schemas. Tests
on both harnesses pin the ANY-match contract independently.

---

## Related tests

* `tests/test_bench_needles.py` — 13 regression tests covering
  ANY-match semantics, path normalization, single-gold legacy
  compatibility, schema sanity, the matrix↔needle sync invariant, and
  the bench-answer-key exclusion.
* `tests/test_bench_citations.py` — the citation parser tests (not
  affected by this change, but exercised together as the bench-data
  test suite).
* `tests/test_bench_path_match_prefix_stripped.py` — added with PR #148;
  pins the prefix-tolerant `_rel_after_sources` behavior so a citation
  with a non-`sources/` path still matches a gold entry whose trailing
  components agree.
