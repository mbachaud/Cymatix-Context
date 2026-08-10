# Invalid qualification attempt: Windows virtualenv snapshot

This non-scored control arm completed 22 Codex agent steps in checkpoint 1,
then SCBench failed while snapshotting Linux virtualenv symlinks through the
Windows bind mount. The checkpoint was operationally invalid, checkpoint 2 did
not begin, and the Cymatix mate did not run.

The retained receipts, rollout, and SCBench inference log are the evidence of
record. Named runtime environments such as `.venv-test` are excluded from
snapshots, with a focused regression test, in SCBench fork commit
`babf7bf5b3d3d6abed06fe0a501c68d0902d323e`.
