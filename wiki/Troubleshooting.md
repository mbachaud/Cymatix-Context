# Troubleshooting

The failure modes that recur across user reports and git history, condensed to
symptom → fix. Sections are in the same order as
[`docs/TROUBLESHOOTING.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/TROUBLESHOOTING.md),
which is ordered by observed frequency in the issue tracker and carries the
full **Symptom / Cause / Fix / Verify / Prevention** treatment for each entry.

Conventions: snippets are bash unless noted; on Windows use the PowerShell
equivalent (`$env:VAR=...`, `Get-Process`). The default proxy port is `11437`
and the default launcher UI port is `11438`; both are configurable under
`[server]` in `cymatix.toml`.

## Server will not start

### Port 11437 is already in use

- **Symptom.** `OSError: [Errno 98] Address already in use` (POSIX) /
  `[WinError 10048]` (Windows), or `SupervisorError: Port 127.0.0.1:11437 is
  already in use by a non-cymatix process.`
- **Fix.** Identify the listener (`lsof -nP -iTCP:11437 -sTCP:LISTEN`, or
  `Get-NetTCPConnection -LocalPort 11437`), kill it if it is a stray cymatix
  process, or move cymatix to another port via `[server] port` — then update
  every client (Continue `apiBase`, `CYMATIX_MCP_URL`, retriever callers).
  The launcher auto-adopts an orphan cymatix on the same port; bare `uvicorn`
  does not.

### Missing extras

- **Symptom.** Install succeeds but startup fails on
  `ImportError: No module named 'sentence_transformers' / 'spacy' /
  'tree_sitter'`, or `cannot import name 'CpuTagger'`.
- **Fix.** The base wheel ships only the proxy spine; encoders, taggers,
  launcher, and codec live behind extras. Install the set that matches your
  deployment — `pip install -e ".[embeddings,cpu]"` (lean),
  `".[embeddings,cpu,launcher,launcher-tray]"` (desktop), or `".[all]"` — then
  `python -m spacy download en_core_web_sm` and restart. Verify with
  `python -c "import cymatix_context; print(cymatix_context.CpuTagger)"`; a
  `None` means `spacy` or the pipeline is missing. Note the proxy itself wants
  `embeddings` for the SEMA cache even though the neural retrieval tiers are
  default-off, which is why the lean set includes it.

### `cymatix.toml` is malformed

- **Symptom.** `cymatix.toml is malformed (...) — using defaults` in the log,
  or `ConfigError: [abstain].mode='per_classifier' requires an
  [abstain.default] block`.
- **Fix.** Validate with
  `python -c "import tomllib; tomllib.load(open('cymatix.toml','rb'))"`. The
  usual causes: lists without brackets (`replicas = ["a", "b"]`), sub-tables
  interleaved with other sections instead of following their parent, and a
  deprecated `[compressor] device` (legacy: `[ribosome] device`) that should
  move to `[hardware]`. On the malformed path the loader silently falls back
  to defaults — port 11437, store path `genome.db` relative to cwd — which is
  rarely what you configured; the `[abstain]` case refuses to boot instead.

## Runtime and integration

### Ollama unreachable, wrong upstream, or model not pulled

- **Symptom.** `/health` shows `"upstream_reachable": false`;
  `/v1/chat/completions` returns 502 or `httpx.ConnectError`. With the
  compressor enabled the log adds `TranscriptionError: Ribosome model call
  failed entirely`.
- **Fix.** Confirm Ollama is up (`curl -s http://localhost:11434/api/tags`
  must return JSON), confirm the URL cymatix is actually using via `/health`,
  correct `[server] upstream` or `CYMATIX_SERVER_UPSTREAM`, and pull the
  models you configured. Note the retrieval path itself never calls the
  upstream — this affects the chat proxy and the optional compressor only.

### Knowledge store is locked

- **Symptom.** `sqlite3.OperationalError: database is locked` on ingest or
  `/context`, usually with two processes on the same store or a backfill
  script writing while the server is up.
- **Fix.** Stop all but one process pointing at the file, then checkpoint the
  WAL before touching it:

  ```bash
  curl -X POST "http://127.0.0.1:11437/admin/checkpoint?mode=TRUNCATE"
  sqlite3 genomes/main/genome.db "PRAGMA journal_mode; SELECT COUNT(*) FROM genes;"
  ```

  Expected: `wal` followed by an integer document count. One server per store —
  give a side deployment its own file with `CYMATIX_STORE_PATH` (legacy:
  `CYMATIX_GENOME_PATH`). Run long backfills against a snapshot copy and
  hot-swap during a maintenance window.

### A frontier model answers anyway when `/context` returns a miss

- **Symptom.** The response carries `miss { do_not_answer_from_genome: true }`
  and a `<cymatix:no_match reason="..."/>` token, and the model answers from
  its training prior regardless.
- **Fix.** The server-side envelope is enforced; the *prompt-level* contract
  is the caller's responsibility. Prepend
  `cymatix_context.agent_prompt.full_fragment()` to the agent's system prompt,
  register the escalation tools named in `escalate_to`, and make the agent
  loop tool-call-before-answer. Keep `recommendation = "refresh"` (the answer
  is here but stale) distinct from `"escalate"` (the answer is not here).
  See [Agent Contract](Agent-Contract).

### Dense recall returns nothing

- **Symptom.** Debug logs show `query_genes_dense_recall: empty matrix,
  falling back to lexical-only`, and needle-bench retrieval rate stays low.
- **Fix.** First check whether you meant to be running dense at all — it is
  **default-off since 2026-08-15** and ingest-time dense writes are off since
  2026-08-19, so this is expected behavior at shipped defaults. If you have
  deliberately set `[retrieval] dense_embedding_enabled = true`, the
  `embedding_dense_v2` column must be populated: stop cymatix, snapshot-copy
  the store, run `python scripts/backfill_bgem3_v2.py <copy>` until it reports
  `coverage=100.00%`, then hot-swap it in.

## Platform

### FTS5 missing on macOS

- **Symptom.** `sqlite3.OperationalError: no such module: fts5`, or
  "FTS5 not available — content search disabled" in the log, and the
  full-text tier silently drops out of the fusion ranker.
- **Fix.** Apple's bundled CPython links a stripped system SQLite. Install
  `brew install sqlite`, rebuild or reinstall Python against it (pyenv needs
  `LDFLAGS` / `CPPFLAGS` pointing at the brewed prefix), recreate the venv,
  reinstall. Never use `/usr/bin/python3` for cymatix.

### Python 3.14 wheel mismatches for torch / pystray

- **Symptom.** `Could not find a version that satisfies the requirement
  torch`, `Failed building wheel for pystray`, or an import-time segfault.
- **Fix.** Build a venv on 3.12 or 3.13 and reinstall. If you must stay on
  3.14, install only `".[embeddings,cpu]"` — the proxy and CPU encoders work,
  the tray and the DeBERTa backend do not.

### Tray icon missing on Linux or macOS

- **Symptom.** `--tray requires pystray + Pillow`, or the launcher starts and
  the dashboard works but no icon appears in the panel.
- **Fix.** `pystray` is LGPL-3 and ships only via the opt-in extra:
  `pip install "cymatix-context[launcher-tray]"`. On GNOME 3.26+ also install
  and enable `gnome-shell-extension-appindicator`, then log out and back in;
  Wayland-only sessions need a StatusNotifier bridge such as waybar. Verify
  with
  `python -c "from cymatix_context.launcher.tray import is_tray_available; print(is_tray_available())"`.

## Ingest and calibration

### spaCy NER model not downloaded — `/ingest` returns 422

- **Symptom.** Every `/ingest` POST returns HTTP 422 and nothing is ingested;
  the error body names the fix. On a contributor install the ingest-path tests
  skip with the same message.
- **Fix.** `python -m spacy download en_core_web_sm`, then restart — the
  tagger's pipeline cache is process-local. Re-send every document whose
  ingest failed; none of them were stored
  ([#313](https://github.com/mbachaud/Cymatix-Context/issues/313)).

### Calibration drift — `per_classifier` floors gone stale

- **Symptom.** `/context` abstains on queries it used to answer (or the
  reverse) and bench retrieval rates drift while no code has changed.
- **Fix.** The `[abstain.<cls>]` floors are fit empirically against a bench
  JSONL; a corpus that has grown or shifted topic mix invalidates them, and
  the loader accepts stale values without warning. Regenerate the located
  bench, re-run `scripts/calibrate_thresholds.py`, diff the new floors against
  the live ones (drift > 0.10 is suspicious), and paste them in. Falling back
  to `[abstain] mode = "global"` restores the pre-calibration behavior while
  you investigate. Recalibrate after any large ingest, shard split, compressor
  model change, or fusion-mode flip.

## Gotchas that look like bugs

- **"No relevant context" on a query you know is covered.** Retrieval matches
  query keywords against the tags assigned at ingest. Add mappings under
  `[synonyms]` in `cymatix.toml` before assuming retrieval is broken.
- **Store path.** The default is `genomes/main/genome.db`, relative to the run
  directory — not the project root. Delete the file to start fresh; it
  auto-creates on first use.
- **Fusion mode.** The default is `"rrf"` (since 2026-07-06; +12pp
  gold-document delivery over additive on the hardest internal bed, 0.74 vs
  0.62). `"additive"` remains as the legacy accumulator and is scheduled for
  condition-gated removal. Under RRF the abstain gates run ratio-only —
  the absolute floors were calibrated for additive.
- **Sharded corpora.** The sharded adapter trails the unsharded engine by
  ~31pp recall@10 on the xl bed (dense recall and co-activation are not yet at
  parity across shards,
  [#275](https://github.com/mbachaud/Cymatix-Context/issues/275)). Prefer
  unsharded for accuracy-sensitive corpora.
- **Session delivery elides repeats.** With
  `[budget] session_delivery_enabled = true`, documents already shipped to a
  session are not shipped again — which looks like missing context in a
  benchmark harness. Pass `ignore_delivered: true` in the `/context` body, or
  turn the flag off, when measuring. (The ~40% multi-turn saving is an
  **unverified design estimate** pending the
  `cymatix_session_tokens_saved_total` counter.)
- **`confidence` is not yet a trust signal.** The know/miss contract *shape*
  is stable and load-bearing, but the scalar is under active recalibration —
  at shipped defaults the KnowBlock does not fire
  ([#287](https://github.com/mbachaud/Cymatix-Context/issues/287),
  [#239](https://github.com/mbachaud/Cymatix-Context/issues/239)). Branch on
  `found` / `reason`.
- **Biology names in the code are not a different system.** `gene`, `genome`,
  and `ribosome` are the same things as document, knowledge store, and
  compressor. Both vocabularies work in code; wire fields and file paths keep
  the legacy spellings. See [Lexicon](Lexicon).

## Still stuck

If your symptom is not above and `/health` reports a non-zero document count
with `upstream_reachable = true`, open an issue at
<https://github.com/mbachaud/Cymatix-Context/issues> with `/health`, `/stats`,
the relevant log lines, and your `cymatix.toml` with secrets redacted. Or ask
on Discord: <https://discord.gg/pX7x7pA3Da>.

## Go deeper

- [`docs/TROUBLESHOOTING.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/TROUBLESHOOTING.md) — the full symptom / cause / fix / verify / prevention document
- [`docs/SETUP.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/SETUP.md) — prerequisites, extras matrix, and the silent-failure section
- [`docs/operator-runbooks.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/operator-runbooks.md) — backfill and calibration runbooks with rollback steps
- [`docs/config-reference.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/config-reference.md) — every setting, default, and env override
- Next: [Getting Started](Getting-Started) · [Configuration](Configuration) · [Observability](Observability) · [Agent Contract](Agent-Contract)
