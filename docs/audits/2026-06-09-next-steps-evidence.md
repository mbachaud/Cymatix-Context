# Next Steps — evidence-backed roadmap (2026-06-09 research loop)

Produced by a 3-agent research/examine/document loop (issues+PRs audit,
public-product hardcoding audit, retrieval-tuning evidence study) at
v0.7.1. Companion spec: `docs/specs/2026-06-09-retrieval-profiles.md`.

## State

Backlog flushed 2026-06-09 (merge train #182–#200). Open: #165 (storage —
~70% executed by #193, needs corpus-scale verification) and #93 (500K
blob-vs-sharded bench — every named blocker now merged). The bottleneck is
measurement, not triage.

## Ranked next steps

1. **Rerun the 500K ERB sharded build** (auto-subshard + resume now merged).
   Prior attempt: 19h32m, throughput collapse 27→0.12 genes/s on an 18 GB
   single shard; predicted clean build 10–14 h. (M)
2. **500K recall sweep + scored Q&A vs published baselines** (BM25 recall
   68.4%; correctness BM25 68.8 / Vector 51.4 / Onyx+GPT-4 72.4). Closes #93.
   Helix's own curve so far: 83% @10K → 71% @50K (all misses `basic`,
   14/17 monotone). (M)
3. **Verify #193 at corpus scale and close #165** (predicted ~13 GB back;
   single-shard confirmation measured −24% file size). Then ship the
   residual schema wins: drop legacy `embedding` TEXT column + `complement`
   for chromatin<2 (~2–3 GB, zero recall risk). (S)
4. **Run the dense_additive_weight sweep on ERB and flip the default**
   (#138 evidence: −19pp from gold eviction at 4.0 on prose). Harness ships
   (#188); no tracking issue existed before this loop. (S)
5. **Run the SPLADE scale curve; set auto-toggle thresholds** (#164:
   21.1% disk for 0pp at 850K; knobs shipped in #189). (M)
6. **Investigate the `basic`-type monotone miss-set** (83→71→? recall
   curve) once the 500K fixture exists — stable misses are a targeted
   lexical/synonym fix. (M)
7. **Implement retrieval profiles per the spec** (3 layers: auto-calibrate,
   classifier-owned semantic arm, 3 small corpus profiles). (L)
8. **Decide the Wall-2 orphans**: PRs #158/#160 (parallel fan-out + SPLADE
   prefilter for ~870M FLOPs/query dense cost) were closed UNMERGED;
   master has no Wall-2 lever. Re-land or document supersession. (M–L)

## Public-product hardcoding (wave 2 — wave 1 shipped in this PR)

- Plumb the documented `[retrieval]` tier weights into the ADDITIVE path
  (today they only bind under RRF — 9 documented knobs are dead in the
  default mode; inline literals at knowledge_store.py 1846/1886/1940/1979/
  2037/2084/2218/2281/2341).
- Citation shortener anchors on literal `Projects`/`sources` path segments
  (context_manager.py ~1527-1535) → strip configured ingest roots instead.
- Model-ID config knobs: `[ingestion] splade_model`, `[retrieval]
  dense_model`, sema model (currently hardwired naver/BAAI/MiniLM).
- Expose silent recall ceilings: SPLADE indexes only `content[:1000]`
  (storage/indexes.py), dense passage cap 2000 chars (bgem3_codec.py).
- Budget-tier + abstain constants (tier_logic.py 144-145, 221-239) were
  calibrated on owner-corpus probes and set per-turn token spend — expose
  as `[budget]`/`[abstain]` knobs (pairs with profiles work).
- Deny-list extensibility + non-English `locale/` demotion policy
  (knowledge_store.py 75-110).
- Small-model/MoE decoder table (context_manager.py 218-231) misses
  mistral/deepseek/granite — parse `:NNb` tags generically + override knob.
- De-Windows the tray edges (powershell-only installer spawn, .bat-only
  restart); repo hygiene: scripts/.ingest_progress, overnight_logs/,
  GEMINI.md, `[classifier]`/`[know]` doc-vs-code drift in CLAUDE.md.
