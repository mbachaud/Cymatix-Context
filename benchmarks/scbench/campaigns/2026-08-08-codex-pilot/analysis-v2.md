# SCBench paired regression analysis v2

Verdict: **no_detectable_difference**
Safety gates passed: **yes**

## Corrected regression estimand

- Median paired delta (Cymatix - control): 0.0
- Problem-clustered 95% interval: [-0.035172272354388856, 0.020547945205479423]
- Prior tests passed: control 4285/5908; Cymatix 4290/5908
- Head to head: {'cymatix_better': 24, 'control_better': 23, 'tied': 17}
- Strict-score head to head (all checkpoints): {'cymatix_better': 35, 'control_better': 28, 'tied': 17}

## Safety gates

| Gate | Status | Observed | Threshold |
|---|---|---:|---|
| The problem-clustered regression interval does not detect Cymatix harm. | pass | 0.020547945205479423 | upper >= 0 |
| Isolated solve rate is no worse than control by more than 2.5 percentage points. | pass | 0.015625 | >= -0.025 |
| The clustered erosion interval is not entirely above +0.03. | pass | -0.04656539593646253 | lower <= 0.03 |
| The clustered verbosity interval is not entirely above +0.03. | pass | -0.09845080475716539 | lower <= 0.03 |
| Median input tokens increase by no more than 25%. | pass | 1.0835296467480175 | <= 1.25 |
| Median checkpoint elapsed time increases by no more than 30%. | pass | 1.0281399323853833 | <= 1.30 |
| There are zero unresolved integrity or operational failures. | pass | 0 | == 0 |
