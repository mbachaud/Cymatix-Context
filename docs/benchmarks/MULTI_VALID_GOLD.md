# Multi-Valid-Gold Needle Matching

**Status:** shipped 2026-05-14 in `feat/bench-multi-valid-gold` (PR forthcoming).
**Scope:** `benchmarks/bench_claude_matrix.py` (the 10-needle matrix
runner) + `benchmarks/bench_needle.py` (the legacy 10-needle bench).
**Out of scope:** the 1000-needle bench (`bench_needle_1000.py` uses a
different per-needle schema with a single `source` string), the
multi-needle group benches (`bench_multi_needle*.py` use
`gold_source_groups` with stricter all-of semantics), and any retrieval
code.

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

## Related tests

* `tests/test_bench_needles.py` — 13 regression tests covering
  ANY-match semantics, path normalization, single-gold legacy
  compatibility, schema sanity, the matrix↔needle sync invariant, and
  the bench-answer-key exclusion.
* `tests/test_bench_citations.py` — the citation parser tests (not
  affected by this change, but exercised together as the bench-data
  test suite).
