# Cymatics ablation — the experimental flag does NOT clear (2026-07-29)

> ## ⚠️ CORRECTION 2026-08-08 — finding 1 is RETRACTED (arm C never ran)
>
> **Arm C was a no-op duplicate of arm A.** The harness varied the spread by
> setting `cfg.cymatics.peak_width`, but that config field **is never read**:
> the runtime derives its peak width from `[budget] splice_aggressiveness`
> (`context_manager.py:1228`, `aggressiveness_to_peak_width`) and passes
> `self._cymatics_peak_width` to the scoring call (`:2383`). Arm C therefore
> ran with the shipped spread of 3.0, identical to arm A.
>
> The table below is self-corroborating on this point: arm C reports
> **exactly** arm A's `recall@12 0.667 / MRR 0.446`, and "18/18 identical
> ranks" is the signature of an arm that did not vary anything — not an
> exact scientific result.
>
> **What is retracted:**
> - Finding 1 ("The Gaussian spread does nothing — exactly nothing"). The
>   experiment never collapsed the spread. The spread's contribution is
>   **unmeasured**, not zero.
> - "Cymatics has now been isolated against a hashed bag-of-words control
>   and is indistinguishable from it." That control **never ran**. The
>   README's open question is **still open**.
> - Path 1 and path 3 under "What clears the flag" — both are premised on
>   the retracted inertness result.
>
> **What still stands:** finding 2 (random-bin control, arm D) and finding 3
> (cymatics off, arm B) — both arms used knobs that *are* read
> (`cymatics.enabled` at `context_manager.py:1225`; arm D monkeypatches the
> module-level `term_to_frequency`, called at `cymatics.py:143/162/330/343`).
> The recall@12 structural argument stands. **The verdict — "the flag stays"
> — is unchanged, and is now better supported:** the isolation that was
> claimed to have happened did not.
>
> The harness is fixed as of the 2026-08-08 audit remediation
> (`benchmarks/dogfood/ablate_cymatics.py` now overrides
> `m._cymatics_peak_width` post-construction). **Arm C must be re-run before
> any spread claim is made.** Tracking: the cymatics-ablation issue filed
> 2026-08-08; the underlying dead-knob class is on #219.

The README flags cymatics in falsifiable terms:

> a candidate-reordering signal that has **not yet been isolated against hashed
> bag-of-words or random-bin controls** — treat it as an experimental cheap
> feature, not a proven one

~~It has now been isolated against exactly those two controls~~ It was run
against both controls on the dogfood bed (6,427 genes, 18 verified needles,
gene_id scoring), **but only the random-bin control actually varied anything**
— the hashed-BoW arm was inert (correction above). Harness:
`benchmarks/dogfood/ablate_cymatics.py`. Data:
`data/2026-07-29-cymatics-ablation.json`.

**Verdict: the flag stays.** ~~Two of the three findings are firm; the third is
suggestive and explicitly not claimed as established.~~ **As corrected
2026-08-08:** one finding is firm (2), one is suggestive and not claimed as
established (3), and one is **retracted** (1).

## Arms and results

| arm | recall@12 | MRR | ranks identical to A |
| --- | ---: | ---: | ---: |
| **A — shipped** (MD5 bins, Gaussian spread 3.0) | 0.667 | 0.446 | — |
| **B — cymatics off** | 0.667 | **0.484** | 11/18 |
| ~~**C — hashed bag-of-words** (spread collapsed)~~ **VOID — see correction** | 0.667 | 0.446 | **18/18** |
| **D — random bins**, seed 0 / 1 / 2 | 0.667 | 0.446 / 0.473 / 0.473 | 16/18, 17/18, 17/18 |

## ~~1. The Gaussian spread does nothing — exactly nothing~~ RETRACTED 2026-08-08

> **This finding is withdrawn.** Arm C never collapsed the spread — the knob it
> set is not read by the runtime (see the correction at the top of this file).
> The 18/18 identity is what a duplicate of arm A produces. The spread's
> contribution is **unmeasured**. The original text is kept below, struck, so
> the record of what was claimed stays legible.

~~Collapsing `peak_width` from 3.0 to ~0, so each term lands in a single bin with
no spread, produces **18 of 18 identical needle ranks**. Not "no significant
difference" — the same integer for every needle.~~

~~This is not a statistical claim, it is an exact one, and it is the direct answer
to the README's open question. **Cymatics has now been isolated against a hashed
bag-of-words control and is indistinguishable from it.** The 256-bin spectrum
with Gaussian superposition is, on this corpus, hashed BoW cosine with extra
arithmetic.~~

~~That does not make the feature wrong — hashed BoW cosine is a legitimate cheap
signal. It makes the *description* wrong wherever it implies the spectrum
contributes something the hashing does not.~~

## 2. Bin placement is arbitrary, as designed

Re-seeding the hash so bins are assigned differently but still consistently
between query and document changes 1–2 needle ranks, inside the spread across
seeds. So MD5's particular layout carries no information.

This is the **expected and correct** result — the README already says "nearby
bins are hash placement, not related meaning" — and the control confirms the
docs are honest on that point. It is reported as a pass, not a defect.

## 3. The stage is net slightly negative here — suggestive, not established

Turning cymatics **off** gives the highest MRR of any arm (0.484 vs 0.446) and
changes 7 of 18 needles:

| needle | cymatics on | off |
| --- | ---: | ---: |
| driftwatch_version | 3 | **1** |
| driftwatch_default_secret | 2 | **1** |
| mek_guardrails | 2 | **1** |
| scorerift_import_hygiene_confidence | 7 | **6** |
| cymatix_splice_aggressiveness | 29 | **28** |
| scorerift_divergence_threshold | **1** | 2 |
| scorerift_confidence_gate | **1** | 2 |

Five improve with it off, two get worse. **This is not a significance test.** At
n=18 an MRR delta of 0.038 is about four rank positions; that is well inside
what this bed can produce by chance. The honest statement is that cymatics shows
**no measurable benefit and a possible small cost**, not that it harms retrieval.

## Why recall@12 is identical everywhere — and why that is not evidence

Every arm scores recall@12 = 0.667, including cymatics off. That is **an
artifact of where the stage runs, not a finding.**

`blend.py:213` applies cymatics to `candidates` — the list *after* candidate
selection and truncation. It reorders within the delivered window; it can never
pull a gene in from rank 30. So recall@k is **structurally incapable** of
measuring this stage, and any past or future report using recall@k to justify
cymatics is measuring nothing.

MRR is the only metric in this harness that can move, which is why the verdict
rests on it — and why the verdict is correspondingly weak.

Instrumentation confirms the stage genuinely executes rather than silently
no-op'ing: 12 `flux_score_dispatch` calls per query, 9 non-zero bonuses,
mean 0.176 against base scores of ~4.15 (≈4%).

## What clears the flag, and what does not

**Not cleared**, on this evidence. ~~To clear it the feature would need to beat
the hashed-BoW control, and it is byte-identical to it.~~ **(Corrected
2026-08-08: the hashed-BoW control never ran — see the top of this file. The
feature is uncleared because it has not been isolated, not because it lost.)**

Three honest paths forward:

1. ~~**Correct the README** to describe what the measurement shows: it is hashed
   bag-of-words cosine over 256 bins, and the Gaussian spread is inert.~~
   **VOID (2026-08-08)** — premised on the retracted finding 1. The README's
   existing "not yet isolated" caveat is *accurate* and must stay. The real
   path-1 action is to **re-run arm C on the fixed harness**.
2. **Measure it where it can actually act.** Move the stage before candidate
   truncation, or evaluate it with a metric scoped to within-window ordering on
   a bed with many more needles. Its current position makes it nearly
   unmeasurable by construction.
3. ~~**Retire the spread.** If `peak_width` is provably inert, it is compute and a
   configuration knob buying nothing.~~ **VOID (2026-08-08)** — `peak_width` was
   never shown inert; it was never varied. Retiring the spread on this evidence
   would delete an unmeasured feature. (The *config knob* `[cymatics] peak_width`
   is separately dead — parsed but never read — which is the #219 dead-knob
   class, not a statement about the spread itself.)

## Caveats

- **One bed, n=18.** ~~Enough to establish the exact identity in finding 1,~~ not
  enough to condemn the feature on finding 3. (Finding 1 retracted 2026-08-08.)
- ~~**`peak_width=0.01`, not 0** — the spread is collapsed, not mathematically
  removed. Given 18/18 identical ranks the distinction is immaterial here.~~
  **Moot** — the value never reached the scorer at all.
- The bed is 88% one repository, and the corpus-frequency defect in #327 is
  live underneath all of these arms. It applies equally to every arm, so the
  *comparison* holds, but absolute numbers will move once that is fixed.
