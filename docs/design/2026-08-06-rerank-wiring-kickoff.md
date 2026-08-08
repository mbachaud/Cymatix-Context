# Rerank production wiring kickoff — #341 (pre-cap top-50, decoupled, daemon-served)

Session prompt for picking up #341. Everything cited is on
`fork1/encoder-daemon` (PR #332) or master (#330/#331 merged 2026-08-06).
Evidence base: `docs/benchmarks/2026-08-06-retrieval-layer-ledger.md`,
`2026-08-04-rerank-and-fork1-inputs.md` §1, `docs/ROADMAP.md` Track 4.

## Prompt

Wire cross-encoder rerank into production per **#341**, in a NEW worktree
branched from `fork1/encoder-daemon` (or master if PR #332 has merged —
check first; the receipts tooling and encoder daemon it needs live there).

**Why (measured):** the 2026-08-04 probe is GO — ms-marco-MiniLM over the
fused top-50 at 829k: recall@12 0.433→0.500 (+6.7pp), 100% of pool
headroom captured, 0 losses, 219ms/50 pairs on GPU (1799ms CPU). Three
2026-08-06 findings converge on it: near-cutoff slips are the majority
recall-loss class at every scale (#340 separates them from static-miss);
the erb_039 synonym entry keeps gold in the pool at rank 29 — worthless
until pre-cap rerank converts pool presence to recall; and the ANN-gate
delivered-gold tension (#335) wants rerank deciding final order.

**Current wiring is wrong in three ways:** blend.py reranks only the
post-cap ~12 (useless as wired); enablement is coupled to the deberta
splice backend; `[hardware] rerank_device` (landed 51f3f4a) is declared
but never consumed — deberta_backend.py:65-66 uses the global device.

**Build:**
1. **Pre-cap rerank path**: rerank the fused top-50 BEFORE the
   `max_genes` cap. Find the real seam (post-fusion, pre-cap — the
   RRF/additive fuser output in knowledge_store.query_docs, not
   blend.py's capped list); keep the existing blend.py path for
   back-compat or delete it with receipts, don't leave both live.
2. **Decoupled enablement**: `[retrieval] rerank_enabled` (default
   false) + optional per-class map following the #255
   `rerank_combinator_by_class` pattern; no coupling to splice backend.
3. **Device**: consume `resolve_layer_device("rerank")` at model load.
4. **Daemon endpoint**: `/encode/rerank` (query, texts[] → scores[]) in
   `cymatix_context/encoder_daemon.py` — the cross-encoder was in the
   fork-1 design from day one ("and the cross-encoder when rerank
   lands"). RemoteCodec-style client branch with the existing circuit /
   shared ready-gate / in-process-fallback machinery in
   `backends/encoder_client.py`. Batch form; per-item timeout scaling
   exists — reuse it. Capacity self-report gains the rerank family.
5. **Delivered-gold metric (#335) rides along**: add it to the ladder
   receipts first (small), because it is the metric that shows rerank's
   delivery effect and settles the coact/ANN-gate questions for free.

**Receipts (bars deliberately below the probe's ceiling):**
- (a) 829k on/off A/B, `erb_829k_nosplade.db` (34.4GB — serve with
  `splade_enabled=false` or the default config re-creates empty splade
  tables), 30 blob needles, order-reversed pairs, `--per-query`:
  **accept ≥ +5pp recall@12 at ≤ +5% p50** (probe: +6.7pp at ~+1%).
- (b) 100k on/off A/B (the carve is sema-backfilled — fine): direction
  + cost; no recall bar (near-cutoff class is thinner at 100k).
- (c) dogfood latency receipt: document the cost honestly (219ms on a
  ~0.5s query is large in relative terms) — the DEFAULT stays off; the
  receipt informs per-class/per-size gating, not a global flip.
- (d) daemon capacity receipt incl. the rerank family (existing probe:
  `benchmarks/dogfood/erb/encoder_daemon_capacity.py`).
- Splice interaction check via the ring (`/debug/pipeline/recent`):
  pre-cap rerank must NOT change the candidate count entering splice
  (rerank@50 then cap to max_genes) — verify, don't assume; the ledger's
  claim-1 mechanism (25200/n splice cliff) is the reason.

**Discipline (non-negotiable, campaign-standard):** TDD, suite green
(~3,700 non-live); ratio gates only; order-reversed paired A/Bs;
`--per-query` receipts; ON arms require daemon-access-log proof of
`/encode/*` traffic (silent-fallback trap); update #341/#335/#340 and
`docs/ROADMAP.md` Track 4 as receipts land, and open the PR per repo
convention.

**Traps (each cost the last campaign a debugging pass):**
- `python script.py` puts the SCRIPT dir at sys.path[0] → cymatix_context
  resolves to the editable install (master), silently ignoring your
  worktree. Pin PYTHONPATH to the worktree for any out-of-tree client;
  heredoc/`-m` forms from the worktree cwd are safe.
- Git-Bash job PID ≠ Windows PID: kill bench processes by port owner
  (`Get-NetTCPConnection`), and NEVER by a bare 'uvicorn' regex — it
  matches the LIVE backend on 11437 (tray-supervised, it will respawn
  and confuse your cleanup). TaskStop does not kill detached bash trees;
  kill the spawner bash first.
- venvs: main `.venv` = CPU torch, DLLs locked by live MCP processes —
  never swap torch in place; CUDA venv at `f:/tmp/venv-cuda`
  (torch 2.11+cu128 + headroom). Verify optional-dep parity — headroom
  presence AUTO-ENABLES splice and its absence silently switches you to
  trim (2.5s/query difference at 100k).
- Raw `KnowledgeStore()` ctor defaults disable dense/splade vs config —
  go through `CymatixContextManager(load_config())` or pass knobs.
- The ablation ladder ALWAYS runs a baseline control arm; on a writable
  bed the shipped config re-creates empty splade tables — drop them or
  serve read-only.
- Encoder daemon: port 11439, spawn-token env
  `CYMATIX_ENCODER_SPAWN_TOKEN`, trust the token on /ready, not a 200.
- Beds: `f:/tmp/erb_carve/erb_829k_nosplade.db` (34.4GB, RAM-cacheable),
  `erb_100k.db` (6.4GB, sema-backfilled), needles
  `benchmarks/dogfood/erb/needles_resolved_{blob,carve}.json` + gold
  files alongside. Full-469 baseline for context:
  `full469_nosplade_c1.json` (r@12 0.676, p50 18.9s, per-needle).

**Deliverables:** pre-cap rerank + knobs + daemon endpoint + client seam
+ tests + the four receipts + delivered-gold in the ladder + a
`docs/benchmarks/` verdict vs the bars + issue/roadmap updates + PR.
