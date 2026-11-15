# PRD: Lexical rescue + merge-depth — recover literal/entity misses and deepen the candidate pool

- **Date:** 2026-05-30
- **Author:** Max (w/ Claude)
- **Status:** DRAFT — awaiting sign-off
- **Touches:** retrieval candidate assembly + ranking (sharded path), config/flags → PRD-first.
- **Constraint:** deterministic, LLM-free (ribosome stays off). No embedding change.
- **Lineage:** replaces the shelved P1' merge-reorder (NULL on labeled Onyx recall — reordering the pool we already have ≈ no lift). Per the 2026-05-30 council, the levers are getting *better/more candidates into the pool*, not reordering it.

---

## 1. Problem & what we learned

recall@200 diagnostic: gold is partly **retrieved-but-buried** (basic @10 24.6% → @200 60.6%) and partly **never-retrieved** (~78% of semantic gold absent from the top-200 = embedding-reach ceiling). The P1' experiment (reorder the cross-shard merge) came back **null-to-negative on labeled Onyx** — confirming that *reordering* the existing pool isn't the lever.

Two failure modes remain addressable **deterministically**, and they are distinct:
1. **Literal/proper-noun "needle" misses** (~17 true entity-lookup misses from prior bench memory): the gold is a literal term match that BM25/FTS finds easily, but dense+fusion **buries or never surfaces** it. Reordering can't fix it (it may not be in the returned pool at all); a **direct lexical injection** can.
2. **Pool-horizon misses**: a shard's gold ranks beyond `per_shard_fetch = 2·max_genes` *within that shard*, so it never reaches the merge at all.

## 2. Honest mechanism analysis (so we don't repeat the P1' mistake)

- **`lexical_rescue` is the PRIMARY recall@10 lever.** It runs BM25 (`genes_fts`) + promoter-index lookup and returns ranked **source_ids** for literal needles (`retrieval/lexical_rescue.py`, currently **dead code** — zero refs). Injecting those sources into the top-k **bypasses the dense/fusion ranking that buries them** — the only thing that actually moves recall@10 for the needle bucket.
- **Merge-depth (raise `per_shard_fetch`) is a SUPPORTING/enabling lever, NOT a standalone recall@10 win.** Raising it pulls more candidates into the merge pool → helps recall@**high-k** and is a *prerequisite* for rescue (gold must be in the pool to be promoted). But for gold already in the pool yet buried by ranking, more candidates do **not** lift recall@10 (they compete it *down*) — that's exactly the P1' lesson. So merge-depth is justified as "stop dropping gold below the per-shard horizon," not as a recall@10 driver by itself.
- **Out of scope:** the ~78% never-retrieved semantic tail is an **embedding-reach ceiling** — neither lever touches it. Honest framing: this PRD targets the needle bucket + the pool horizon, not the embedding ceiling.

## 3. Design

### 3a. Lexical rescue → sharded, injected into the ranked output (primary)
`lexical_rescue_sources(query, *, genome_path, limit, exclude_source_ids)` opens **one genome** and returns source_ids — it is **not** sharded-aware. Wiring:
1. **Sharded fan-out:** run `lexical_rescue_sources` per routed shard (reuse the existing fan-out + `_blas_limit`), union via the module's own `merge_source_ids` (dedupes by normalized path), cap at a small `HELIX_LEXICAL_RESCUE_K` (default e.g. 4).
2. **Map → genes + inject:** resolve rescued source_ids to their best gene per source (via `fingerprint_index` / per-shard lookup), inject into the `/fingerprint` ranked list **before the top-k cut**, at a bounded rank/score so rescue *fills gaps* without dominating (mirror the module's docstring intent: "after packet sources, before fetch"). **Open question for sign-off:** exact injection rank/score policy (append-then-cap vs interleave-at-fixed-rank vs score-floor) — I'll prototype the least-invasive (bounded append above the cut) first.
3. **Flag-gated:** `HELIX_LEXICAL_RESCUE=0` default → byte-identical; on → rescue active.

### 3b. Merge-depth knob (supporting)
`per_shard_fetch = 2·max_genes` (shard_router.py:441) → make the multiplier `HELIX_PER_SHARD_FETCH_MULT` (default **2** = byte-identical; try 4). Raises pool depth before the merge cut. Watch the latency cost (more per-shard candidates → bigger merge).

## 4. Measurement gate (NON-NEGOTIABLE — the P1' lesson)

Gate every flip on **labeled Onyx recall**, full-power (≥100/type, ideally 300q), recall@{1,10,50,200}, plus an XL sanity pass. **Falsifiable targets:**
- **lexical_rescue:** recovers **≥ ~8 of the 17** known entity-lookup misses at recall@10, with **no** recall@10/@50 regression on the rest. *Falsifier:* < 2/17 recovered → those misses are **semantic, not lexical** → rescue is the wrong tool, embedding is the only lever (kill it).
- **merge-depth:** lifts recall@50/@200 with **flat-or-up** recall@10. *Falsifier:* recall@10 drops (gold competed down) → revert the multiplier.
- A/B is one labeled run per flag (reuse the `recall_p1_compare.py` harness + the diagnostic baseline on the same IDs).

## 5. Risks
| risk | mitigation |
|---|---|
| BM25 rescue injects noise (false-positive literal matches) crowd the top-k | small `limit` (4), inject **below** genuine high-confidence hits, bounded score; measure @10 regression |
| the 17 misses are semantic not lexical → rescue is a no-op | the falsifier above kills it cheaply before more build |
| sharded fan-out of FTS adds latency | FTS is cheap vs the dense matmul that already dominates (~60s); negligible |
| merge-depth pushes gold *down* at @10 (the P1' failure mode) | default mult unchanged; gate @10 must not regress |
| `lexical_rescue` opens its own sqlite conn per shard | reuse the router's open shard handles, not fresh connections |

## 6. Sequencing & THE sign-off decisions
1. **Scope/order:** `lexical_rescue` first (the real recall@10 lever), measure, then merge-depth? Or both together? — *Recommend: lexical_rescue first (it's the recall@10 driver); merge-depth as a fast follow only if rescue needs deeper pool to find candidates.*
2. **Injection policy (3a.2):** OK to prototype "bounded append above the cut" first and let the labeled A/B pick the policy?
3. **Acknowledge scope:** the 78% never-retrieved semantic ceiling is explicitly **out of scope** (embedding work) — agreed?

→ Confirm scope + injection-prototype + the out-of-scope ack, and I implement `lexical_rescue` sharded wiring TDD-first off origin/master, gate on labeled Onyx recall before anything ships.
