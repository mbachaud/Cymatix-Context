# Opus 5.5 vs Sonnet 5.5 searching the ERB crater, Jev as judge (EXPERIMENTAL)

2026-10-25. Same experiment as
[2026-10-23-sonnet-crater-search-jev.md](2026-10-23-sonnet-crater-search-jev.md),
with Claude Opus 5.5 as the searcher in place of Sonnet 5.5. The question: does
a stronger model fix the search loop's main failure, answering from a sibling
document when gold never shows up?

**Receipt:** `benchmarks/dogfood/jev/opus_crater_search_erb_c4000_2026-10-25.json`
(harness commit `40721bb2`, clean tree, 0 behind master, all transcripts
included). **Harness:** `benchmarks/dogfood/jev/sonnet_crater_search.py
--answer-model claude-opus-5-5`. **Compared against:**
`sonnet_crater_search_erb_c4000_2026-10-23.json`.

## What changed, what did not

| | |
|---|---|
| Changed | searcher model only: `claude-opus-5-5` (all 345 model turns report Opus in `modelUsage`) |
| Same | 50 crater needles, system prompt, commands, 15-command budget, k=12, 947k c4000 bed and profile, effort high, Opus grader and grader prompt, clean-room `claude -p` 2.1.308 |
| Jev | jev-1.14.0, no-incoming judge. The run directory's Jev cache was seeded from the Sonnet run, so any (needle, document) or (needle, query) pair both runs touched got the identical score; 669 new Jev calls |
| Usage limit | not hit (0 waits) |

## Results

| | correct | partial | incorrect | NOT_FOUND |
|---|---:|---:|---:|---:|
| Sonnet 5.5 (10-23) | 34 | 3 | 11 | 2 |
| **Opus 5.5** | **33** | **3** | **11** | **3** |
| — basic (25), Sonnet / Opus | 18 / 18 | 1 / 1 | 6 / 5 | 0 / 1 |
| — semantic (25), Sonnet / Opus | 16 / 15 | 2 / 2 | 5 / 6 | 2 / 2 |

**No difference.** Opus found gold on 35 of 50, the same as Sonnet, and
answered 33 of those correctly.

**Different needles, though.** The label changed on 16 of 50 needles: 7 better
under Opus, 7 better under Sonnet, and 2 that swapped a wrong answer for
NOT_FOUND. That is a sign-test null. Gold was seen by both on 30 needles, by
only one model on 10 (5 each), and by neither on 10. Whether gold surfaces
depends more on which rephrasings a run happens to try than on the model.

| | n |
|---|---:|
| correct under both | 27 |
| correct under either | 40 |
| gold seen by either | 40 |
| gold seen by neither | 10 |

**The sibling-document failure persists, but Opus searches longer first.** Of
Opus's 14 wrong or partial answers, 13 came without gold ever appearing
(Sonnet: 11 of 14). Sonnet gave 5 of its 11 such answers within 2 commands;
Opus gave 2 of 13, and its median before a wrong answer was 13 commands. Two
Opus answers came straight from the turn-0 packet. On 8 needles both models
answered from a sibling without seeing gold.

**Steps and tokens.**

| | Sonnet | Opus |
|---|---:|---:|
| commands, median / mean / max | 4 / 5.6 / 15 | 4 / 5.9 / 14 |
| budget exhausted | 4 | 0 |
| unparseable re-asks | 7 | 0 |
| command mix (SEARCH / READ / GREP / ALL) | 102 / 112 / 46 / 21 | 91 / 115 / 50 / 39 |
| input tokens per needle, median | 99.8k | 78.1k |
| output tokens, total (max per needle) | 192k (128.6k) | 62.8k (3.7k) |
| correct: commands / input, median | 3 / 82.6k | 3 / 58.8k |
| incorrect: commands / input, median | 5 / 107.3k | 13 / 463.1k |
| model wall time, sum | 2,487 s | 2,304 s |
| CLI cost, total (median per needle) | $48.43 ($0.43) | $78.67 ($0.70) |

Opus reaches a correct answer on less context than Sonnet and had no runaway
hidden-thinking reply. It costs 62% more per run (CLI list-price estimate).

**Jev as an answer gate is weaker on Opus's answers.** Max score seen in the
loop, correct (33) vs wrong or partial (14):

| gate | AUROC Sonnet / Opus | Opus ≥ 0.7 keeps (correct / wrong) | Opus ≥ 0.9 keeps |
|---|---:|---:|---:|
| max `judge_p` | 0.784 / 0.721 | 32 / 12 | 28 / 8 |
| max `packet_p` | 0.750 / 0.640 | 19 / 6 | 13 / 1 |

Opus's wrong answers come after long searches, which see more documents and so
more high-scoring siblings. Both gates are post hoc, as in the Sonnet write-up.

## Reading

A stronger searcher does not move crater correctness at n=50: about two-thirds
correct either way. What separates correct from wrong is whether gold surfaced,
and that varies run to run. Gold surfaced in at least one of the two runs on 40
needles, and the answer was correct in at least one run on 40. So the obvious
next lever is more independent search attempts per question, with a way to
pick between their answers, not a larger model. That is untested here.

## Caveats

- n = 50, one run per model, one grader. The 16 changed labels show how large
  run-to-run variation is; part of the Sonnet/Opus difference on any single
  needle is that variation, not the model.
- Seeding the Jev cache makes shared (needle, document) scores identical
  across runs, which removes Jev's ~0.02 run-to-run jitter from the comparison
  but means Jev is not independently re-sampled.
- Turn-0 set parity with the Jev receipt is 50/50 here and was 48/50 in the
  Sonnet run (order parity 48/50 vs 44/50): cymatix's turn-0 packet varies
  slightly across processes. Gold is absent from turn 0 in every case.
- One grader reply (erb_277) put a sentence before `LABEL:` and failed the
  parse; that needle was re-graded with the same prompt and came back correct.
  `run.log` and `run2_grade.log` in the run directory keep both.
- `READ`, `GREP` and `ALL` see the whole store; this measures an agent with
  store access, not the retrieval pipeline alone.
