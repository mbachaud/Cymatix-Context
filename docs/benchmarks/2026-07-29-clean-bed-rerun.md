# Clean-bed re-run — one finding strengthens, one halves (2026-07-29)

> ## ⚠️ CORRECTION 2026-08-08 — arm C was inert here too; finding 1 RETRACTED
>
> This re-run used the same defective harness arm as the original verdict:
> it collapsed the spread by setting `cfg.cymatics.peak_width`, a config
> field the runtime **never reads** (peak width is derived from `[budget]
> splice_aggressiveness` at `context_manager.py:1228`). Arm C ran with the
> shipped spread 3.0 — a duplicate of arm A. The table shows it: arm C
> reports **exactly** arm A's `MRR 0.459 / median 3.5`.
>
> **Retracted:** finding 1 ("the Gaussian spread does exactly nothing —
> replicated") and every downstream phrase resting on it, including "its
> distinguishing mechanism is provably inert". The spread is **unmeasured**
> on this bed as well; the "replication" replicated a no-op.
>
> **Untouched:** findings 2 and 3, which are the substantive ones. Arms B
> (`cymatics.enabled`) and D (reseeded `term_to_frequency`) both used live
> knobs. Cymatics still **never helps a single needle on the clean bed and
> loses to its own random-bin control** — the case against the stage rests
> on those, and they stand unaltered.
>
> Harness fixed 2026-08-08; arm C must be re-run before any spread claim
> (#354). Full retraction:
> `docs/benchmarks/2026-07-29-cymatics-ablation-verdict.md`.

The dogfood bed was contaminated: it ingested its own needle harness (queries
*and* expected answers) and our bench write-ups, and `needles_dogfood.py` was
counted as gold for all 8 cymatix needles. Details and damage assessment in the
`fix(dogfood)` commit; no reported rank had been produced by a harness gene
(0/18), so nothing was invalid — but the bed was rebuilt clean regardless.

Both headline findings were re-run against the decontaminated bed. **They moved
in opposite directions**, which is the point of doing it.

| | contaminated | clean |
| --- | --- | --- |
| genes / files | 6,427 / 1,155 | **6,272 / 1,117** |
| identity | `eb2c7403176ce36c…` | **`13f8bbe3d3a37912…`** |
| gold files | 71 (2 were harness) | **61, all legitimate** |
| baseline recall@12 | 0.667 | 0.667 |
| baseline MRR | 0.446 | **0.459** |
| baseline median rank | 4.5 | **3.5** |

Removing the bench literature improved the cymatix ranks markedly (41→15,
31→18, 29→13) without moving recall@12 — those needles were deep either way.

## Cymatics: verdict strengthens, and hardens

| arm | MRR | median rank | ranks identical to shipped |
| --- | ---: | ---: | ---: |
| **A — shipped** (MD5 bins, spread 3.0) | 0.459 | 3.5 | — |
| **B — off** | **0.552** | **2.5** | 15/18 |
| ~~**C — hashed bag-of-words**~~ **VOID — inert arm, see correction** | 0.459 | 3.5 | **18/18** |
| **D — random bins** (3 seeds) | 0.487 | 3.5 | 17/18 each |

Three results, all firmer than on the contaminated bed
(**finding 1 retracted 2026-08-08 — see the correction above; 2 and 3 stand**):

1. ~~**The Gaussian spread does exactly nothing — replicated.** 18/18 identical
   ranks with the spread collapsed, on an independent corpus build. Cymatics is
   hashed bag-of-words cosine; the spectrum framing describes arithmetic that
   changes no output.~~ **RETRACTED** — the arm never collapsed the spread.
2. **It never helps a single needle.** Turning it off improves three
   (`driftwatch_version` 3→1, `driftwatch_default_secret` 2→1, `mek_guardrails`
   2→1) and improves nothing in the other direction. On the contaminated bed it
   at least won two; on the clean bed it wins **zero**.
3. **Its own random control beats it.** Random bin assignment scores MRR 0.487
   against the shipped 0.459 — identically across all three seeds.

So: never helps, demotes three, and loses to random placement. ~~and its
distinguishing mechanism is provably inert~~ (**retracted 2026-08-08** — the
mechanism is unmeasured, not inert). **The experimental flag does not clear**,
and there is still a defensible case for deprecating the stage rather than
merely relabelling it — that case now rests on the behavioural arms (never
helps, loses to random), which is the stronger ground anyway.

Standing caveat: it moves only 3 of 18 needles, so this is a small effect
measured on one bed. It is enough to refuse to clear a flag. Removal needs a
second bed.

## Corpus-frequency discipline: halves, and turns mixed

The df-filter arm — dropping query terms whose corpus document-frequency exceeds
20%, as a proxy for giving the flat tiers the IDF discipline BM25 already has:

| | contaminated | clean |
| --- | --- | --- |
| recall@12 | 0.667 → **0.778** (+11.1pp) | 0.667 → **0.722** (+5.5pp) |
| cymatix recall@12 | 0.250 → **0.500** | 0.250 → **0.375** |
| profile | **6 up, 0 down** | **3 up, 3 down** |

On the clean bed:

```text
improves:  fusion_default 33->17   genome_path 18->7   splice 13->5   max_genes 14->5
regresses: know_emit_floor 15->44  proxy_port 7->28    expression_tokens 6->13
```

**Half the reported effect was an artifact of the contaminated bed**, and the
profile is no longer one-sided. `know_emit_floor` regressing 15→44 is severe.

This does not refute the underlying diagnosis — the flat tiers genuinely lack
corpus-frequency discipline, and `tag_exact_weight` is genuinely a flat 3.0 on
tags covering 25–39% of the corpus. It refutes the **blunt instrument**. Hard
term-dropping discards signal along with noise: "cymatix" is uninformative for
`fusion_mode` but load-bearing for `proxy_port`, where the server config
genuinely is about cymatix.

**Scale, don't drop** was flagged as the right shape when the +11.1pp was
reported; the clean bed makes it a requirement rather than a refinement. The
proper fix multiplies each tier's contribution by term selectivity instead of
removing terms from the query, and it needs measuring on its own — the number to
beat is +5.5pp, not +11.1pp.

## What this says about the campaign

The contamination changed one verdict's magnitude by 2x and flipped another's
profile from one-sided to mixed. Neither was invalid, but both would have been
over-claimed.

For the [feature isolation campaign](../specs/2026-07-29-feature-isolation-campaign.md)
this is a concrete argument for a standing precondition: **every arm runs against
a bed with a recorded identity digest, and the digest goes in the receipt.**
`13f8bbe3d3a37912…` is the current one. Any result quoted against a different
digest is not comparable, and any bed whose digest is unrecorded cannot be
audited later.

It also argues for re-running the ladder's four ruled-out axes on the clean bed
before treating them as settled. The depth null is safe — it follows from
recall@50 = 1.000, which still holds. The dense-weight and rerank-combinator
nulls were measured under contamination and should be repeated.
