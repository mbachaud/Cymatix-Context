# Troubleshooting

This guide collects the failure modes that recur across user reports
and git history for `cymatix-context`. Each section follows a fixed
shape: **Symptom**, **Cause**, **Fix**, **Verify**, **Prevention**.

Sections are ordered by observed frequency in the issue tracker. If
you only have a minute, scan the symptom headers first — most users
land on the right section in a single hop.

> Conventions used below: shell snippets are bash unless noted; on
> Windows use the equivalent `PowerShell` form (`$env:VAR=...` for
> env vars, `Get-Process` for `lsof`, etc.). Paths are written with
> forward slashes. The default proxy port is `11437`; the default
> launcher UI port is `11438`. Both are configurable in `cymatix.toml`
> under `[server]` and via `--port` on the launcher CLI.

---

## Server will not start: port 11437 is already in use

**Symptom.** `python -m uvicorn cymatix_context.server:app --port 11437`
exits with `OSError: [Errno 98] Address already in use` (POSIX) or
`OSError: [WinError 10048] Only one usage of each socket address ...`
(Windows). The launcher variant raises:

```
SupervisorError: Port 127.0.0.1:11437 is already in use by a
non-cymatix process. Free the port or change --cymatix-port.
```

**Cause.** Another process is already bound to `127.0.0.1:11437`. The
launcher's supervisor (`cymatix_context/launcher/supervisor.py:281-319`)
will quietly *adopt* an orphan cymatix on that port if it can identify
its command line, but a non-cymatix listener (an old Ollama instance, a
dev script, a different Python venv) can only be cleared manually.

**Fix.**

1. Identify the listener.

   - Linux / macOS:
     ```bash
     lsof -nP -iTCP:11437 -sTCP:LISTEN
     ```
   - Windows (PowerShell):
     ```powershell
     Get-NetTCPConnection -LocalPort 11437 | Select-Object OwningProcess
     Get-Process -Id <pid>
     ```

2. If it is a stray cymatix process from a prior session:
   ```bash
   kill <pid>          # POSIX
   taskkill /F /PID <pid>   # Windows
   ```
   Confirm the port frees up before retrying.

3. If you cannot free port 11437, change the cymatix port. Edit
   `cymatix.toml`:
   ```toml
   [server]
   port = 11537
   ```
   Then update every client (Continue config `apiBase`, the MCP
   server `CYMATIX_MCP_URL`, `cymatix_context.adapters.retriever` callers
   — see `cymatix_context/config.py:175-179` for the default and the
   list of touched modules).

4. Restart cymatix:
   ```bash
   python -m uvicorn cymatix_context.server:app --host 127.0.0.1 --port 11537
   ```

**Verify.** From a second shell:
```bash
curl -s http://127.0.0.1:11537/health | python -m json.tool
```
Expected: a JSON object with `"genome_genes": <int>` and
`"upstream_url": "http://localhost:11434"`. A `Connection refused`
means the server did not bind; re-check step 1.

**Prevention.** Use the launcher (`start-helix-tray.bat` /
`python -m cymatix_context.launcher.app`) instead of bare `uvicorn`. The
launcher auto-adopts an orphan cymatix on the same port instead of
crashing — see `cymatix_context/launcher/supervisor.py:288-312`. If you
do drive uvicorn directly, always Ctrl+C cleanly so the lifespan
shutdown at `cymatix_context/server.py:679-743` runs.

---

## Server will not start: missing extras

**Symptom.** `pip install -e .` succeeds but `python -m uvicorn
cymatix_context.server:app` fails on import with one of:

```
ImportError: No module named 'sentence_transformers'
ImportError: No module named 'spacy'
ImportError: No module named 'tree_sitter'
ImportError: cannot import name 'CpuTagger' from 'cymatix_context'
```

**Cause.** The base `cymatix-context` wheel ships only the proxy spine
(`fastapi`, `uvicorn`, `httpx`, `pydantic`, `filelock`). All
encoders, taggers, the launcher, and the codec live behind optional
extras declared in `pyproject.toml` lines 38-97. The package
`__init__.py:24-39` soft-imports `CpuTagger` and `SemaCodec` so the
import does not crash, but downstream features silently degrade. The
proxy itself, however, requires `embeddings` for the SEMA cache.

**Fix.**

1. Pick the extras that match your deployment:

   ```bash
   # Proxy + ingestion only (lean ~150 MB):
   pip install -e ".[embeddings,cpu]"

   # Proxy + launcher + tray (most desktops):
   pip install -e ".[embeddings,cpu,launcher,launcher-tray]"

   # Everything (~2 GB; pulls torch + headroom + tree-sitter):
   pip install -e ".[all]"
   ```

2. Download the spaCy model used by the CPU tagger
   (`cymatix_context/tagger.py:56-57`):
   ```bash
   python -m spacy download en_core_web_sm
   ```

3. Restart the server.

**Verify.**
```bash
python -c "import cymatix_context; print(cymatix_context.CpuTagger)"
```
Expected: `<class 'cymatix_context.tagger.CpuTagger'>`. A `None` means
the `spacy` extra is missing or `en_core_web_sm` is not installed.

**Prevention.** Use the one-click installers — `setup-helix.bat` on
Windows runs `deploy/windows/setup-helix.ps1`, which pins the
correct extras for the platform. For headless servers, pin the extras
in your project's lockfile so CI matches production.

---

## Server will not start: cymatix.toml is malformed

**Symptom.** Server logs:
```
cymatix.toml is malformed (Expected '=' after a key in a key/value pair (at line 47, column 12)) — using defaults
```
or, for the per-classifier abstain block:
```
ConfigError: [abstain].mode='per_classifier' requires an
[abstain.default] block (loader §6); none found.
```

**Cause.** The TOML loader at `cymatix_context/config.py:528-533` falls
back to `HelixConfig()` defaults on a `tomllib.TOMLDecodeError` and
emits a single `log.error` line. Defaults bind to port 11437 with
`genome.path = "genome.db"` (relative to cwd) and Ollama at
`http://localhost:11434` — which is rarely what an operator tuning
`cymatix.toml` expects. The Stage 4 `ConfigError` at
`cymatix_context/config.py:751-756` is harder: the loader raises and the
server refuses to start because the per-classifier abstain mode has
no default block to fall back to. See
`cymatix_context/exceptions.py:37-43`.

**Fix.**

1. Validate the TOML manually:
   ```bash
   python -c "import tomllib; tomllib.load(open('cymatix.toml','rb'))"
   ```
   Any traceback points at the first malformed line.

2. Common gotchas:

   - **Lists must use brackets.** `replicas = path1, path2` is
     invalid; use `replicas = ["path1", "path2"]`.
   - **Sub-tables follow their parent.** `[abstain.default]` must come
     after the last `[abstain]` table key, not interleaved with
     `[server]`.
   - **`[ribosome] device` is deprecated.** The loader at
     `cymatix_context/config.py:822-834` warns "deprecated" + "override"
     when both `[ribosome] device` and `[hardware] device` are set.
     Move the value to `[hardware]`.

3. If using `[abstain].mode = "per_classifier"`, add the required
   default block (see `cymatix.toml:459-463` for an example):
   ```toml
   [abstain.default]
   abstain_top = 0.40
   focused_top = 0.65
   tight_top = 1.10
   foveated_alpha = 1.0
   ```

4. Restart the server.

**Verify.**
```bash
python -c "from cymatix_context.config import load_config; \
  cfg = load_config(); \
  print(cfg.server.host, cfg.server.port, cfg.genome.path)"
```
Expected: the values from `cymatix.toml`, not the
`(127.0.0.1, 11437, genome.db)` defaults.

**Prevention.** Keep `cymatix.toml` under version control and run the
one-line validator above as a pre-commit check. The repo's
`cymatix.toml` doubles as a worked example with inline comments — diff
against it after every upgrade.

---

## Ollama is unreachable / wrong base_url / model not pulled

**Symptom.** `/health` returns `{"upstream_reachable": false, "detail":
"ConnectError: [Errno 111] Connection refused"}`. Chat requests
return `httpx.ConnectError` or HTTP 502 from `/v1/chat/completions`.
The server log shows:
```
TranscriptionError: Ribosome model call failed entirely
```
when the compressor is enabled (`cymatix_context/exceptions.py:29-30`).

**Cause.** The proxy's upstream probe at
`cymatix_context/server.py:492-532` tries `/api/tags`, `/v1/models`, and
`/health` in order; if all three fail it returns
`{"reachable": false, "detail": <last_error>}`. Any of three things
break it:

1. Ollama is not running.
2. `[server] upstream` (or the `CYMATIX_SERVER_UPSTREAM` env override at
   `config.py:616-617`) points to the wrong host/port.
3. The compressor model in `[ribosome] model` is configured but has not
   been pulled, so Ollama 404s on the first generation call.

**Fix.**

1. Confirm Ollama is up:
   ```bash
   curl -s http://localhost:11434/api/tags
   ```
   The response must be JSON. A connection-refused means Ollama is
   not running — start it with `ollama serve`.

2. Confirm the upstream URL cymatix is using:
   ```bash
   curl -s http://127.0.0.1:11437/health | python -c \
     "import sys, json; d=json.load(sys.stdin); \
      print(d['upstream_url'], d.get('upstream_reachable'))"
   ```

3. If the URL is wrong, fix `cymatix.toml`:
   ```toml
   [server]
   upstream = "http://localhost:11434"
   ```
   Or set the env override:
   ```bash
   export CYMATIX_SERVER_UPSTREAM="http://localhost:11434"
   ```

4. Pull the compressor model and the chat model:
   ```bash
   ollama pull gemma4:e2b      # ribosome (cymatix.toml [ribosome] model)
   ollama pull gemma4:e4b      # chat
   ```

5. Restart cymatix.

**Verify.**
```bash
curl -s http://127.0.0.1:11437/health
```
Expected: `"upstream_reachable": true` with a `probe` field naming the
endpoint that succeeded (typically `/api/tags`).

**Prevention.** Pin `keep_alive = "30m"` in `[ribosome]` (already the
default in `cymatix.toml:26`) so the model stays resident. If your
launcher routes the chat upstream through Headroom, note that
`cymatix_context/launcher/app.py:287-338` rewrites `CYMATIX_SERVER_UPSTREAM`
to the Headroom port automatically; check `/health` after toggling
`[headroom] enabled` to confirm the URL you expect.

---

## Tray icon is missing on Linux or macOS

**Symptom.** `start-helix-tray.bat` (or the equivalent `python -m
cymatix_context.launcher.app --tray` invocation) exits with:
```
--tray requires pystray + Pillow. Install with:
pip install cymatix-context[launcher-tray]
```
or, on Linux specifically: the launcher starts, the dashboard works,
but no system-tray icon appears in the panel.

**Cause.** Two layered reasons:

1. **License-driven exclusion.** `pystray` is LGPL-3, so the
   `cymatix-context` core wheel does not bundle it (see
   `cymatix_context/launcher/tray.py:19-25`). It only installs when you
   opt in via the `launcher-tray` extra, declared in
   `pyproject.toml:70-77`. The fail-fast at
   `cymatix_context/launcher/app.py:530-535` triggers when `pystray` or
   `Pillow` is missing.

2. **Linux desktop compatibility.** `pystray` uses `AppIndicator3` on
   GNOME and `Xlib` elsewhere. GNOME 3.26+ removed the legacy
   StatusNotifier protocol — without the
   `gnome-shell-extension-appindicator` extension installed, the icon
   exists but never renders. Wayland-only sessions on Sway / Hyprland
   need a third-party tray (`waybar`, `swaync`) bridging
   StatusNotifier.

**Fix.**

1. Install the extra:
   ```bash
   pip install "cymatix-context[launcher-tray]"
   ```

2. On GNOME (Ubuntu 22.04+, Fedora 38+):
   ```bash
   sudo apt install gnome-shell-extension-appindicator   # Debian/Ubuntu
   sudo dnf install gnome-shell-extension-appindicator    # Fedora
   gnome-extensions enable ubuntu-appindicators@ubuntu.com
   ```
   Log out and back in.

3. On macOS, no extra system packages are needed; the icon lives in
   the menu bar. If it does not appear, check
   `~/.helix/launcher/launcher.log` for `pystray` import errors —
   typically a Pillow ABI mismatch on Python 3.14 (see the wheel
   mismatch section below).

4. Re-run with the tray flag:
   ```bash
   python -m cymatix_context.launcher.app --tray
   ```

**Verify.**
```bash
python -c "from cymatix_context.launcher.tray import is_tray_available; \
  print(is_tray_available())"
```
Expected: `True`. A `False` means `pystray` or `PIL` is still not
importable in the active Python environment.

**Prevention.** If the tray is core to your workflow, document the
GNOME extension prerequisite alongside the install instructions.
Headless servers do not need the tray — drop `--tray` and run the
launcher in dashboard-only mode.

---

## genome.db is locked

**Symptom.** Ingestion or `/context` requests fail with:
```
sqlite3.OperationalError: database is locked
```
Often correlated with multiple cymatix processes pointed at the same
`genome.db`, or with a long-running write script (`scripts/ingest_*.py`,
the backfill scripts) running while the server is up.

**Cause.** `genome.db` is opened in WAL mode at
`cymatix_context/genome.py:468-475`. WAL allows concurrent readers, but
SQLite still serializes writers. A 30-second `busy_timeout` is set
(`genome.py:471`), so genuine contention only surfaces above that
threshold. The most common triggers in practice:

1. Two cymatix uvicorn processes binding the same `genome.path` — only
   the first one's write lock survives; the second times out.
2. A backfill script (`scripts/backfill_*.py`) writing while the
   server still holds a long-lived read connection. The reader at
   `genome.py:499-508` uses `isolation_level=None` to avoid pinning
   WAL snapshots, but third-party scripts may not.
3. WAL not checkpointed before a snapshot copy — the `.db` file is
   stale even though `.db-wal` has the latest writes.

**Fix.**

1. Stop every cymatix process pointing at the same DB:
   ```bash
   ps aux | grep "cymatix_context._asgi"     # POSIX
   tasklist | findstr python                # Windows
   ```
   Kill all but one.

2. Force a WAL checkpoint via the admin endpoint
   (`cymatix_context/server.py:2876-2880`):
   ```bash
   curl -X POST "http://127.0.0.1:11437/admin/checkpoint?mode=TRUNCATE"
   ```
   Expected response: `{"checkpointed": true, "mode": "TRUNCATE"}`.

3. If you need to inspect or copy `genome.db`, do it after
   step 2 — the `.db-wal` file should now be empty or absent.

4. For long-running backfill scripts, run them against a snapshot
   copy first, verify, then hot-swap during a maintenance window
   instead of writing to the live DB.

**Verify.**
```bash
sqlite3 genomes/main/genome.db "PRAGMA journal_mode; \
  SELECT COUNT(*) FROM genes;"
```
Expected: `wal` followed by an integer document count. If the `SELECT`
hangs, another writer is still holding the lock.

**Prevention.** One server per `genome.db`. If you need a side
deployment, set `CYMATIX_GENOME_PATH=genomes/side/genome.db` (see
`cymatix_context/config.py:611-612`) so the side server uses a separate
file. The lifespan shutdown at `server.py:717` runs
`checkpoint("TRUNCATE")` automatically, so always Ctrl+C cleanly.

---

## Frontier model returns plausible-but-wrong answers when /context indicates a miss

**Symptom.** Claude Opus / GPT-5 / Gemini 3 Pro receives a `/context`
response with `miss { do_not_answer_from_genome: true }` and a
`<helix:no_match reason="..."/>` token in `expressed_context`, but
answers from training prior anyway.

**Cause.** The agent's system prompt does not import the
cymatix-context know/miss contract fragment. Without explicit rules
teaching the model to honor the `do_not_answer_from_genome` flag and
call an escalate tool, it falls back to its training prior to
fabricate an answer. The contract is load-bearing only when the agent
prompt teaches it.

The Stage 6 spec (`docs/specs/2026-05-08-stage-6-know-miss-blocks.md`
§12) is explicit about this: the runtime envelope validator in
`cymatix_context/schemas.py:521-600` rejects malformed `MissBlock`s
server-side, but the prompt-level contract that turns
`do_not_answer_from_genome=True` into actual escalation is the
caller's responsibility. The compiled fragment lives at
`cymatix_context/agent_prompt.py:25-92`.

**Fix.**

1. In your agent SDK setup, prepend
   `cymatix_context.agent_prompt.CYMATIX_NO_MATCH_FRAGMENT` (or the
   combined `full_fragment()`) to the system prompt:

   ```python
   from cymatix_context.agent_prompt import (
       CYMATIX_NO_MATCH_FRAGMENT,
       CYMATIX_REFRESH_FRAGMENT,
       full_fragment,
   )

   system_prompt = full_fragment() + "\n\n" + your_existing_system_prompt
   ```

2. Register escalation tools (`grep` / `rag` / `web` / `ask_human`)
   so the model has somewhere to route to. Cymatix only signals which
   CLASS of tool to invoke; you implement the tool itself. The
   permitted set is enforced at
   `cymatix_context/schemas.py:564-569` (`ESCALATE_TARGETS`).

3. Ensure your agent has a tool-call-before-answer loop: when `miss`
   is present (or the `<helix:no_match/>` tag appears in
   `expressed_context`), the model emits a tool call from the
   `escalate_to` list, waits for the result, then composes the reply.

4. Distinguish `recommendation = "escalate"` from
   `recommendation = "refresh"` — the Stage 7 fragment at
   `cymatix_context/agent_prompt.py:59-82` covers refresh semantics.
   Refresh means "the answer is here, just out of date — fetch and
   retry"; escalate means "the answer is NOT here — go ask
   elsewhere". Conflating them defeats the contract.

**Verify.** Run a query you know is NOT in the knowledge store (e.g., a
synthetic UUID). Expected: the agent emits a tool call (grep / rag /
web), not an answer. Inspect via your agent's trace, or run the
offline compliance harness:
```bash
python scripts/eval_agent_compliance.py --jsonl <your_trace.jsonl>
```
The Stage 6 spec recommends a >=95% compliance bar.

**Prevention.** Add a unit test that mocks `/context` returning a
`MissBlock` and asserts the agent issues a tool call. Wire the
fragment injection into your agent SDK boot path so it cannot be
forgotten — the runtime contract is enforced server-side, the prompt
contract is enforced only by you.

---

## Dense recall returns nothing / /context retrieval rate plateaus low

**Symptom.** `bench_needle_1000.py` retrieval rate stays under 30%;
`/context` debug logs show
`query_genes_dense_recall: empty matrix, falling back to lexical-only`.
The knowledge store was ingested before the 7-stage merge, or pulled but never
backfilled.

**Cause.** Stage 2's backfill was not run after pulling the 7-stage
merge. The `embedding_dense_v2 BLOB` column is empty; the in-memory
dense matrix loader at `cymatix_context/genome.py` returns `[]` and
emits a one-time WARN. Stage 2 promoted dense from a 12-candidate
re-ranker to a parallel first-class recall source returning top-K=500
over the full corpus (spec
`docs/specs/2026-05-08-stage-2-dense-recall.md` §1). Without the v2
column populated, dense recall silently degrades to lexical-only.

**Fix.**

1. Stop cymatix:
   ```bash
   _stop_bench_helix.bat       # Windows bench helper
   # Or Ctrl+C the uvicorn process
   ```

2. Snapshot-copy `genomes/main/genome.db` to a working location:
   ```bash
   cp genomes/main/genome.db genomes/main/genome.db.backfill-working
   ```

3. Run the backfill:
   ```bash
   python scripts/backfill_bgem3_v2.py \
     genomes/main/genome.db.backfill-working
   ```
   Wall-clock estimate: ~30-90 min on CPU sentence-transformers
   BGE-M3, ~5-15 min on GPU (FlagEmbedding). Idempotent and
   resumable: rows with a non-NULL v2 BLOB are skipped (script
   header at `scripts/backfill_bgem3_v2.py:1-22`).

4. Verify the script ends with `coverage=100.00%`.

5. Hot-swap the populated DB into place during a maintenance window:
   ```bash
   mv genomes/main/genome.db genomes/main/genome.db.pre-backfill
   mv genomes/main/genome.db.backfill-working genomes/main/genome.db
   ```

6. Restart cymatix.

**Verify.**
```bash
sqlite3 genomes/main/genome.db \
  "SELECT COUNT(*) FROM genes WHERE embedding_dense_v2 IS NOT NULL;"
sqlite3 genomes/main/genome.db "SELECT COUNT(*) FROM genes;"
```
Expected: both counts are equal. A delta means the backfill is
incomplete — re-run step 3.

**Prevention.** After every cymatix-context upgrade, check the
changelog for new backfill operator actions. The post-merge runbook
lives in `docs/operator-runbooks.md`; new backfills are also called
out in the corresponding stage spec under `docs/specs/`.

---

## SQLite FTS5 missing on macOS

**Symptom.** First cymatix start (or the first `/ingest` call) raises:
```
sqlite3.OperationalError: no such module: fts5
```
The server log records "FTS5 not available — content search disabled"
at `cymatix_context/genome.py:803`. Tier 3 (full-text content match)
silently drops out of the 9-tier fusion ranker.

**Cause.** Apple's bundled CPython on Big Sur and later links against
a stripped system SQLite that omits FTS5 and JSON1. Homebrew's
SQLite ships FTS5, but `pyenv install` does not pick it up unless you
explicitly point it at the brewed copy. Linux distros and Windows do
not hit this — CPython 3.11+ wheels for those platforms bundle a
recent SQLite with FTS5 enabled.

**Fix.**

1. Install Homebrew SQLite:
   ```bash
   brew install sqlite
   ```

2. Rebuild your Python (or install a fresh one) with the brewed
   SQLite linked in. For pyenv:
   ```bash
   LDFLAGS="-L$(brew --prefix sqlite)/lib" \
   CPPFLAGS="-I$(brew --prefix sqlite)/include" \
   PYTHON_CONFIGURE_OPTS="--enable-loadable-sqlite-extensions" \
   pyenv install 3.12.7
   pyenv shell 3.12.7
   ```

3. Re-create the venv and reinstall cymatix:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e ".[embeddings,cpu]"
   ```

4. Restart cymatix.

**Verify.**
```bash
python -c "import sqlite3; \
  c = sqlite3.connect(':memory:'); \
  c.execute('CREATE VIRTUAL TABLE t USING fts5(x)'); \
  print('ok')"
```
Expected: `ok`. Anything else means FTS5 is still not linked. See
`docs/SETUP.md:33-56` for the same recipe with extra context.

**Prevention.** On macOS, never use the system Python (`/usr/bin/python3`)
for cymatix. Use pyenv, conda, or Homebrew Python — all of them link
against a current SQLite with FTS5. Pin the Python version in your
project README so contributors do not regress.

---

## Python 3.14 wheel mismatches for torch / pystray

**Symptom.** `pip install -e ".[all]"` fails on a 3.14 venv with one
of:
```
ERROR: Could not find a version that satisfies the requirement torch
ERROR: Failed building wheel for pystray
ImportError: dynamic module does not define module export function (PyInit__C)
```
Or the install completes but `python -c "import torch"` segfaults at
import.

**Cause.** PyTorch and `pystray` (with its Pillow dependency) lag
upstream Python by months. CPython 3.14 ships in February 2026; at
the time of the 7-stage merge their wheel matrices target 3.11-3.13.
`pyproject.toml:10` declares `requires-python = ">=3.11"`, but the
classifier list (lines 21-23) only certifies 3.11-3.13. See
`docs/SETUP.md:23-28` for the same warning.

**Fix.**

1. Install Python 3.12 or 3.13 alongside your existing 3.14:
   ```bash
   pyenv install 3.13.1   # POSIX
   # Or: download an installer from python.org for Windows
   ```

2. Create a fresh venv on 3.13:
   ```bash
   python3.13 -m venv .venv-313
   source .venv-313/bin/activate     # POSIX
   .venv-313\Scripts\activate.bat    # Windows
   ```

3. Reinstall cymatix-context:
   ```bash
   pip install -e ".[all]"
   ```

4. If you must stay on 3.14, install only the extras with binary
   wheels available:
   ```bash
   pip install -e ".[embeddings,cpu]"   # no torch, no pystray
   ```
   The proxy and CPU encoders work; the launcher tray and DeBERTa
   rerank backend will not.

**Verify.**
```bash
python --version
python -c "import torch; print(torch.__version__)"
python -c "import pystray; print(pystray.__version__)"
```
Expected: a Python version line in the 3.11-3.13 range, then a
`torch` and `pystray` version with no segfault. On 3.14, expect at
least one of the latter two to fail or be missing.

**Prevention.** Pin the supported Python range in your project
README. The `.python-version` file (used by pyenv) is a good lock
mechanism. CI matrices should test against the lowest-supported
version (3.11) and the recommended one (3.13) until upstream wheels
catch up.

---

## spaCy NER model not downloaded — ingestion falls back, /context quality degrades silently

**Symptom.** `/ingest` POSTs succeed but ingested document quality drops
visibly: tag set is sparse, entity-graph (`[ingestion] entity_graph =
true`) edges fail to populate, and `/context` retrieval rate on
entity-heavy queries falls. The server log shows a single
`OSError: [E050] Can't find model 'en_core_web_sm'` at first ingest
and then nothing further.

**Cause.** `cymatix_context/tagger.py:52-64` lazy-loads
`en_core_web_sm` on first use. The package `__init__.py:24-28`
soft-imports `CpuTagger` so an `ImportError` at module load does not
crash the server, but a missing *model* is a runtime error inside
`_get_nlp()` and surfaces only when ingestion actually runs. The
tagger has no graceful regex fallback — when the spaCy load fails,
the call stack unwinds and that ingest document gets none of the NER /
noun-chunk enrichment, dropping its tag count.

**Fix.**

1. Install the model:
   ```bash
   python -m spacy download en_core_web_sm
   ```
   This pulls ~12 MB and writes into the active venv's
   `site-packages/en_core_web_sm/`.

2. Restart cymatix. The tagger's `_nlp` cache is process-local; in-flight
   workers will not pick up a freshly downloaded model.

3. Re-ingest any documents that were ingested while the model was
   missing:
   ```bash
   python scripts/docs_tidy_reingest.py <path>
   ```
   Or use the `/admin/refresh` endpoint to trigger a retrieval-layer
   refresh after re-ingest.

**Verify.**
```bash
python -c "import spacy; \
  nlp = spacy.load('en_core_web_sm'); \
  doc = nlp('cymatix-context fronts Ollama at localhost:11434.'); \
  print([(e.text, e.label_) for e in doc.ents])"
```
Expected: a non-empty list of entities (at minimum a `PRODUCT` /
`ORG` hit). A blank `[]` or an `OSError` means the model is missing
from this venv.

**Prevention.** Wire the model download into your install script.
For pip-only installs, append the download line to your project's
post-install step:
```bash
pip install "cymatix-context[cpu]" && python -m spacy download en_core_web_sm
```
Production deployments should bake the model into the container
image so cold starts do not race the network.

---

## Calibration drift — per_classifier mode with stale floors

**Symptom.** The Stage 4 calibrated floors in `cymatix.toml`
(`[abstain.factual]`, `[abstain.multi_hop]`, etc.) were generated
weeks ago against a different knowledge store snapshot. `/context` now
abstains on queries it used to answer (or vice versa). Bench
retrieval rates drift while no code has changed.

**Cause.** `[abstain].mode = "per_classifier"` reads per-classifier
floors from the `[abstain.<cls>]` blocks and uses them as hard
gates. Those floors are calibrated empirically by
`scripts/calibrate_thresholds.py` from a bench JSONL of located
queries — if the knowledge store has grown (or shifted in topic mix) since
the calibration run, the score distribution shifts too, and the old
floors no longer reflect reality. The loader at
`cymatix_context/config.py:718-757` accepts the stale values without
warning; only the bench tells you they are wrong. There is no
auto-recalibration.

**Fix.**

1. Regenerate the located bench:
   ```bash
   python benchmarks/bench_needle_1000.py \
     --genome genomes/main/genome.db \
     --output results/located_n1000.jsonl
   ```

2. Recalibrate (script header at
   `scripts/calibrate_thresholds.py:1-25`):
   ```bash
   python scripts/calibrate_thresholds.py \
     --input results/located_n1000.jsonl \
     --genome genomes/main/genome.db \
     --output-toml cymatix.toml.calibrated
   ```

3. Diff the new floors against the live ones:
   ```bash
   diff cymatix.toml cymatix.toml.calibrated
   ```
   Review every `abstain_top` / `focused_top` / `tight_top` change —
   floors that drift by >0.10 are suspicious; check the bench's hit /
   miss distribution for the affected classifier class.

4. Replace the `[abstain.*]` blocks in the live `cymatix.toml` with the
   recalibrated values. Restart cymatix.

5. If you want to fall back to the global mode while you investigate:
   ```toml
   [abstain]
   mode = "global"
   ```
   This restores the legacy hard-coded floors
   (`TIGHT_SCORE_FLOOR=5.0`, `FOCUSED_SCORE_FLOOR=2.5`, abstain at
   `2.5`) — pre-Stage-4 behavior byte-for-byte (see comment at
   `cymatix.toml:421-432`).

**Verify.** Compare retrieval rate before and after on the same
bench:
```bash
python benchmarks/bench_needle_1000.py \
  --genome genomes/main/genome.db \
  --report
```
Expected: retrieval rate within 1-2 pp of the figure recorded the
last time the floors were calibrated. A larger gap means the knowledge store
has shifted enough to warrant a recalibration cadence (monthly is a
reasonable default).

**Prevention.** Recalibrate after any of: large ingestion run,
shard split, compressor model change, or fusion-mode flip
(additive ↔ rrf). Pin the calibration JSONL alongside the floors so
reviewers can see what data the threshold was fit against.

---

## When to file an issue

If your symptom does not match any section above and `/health`
reports `genome_genes > 0` and `upstream_reachable = true`, file an
issue: <https://github.com/SwiftWing21/cymatix-context/issues>. Include
`/health`, `/stats`, the relevant log lines, and your `cymatix.toml`
with secrets redacted.
