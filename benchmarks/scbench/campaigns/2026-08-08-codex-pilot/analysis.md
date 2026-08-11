# SCBench paired regression analysis

Graduated: **no**

## Paired regression cells

| Cell | Count |
|---|---:|
| Neither | 57 |
| Control only | 0 |
| Cymatix only | 2 |
| Both | 5 |

## Graduation gates

| Gate | Status | Observed | Threshold |
|---|---|---:|---|
| At least two net regression events are prevented by Cymatix. | fail | -2 | >= 2 |
| The relative reduction in regression rate is at least 15%. | fail | -0.24444444444444446 | >= 0.15 |
| Isolated solve rate is no worse than control by more than 2.5 percentage points. | pass | 0.015625 | >= -0.025 |
| Median paired erosion is no worse than control by more than 0.03. | fail | 0.03084718887326976 | <= 0.03 |
| Median paired verbosity is no worse than control by more than 0.03. | fail | 0.030608551156244473 | <= 0.03 |
| Median input tokens increase by no more than 25%. | pass | 1.0835296467480175 | <= 1.25 |
| Median checkpoint elapsed time increases by no more than 30%. | pass | 1.0281399323853833 | <= 1.30 |
| There are zero unresolved ingest, packet, version, receipt-integrity, or evaluation-contamination failures. | pass | 0 | == 0 |
