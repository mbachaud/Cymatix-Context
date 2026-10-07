# Compaction recall, stage 1: what survives a compaction, and can retrieval bring back the rest?

**Status:** stage 1 (retrieval-level, no model calls). Pilot scale: 19 compaction events.
**Harness:** `benchmarks/compaction_recall/` (tests: `tests/test_compaction_recall.py`).
**Motivation:** Schwartz, *Claude-shaped science* (Anthropic, Oct 2026) reports that long
agentic research sessions lost key details at compaction, and that the working fix was
having the agent consolidate files and keep a current plan. This bench asks the same
question on our own long sessions, with numbers.

## Question

When Claude Code compacts a session, the agent continues from a summary, a few preserved
messages, and re-attached files (recent reads, the plan file, invoked skills). Some details
from before the compaction are needed again afterwards. For each such detail:

1. Was it in what the agent was actually handed (`today`)?
2. Would a retriever over the pre-compaction history have handed it back, and is the
   shipped cymatix pipeline any better at that than plain BM25 or a dense retriever?
3. Does a handoff / plan document carry it?

## Method

**Events.** Every `compact_boundary` in `~/.claude/projects/*/*.jsonl`, deduped by boundary
uuid (resumed sessions copy history into new files; 26 boundary lines → 19 events). History
is reconstructed from the `parentUuid` tree, not file order: rewinds leave abandoned
branches in the file. After a partial compaction the continuation hangs off the preserved
tail, not off the boundary (`test_post_window_continues_from_the_preserved_tail`).
Thinking blocks are excluded everywhere.

**Needles.** A needle is a concrete detail that appears before the compaction *and that the
agent itself wrote again after it* (assistant text or a tool-call input) before the next
compaction. Re-use is the evidence the detail mattered. Classes: commit SHAs (keyed on the
7-char prefix), file paths (last two segments), `#NNN` refs, numbers with ≥3 significant
digits (`0.664`, `829k`, `947,000`; generic `0.05`/`0.80` excluded), and identifiers
(snake_case / UPPER_SNAKE with ≥3 parts, CamelCase with ≥3 humps; harness parameter names
excluded). Each needle records where the agent got it, when visible: a post-compaction tool
result (`refetched`), the user (`user_supplied`), or nothing visible (`unprompted`).

**Qualifier needles.** If the last pre-compaction assistant sentence mentioning the needle
carries a hedge word (pending, unverified, superseded, refuted, …), the needle also scores
whether the arm keeps a hedge word within 160 chars of it. This is the "one unproven lemma"
failure from the article, measured with a crude lexical proxy.

**Queries.** The retrieval query is the post-compaction text immediately around the first
re-use (≤600 chars of the preceding unit + the re-using line), with every variant of the
needle masked. It is a proxy for "what the agent was doing when it needed the detail", not
an agent-authored search query; stage 2 replaces it with model-written queries.

**Arms.**

| arm | what it delivers |
|---|---|
| `today` | summary + preserved messages + re-attached files/plan/skills (what Claude Code injected) |
| `summary` | the compaction summary alone |
| `plan` | the plan-file attachment alone (events that have one) |
| `handoff` | the last `*handoff*.md` written before the boundary (events that have one) |
| `bm25` | SQLite FTS5 BM25 over the pre-compaction history, top 12 chunks |
| `dense` | BGE-M3 (fp16, max_seq 512) cosine over the same chunks, top 12 |
| `cymatix` | shipped pipeline on a fresh store of the same chunks, `build_context(read_only, ignore_delivered)`, scored on the `expressed_context` it would inject |

All retrieval arms index identical chunks (≤1200 chars, line/whitespace split, exact
duplicates dropped). A hit is the needle's canonical key present in the delivered text.
`today ∪ X` asks what retrieval adds on top of what the agent already had.

## Results (run-01)

**Receipt:** `benchmarks/compaction_recall/receipts/run-01_2026-10-07.json` (per-needle rows
stay local in `F:/tmp/compaction-recall/run-01/`) · harness commit `30024c9a`
(0 behind `origin/master`; the receipt's `dirty: true` is this doc, untracked during the
run — `git diff HEAD -- benchmarks tests` was empty) · 14 transcripts, 19 events, **834
needles** (ident 349, path 326, number 83, ref 53, sha 23), needle-set sha256
`ffbbcb692cc0…` · cymatix config `./cymatix.toml` as loaded: rrf, rrf_k 20, eps_band ×5,
dense/SPLADE off, ingest encoders off, budget 7000 tokens, min_delivered 12 ·
`PYTHONHASHSEED=0` · Python 3.14.3 · k = 12 · 894 s.

| arm | hit | macro over events | median tokens | ∪ today | recovers today's misses |
|---|---|---|---|---|---|
| `today` (what the agent got) | **0.445** | 0.494 | 16,886 | — | — |
| `summary` alone | 0.325 | 0.364 | 4,236 | 0.445 | 0.000 |
| `plan` (n = 306, 8 events) | 0.180 | 0.111 | 7,154 | — | 0.000 |
| `handoff` (n = 21, 2 events) | 0.190 | 0.118 | 1,854 | — | 0.000 |
| `bm25` | **0.594** | 0.609 | 2,889 | **0.727** | **0.508** |
| `dense` (BGE-M3) | 0.448 | 0.477 | 2,648 | 0.649 | 0.367 |
| `cymatix` (shipped) | 0.518 | 0.524 | 3,617 | 0.699 | 0.458 |

Paired: cymatix vs BM25 **+55 / −118** (cymatix loses, net −63 needles, −7.6 pp);
cymatix vs dense **+134 / −76** (cymatix wins). In 98 of the 118 BM25-only needles cymatix
delivered its full 12 chunks, so the loss is ranking, not seat count.

By class (hit): numbers are where cymatix falls furthest behind BM25 (0.325 vs 0.506), then
refs (0.528 vs 0.736) and SHAs (0.348 vs 0.478); identifiers are closest (0.639 vs 0.696).

By origin: 473 needles were re-fetched from a tool result before the agent re-used them,
23 came from the user, 338 had no visible source after compaction. `today` held 36% of the
re-fetched ones: the agent went back to the files for them, which is what the article's
"consolidate files" fix makes routine.

Qualifier needles: n = 12. Too few to read; reported in the receipt only.

## What it says

1. **Compaction drops most of the details that get used again.** Only 44.5% of re-used
   details were in what Claude Code handed the agent, and the summary alone held 32.5%.
2. **A plan or handoff doc doesn't carry them either** (18–19%). That fits what the docs
   are for: they hold state and pointers (what's next, what was decided, where the
   receipts are), not the long tail of specifics. They're complements to retrieval, not
   substitutes.
3. **Retrieval over the raw history recovers about half of what compaction dropped.**
   today ∪ BM25 reaches 0.727.
4. **On this task the shipped cymatix pipeline is below plain BM25** (−7.6 pp, paired
   +55/−118) and above BGE-M3 dense (+134/−76). Same signature as the published-comparator
   summary (`2026-09-04-published-comparators.md`): BM25-class, with fused lanes displacing
   the top lexical hit on short lexical queries. The queries here are lexical-heavy proxies,
   which favours BM25. Stage 2 (model-written queries) is the fairer test of the extra
   lanes.

## Determinism (run-02 vs run-01)

Run-02 (`F:/tmp/compaction-recall/run-02-determinism`) rebuilt every store from scratch at
the same commit, same needle set (`ffbbcb692cc0…`), `PYTHONHASHSEED=0`, and diffed every
per-needle row (`python -m benchmarks.compaction_recall.compare_runs run-01 run-02`):

| arm | rows | identical | differing | hit flips |
|---|---|---|---|---|
| `bm25` | 834 | 834 | 0 | 0 |
| `cymatix` | 834 | **832** | **2** | **0** |
| `today` / `summary` | 834 | 834 | 0 | 0 |

Both cymatix diffs are in one event (`17b68b9c`, the longest incrementally-ingested chain):
the same needle verdict, but a different delivered size (1,601 vs 1,806 tokens; 3,387 vs
3,385). So **retrieval outcomes reproduced exactly, delivered bytes did not** when the store
was rebuilt by fresh ingest. This is narrower than the 2026-09-04 witness sweep, which
re-queried prebuilt beds bit-exactly. Unverified hypothesis: an ingest-time input that varies
with wall-clock time (created-at timestamps feeding the freshness gate or a tie-break) and
not with content. Root cause not chased in this pass.

## Limitations

- **Pilot n.** 19 events from one user's sessions (mostly this repo, plus a WoW addon, a
  .NET bot and an Electron app). Not a population claim.
- **Proxy queries.** Masked use-context, not what an agent would type into a search tool.
- **Lexical needles.** Exact-token details only; a decision or rationale that is paraphrased
  rather than restated is invisible to this stage. That biases toward lexical retrievers.
- **Re-use ≠ loss.** Many needles were re-fetched from files after compaction, which is the
  normal workflow, not a failure. The `by_origin` split separates these.
- **Hedges.** Qualifier needles are few and the hedge test is a word-proximity proxy.
- **Per-session haystack.** Each event searches only its own session's history, the easy
  case. A cross-session archive is a harder, more realistic stage-1b.

## Reproduce

```bash
python -m benchmarks.compaction_recall.run --dry-run
python -m benchmarks.compaction_recall.run --out-dir F:/tmp/compaction-recall/<run-id>
```

Per-needle rows quote transcript text and stay outside the repo; `receipt.json` carries only
aggregates and identity (commit, behind-count, transcript sha256s, needle-set sha256, the
cymatix config as loaded at runtime).
