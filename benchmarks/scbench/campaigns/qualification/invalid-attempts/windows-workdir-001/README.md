# Invalid qualification attempt: Windows Docker workdir

This non-scored control arm was invalidated before any Codex tokens were used.
Docker rejected `\\workspace` as a container working directory, so only
checkpoint 1 emitted an error result and the Cymatix mate did not run.

The retained `pair.json`, arm receipt, stdout log, and SCBench inference result
are the evidence of record. The root cause was fixed and covered by Windows and
live-Docker tests in SCBench fork commit
`c9e38fad51240bbe50589af308a4865199a58bfa`.
