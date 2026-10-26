# Sonnet 5.5 answers on ERB packets: does Jev help? (EXPERIMENTAL)

2026-10-23. Answer-level follow-up to
[the Jev System One packet judge](2026-10-23-jev-system-one-packet-judge.md),
which showed that Jev can tell whether a packet holds gold (AUROC 0.906) and can
rank gold first (MRR 0.921). That run never checked an answer. This one does.

**Receipt:** `benchmarks/dogfood/jev/sonnet_answer_ab_erb_c4000_2026-10-23.json`
(harness commit `3455571a`, clean tree, 0 behind master). **Harness:**
`benchmarks/dogfood/jev/sonnet_answer_ab.py`. **Inputs:** the Jev ERB c4000
classcap receipt and needle banks at `f1a23ae3`, ERB `questions.jsonl`
(`gold_answer` + `answer_facts`), and the 947k c4000 bed (read-only). Packet
text was checked character for character against what Jev saw. No retrieval
reruns and no new Jev calls.

## Setup

| | |
|---|---|
| Answerer | Claude Sonnet 5.5, effort high, one shot: `ANSWER: ...` or `NOT_FOUND` |
| Grader | Claude Opus 5.5, effort high, blind to arm and cohort, scores against ERB gold answer + answer facts |
| Transport | `claude -p` 2.1.308 in a clean room: no CLAUDE.md, memory, hooks, tools or MCP |
| crater (50) | semantic + basic needles where c4000 classcap did **not** deliver gold (25 each); seeded draw from 115, leaving out the 10-19 strat-40 set |
| ranked (50) | delivered needles where Jev's top chunk is gold; seeded draw from 252, proportional by ERB type |
| arm `cymatix` | the delivered documents in cymatix rank order |
| arm `jev` | the same documents, reordered by Jev's per-chunk probability, each tagged with it, plus Jev's packet probability |
| offline gate | cymatix-arm answers, abstaining when Jev's packet probability is below t |

## Results

| cohort | arm | correct | partial | incorrect | NOT_FOUND |
|---|---|---:|---:|---:|---:|
| crater | cymatix | 0 | 3 | 7 | 40 |
| crater | jev | 0 | 3 | 6 | 41 |
| ranked | cymatix | 37 | 10 | 0 | 3 |
| ranked | jev | 36 | 11 | 0 | 3 |

**Reordering and annotating with Jev does nothing measurable.** 96 of the 100
paired labels are identical. Of the 4 that changed, 2 went wrong → NOT_FOUND
(crater), 1 went NOT_FOUND → partial (crater) and 1 went correct → partial
(ranked). Order hardly matters to Sonnet on packets of at most 48k characters:
on ranked needles where cymatix put gold below rank 1, the cymatix arm was
already 14/17 correct.

**Jev helps as a gate.** Sonnet's own NOT_FOUND is the first gate: 40 of 50
abstentions on the crater. Jev catches part of what gets past it.

| Jev gate on the cymatix arm (all 100) | answered | correct | wrong |
|---|---:|---:|---:|
| none (Sonnet's own NOT_FOUND only) | 57 | 37 | 20 |
| packet_p ≥ 0.5 | 52 | 36 | 16 |
| packet_p ≥ 0.7 | 47 | 35 | 12 |

Among the 57 committed answers, Jev's packet probability separates right from
wrong at **AUROC 0.774**. Cymatix's own `top_score` reaches **0.535**. With the
same number of answers dropped (the 12 lowest), Jev keeps 34 correct and 11
wrong; `top_score` keeps 31 correct and 14 wrong.

**Crater: no lucky answers.** With gold absent, no answer was correct. The 10
wrong answers come from sibling documents: a different ADR, another service's
SLO, a nearby latency target. Jev was fooled on several of them (packet_p 0.94,
0.76, 0.76), so the gate stops some of these wrong answers, not all.

**Ranked: the failure is incompleteness, not error.** No answer was incorrect.
Every one of the 6 `project_related` needles came back partial: those are long
multi-part answers where Sonnet left out items. Three NOT_FOUND came despite
gold being delivered, and Jev scored all three low (0.12, 0.32, 0.08), which
suggests the gold chunk only partly holds the answer.

## Caveats

- n = 50 per cohort, a single run, a single grader with no inter-grader check.
  A 1–3 needle difference is noise.
- The ranked cohort was *selected* on Jev's top chunk being gold, so it is the
  easy end of the delivered set. 74% correct is not ERB answer accuracy.
- The packet is the delivered genes' full text (what Jev judged), not cymatix's
  token-budgeted `expressed_context`.
- Not comparable to the 10-19 crater rephrase run (8/40 correct, 18
  hallucinated). That run had a multi-search loop, forced a final answer, used
  a different needle set, and its `claude -p` calls carried the operator's
  CLAUDE.md, hooks and plugin tools.
- Spend (CLI list-price estimate): Sonnet $11.06 for 200 calls (2.64M input
  tokens), Opus $1.76 for 113 grader calls.
