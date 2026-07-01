# External-bench run plan — 3 arms + pre-run gap closure

**Date:** 2026-07-01 · **Executes:** council plan step 2
(`docs/research/2026-07-01-next-bench-wave-consensus.md`) · **Leads:** Max + Claude
**Inputs:** E1 repo verification + R1 web landscape refresh (2026-07-01, citations inline).

## 0. Landscape deltas since the 2026-06-04 strategy doc (R1, verified)

- **ContextBench:** leaderboard unchanged (Sonnet-4.5 Pass@1 53.0 / context-F1
  0.344 top; only 4 foundation models) — our packet numbers still land against
  the same field. **Harness got 2026-06-12 commits** (vendored Prometheus-agent
  integration; Multi-SWE-bench JSONL loading fix) — **re-pin the scorer venv to
  post-2026-06-12 HEAD** before any non-Python split.
  [leaderboard](https://contextbench.github.io/) · [commits](https://github.com/EuniAI/ContextBench)
  Dataset license **still unstated** on the HF card — keep results internal
  until cleared for public claims. [HF card](https://huggingface.co/datasets/Contextbench/ContextBench)
- **RACE-bench:** still arXiv-only, v1, zero artifact links — remains
  metric-inspiration only (OverPrediction@RelevantFiles).
  [arXiv 2603.26337](https://arxiv.org/abs/2603.26337)
- **NEW — SWE-Explore** (submitted 2026-06-05): line-budgeted **ranked
  retrieval** over repo+issue — 848 issues, 10 languages, 203 repos, line-level
  gold, coverage/ranking/context-efficiency metrics, **code+data released**.
  This is helix's exact operating mode (ranked regions under a token budget)
  and its 10-language spread directly stresses our python-only symbol-ref
  caveat. Candidate **arm-for-arm addition after the core 3-arm run**; check
  repo/dataset license first.
  [arXiv 2606.07297](https://arxiv.org/abs/2606.07297) ·
  [code](https://github.com/Qiushao-E/SWE-Explore-Bench) ·
  [data](https://huggingface.co/datasets/SWE-Explore-Bench/SWE-Explore-Bench)
- **CodeRAG-Bench:** dormant since 2024-11; **repo has NO license set** — do
  not publish CodeRAG-derived numbers externally until resolved (CC-BY-SA
  applies to the data; the harness license is the gap).
  [repo](https://github.com/code-rag-bench/code-rag-bench)
- **RepoBench-R:** dormant (v1.1, data through Dec 2023); fine as a
  low-effort corroboration arm, CC-BY-4.0. [repo](https://github.com/Leolty/repobench)
- **Reading list before novelty claims:** LARGER — lexically-anchored
  structural repo retrieval ([arXiv 2605.16352](https://arxiv.org/pdf/2605.16352),
  unconfirmed) — methodological kin to lexical-first + symbol graph; and the
  stale-repository-context diagnostic ([arXiv 2605.14478](https://arxiv.org/pdf/2605.14478),
  unconfirmed) — external corroboration for the freshness-gate story.

## 1. Pre-run gaps to close (E1 findings — all small)

1. **`helix_probe_nosema.toml` is not in the tree.** The regression log's
   method notes reference it; it exists (if anywhere) only untracked on the
   rig. Action: locate on rig and **commit it** (bench configs must be
   reproducible), or reconstruct: lexical config, SEMA off
   (`[ingestion] sema_embed_on_ingest = false`), dense off, defaults otherwise.
2. **AST-vs-regex path counter is absent** (Phase-0 observability; council
   finding #10's sibling). `CodonChunker._chunk_code` silently falls back to
   regex on `ImportError`/`ValueError`. Without it, "the AST path actually
   fired" is an assumption, not an assertion. Apply this patch on the
   cast-merge branch **before** the run (also emit once per ingest batch to the
   log):

   ```python
   # fragments.py — in _chunk_code, at the AST-success and fallback branches:
   try:
       from ..telemetry import tier_fired_counter
       tier_fired_counter().add(1, {"tier": "chunk_ast"})      # AST branch
       # tier_fired_counter().add(1, {"tier": "chunk_regex"})  # fallback branch
   except Exception:
       pass
   log.info("chunking path: %s for %s", "ast" | "regex", source_hint)
   ```

   Reuses the existing `helix_tier_fired_total` series (no new instrument);
   the bench assertion is then `chunk_ast > 0 and chunk_regex == 0` for code
   corpora, greppable from logs even with OTel off.
3. **CLAUDE.md knob sync** (merge-gate; helix.toml + config.py already agree
   on the WS branch — only CLAUDE.md lags). Add to the `[ingestion]` /
   `[retrieval]` rows of the config table in the merge commit:
   - `[ingestion] sema_embed_on_ingest` (default true; false = no MiniLM load
     at ingest, TCM falls back to text)
   - `[ingestion] symbol_graph` (default **false** — dark-shipped WS2)
   - `[retrieval] symbol_expansion_cap` (default 8; 0 disables, <0 unbounded)
4. **Arm-C config variant doesn't exist** — create `helix_probe_symbol.toml` =
   arm-B TOML + `[ingestion] symbol_graph = true` +
   `[retrieval] symbol_expansion_cap = 8`.

## 2. The arms

| Arm | Build | Config | What it answers |
|---|---|---|---|
| A — BM25 foil | frozen baseline | (hardcoded in harness) | the dump baseline |
| B — cAST default | cast-merge (#228+#229) | `helix_probe_nosema.toml` | PRD Phase-1: does cAST beat BM25 on unseen corpora? |
| C — symbol arm | ws3-pagerank (#230+#231) | `helix_probe_symbol.toml` (gap #4) | held-out evidence for the WS2/WS3 default decision |

Runner (per regression-log method): `cb_helix_pred.py --tag <arm> --config
<toml> --workers 3`, scored by the (re-pinned, §0) scorer venv over the
common instance-id intersection. RepoBench-R / CodeRAG-Bench per their
runbooks, same three arms.

## 3. Run discipline

- **OTel on for every arm** (`HELIX_OTEL_ENABLED=1`) — each run doubles as G1
  capture + calibration data (dense-cosine, know-confidence, PKI series all
  land in Grafana now).
- **AST assertion** (gap #2) checked before scoring any arm-B/C run.
- **Load annotation:** per the 2026-07-01 method note, record rig load with
  every run; latency comparisons only within matching load profiles.
- **Held-out discipline:** no knob changes between arms; the cap sweep
  {4, 8, 16} runs only if arm C shows ≥ +1pp on either external bench, and on
  a corpus not used for tuning.
- **Decision rules** (unchanged from council): B ≤ BM25 → halt code-track
  merges, re-open chunking. C ≥ +1pp → cap sweep + revisit default-on +
  unlock scope-gap work. C regresses on both → WS2/WS3 stays dark permanently.

## 4. After the core run

- SWE-Explore arm (§0) if licenses clear — its context-efficiency metric is
  the publishable form of our injected-tokens story.
- Gate run per the judge protocol in
  `docs/specs/2026-07-01-abstain-search-escalation.md` §4 (≥350 answered,
  trinary cross-family judge, risk-coverage reporting) — first measured G2
  datapoint.
