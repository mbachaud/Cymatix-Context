# Issue #165 — path_key_index Storage Audit: Council Consensus

Date: 2026-06-09 · Probes run against the v2 Onyx corpus (`enterprise_rag_onyx_full_2`)
shards `linear.genome.db` (70,159 genes) and `slack__incidents.genome.db` (46,312 genes).
Probe scripts: `probe_pki.py`, `ablate_pki.py` (session outputs).

---

## 1. The Decision

**Question:** Should `path_key_index` (34.1% of the 47.24 GB v2 corpus) be slimmed,
restructured, or left alone?

**Options:**
- **A — Leave as-is.** Storage is cheap; the Tier-0 signal works.
- **B — Cheap slim (DDL + compaction, no read-path change):** drop the dead
  `idx_pki_lookup`, prune rows in pairs above `PKI_NOISE_CUTOFF`, convert the table
  to `WITHOUT ROWID`.
- **C — Normalize:** replace the materialized cartesian with two per-gene token
  tables (`gene_path_tokens`, `gene_kv_keys`), derive pairs at query time.
- **D — Full fingerprint redesign** per #159 (entities-weighted, IDF filtering,
  AND-then-OR routing).

**Stakes:** 16.1 GB on this corpus alone; ingest write amplification; wrong move =
a multi-hour fixture rebuild redone twice.

---

## 2. Empirical Probe Results (the evidence base)

| Probe | linear shard | slack__incidents | Meaning |
|---|---|---|---|
| Rows are exact `P×K` cartesian per gene | 100% sampled, **0 / 69,001 genes non-cartesian globally** | 100% sampled | The table stores **zero information** beyond two per-gene sets |
| Rows per gene | 123.5 (14.6 pt × 8.7 kk) | 113.3 (11.2 × 10.8) | ~19 KB/gene routing cost confirmed |
| Normalized rows would be | 23.2 | 22.0 | **5.4× row expansion** from materialization |
| Rows in pairs above `PKI_NOISE_CUTOFF=200` | **38.4%** | **38.1%** | Hard-skipped by the Tier-0 scorer — can never contribute score; pure dead weight |
| EXPLAIN QUERY PLAN, live lookup | `COVERING INDEX sqlite_autoindex_path_key_index_1` | same | **`idx_pki_lookup` is never used** (strict prefix of the 3-col PK) — ~3.3 GB / 7.0% of corpus is dead |
| EQP, upsert delete | `idx_pki_gene` | same | The only consumer of `idx_pki_gene` is the ingest delete path |
| Score-invariance ablation (40 realistic queries, replicated Tier-0 scoring incl. IDF + cutoff + cap) | prune: **40/40 identical** · normalize: **40/40 identical** (1e-9 tol) | — | Both B and C are **provably recall-invariant** on this corpus |

Top dead pairs are corpus-layout boilerplate: `("bench"|"enterpriserag"|"generated"|"sources"|"linear") × ("url"|"https")` — path tokens shared by *every* gene in the
fixture, crossed with near-universal kv keys.

### Savings model (on the issue's 16.0 GB breakdown)

| Move | Mechanism | Est. saved |
|---|---|---|
| Drop `idx_pki_lookup` | EQP-proven never used | −3.3 GB (7.0%) |
| Prune dead pairs (>200) | 38% of rows across remaining structures | −4.9 GB |
| `WITHOUT ROWID` | table + PK autoindex currently store every row **twice** | −2.0 GB further |
| **B total** | | **≈ −10 GB (~21% of corpus)** |
| **C total** (5.4× row cut, subsumes all of B) | | **≈ −12.5 GB (~27%), plus 5.4× ingest write-amp reduction** |

---

## 3. Personas

1. **Pragmatic Engineer** — ships working software, hates risky migrations
2. **Scale Architect** — storage growth, write amplification, 500K+ corpora
3. **Retrieval Scientist** — recall quality, #159 router-discrimination coordination
4. **Devil's Advocate** — hidden costs, challenges the premise

## 4. Independent Analysis

### Pragmatic Engineer — favors **B now**
- **Position:** B is DDL + an offline compaction pass. Zero read-path changes, zero
  recall risk (proven), shippable this week. C touches Tier-0 scoring — the
  highest-confidence retrieval tier — for storage you can mostly get with B.
- **Strongest argument:** the two biggest line items (`idx_pki_lookup`, dead pairs)
  are *literally unused* — deleting unused data is the safest optimization that exists.
- **Key concern with C:** a query-time join replaces one covering-index scan; the
  Tier-0 latency budget is tight on 100-shard fan-out.
- **Scores:** A:2 B:9 C:6 D:4
- **Would flip to C if:** the #159 redesign lands anyway and the read path is being
  rewritten regardless.

### Scale Architect — favors **C**
- **Position:** the cartesian is O(P×K) where O(P+K) carries identical information —
  that's a structural defect, not a tuning issue. At 850K genes it's ~105 GB of
  fingerprint index; B shrinks the defect, C removes it.
- **Strongest argument:** 5.4× ingest write amplification (123 inserts/gene vs 23)
  is also a *build-time* cost — fixture builds and live ingest both pay it on every
  upsert, forever.
- **Key concern with B:** pruning needs global pair cardinality, which is only
  knowable offline — so freshly ingested genes regrow dead rows until the next
  compaction. B decays.
- **Scores:** A:1 B:6 C:9 D:7
- **Would flip to B-only if:** Tier-0 join latency regresses >2× on the xl-sharded bench.

### Retrieval Scientist — favors **B now, C inside D**
- **Position:** scoring is invariant under both (proven), so the decision is purely
  cost/risk. But #159 already plans to rebuild this fingerprint (entities-weighted,
  IDF-filtered). Doing a standalone C migration, then redoing the schema for #159,
  is two multi-hour rebuild campaigns for one outcome.
- **Strongest argument:** the probe confirms #165's hypothesis — the index does
  inventory work, not pruning work; 38% of it can't even do inventory. Reclaim the
  free 21% now, spend the schema-change budget once, in the #159 redesign.
- **Key concern:** noise *path tokens* (`sources`, `generated`, `bench`) indicate
  `_PATH_NOISE_TOKENS` should learn corpus-relative roots — a small ingest fix with
  outsized effect (5 of the top 8 dead pairs).
- **Scores:** A:2 B:8 C:6 D:8
- **Would change mind if:** #159 is deprioritized for >1 quarter — then C stands alone.

### Devil's Advocate — challenges the premise
- **Position:** before spending any effort: this is *bench fixture* storage on a dev
  box, not production. The real product cost is per-user genome.db files, which are
  tiny. Is 16 GB on F:\ worth engineering time? …Conceded: yes, partially — because
  ingest write-amp (Scale's point) and the #176-adjacent multi-hour fixture builds
  are *time*, not just disk. But A deserves a fair score.
- **Strongest argument:** every option except A requires re-touching 47 GB of
  fixtures; the rebuild itself has historically been the riskiest operation in this
  repo (resume/pause PRs exist *because* builds fail).
- **Key concern with B:** `WITHOUT ROWID` on a table with a 3-col TEXT PK changes
  page-fill behavior; verify with a real shard before fleet-wide VACUUM.
- **Scores:** A:5 B:8 C:5 D:5
- **Would flip:** if compaction is implemented as a *new-file rewrite* (like
  `/admin/compact`), making it resumable and crash-safe, B's risk goes to ~zero.

## 5. Conflict Map

| Topic | Agrees | Disagrees | Confidence |
|---|---|---|---|
| `idx_pki_lookup` is dead → drop | All four | — | **High** (EQP-proven) |
| Dead-pair pruning is safe | All four | — | **High** (40/40 invariant) |
| Cartesian is a structural defect | Scale, Retrieval, Pragmatic | Devil's (says: only at fixture scale) | High |
| Do C as standalone migration now | Scale | Pragmatic, Retrieval, Devil's | **Low** — defer into #159 |
| Tier-0 join latency risk of C | Pragmatic, Devil's | Scale (claims negligible) | Medium — needs bench before C ships |
| B decays without recurring compaction | Scale, Devil's | — | High — make it an `/admin` op, not a one-off |

**Cross-validated risks:** (1) C's read-path latency unmeasured — flagged by 2;
(2) fixture-rebuild fragility — flagged by 2; (3) B's regrowth without periodic
compaction — flagged by 2.

**Blind spot:** no persona represents downstream agent consumers of `/context/packet`
fingerprints; assumed unaffected because Tier-0 output is invariant.

## 6. Consensus Recommendation

**Decision:** **Option B now** (drop `idx_pki_lookup` + dead-pair compaction +
`WITHOUT ROWID`), implemented as a resumable `/admin`-style compaction op. **Fold
Option C into the #159 fingerprint redesign** rather than running it as a standalone
migration. Bonus fix: extend `_PATH_NOISE_TOKENS` handling to corpus-root boilerplate
tokens at ingest (kills ~5 of the top 8 dead-pair families at the source).

**Confidence:** High
**Unanimous:** No — dissent from **Scale Architect**: normalization is the actual fix
and B leaves a decaying structure in place. Valid: B without recurring compaction
regresses on fresh ingest. Mitigated below.

### Why This Option
~21% of corpus storage back with *zero* read-path changes and probe-proven score
invariance, while the schema-change budget is spent once — inside the #159 redesign
that this audit's data (inventory-not-pruning) directly motivates.

### Mitigations for Top Concerns
1. **B decays (Scale)** → ship compaction as a repeatable `helix diag`/`/admin/compact-pki`
   op; the query-time `PKI_NOISE_CUTOFF` already guarantees regrown rows stay inert.
2. **C latency unknown (Pragmatic)** → before #159 implementation, bench the
   two-table join emulation (already written in `ablate_pki.py`) on the xl-sharded
   fixture; gate the redesign on p95.
3. **Rebuild fragility (Devil's)** → compaction = write-new-file + atomic swap,
   reusing the #183 resume machinery patterns.

### Reversibility
B is fully reversible: `idx_pki_lookup` can be recreated with one DDL statement;
pruned rows regrow from `genes.source_id` + `genes.key_values` (source of truth is
untouched). C-in-#159 is the expensive-to-reverse step — which is exactly why it
should ride the redesign, not precede it.

### Review Triggers
- Tier-0 `pki` tier-contribution rate or KV-harvest bench recall moves after compaction → halt rollout.
- #159 slips >1 quarter → revisit standalone C (Scale's position wins).
- A 500K+ build shows pki ingest time >15% of wall clock → pull C forward.

---

> This analysis simulates multiple specialist perspectives to surface risks
> and tradeoffs. It is not a substitute for input from actual domain experts
> on your team. The personas are heuristic models — real specialists may
> identify concerns not captured here. Use this as a structured starting
> point for team discussion, not as a final verdict.
