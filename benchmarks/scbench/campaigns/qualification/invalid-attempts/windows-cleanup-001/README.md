# Invalid qualification attempt: Windows workspace cleanup

This non-scored control arm completed all four checkpoints and 139 total agent
steps. SCBench reported `$1.3548` in cumulative cost accounting, but then marked
the problem failed because Windows could not delete the Linux `.venv/lib64`
symlink in the temporary bind-mounted workspace. Consequently `run_info.yaml`
was not emitted and the Cymatix mate did not run.

The retained checkpoint results, receipts, rollouts, and logs are the evidence
of record. The persistent container now deletes `.venv` and `.venv-*` before
unmounting the workspace, with a focused cleanup-order regression test, in
SCBench fork commit `b85ccbd960b066aa64758b68f4261e8508f5cc1c`.
