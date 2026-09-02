# Note on pool_depth_addendum_2026-09-01.json (added 2026-09-01, receipt left unmodified)

Two fields in the addendum receipt read as results and are not:

- `section_a_ceiling.meets_080_target: true` is a flag on an ORACLE ceiling
  (`oracle_ceiling_over_deep_pool` = (314 + 113) / 470 = 0.9085). It assumes a perfect selector
  over the 1000-deep admitted pool. The measured ranker moved gold's fused rank worse on 69 of the
  107 needles where gold is in both pools and better on none, and a rank-preserving depth-1000
  arm projects 0.634 [0.572, 0.661] delivered vs 0.668 shipped
  (`verify_a1_recompute_2026-09-01.json`). Read the field as
  `oracle_ceiling_exceeds_080`, never as "the target is met".
- `verdict: "PASS"` is carried over from the main ARM-1 receipt; the addendum has no kill of its own
  (its own `kill_criterion` says so).

Also on the record from the same verification: the latency curve in `section_c_latency_curve`
ran on 25 misses only, with no hit controls at depths 200/500/2000, so it measures admission and
serial cost, not delivery. Paired per-needle cost of depth 1000 on the 216 targets is +264 ms
median (+1.4%), +10% at p90, sign test p ~ 0; the handoff's "latency-neutral" is retracted.
