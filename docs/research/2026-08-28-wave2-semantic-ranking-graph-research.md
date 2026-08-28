# Wave 2 — semantic ranking graph without encoders (research synthesis)

**Date:** 2026-08-28 · **Charter:** user, 2026-08-28 ("dispatch some research into
adding a semantic ranking graph to see if we can lift semantic without
encoders") · **Phase parent:** `docs/superpowers/plans/2026-08-24-v09x-retrieval-refinement-phase.md`
· **Wave-1 record:** `docs/superpowers/plans/2026-08-27-ranking-under-width-wave1.md`

Produced by a 7-agent research fan-out (3 grounding, 4 external survey) plus an
adversarial skeptic pass that tried to kill every candidate lane against the
wave-1 facts before proposing compute. Constraint from the charter: **no neural
encoder inference anywhere in the lane** (BGE/SPLADE/cross-encoder all
excluded; the CPU spaCy tagger that already runs at ingest is in-bounds).

## 1. The crater, measured precisely (from the committed w1c receipts)

125/470 needles are type `semantic` (`erb_175..erb_299`; 62/235 in the half
set). Under the graduated combo config (`w1c_k20_eps_all_full`):

| category | n | mechanism |
|---|---|---|
| (a) delivered | 39 (0.312) | fine |
| (b) top-12, not delivered | 5 | gold at map rank 9–12 but `delivered_count` 2–8 — the token budget cuts above gold. **A delivery/budget fix, not a ranking lane** — cheapest delivered points on the board. |
| (c) stalled: in map, rank 13–45 | 20 | 100% band-rescue-reachable by construction — the shipped bm25 shortlist bounds the scored map at 50 docs (max any-gold rank over all 470 needles = 50 in both receipts). |
| (d) static: absent from map | 61 | shortlist deletes gold. At shortlist-off their gold sits at map rank min 104 / **median 49,310** / p75 109,230 / max 253,017 (n=27 half-set members); only 3 needles under rank 1000; **erb_186 and erb_192 are pool-absent even at off** — unreachable by any ranking lane. Addressable static cohort = 59. |

**The crater is bimodal with nothing in between.** No score-adjustment
mechanism lifts rank-10⁴–10⁵ gold; only a lane that *injects new documents
into the head* can touch (d). Any lane must be scored against which cohort it
addresses: (c) ceiling ≈ +0.16 semantic r@12 (+0.043 overall); (d) is where
the crater actually lives.

**What kind of paraphrase gap:** semantic questions systematically replace the
discriminative surface form with a lay description — (i) *entity-attribute
resolution* ("small legal document review firm … website contact form … late
Feb 2026" = the BriefWave Legal LLC CRM record; dominant in the stalled
cohort, which already half-matches lexically) and (ii) *concept/jargon
synonymy* ("low bit math … safest numeric mode" = precision annealing;
"turnaround … North America vs Europe/Asia-Pacific" = provisioning SLA
US/EMEA/APAC; dominant in the deep-static cohort). Universal confound: the
tokens that DO overlap (gpu, latency, inference, the corpus's own company
name) are corpus-ubiquitous — term-frequency mass flows to noise, which is
why every fusion shape froze at ~0.32 and why any new signal must be
banded/gated (wave-1 lesson 4 applies to graph edges too).

## 2. Substrate audit — what actually exists on the bed

The load-bearing discovery of the grounding pass:

- **Every query-learned graph is EMPTY on the bench bed.** `harmonic_links` =
  0 rows, `co_activated_with` = [] (receipt `storage_audit_blob.json`; the
  v09x builder pins `CYMATIX_BFM_SEED_EDGES=0`, and ladder rails run
  `read_only` + `CYMATIX_DISABLE_LEARN=1`). Consequently Tier 5 harmonic,
  SR (Tier 5.5), ray-trace/harmonic_bin, and `/context/expand`
  forward/backward are **structurally inert** in every benchmark ever run on
  these beds — and on any fresh bulk-ingested install.
- **`gene_relations` COVER edges are the one populated gene–gene substrate
  and have ZERO query-time consumers.** Written at ingest by
  `auto_link_by_entity` (`storage/co_activation.py:29-63`: ≥2 shared
  entities, top-10 peers, confidence = shared/len): **8,504,903 edges on the
  947k v09x bed** (measured this pre-flight), forward-indexed
  (`idx_gene_relations_rel_a`). Nothing reads them at query time
  (`expand_coactivated` reads only `typed_co_activated` — never written
  anywhere — and SYMBOL_REF=101 rows).
- **`entity_graph`** (bipartite entity→gene, ~10M rows, spaCy NER +
  CamelCase, ~12/gene) is populated; its reader (Tier 5b) is default-off and,
  as wired, is a flat +0.5 **unbanded** additive on in-pool candidates — a
  verbatim eps_band violation with a degenerate (alphabetical) RRF tier.
  Do not flip it as-is.
- **The SR walker is the right chassis, pointed at the wrong table.**
  `retrieval/sr.py` has a bulk adjacency cache, frontier cap (2000),
  discounting (γ=0.85, k=4) and a score cap, and Tier 5.5 CAN add new docs to
  the pool (`knowledge_store.py:3645`) — but it walks
  `co_activated_with`+`harmonic_links`, both empty. Retargeting `_fill_cache`
  at `gene_relations relation=5` is a small change that inherits a tested,
  latency-bounded walker.
- **The existing co-activation "pull-forward" is not a rescue lane:** appended
  neighbors are truncated by `result[:limit]` after the fused top-k
  (`knowledge_store.py:4048-4064`) — always full at 947k. A real
  append-below-head lane is new wiring.
- **Cymatics 256-bin spectrum scoring is a measured ranking no-op** (receipt
  `wave3d_cymatics_354_100k_carve.json`: shipped == off == bag-of-words ==
  two random-bin controls at r@12 0.6879; bins are MD5 hashes). Do not build
  a semantic signal on it.
- **The hand [synonyms] map measures mildly negative** (`synonyms_ab_100k_run1.json`:
  0.80 vs 0.8333 without, n=30 probe).

## 3. Encoder-receipt audit (wave-1 open question, resolved)

**Verdict: UNGATED — all 14 `enc_iso_*` receipts, unambiguously.** Every
2026-08-14/15 encoder-isolation receipt ran `rrf_gate_enabled=false` /
`rrf_gate_top_m=0` at `rrf_k=60` (committed `enc_all_off.toml` at all five
receipt SHAs has zero `rrf_gate*` keys; code defaults at those SHAs inert; no
receipt `knobs_changed` touches gate knobs). Scope correction for the record:
**"dense demotes gold" is a finding about ungated-RRF-k60 dense**; gated
dense is an unmeasured cell. This does NOT reopen the dense default (the
delivered-basis case stands, and the user has excluded encoders for wave 2) —
the transferable content is the **gating lesson generalizes to any deep-pool
signal, neural or not**, and wave-1b's engaged-gate arm adds the counterweight
that a gate only limits noise mass, it cannot rank width (0.345 vs 0.353 at
shortlist-off). Also: the enc_iso receipts are aggregate-only (no per-needle
records, pre-v09x untyped needle sets) — no per-type dense contribution
exists; dense's entire demonstrated upside was +0.019 pool recall (~9/469)
against −0.207 fr@12 / −0.141 delivered.

## 4. External evidence (compressed; full agent reports in the session archive)

- **The published template for cat (d) is GAR** (CIKM 2022): offline corpus
  graph (top-8 BM25-kNN neighbors per doc at 8.8M-passage scale), query-time
  frontier expansion of the scored pool from high-scoring docs — up to +8%
  nDCG and recall of docs the first stage never returned, scorer-agnostic.
  This is append-into-pool from head seeds, exactly the shape the crater
  needs.
- **The graph walk itself carries real weight in HippoRAG** (+10.9/+27.7 R@5
  from PPR vs identically-seeded no-walk), BUT its headline numbers use
  encoder query→node linking; HippoRAG 2's ablation prices the non-neural
  linking rung at **−12.5 R@5** — linking granularity is the whole game and
  encoder-free systems sit on its measured-worst rung. TERAG (string-match
  linking + PPR) retains ~80–100% of HippoRAG-family EM/F1 at 3–11% token
  cost — the existence proof that a mostly-lexical link + walk can work on
  entity-chain tasks. **No published system demonstrates paraphrase-gold lift
  via co-occurrence walks** — that transfer is the open bet.
- **Constrained propagation or nothing:** Salton & Buckley 1988 (unconstrained
  spreading activation loses to plain retrieval), Crestani's survey (all SA
  wins come from fan-out limits + decay), Peat & Willett 1991 (global
  co-occurrence expansion selects frequent low-discrimination terms). Diaz
  2005 score regularization is the principled "banded influence" formulation.
  Forward-push local PPR is O(1/(α·ε)) graph-size-independent — <100ms CPU at
  947k nodes with degree caps is realistic.
- **RM3/PRF is head-negative on our defining metrics** (TREC DL: RR and
  nDCG@1/@3 down while MAP/R@1000 rise) and its feedback set is gold-free by
  definition on every crater needle — guaranteed drift.
- **GraphRAG/LazyGraphRAG/neo4j:** GraphRAG publishes NO retrieval
  recall/precision (LLM-judged win rates only, >30-point position bias
  known); indexing 947k docs ≈ $12–45k or 35–110 days local. LazyGraphRAG's
  index is encoder-free (noun-phrase concepts + co-occurrence + Leiden ≈
  substrate-isomorphic to our existing entity_graph + COVER edges) but its
  QUERY path needs LLM subquery decomposition + embedding best-first ranking
  + an LLM relevance assessor — the banned parts carry the quality claims,
  and the implementation is not public. Neo4j: no published benchmark
  anywhere shows a graph ENGINE improving ranking quality over the same graph
  in a relational store — every delta is latency/ops, and 1–2-hop
  capped-frontier walks on indexed SQLite adjacency are already in the same
  latency class. **The graphrag/neo4j comparison the user asked for resolves
  to: there is nothing in those stacks, encoder-free, that our existing
  SQLite entity/COVER substrate cannot replicate for ranking purposes.**
- **Learned rankers:** Cormack SIGIR'09 — RRF meta-fusion beat every
  supervised learner on LETOR3 small-feature sets; DS@GT 2025 — learned
  rerankers mostly re-express first-stage signals; MSLR's top LTR features
  are PageRank variants, i.e. the lift comes from ORTHOGONAL (graph) feature
  families, which this bed does not have until a walk lane builds them.
  470 needles supports linear/convex models and shallow GBDT with
  group-k-fold by needle — not full LambdaMART.

## 5. Lane verdicts (adversarial pass; fatal facts recorded to prevent re-litigation)

| lane | verdict | deciding fact |
|---|---|---|
| 1. Query-time walk over COVER edges (SR chassis retargeted) | **SURVIVES-WITH-CONDITIONS** | Only lane with cat-(d) upside (GAR mechanism); substrate populated, zero ingest work; PPR latency safe. Conditions: degree-cap/hub guard; enters ONLY as append-below-head (new wiring — the existing pull-forward truncates) or eps-band-confined; MUST pass the reachability pre-flight (§6) before any bench compute. |
| 2. Co-occurrence/PMI term expansion (into [synonyms] or a tier) | **KILLED** | Entry mechanism for a solved entry problem (97% pool entry); Peat & Willett negative; the existing map already measures negative; the gap is compositional descriptions → proper nouns, unrepresentable in unigram/bigram PMI; unbanded pre-tier seam. |
| 3. PRF/RM3 through the synonyms seam | **KILLED** | Feedback set is gold-free by definition on every target needle (gold rank 13+/49k+) → guaranteed drift; RM3 is head-negative on head metrics; no winning cell even know-gated. |
| 4. LazyGraphRAG-style concept graph | **KILLED** (as a distinct lane) | Its query path needs LLM+embedding components (banned) that carry the quality claims; its encoder-free residue is substrate-isomorphic to entity_graph+COVER already on the bed → strictly dominated by lane 1 at higher build cost. |
| 5. Entity-graph anchoring (Tier 5b / query→entity entry) | **KILLED for (d)** — semantic queries replace the entity with a description; nothing to match. **SURVIVES narrow for (c)**: eps-band-confined entity-overlap discrimination inside the 50-doc map (NOT the shipped flat +0.5), gated on the offline discrimination audit (§6). Ceiling +0.16 semantic r@12, low confidence (wave-1 banded additives barely moved ranks ≥15). |
| 6. PLR / learned combiner with graph features | **KILLED standalone** (query-level confidence head cannot order docs; non-zero features = the nine-config-swept signal set frozen at 0.32; 470-needle circularity). **Conditional survivor** as the delivery vehicle for lane-1 features (shallow banded combiner, group-k-fold, half-set holdout) — strictly downstream of lane 1. |
| 7. Neo4j adoption | **KILLED** | No ranking-quality delta exists to buy; pure migration cost against the bed-file discipline. |

Adjacent free win outside all lanes: **cat (b)** — 5 semantic needles (plus
non-semantic analogues) lose delivered gold to the token budget at map rank
9–12. A delivery-order/budget fix is the cheapest delivered_gold on the
board; flag for wave-2 scope as its own small arm.

## 6. Pre-flight falsifiers (zero serve compute, read-only SQL — run before any arm)

**F1 — COVER reachability (gates lane 1).** For the 81 non-delivered semantic
needles under the combo config (20 stalled + 61 static, cohorts recomputed
from the committed receipt), seed a bidirectional degree-capped (>1000 never
expanded) BFS over `gene_relations relation=5` from proxy heads (raw FTS5
bm25 top-50 / top-12 — a superset of any fused head, since the scored map is
the shortlist survivor set) and ask: is any gold gene within 1–2 hops?
**Pre-registered kill rule (skeptic):** if <~30% of the static cohort's gold
is 2-hop-reachable, lane 1's ceiling collapses to cat (c) and it must beat
lane 5 on cost to proceed. Tool:
`benchmarks/dogfood/erb/w2_cover_reachability.py`.

**RESULT (2026-08-28, receipt `w2_cover_reachability_preflight_2026-08-28.json`):
LANE 1 PASSES ITS KILL GATE — decisively on reach, with a sharp
discrimination caveat from the frontier sizes.**

| cohort | seeds | ≤1 hop | ≤2 hops | rate 2-hop | median frontier 1-hop / 2-hop |
|---|---|---|---|---|---|
| static (61) | top-50 | 16 | 44 | **0.721** | 960 / 47,144 |
| static (61) | top-12 | 4 | 24 | 0.393 | 218 / 12,672 |
| stalled (20) | top-50 | 18 | 20 | 1.000 | 974 / 44,230 |
| stalled (20) | top-12 | 14 | 19 | 0.950 | 255 / 11,536 |
| budget_cut (5) | top-50 | 5 | 5 | 1.000 | 813 / 46,626 |

Reading: the pre-registered kill threshold (<30% static 2-hop reach) is
cleanly passed on both seed widths — the ingest-built COVER graph really
does connect the head to the crater's gold. But reach ≠ rescue: at 2 hops
the median frontier is ~47k candidates (gold is 1-in-47k by membership
alone), so the 2-hop tail pays off only if walk MASS (PPR-style, weighted
by edge confidence and damped through degree) concentrates on gold — that
is precisely what the W2.1 arm measures, with precision-at-append as the
metric. The near-term value is concentrated in the **1-hop band**: 16
static + 18 stalled gold sit one COVER hop from the top-50 head inside a
median frontier of only ~960 (255 from the top-12) — a
needle-in-a-thousand that confidence-weighted 1-hop spread has a real shot
at, worth up to ~+16 semantic delivered needles on its own (semantic
delivered 0.312 → ~0.44 if half convert). Registered expectation for W2.1:
most of any lift comes from 1-hop rescues; 2-hop rescues are the stretch
goal that decides whether the walk graduates from spread to PPR.

**F2 — entity-overlap discrimination (gates lane 5-narrow).** For the 20
stalled needles: score entity-overlap(query content words ∪ tags, doc
entities via `entity_graph`) for gold vs each doc outranking it. Kill rule:
if gold's overlap does not beat the median outranking distractor on >~half
the stalled needles, the feature is non-discriminative — dead with zero
compute. Requires the above-gold doc lists (not in the committed per_query
records) — needs one read-only serve replay of the 20 needles to capture the
map head, or a receipt-shape extension; defer to arm-prep if F1 funds a
serve-run window anyway.

## 7. Proposed wave-2 program (each step conditional on the previous)

1. **W2.0** — F1 pre-flight (done, results above) + F2 when a serve window
   opens.
2. **W2.1 `w2_cover_walk`** (if F1 passes): retarget the SR walker at COVER
   edges; walk from the fused head (top-M seeds); output enters as
   **append-below-head** (fix the `result[:limit]` truncation) and/or
   eps-band-confined contribution — never a free additive. One ladder arm,
   same 470-needle sequence, k=12, cold bed, paired per-needle vs
   `ladder_v09x_w1c_k20_eps_all_full` (the pairing reference). Registered
   prediction: static-cohort rescues bounded by F1's 2-hop reach rate;
   stalled-cohort lift bounded by the band.
3. **W2.2 entity-overlap in-band feature** (if F2 passes): eps-band-confined
   overlap bonus, small weight sweep, one arm, paired vs the same reference.
4. **W2.3 shallow banded combiner** (only if W2.1 features prove out): train
   on the 235-needle half set (group-k-fold by needle), evaluate held-out
   235; kill if held-out semantic delta ≤ 0.
5. **W2.4 budget-cut fix for cat (b)** (independent, cheap): delivery-order
   change so gold at map rank ≤ 12 survives the token cut; measured on the
   delivered basis where it is the entire effect.

Rules inherited: cold-bed house rule (≥1h since manager-mediated access);
paired tables share the combo receipt's needle sequence; rule 6 applies to
any default flip that falls out.

## 8. Bookkeeping notes

- Standardize the static-depth citation on **median 49,310 (n=27, committed
  `ladder_v09x_shortlist_off_2026-08-27.json`)**; the wave-1 plan's
  "median 31,714" is the 43-needle baseline-static slice of the same
  phenomenon measured on the half set — both say noise-floor, cite the
  committed-receipt number going forward.
- erb_186 / erb_192 are excluded from every lane's addressable ceiling
  (pool-absent at shortlist-off — recall misses, not ranking misses).
- Cross-source contradictions from the fan-out (PRF survey's own
  recommendation vs its own head-negative table; PLR-feature proposal vs the
  empty co-activation graph) are resolved in the verdicts above; the full
  skeptic report with all six contradictions is archived with the session.
