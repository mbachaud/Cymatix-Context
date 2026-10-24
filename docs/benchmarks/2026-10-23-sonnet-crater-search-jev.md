# Sonnet 5.5 searches the ERB crater with Jev as the judge (EXPERIMENTAL)

2026-10-23. Follow-up to the one-shot answer A/B (PR #523). In that run, Sonnet
answered the 50 crater needles from the single delivered packet and got none
right: 0 correct, 3 partial, 7 incorrect, 40 NOT_FOUND. Crater needles are the
ones where c4000 classcap did not deliver gold. This run lets Sonnet search for
gold itself: rephrase, re-query cymatix, keyword-search the store, read
documents. Every step's tokens are counted. Jev judges every document cymatix
returns.

**Receipt:** `benchmarks/dogfood/jev/sonnet_crater_search_erb_c4000_2026-10-23.json`
(harness commit `ef089cf3`, clean tree, 0 behind master; every per-step
transcript is in the receipt). **Harness:**
`benchmarks/dogfood/jev/sonnet_crater_search.py`. **Inputs:** the 50 crater
needles of PR #523's sample, the Jev ERB c4000 classcap receipt
`jev_erb_packet_know_c4000_classcap_2026-10-23.json`, gold ids
`gold_by_needle_grain4000.json`, ERB `questions.jsonl` (`gold_answer` +
`answer_facts`), and the 947k c4000 bed, read-only.

## Setup

| | |
|---|---|
| Searcher | Claude Sonnet 5.5, effort high, multi-turn via `claude -p --resume`; each reply ends in one command |
| Commands | `SEARCH` (cymatix `build_context`, k=12, read-only, ignore_delivered), `GREP` (FTS5 BM25, any word, top 10), `ALL` (every word), `READ` (one document), `ANSWER`, `NOT_FOUND` |
| Budget | 15 commands; an unparseable reply is re-asked and does not count; 3 in a row end the loop |
| Jev | jev-1.14.0, no-incoming judge (PR #522): no cymatix ranks, packet order shuffled. Each SEARCH document gets `judge_p` against the **original** question, the packet gets `packet_p`; the 4 highest-`judge_p` documents are shown in full, the rest as one-liners |
| Turn 0 | harness-run SEARCH with the original question, i.e. the one-shot packet. Gold is absent from it by construction |
| Grader | Claude Opus 5.5, effort high, against ERB gold answer + answer facts |
| Transport | `claude -p` 2.1.308 in a clean room: no CLAUDE.md, memory, hooks, tools or MCP |
| Oracle only | gold gene ids are recorded per step (gold seen, gold shown in full), never shown to the model |

## Results

| | correct | partial | incorrect | NOT_FOUND |
|---|---:|---:|---:|---:|
| one-shot, same 50 (PR #523, cymatix arm) | 0 | 3 | 7 | 40 |
| **search loop** | **34** | **3** | **11** | **2** |
| — basic (25) | 18 | 1 | 6 | 0 |
| — semantic (25) | 16 | 2 | 5 | 2 |

**The crater is reachable by search.** Sonnet found gold on 35 of 50 needles
and answered 32 of those correctly. Of the 40 one-shot NOT_FOUNDs, 28 became
correct. Gold was first surfaced by a rephrased cymatix SEARCH on 25 needles
(12 on the very first rephrase) and by a keyword GREP/ALL on 10.

**The new failure is answering without gold.** Of the 15 needles where gold
never appeared, 11 got a confident answer from a sibling document (10
incorrect, 1 partial). Two were correct anyway (the answer is also in a
non-gold document), and only 2 ended in NOT_FOUND. One-shot Sonnet abstained
when the packet lacked gold; with a search loop it stops abstaining. Five of
the 11 answered within 2 commands, and one (erb_089) answered straight from the
turn-0 packet without searching.

| outcome | n |
|---|---:|
| gold seen, correct | 32 |
| gold seen, partial or incorrect | 3 |
| gold never seen, answered anyway (wrong) | 11 |
| gold never seen, correct anyway | 2 |
| gold never seen, NOT_FOUND | 2 |

**Steps and tokens before answering.**

| | n | commands, median | input tokens, median |
|---|---:|---:|---:|
| correct | 34 | 3 | 82.6k |
| partial | 3 | 5 | 116.1k |
| incorrect | 11 | 5 | 107.3k |
| NOT_FOUND | 2 | 15 | 606.7k |
| all | 50 | 4 (basic 3, semantic 5) | 99.8k |

- Command mix: 102 SEARCH, 112 READ, 46 GREP, 21 ALL, 7 unparseable
  re-asks. Four semantic needles used the full 15-command budget.
- Input side is almost all cache reads: 7.46M cache read, 1.54M cache write,
  680 uncached, over 50 needles. The median needle used 1.1k output tokens.
  One `READ` reply on erb_170 used 128k output tokens of hidden thinking, which
  is 67% of the run's 192k output.
- Spend (CLI list-price estimate): Sonnet $48.43, median $0.43 per needle, mean
  $0.97. Opus grader $0.71 for 48 calls. Jev: 1,383 calls, 3.61M input tokens.

**Jev as an answer gate: a usable signal, not a clean one.** Take the highest
score Jev gave anywhere in a needle's loop, and compare correct answers (34)
with wrong or partial ones (14).

| gate | AUROC | ≥ 0.7 keeps (correct / wrong) | ≥ 0.9 keeps |
|---|---:|---:|---:|
| max `judge_p` | 0.784 | 33 / 10 | 30 / 6 |
| max `packet_p` | 0.750 | 23 / 4 | 13 / 0 |

On 5 of the 10 incorrect answers where gold never appeared, Jev gave a sibling
document 0.9 or higher: the near-duplicates that fool Sonnet also fool Jev. The packet score is the
stricter gate. At 0.7 it cuts wrong answers from 14 to 4, at a cost of 11
correct ones.

## Caveats

- n = 50, a single run, a single grader with no inter-grader check. A 1–3
  needle difference is noise.
- The gate uses the max score seen anywhere in the loop, not the score of the
  document Sonnet actually answered from. It is computed after the fact; Sonnet
  saw the scores but no gate was applied.
- Turn-0 parity with the Jev receipt: 48/50 delivered the same document set
  (44/50 in the same order). The 2 set mismatches swap one non-gold document,
  and gold is absent from both sides.
- The run hit a `claude -p` session limit near the end. The 2 needles cut off
  mid-loop (erb_286, erb_296) were re-run from scratch, and grading was re-run
  after the limit reset. erb_296's first retry failed on a Jev HTTP 529
  overload and was re-run. Pre-rerun files are kept beside the run directory's
  originals as `*.pre-limit-rerun.bak`.
- `READ`, `GREP` and `ALL` see the whole store, unlike the one-shot packet. So
  this measures what an agent with store access can do, not what the
  retrieval pipeline delivers.
