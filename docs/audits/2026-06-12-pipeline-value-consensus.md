# Pipeline Value Consensus — helix-context's Next Architectural Investment

**4-persona consensus council** · 2026-06-12 · repo: `github.com/mbachaud/helix-context` @ v0.7.1 (`pyproject.toml:7`), depth-1 clone at `/tmp/hxc`. All `file:line` citations verified in the clone. Session-telemetry figures supplied by the operator (829K-gene/100-shard fixture, 2026-06) are marked **[telemetry]**.

---

## 1. Decision Frame

**Question.** Where does the next unit of architectural investment go?

| Option | Statement |
|---|---|
| **A** | **Shrink the running system tax** — RSS, query latency, resident encoder duplication, GPU residency, tool/route surface, install weight. |
| **B** | **Finish the dead-code / default-honesty cleanup** — reconcile the code/toml/env/docs config layers, delete or bind dead knobs, kill silent truncations, make docs match the product. |
| **C** | **Dynamize the static knobs** — per-corpus auto-calibration (Layer 1), per-query classifier-owned adaptation (Layer 2), content-aware chunking, auto worker scaling. |
| **D** | **Re-found delivery around navigation** — fingerprint/packet pointers as the primary product instead of spliced-chunk injection. |

**Constraints.** Solo maintainer + agent fleet; one 12 GB-VRAM Windows rig (WDDM multi-CUDA-context livelock, #176); v0.7.1 just shipped after a #182–#200 merge train; ERB 500K rerun in flight (#93, #214 acceptance). The repo's own audits (`docs/audits/2026-06-09-next-steps-evidence.md`, `docs/audits/2026-06-10-test-tuning-roadmap.md`, `docs/specs/2026-06-09-retrieval-profiles.md`) say "the bottleneck is measurement, not triage."

**Stakes.** Positioning is in motion: pyproject already re-taglines the project as "Coordinate index layer for LLM context — Helix weighs, doesn't retrieve" (`pyproject.toml:7`) while the serving path still ships spliced chunk content. Investing in the wrong layer burns the only scarce resource (maintainer attention) and could entrench a delivery mode whose value at scale is currently *measured below a 1970s lexical baseline*.

---

## 2. Verified Evidence Base

The four user-question axes, grounded before any persona speaks.

### Axis 1 — Running size / system tax

| Fact | Source |
|---|---|
| 20.3 GB RSS for one serving process @ 829K genes/100 shards; ~1 min/query at `HELIX_SHARD_WORKERS=8`; **default is 1 → ~5 min/query** | **[telemetry]**; default-1 serial fan-out confirmed at `shard_router.py:67-81` ("Default ``1`` → serial fan-out … reference oracle for the determinism regression test") |
| A VRAM/CPU-aware `auto_shard_workers()` already exists (returns 3 on the 12 GB rig) but is wired only into ingest `--shard-workers`, not the serving fan-out | `parallel.py:51-78` |
| Encoder stack (SPLADE + BGE-M3 + sema + spaCy) is loaded **per process**: tray backend, bench server, and build workers each pay it; three processes = three CUDA contexts = the #176 livelock | `CHANGELOG.md` 0.6.5 #191 ("no longer loads SPLADE+BGE-M3 in parent + both spawn workers — three CUDA contexts = the #176 WDDM livelock"); 0.7.0 boots show "encoders warming up" |
| GPU 2.6 GB held at ~28% util during serving — the pipeline is CPU-bound while squatting on VRAM | **[telemetry]** |
| Dense recall costs ~870M FLOPs/query at scale; the two mitigation PRs (#158 parallel fan-out, #160 SPLADE prefilter) were **closed unmerged — "master has no Wall-2 lever"** | `docs/audits/2026-06-09-next-steps-evidence.md` item 8 |
| **MCP surface: 24 tools** (`mcp/mcp_server.py:341-963`), 10,381 chars of tool docstrings ≈ **2.6K tokens of descriptions; ~4–5K tokens of schema per MCP session** once JSON parameter scaffolding (57 params) is included — paid before the first query | AST count over `mcp/mcp_server.py`; the hand-rolled minimal fallback server exposes only **3** tools (`mcp/server.py:193-258`: context/ingest/stats) — a revealed-preference floor |
| **HTTP surface: 51 routes** — admin/debug/bridge/vault = 35 (routes_admin 30 + helpers 5), core retrieval = 5 (routes_context), ingest = 2, sessions/registry/hitl = 9. Agent-facing docs and benches demonstrably exercise ~6–8 of them (`/context`, `/context/packet`, `/fingerprint`, `/ingest`, `/stats`, `/health`, `/context/expand`) | `grep -c @app.` per `server/routes_*.py`; SNOW-2 arm table `docs/audits/2026-06-10-test-tuning-roadmap.md:28-45` |
| Install weight: core is lean (5 deps), but the documented feature path (SPLADE, dense, AST chunking, codec) requires extras whose "dep bulk [is] dominated by sentence-transformers + torch + spacy + tree-sitter + headroom-ai" | `pyproject.toml` dependencies + `[project.optional-dependencies]` incl. its own comments |
| Codebase: 47,146 LOC across 135 Python files in `helix_context/` | clone count |

### Axis 2 — Entropy: defaults drift, dead code, phantom docs

Defaults drift between code and shipped template — **every claimed drift verified**:

| Knob | `config.py` default | shipped `helix.toml` |
|---|---|---|
| `expression_tokens` | 6000 (`config.py:123`) | 7000 (`helix.toml:72`) |
| `max_genes_per_turn` | 8 (`config.py:124`) | 12 (`helix.toml:73`) |
| `splice_aggressiveness` | 0.5 (`config.py:126`) | 0.3 (`helix.toml:75`) |
| `decoder_mode` | "full" (`config.py:127`) | "condensed" (`helix.toml:76`) |
| `sr_enabled` | False — "Dark ship" (`config.py:284`) | true — "Stage-1 bench flip" (`helix.toml:268`) |

The profiles spec itself: "today these are **two undocumented products**" (`docs/specs/2026-06-09-retrieval-profiles.md`).

Further verified entropy:

- **Dark-shipped, measured-zero features:** `sr` / `seeded_edges` / `ray_trace` — "measured 0 — default off" per the profiles spec; `ray_trace_theta=False` (`config.py:291`, `helix.toml:275`), `seeded_edges_enabled=False` (`config.py:296`, `helix.toml:278`), yet `sr_enabled` is split across layers (above).
- **9 dead `[retrieval]` tier weights** (#202/#210): documented knobs bind only under RRF; the default additive path uses inline literals at `knowledge_store.py:1846/1886/1940/1979/2037/2084/2218/2281/2341` (`next-steps-evidence.md`, wave-2 bullet 1). Default `fusion_mode="additive"` (`config.py:377`, `helix.toml:349`).
- **ANN mis-calibration history** (#214/#217): threshold 0.58 "measured on ONE own-code fixture" after already recalibrating 0.35→0.58 between the project's *own* fixtures (#139, `knowledge_store.py:486`; profiles spec). #214's fix note: "a threshold that admits ZERO dense candidates is mis-calibration by definition" (`knowledge_store.py:383-397`). Per-corpus margin-over-random calibration is **shipped but not default** (`ann_threshold_mode="absolute"`, `config.py:343`; persisted reader `knowledge_store.py:602-609,1102`).
- **Silent truncations** (#207 family): SPLADE indexes only `content[:1000]` (`storage/indexes.py:367,378` — still present in clone); dense passages capped at 2000 chars (`bgem3_codec.py:34` `PASSAGE_CHAR_CAP`); compressor truncates at `content[:2000]` (`compressor.py:612`) and falls back to `content[:500]` (`compressor.py:587,738-778`).
- **Phantom docs knobs:** CLAUDE.md:92 documents `[know]` as "confidence_floor, margin_threshold" — the real keys are `emit_floor / s_ref / g_ref / betas[6] / stale_after_days` (`helix.toml:424-438`), and `config.py` contains **no KnowConfig at all**: `[know]` is parsed by a *separate, independent* TOML reader (`scoring/know_calibration.py:308-325`). CLAUDE.md also claims dense recall is "default off" — it is True in both layers (`config.py:323`, `helix.toml:308`) — and pins version 0.5.0 vs shipped 0.7.1. The repo's own audit names "`[classifier]`/`[know]` doc-vs-code drift in CLAUDE.md" as a hygiene item (`next-steps-evidence.md`, final bullet).
- **Net: knobs live in FIVE inconsistent layers** — dataclass defaults, shipped toml, env gates (`HELIX_SHARD_WORKERS`, `HELIX_SEMANTIC_ARM`, `HELIX_BFM_*`), docs, and side-channel TOML readers that bypass `config.py` entirely.
- The honesty fix that just landed (#214 dense-pool floor) moved ERB recall@10 **30.0% → 43.3% (strat-90)** **[telemetry]** — the largest single recall delta in months came from a *truthfulness* repair, not a feature.

### Axis 3 — Static that should (or should not) be dynamic

| Knob | Static today | Dynamic policy | Expected impact / risk |
|---|---|---|---|
| Shard fan-out workers | env default 1 (`shard_router.py:74-77`) | default to `auto_shard_workers()` (`parallel.py:51`), keep `=1` for the determinism test | **~5x query latency** at 100 shards **[telemetry]**; risk: ordering nondeterminism — already fenced by the serial reference oracle |
| Chunk size | 4000 chars ≈ 1000 tok (`fragments.py:66-67`); tree-sitter AST chunking exists but opt-in (`fragments.py:134-140`, `ast` extra) | content-aware/atomic chunks | Roadmap:157 is explicit: under the current OR-scorer **"smaller chunks lose"** (doubles dense FLOPs + FTS/PKI rows, recall already 28% @850K); they *win* only after AND-then-OR routing (#159). Dynamization is **sequenced behind routing**, not free |
| Dense weight | static per-process; w=4.0 evicts gold on ERB prose (−19pp vs dense-off, #138); semantic arm needs 16.0; fresh sweep 2026-06-10: ERB-50K 3 golds evicted ∀w≥2 | profiles spec **Layer 2**: classifier-owned 4.0↔16.0 per query, drop the `HELIX_SEMANTIC_ARM` env gate | high — dissolves the #138 conflict; risk: classifier misroutes prose/code boundary queries |
| ANN threshold | absolute 0.58 default (`config.py:334,343`) | flip `ann_threshold_mode="margin_over_random"` default-on; calibration persisted in genome (Stage-4 machinery shipped, `knowledge_store.py:493-498`) | prevents the #214 class ("absolute thresholds don't transfer" — profiles spec); risk: needs the cross-corpus transfer test (spec measurement 6) first |
| Expression budget | static 7000 cap | **keep static** — measured 8.4% utilization, "cap never binds" (#73, profiles spec); per-query-class caps already exist (Layer 2) | counter-example: not everything static is wrong; dynamizing a non-binding cap is zero-value work |
| PKI noise cutoff | `PKI_NOISE_CUTOFF=200` constant (`storage/indexes.py:119`) | generalize to IDF-style document-frequency ceiling (roadmap:157 — "#165's lesson is that the index was doing inventory, not pruning") | medium; pairs with AND-mode routing |
| SPLADE on/off | `splade_enabled=true` shipped (`helix.toml:201`) at 21.1% of disk for **0pp** recall @850K (#164) | size-aware auto-toggle knobs already shipped default-off (#189, `splade_auto_*`) | 9.96 GB back at 850K; thresholds await the #164 scale curve |
| Splice aggressiveness | static and *drifted* (0.5 vs 0.3) | per-query-class | low until compression health (denatured states, `context_manager.py:315,1330,2760`) is measured |

Profiles-spec verdict that disciplines all of Axis 3: **"only ~6 knobs are genuinely corpus-sensitive … most 'tuning' should be automatic"** — small Layer-3 profiles, Layer-1 auto-calibration, Layer-2 per-query adaptation.

### Axis 4 — Does the delivered chunk reduce downstream inference requirements at all?

**FOR injection:**
- Own-code/needle regime is strong at small scale: 83% @10K (→71% @50K, misses all `basic`-type, 14/17 monotone) (`next-steps-evidence.md` item 2); SIKE curated needles 10/10 retrieval across a 0.6B→Opus ladder (roadmap bench table).
- Dewey: `key+filename` 30% R@1 (vs 10% for 4-axis queries — extra axes *hurt* under additive fusion) (roadmap:153 + bench table); filename_anchor +24pp R@1 where queries name files (profiles spec).
- Session-delivery elision claims ~40% token saving multi-turn (CLAUDE.md) — but roadmap:139 explicitly lists the telemetry counter needed to "prove (or falsify)" it.
- know/miss contract gives calibrated trust (`scoring/know_decision.py`, Stage-6 spec) — a property a bare BM25 endpoint does not have.

**AGAINST injection:**
- Fresh 480-question ERB: **30.0% recall@10 pre-#214, 43.3% strat-90 after** **[telemetry]** vs **published BM25 68.4 recall / 68.8 correctness / Onyx+GPT-4 72.4** (#93, `next-steps-evidence.md:20`, roadmap:85). A plain lexical baseline currently more-than-doubles helix's chunk-surfacing at enterprise scale; recall is **28% @850K** (roadmap:157).
- SNOW LLM arm: qwen3:4b consuming the delivered tier cascade scored **miss_rate 0.9, triage_accuracy 0.0, answered@T0 0%, ~73,000× latency overhead** (12.3 s vs 0.2 ms oracle), N=10, arm abandoned (roadmap:20).
- Deliveries are small: 8.4% of the 7K budget ≈ **~590 tokens/query actually shipped** (profiles spec); compression health can read "denatured" (ellipticity < 0.3 — "context is unreliable", `context_manager.py:2760`), measured 0.5× on QA smoke **[telemetry]**.
- Navigation substrate already exists and is sound: "every gene already has a globally unique `gene_id` — **uniqueness is solved storage-side**" (roadmap:148); 850K census: 94.8% of chunks carry a globally-unique key **[telemetry]**; `/context/packet`, `/fingerprint` (with `profile: fast|balanced|quality`), `/context/expand` all ship today (`server/routes_context.py`, `routes_registry.py`).
- The roadmap's own thesis: "Helix's stretch capability is now *agentic navigation*, and nothing measures it … **an agentic arm is helix's only credible path above that line**" (roadmap:26) — and R@1 fails "not for lack of a unique key but because the query rarely *expresses* one and the scorer treats key matches as one additive vote among 12" (roadmap:153).

**The settling measurement exists on paper:** SNOW-2 five-arm design (#208; roadmap:28-45) — A injection-only `/context`, B packet-injection, C MCP-agentic (`helix_fingerprint`→`gene_get`/`packet`/`neighbors` loops), D CLI-agentic, E know/miss-driven escalation — scored as a **cost-of-correctness frontier** (correctness vs tokens vs wall time) on 10K/50K/500K fixtures, model ladder from qwen3:8b up to one Claude tier. All five surfaces exist; the arm runner, escalation module, and ERB adapter do not.

---

## 3. Independent Persona Analyses

*Each persona analyzed independently against all four axes. Disagreements are genuine and preserved.*

### 3.1 Systems Economist — "price every delivered token"

**Axis reads.**
1. *Tax:* One serving process = 20.3 GB RSS delivering ~590 useful tokens/query at 1–5 min/query — that is gigabyte-minutes per kilobyte of context, before the answering model runs. The encoder stack is paid 2–3× (tray + bench + workers) for a pipeline that is CPU-bound while holding 2.6 GB VRAM hostage on a 12 GB rig. An MCP host pays ~4–5K schema tokens/session for 24 tools — schema overhead alone ≈ 8 queries' worth of delivered content before the first call; the project's own minimal server ships 3 tools. 35 of 51 HTTP routes are admin/ops surface with no observed agent consumer.
2. *Entropy:* Drift is a *cost multiplier* — the worst latency default (`workers=1`) and the most expensive dead weight (SPLADE 21.1% disk for 0pp) are both **config-honesty failures with cost units attached**.
3. *Static→dynamic:* I only pay for dynamization with a cost line: auto worker scaling (5×) and SPLADE auto-toggle (~10 GB) qualify; classifier-owned dense weight is a recall play, not a cost play.
4. *Value:* If SNOW-2's frontier shows a ~150-token fingerprint matching a ~590-token splice at equal correctness, the entire compression stage is stranded capital.

**Position.** **A**, executed cheaply: flip `HELIX_SHARD_WORKERS` default to `auto_shard_workers()`, lazy-load/share encoders across processes, default SPLADE auto-toggle, prune the MCP schema to the demonstrated core (~8 tools) with the rest behind a `helix_admin` umbrella. Re-land #158/#160 only if 500K serving is a near-term product goal.

**Strongest argument.** The highest-ROI items in the whole backlog are one-liners: a default flip worth 5× latency and a lazy-load worth ~5–8 GB per duplicate process. No other option pays back in days.

**Key concern.** Optimizing the cost of a product whose value is unproven (Skeptic's point) risks gold-plating a cowpath — which is why my A is confined to reversible config-level wins, not Wall-2 engineering.

**Scores.** A **8**, B **7**, C **5**, D **6**.

**Flip conditions.** → D if SNOW-2 shows navigation ≥ injection at equal correctness (pointers are nearly free to serve); → C if the dense-weight sweep on real ERB questions shows per-query adaptation recovering >10pp (recall is revenue); A drops to 4 if lazy-loading + worker default land and RSS still exceeds ~10 GB (then the tax is structural, in the index design).

### 3.2 Pipeline Archaeologist — "entropy is the active failure mode"

**Axis reads.**
1. *Tax:* The tax is partly sedimentary: SPLADE-on by default at 0pp, the dead PKI index (#165: 34.1% of corpus, 38% dead rows, fixed in #193), three encoder copies — strata of unevicted decisions.
2. *Entropy:* Five config layers; five verified default drifts ("two undocumented products" — the spec's words); 9 dead documented knobs in the default path; `[know]` parsed by a shadow loader `config.py` doesn't know about; CLAUDE.md documenting knobs that don't exist, a version two majors stale, and a dense-recall default stated backwards; SPLADE silently indexing 25% of each chunk. **Every recent headline incident — #202/#210, #207, #214/#217 — is the same bug: the system not telling the truth about itself.**
3. *Static→dynamic:* Dynamizing a five-layer config multiplies the states an auditor must reason about. Calibration-at-ingest (Layer 1) is the *exception* I endorse: a computed value persisted in the genome is more honest than a shipped absolute.
4. *Value:* The 30→43.3 jump came from #214 — an honesty fix. Until the cleanup is done we do not actually know what injection scores; the BM25 gap is partly *measurement* of a misconfigured product.

**Position.** **B**, scoped and finite: reconcile the five drifts to one canonical default set; bind-or-delete the 9 dead weights; surface the `[:1000]`/`[:2000]`/`[:500]` truncations as config with loud logs; merge the `[know]` shadow loader into `config.py`; regenerate CLAUDE.md/config-reference from code; ship the two telemetry counters that verify the 40%-elision and know-floor claims (roadmap:136-139).

**Strongest argument.** B is the only option that raises the validity of every measurement the other three options depend on. Funding A, C, or D first means pricing, tuning, or re-founding a system whose declared behavior is provably not its actual behavior.

**Key concern.** D re-founds delivery on top of an unaudited base — SNOW-2 run against drifted defaults would compare five arms of a product that doesn't exist as configured. C (beyond Layer 1) multiplies entropy.

**Scores.** A **6**, B **9**, C **4**, D **5**.

**Flip conditions.** B is *finishable*: when the drift inventory is zero, dead knobs are bound or deleted, docs are generated, and the two truth-counters are live, B's score collapses to 3 and I hand off to the gate. If the ERB 500K rerun post-#214 still reads <50% with honest config, the gap is architectural, and I flip toward D.

### 3.3 Adaptive-Systems Architect — "the failures are static absolutes meeting new distributions"

**Axis reads.**
1. *Tax:* The 5-min default query is a static-knob failure (workers=1); auto-scaling already exists in `parallel.py` and is simply unwired. Most of A is C wearing a cost hat.
2. *Entropy:* Drift is what static knobs do over time — the durable fix is fewer hand-set absolutes, not better-synced ones. (Conceded: you can't calibrate against dirty signals; B's truncation and dead-knob fixes are upstream of me.)
3. *Static→dynamic:* The incident record is one-sided: 0.58 measured on one fixture, recalibrated twice between the project's own corpora; w=4.0 evicting gold on prose (−19pp) while the semantic arm needs 16.0; SPLADE flat-on costing 21.1% disk for 0pp. Layer-1 (calibration persisted in the genome — machinery shipped, default off at `config.py:343`) and Layer-2 (classifier-owned dense weight; the classifier already runs at Stage 0 for free) are the highest recall-per-effort levers on the board. Discipline: the budget cap (8.4% util) should *stay* static — dynamize only where evidence says values diverge.
4. *Value:* Chunk-vs-pointer is itself a per-query routing decision — locator-bearing queries want pointers (AND-route, #159: cost O(collision-set), independent of N), prose queries want synthesized content. D-as-religion is as wrong as injection-as-religion; the right end state is the classifier choosing the delivery mode.

**Position.** **C**, sequenced: Layer-1 auto-calibration default-on (after the cross-corpus transfer test, spec measurement 6) + Layer-2 semantic arm + auto workers; chunk-size dynamization explicitly *deferred behind* #159 AND-then-OR routing per roadmap:157 ("under the current OR-scorer, smaller chunks lose").

**Strongest argument.** Every off-corpus deployment will re-trigger the #214 class until thresholds travel with the corpus. Profiles spec already proved the search space is small ("only ~6 knobs are genuinely corpus-sensitive") — this is a bounded investment with a named acceptance bench per knob.

**Key concern.** Dynamizing against signals that are currently lies (truncated SPLADE docs, dead weights) builds feedback loops on noise — so B's core must land first; and D's re-found, done now, would freeze delivery just as the routing layer (#159) is about to change what's worth delivering.

**Scores.** A **5**, B **6**, C **8**, D **7**.

**Flip conditions.** If `calibrate_thresholds.py` on enterprise_rag_10k diffs ≈0 from the own-code 0.58/5.0/2.5 absolutes (spec measurement 6), corpus-sensitivity is overstated and C drops to 5. If SNOW-2 arm C/E beats arm A by a wide margin, I fold C into D's routing layer and call it the same program.

### 3.4 Inference-Value Skeptic — "show me one token that changed the answer"

**Axis reads.**
1. *Tax:* 20 GB and minutes of latency would be defensible if the product worked. The damning ratio isn't cost — it's cost *per delivered token that alters downstream output*, a quantity nobody has ever measured.
2. *Entropy:* The honesty program is what produced the indictment (dead weights, gated-to-zero dense, truncated index). Cheap, truth-increasing — fund it. But don't confuse cleaning the machine with proving the machine's output matters.
3. *Static→dynamic:* Tuning knobs on an unproven value chain is rearranging weights inside a number that's losing to `rank_bm25` by 25–38pp. The one dynamization I'd buy is the one that changes the *product*: route pointer-vs-content.
4. *Value — the core brief:* Retrieval-side, at enterprise scale, the 7-stage pipeline surfaces gold in the top-10 for 30–43% of questions where published BM25 does 68.4. Consumer-side, the only LLM-arm test ever run scored 90% miss at 73,000× latency. Budget-side, deliveries fill 8.4% of cap, and the compressor has a defined "denatured" state it actually emits (0.5× on QA smoke). Meanwhile 94.8% of chunks are pointer-addressable, uniqueness is "solved storage-side" (roadmap:148), the packet/fingerprint surface ships today, and the project's own tagline already concedes the thesis: "**Helix weighs, doesn't retrieve**" (`pyproject.toml:7`). Per delivered token, what fraction changes the downstream model's output? *Nobody knows. That is the scandal.*

**Position.** **D** — but the honest version: stop net-new investment in chunk polishing now (B-aligned), promote packet/fingerprint to the documented primary surface, and build SNOW-2's arm runner before any further pipeline tuning. The strongest pro-injection counters I must concede: 83% @10K own-code and Dewey 30% R@1 prove value *in the small-corpus, key-expressing regime*; SNOW v1's 90%-miss cuts **against navigation too** — it was the LLM *navigating* tiers that failed, so a weak local consumer may be even worse at following pointers than at reading pasted text; the 73,000× figure compares against an in-process string-matcher and is rhetorically inflated; and BM25 68.4 is a published number, not a same-harness rerun. Which is exactly why the settling measurement is SNOW-2 (#208): five arms, same questions, same fixture, correctness-per-token frontier, ladder starting at qwen3:8b.

**Strongest argument.** Every existing metric is retrieval-side (did gold appear?), not inference-side (did the answer improve per token spent?). A system whose stated purpose is "reduce downstream inference requirements" has never once measured downstream inference requirements.

**Key concern.** Sunk-cost gravity: stages 3–5 (re-rank/splice/assemble) are the identity of the codebase ("ribosome"), so the org will keep tuning them by default. Also my own risk: if capable models (qwen3:8b+, Claude tier) navigate well but the target audience runs 4B locals, D is a product for consumers helix doesn't have.

**Scores.** A **3**, B **7**, C **4**, D **7**.

**Flip conditions.** If SNOW-2 arm A/B (injection) beats arms C–E on the correctness-per-token frontier — or if the post-#214 honest-config ERB rerun closes to within ~10pp of BM25 — injection is vindicated; I drop D to 4 and concede the pipeline earns its tax. If qwen3:8b still can't triage fingerprints, D dies for the local-LLM market regardless.

---

## 4. Conflict Map, Cross-Validated Risks, Blind Spots

### Conflict map

| Topic | Agrees | Disagrees | Confidence |
|---|---|---|---|
| Default/doc honesty cleanup is the floor before anything else is trustworthy | All 4 | — | **High** |
| SNOW-2 (#208) five-arm frontier is the decisive measurement for the delivery question | All 4 | — | **High** |
| Config-level tax quick wins (workers default→auto, encoder lazy-load/share, SPLADE auto-toggle) ride with the next wave | Economist, Archaeologist, Architect | Skeptic (indifferent: "cheaper cowpath is still a cowpath") | **High** |
| Chunk-injection value at >100K genes is unproven-to-negative | Economist, Architect, Skeptic | Archaeologist (partial: gap may shrink under honest config; #214 alone was +13.3pp) | **Med-High** |
| Re-found delivery around navigation NOW | Skeptic | Archaeologist (re-founding on unaudited base), Economist (gate it on SNOW-2), Architect (routing layer #159 first; delivery mode should be classifier-chosen, not re-founded) | **Low — genuine conflict** |
| Dynamize knobs this wave | Architect (Layer 1+2 are shipped machinery, just default-off) | Archaeologist (multiplies audit states), Skeptic (tuning an unproven value chain) | **Medium — genuine conflict** |
| System tax is the urgent fire | Economist | Architect, Skeptic (symptom of index/delivery design, not the disease) | **Medium** |
| Budget cap should stay static (8.4% util) | All 4 (incl. Architect, citing the spec) | — | **High** |

### Cross-validated risks (flagged independently by 2+ personas)

1. **Measurement contamination** (Archaeologist, Skeptic, Architect): five config layers + drifted defaults + silent truncations mean existing benches measured a product that was never the shipped product. Any A/C/D decision made on pre-cleanup numbers inherits the error.
2. **The weak-consumer trap** (Economist, Archaeologist, Skeptic-as-self-risk): SNOW v1 is direct evidence that a 4B local model cannot navigate (90% miss, triage 0.0). D's premise is untested with capable consumers, and helix's positioning is *local* LLMs.
3. **Broken candidate surfacing poisons both delivery modes** (Architect, Skeptic): at 28% recall @850K, neither an injected chunk nor a pointer reaches the right document. The #159 AND-then-OR route is upstream of the A–D choice entirely.
4. **Flagship claims steering strategy without telemetry** (Economist, Archaeologist): the ~40% elision saving and the ~150-tok fingerprint economics are quoted in docs but explicitly unverified — roadmap:139 names the missing counter (`helix_session_tokens_saved_total`).
5. **Bench-fleet debt** (Economist, Archaeologist): ~40 benchmark scripts, many stale vs v0.7.x (roadmap §2 verdicts: RERUN/RETIRE) — the measurement instrument itself carries maintenance tax and staleness risk.

### Blind spots (perspectives not represented)

- **No third-party deployment or usage telemetry.** Which of the 51 routes / 24 tools real agents call is inferred from the owner's own sessions only.
- **Baseline comparability.** BM25 68.4/68.8 are *published* ERB-paper numbers, not same-harness reruns; the 25–38pp gap could shrink (or grow) under identical conditions.
- **Task-level outcomes are absent everywhere.** All current metrics stop at retrieval; SNOW-2 fixes this only if it actually runs with answer-correctness scoring.
- **The 73,000× latency figure** compares an LLM to an in-process string-matching oracle — directionally real, rhetorically inflated.
- **Panel gaps:** no security/multi-tenant perspective (51 routes incl. `/admin/shutdown`, `/admin/swap-db` on a localhost-trust model), no packaging/DX persona (extras complexity), no end-user (IDE/chat) persona.
- **Single-rig evidence:** all performance data comes from one Windows/WDDM 12 GB machine with known CUDA-context pathologies (#176).

---

## 5. Consensus Recommendation

**Decision:** **Option B now — a scoped, finishable default-honesty cleanup (1–2 weeks) — with the two config-level Option-A quick wins folded in, and SNOW-2 (#208) funded immediately after as the gate that decides D-vs-C.**

Concretely, in order:
1. **B-core:** reconcile the 5 verified config.py↔helix.toml drifts to one canonical set; bind-or-delete the 9 dead `[retrieval]` weights (`knowledge_store.py:1846-2341`); make the SPLADE `[:1000]` / dense `[:2000]` / complement `[:500]` truncations configurable and logged; fold the `[know]` shadow loader into `config.py`; regenerate CLAUDE.md/config-reference from code (fix `[know]` key names, dense-default polarity, version).
2. **A-lite riders:** `HELIX_SHARD_WORKERS` default → `auto_shard_workers()` (keep `=1` pinned in the determinism test); encoder lazy-load + shared residency across tray/bench/workers; enable SPLADE size auto-toggle once the #164 curve is run.
3. **Truth counters:** ship `helix_session_tokens_saved_total` + know-decision metrics (roadmap:136-139) so the 40%-elision and know-floor claims become measurements.
4. **Gate:** build SNOW-2's missing pieces (arm runner, arm-E escalation module, ERB adapter) and run the five-arm frontier at 10K/50K (500K when the fixture lands), ladder from qwen3:8b + one Claude tier, **on the post-cleanup config**.
5. **Branch on the gate:** arms C–E win → invest in **D** (navigation-first delivery, with #159 AND-then-OR routing as its core); arms A–B win or tie → invest in **C** Layer-1/Layer-2 (calibration default-on, classifier-owned dense weight) to close the BM25 gap on the injection path.

**Confidence:** **Medium-High** for B-first (highest mean score 7.25, no persona below 6, and it is the only option that raises the validity of every later measurement). **Low-Medium by design** on the D-vs-C branch — that uncertainty is exactly what the gate purchases.

**Unanimous:** **No.**
- *Skeptic dissent:* packet/fingerprint should be promoted to the documented primary surface *now* — the surfaces ship today and the tagline already says "weighs, doesn't retrieve"; waiting a measurement cycle is sunk-cost protection. Valid: the cost of dissent-compliance is low (docs + examples), and the council partially adopts it (see mitigation 5).
- *Architect partial dissent:* Layer-1 auto-calibration **is** threshold-honesty and belongs inside B, not behind the gate. Valid: the machinery is shipped (`ann_threshold_mode`, genome-persisted calibration); the council keeps it gated only on the cross-corpus transfer test (profiles spec measurement 6), which is hours, not weeks.

**Score matrix (1–10):**

| Option | Economist | Archaeologist | Architect | Skeptic | Mean |
|---|---|---|---|---|---|
| A — shrink tax | 8 | 6 | 5 | 3 | 5.50 |
| **B — honesty cleanup** | **7** | **9** | **6** | **7** | **7.25** |
| C — dynamize knobs | 5 | 4 | 8 | 4 | 5.25 |
| D — navigation re-found | 6 | 5 | 7 | 7 | 6.25 |

### Why this option
The last meaningful recall gain (+13.3pp, #214) was an honesty fix; the most expensive latency default (workers=1) and the most expensive dead feature (SPLADE 21.1% disk for 0pp) are honesty fixes with cost units; and the decisive D-vs-C measurement is worthless if run against a config that exists in five mutually contradicting layers. B is short, finishable, and converts every other option from argument into experiment.

### Mitigations for top concerns
1. *Measurement contamination (R1)* → SNOW-2 and all reruns execute only on post-B canonical config; bench harnesses pin the shipped helix.toml and log resolved config at RUN START (pattern already exists, #184).
2. *Weak-consumer trap (R2)* → SNOW-2 ladder starts at qwen3:8b and includes one Claude tier (roadmap:45); report per-model frontiers so "navigation works, but only above X B params" is a representable outcome.
3. *Broken surfacing poisons both modes (R3)* → spike the #159 AND-then-OR fingerprint route during the gate window; it benefits injection and navigation alike, and roadmap:157 already bounds R@1 as coverage × expressibility × tie-break.
4. *Unverified flagship claims (R4)* → truth counters (step 3) ship before any marketing of elision/fingerprint economics.
5. *Skeptic's promotion dissent* → document `/context/packet` + `/fingerprint` as the recommended agent integration in README/CLAUDE.md *as an experiment*, without deleting any injection path, pending the gate.

### Reversibility
- **B:** effectively irreversible but pure-win (deleting drift/dead code; all git-revertable). Lowest-regret option on the board.
- **A-lite riders:** one-line env/default flips behind existing tests — trivially reversible.
- **D and C:** explicitly *not yet committed* — the gate keeps both cheap to abandon. The expensive irreversible move (re-founding delivery, deprecating splice stages) is deferred until evidence exists.

### Review triggers
- **SNOW-2 results land** (any arm × model row): re-run this council's Section 5 branch decision — this is the primary trigger.
- **ERB 500K rerun post-#214** (in flight, #93): strat recall ≥55–60% under honest config would rehabilitate injection-at-scale and weaken D's premise; <45% confirms the architectural reading.
- **Dense-weight sweep on the real ERB question set** (roadmap bench table flags the auto-synth `--queries` gap): if per-query adaptation recovers >10pp, pull C Layer-2 forward.
- **Telemetry falsifies the ~40% elision claim** → revisit `session_delivery_enabled` default and the multi-turn value story.
- **Cross-corpus calibration transfer diff** (spec measurement 6) ≈ 0 → demote C Layer-1 urgency.
- **Post-lazy-load RSS still >10 GB @850K** → the tax is structural (index design), escalate A from config-fixes to architecture (re-open #158/#160).
- **A second real deployment appears** → its calibration/usage data supersedes most single-rig assumptions in this report.

---

## 6. Caveat

> This analysis simulates multiple specialist perspectives to surface risks and tradeoffs. It is not a substitute for input from actual domain experts on your team. The personas are heuristic models — real specialists may identify concerns not captured here. Use this as a structured starting point for team discussion, not as a final verdict.
