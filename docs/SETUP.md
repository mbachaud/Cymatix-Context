# Cymatix Context — Setup Guide

## What this guide covers

This document is the canonical install path for `cymatix-context`. It walks
through three workflows: the daily-driver tray flow (Windows-first, runs
the supervisor + native observability sidecar + headroom proxy + tray
icon), the proxy-only flow (a headless `/context` server suitable for
servers and CI), and the agent-SDK flow (consuming `/context` and
`/context/packet` from a Claude Code or other MCP-aware agent). Section 3
is the extras decision matrix that tells you which `pip install` extras
to add for the features you want; the rest of the guide cross-references
that table. Sections 5 and 6 cover silent-failure modes and the post-2026-05-10
operator actions required for the 7-stage retrieval-fix.

If you only want a one-line install, the README's Quick Start
(`pip install "cymatix-context[all]"` then `start-helix-tray.bat`) is correct
but skips every nuance — every operator who has hit a `/context` empty-result
problem ended up reading this file.

## Prerequisites

- **Python 3.11–3.13.** The wheel classifies these three versions in
  [`pyproject.toml`](../pyproject.toml) lines 21–23. Python 3.14 works in
  practice today but is not classified — `torch` and `pystray` wheel
  availability tends to drift behind upstream Python releases by a few
  months, so 3.14 may force you to build one of those from source. If
  you have a choice, pin 3.12 or 3.13.
- **Ollama** (latest stable), reachable at `http://localhost:11434`.
  Cymatix uses Ollama as its default chat upstream and as the optional
  compressor backend. Verify with `curl -s http://localhost:11434/api/tags`
  — the response must be JSON, not a connection-refused.
- **SQLite with FTS5.**
  - Windows + Linux: bundled with the Python `sqlite3` module that ships
    with CPython 3.11+. Nothing to install.
  - **macOS: the system SQLite that ships with Apple Python often lacks
    FTS5.** Install a current SQLite via Homebrew and ensure your Python
    links the brewed copy:

    ```bash
    brew install sqlite
    # If using pyenv, rebuild your Python so its sqlite3 picks up brew:
    LDFLAGS="-L$(brew --prefix sqlite)/lib" \
    CPPFLAGS="-I$(brew --prefix sqlite)/include" \
    PYTHON_CONFIGURE_OPTS="--enable-loadable-sqlite-extensions" \
    pyenv install 3.12.7
    ```

    Verify FTS5 is available:

    ```bash
    python -c "import sqlite3; conn = sqlite3.connect(':memory:'); \
      conn.execute('CREATE VIRTUAL TABLE t USING fts5(x)'); print('ok')"
    ```

    If that prints anything other than `ok`, FTS5 is not linked.
    Without FTS5 the BM25 tier (Tier 5 of the 9-tier fusion scorer)
    is unavailable and `cymatix ingest` will fail at document-table init.
- **Disk.** A full `pip install -e ".[all]"` pulls roughly 2 GB —
  dominated by `torch`, `sentence-transformers`, `headroom-ai`, and
  `tree-sitter` parser binaries. The lean proxy-only path
  (`embeddings,cpu`) is roughly 150 MB. The knowledge store itself grows linearly
  with corpus size; an 18.5k-document main knowledge store is ~250 MB on disk.

## Extras decision matrix

Cymatix ships a wide pyproject extras surface. Most operators install
exactly two or three extras, not `[all]`. The table below maps each
extra to the runtime feature it gates and a "install when" trigger.
Source: [`pyproject.toml`](../pyproject.toml) `[project.optional-dependencies]`
block, lines 39–113.

| Extra | What it enables | Install when |
|---|---|---|
| `accel` ([`pyproject.toml:43`](../pyproject.toml)) | `orjson` fast-path for JSON encode/decode on the `/context` and `/ingest` paths. | Optional perf boost — small but always-on. |
| `embeddings` ([`pyproject.toml:44`](../pyproject.toml)) | `numpy` + `sentence-transformers`. Powers the SEMA codec (MiniLM 20-dim sparse projection used for cold-tier retrieval) and the BGE-M3 dense recall path. | Semantic retrieval enabled — required for Tier 7 (`ΣĒMA cosine`) and Stage 2 dense recall. |
| `cpu` ([`pyproject.toml:45`](../pyproject.toml)) | `spacy>=3.7` for ingest-time NER. | `[ingestion] backend = "cpu"` — this is the **default** ingest backend. If you are doing any ingestion at all, install this extra. |
| `mcp` ([`pyproject.toml:49`](../pyproject.toml)) | `mcp>=1.0` Python SDK for the MCP shim. Required for `python -m cymatix_context.mcp_server`. | You are integrating with Claude Code, Cursor, Continue, Claude Desktop, or any other MCP host. |
| `nli` ([`pyproject.toml:54`](../pyproject.toml)) | `torch>=2.0` + `transformers>=4.30` standalone — the path used when `[ribosome] backend = "deberta"` for cross-encoder rerank or relation-graph NLI. | You explicitly flipped `[ribosome] backend = "deberta"` in `cymatix.toml`. If you have `embeddings` already, sentence-transformers transitively pulls torch — you only need this extra for the standalone deberta-only path. |
| `otel` ([`pyproject.toml:57`](../pyproject.toml)) | OpenTelemetry SDK + OTLP gRPC exporter + FastAPI instrumentation. Emits metrics, traces, and logs to the OTel collector at `localhost:4317`. | **Silently required by `start-helix-tray.bat`**, which sets `CYMATIX_OTEL_ENABLED=1` unconditionally. Without this extra the cymatix server starts and serves `/context`, but emits no telemetry — Grafana dashboards will be empty. See section 5. |
| `launcher` ([`pyproject.toml:62`](../pyproject.toml)) | `jinja2`, `psutil`, `platformdirs`, `py-cpuinfo`. The `cymatix-launcher` and `cymatix-status` console scripts. | Tray / supervisor flow — required by every flow that uses `start-helix-tray.bat`, `setup-helix.bat`, `backend-with-otel.bat`, or `launcher-with-otel.bat`. |
| `launcher-native` ([`pyproject.toml:63`](../pyproject.toml)) | Everything in `launcher` plus `pywebview`. Adds the native-window dashboard (`cymatix-launcher --native`) instead of opening the dashboard in a browser tab. | You want a native desktop window UI but cannot or will not install LGPL dependencies. |
| `launcher-tray` ([`pyproject.toml:70`](../pyproject.toml)) | Everything in `launcher` plus `pystray>=0.19` + `Pillow>=10` + `pywin32` (Windows only). Enables the system-tray icon with start/stop/restart, "Open Grafana", and "Open Prometheus" menu items. | **Canonical daily-driver flow.** Note: `pystray` is **LGPL-3** licensed. Cymatix itself stays Apache-2.0-clean because `launcher-tray` is a runtime-only optional dep — the cymatix-context wheel does not bundle pystray. If LGPL is a license concern for your distribution, install `launcher-native` instead. |
| `ast` ([`pyproject.toml:75`](../pyproject.toml)) | `tree-sitter` core + parsers for Python, Rust, JavaScript, TypeScript. Used by the code-aware ingest path to extract function/class boundaries for tag-based indexing. | You ingest source code (not just docs / markdown / conversations). |
| `scorerift` ([`pyproject.toml:82`](../pyproject.toml)) | The `scorerift` companion package (CD spectroscope bridge). Powers ScoreRift dimensions in the audit pipeline. | You are running the ScoreRift integration. Most operators do not need this. |
| `codec` ([`pyproject.toml:85`](../pyproject.toml)) | `headroom-ai[proxy,code]>=0.5.21` — Tejas Chopra's CPU-resident semantic compression proxy + dashboard. | You want the Headroom dashboard at `http://127.0.0.1:8787/dashboard` and the tray's "Open Headroom Dashboard" + Start/Restart/Stop menu items. The launcher adopts an already-running headroom proxy on its configured port; otherwise it spawns one. See `[headroom]` in [`cymatix.toml`](../cymatix.toml) lines 163–169. |
| `dev` ([`pyproject.toml:86`](../pyproject.toml)) | `pytest`, `pytest-asyncio`, `numpy`, `spacy>=3.7`, `hypothesis>=6.0`. | You are a contributor running the test suite. Not for production. |
| `all` ([`pyproject.toml:90`](../pyproject.toml)) | The full feature surface **minus** `dev` (contributor-only) and **minus** `launcher-tray` (LGPL). Includes `accel`, `embeddings`, `cpu`, `mcp`, `nli`, `otel`, `launcher`, `launcher-native`, `ast`, `codec`. Does NOT include `scorerift` (separate package). | Convenience for "I want everything Apache-2.0-clean and don't care about disk." Pulls ~2 GB. Install `[all,launcher-tray]` if you also want the tray. |

A few notes on the matrix that have bitten operators:

- `[all]` does **not** include `[launcher-tray]`. If you `pip install "cymatix-context[all]"` then run `start-helix-tray.bat`, the tray icon will not appear and the launcher falls back to the native window or browser. Add `,launcher-tray` explicitly if you want the tray.
- `[all]` does **not** include `[scorerift]`. ScoreRift is a separate companion package and stays opt-in.
- `[embeddings]` transitively pulls `torch`. If you do not need semantic retrieval, you do not need torch — skip this extra and accept that Tier 7 (ΣĒMA cosine) and Stage 2 dense recall will be no-ops.
- `[codec]` pulls `headroom-ai` which itself has nontrivial dependencies. Only install if you want the Headroom proxy lifecycle.

## Install workflows

### Daily-driver tray flow (Windows-first, recommended)

This is what most operators run on a workstation. It launches the
supervisor, the native observability sidecar (Prometheus + Tempo + Loki +
Grafana + OTel collector — managed binaries, not Docker), the cymatix
backend on `:11437`, and the system-tray icon.

```bash
pip install -e ".[all,launcher,launcher-tray,otel]"
```

`[all]` already includes `launcher` and `otel`, but listing them
explicitly documents intent and makes the line copy-paste safe even if
the `all` bundle composition changes.

Then run the one-time setup (creates desktop / start-menu shortcuts and
optionally brings up the observability stack):

```cmd
setup-helix.bat
```

`setup-helix.bat` is a thin wrapper that delegates to
`deploy\windows\setup-helix.ps1`. Useful flags from the wrapper:

- `setup-helix.bat -WithObservability` — also docker-compose up the
  Grafana + Prometheus + OTel stack (advanced; the default native sidecar
  path already covers this).
- `setup-helix.bat -NoShortcuts` — headless install, no `.lnk` creation.
- `setup-helix.bat -SkipPipInstall` — refresh shortcuts without
  reinstalling the package.

If you specifically want the **Grafana telemetry stack without the tray**
(servers, CI, headless workstations) — or want to pre-warm the
collector + Prometheus + Tempo + Loki + Grafana binaries before the
first tray launch — use the dedicated wrapper:

```cmd
:: Windows
scripts\setup-grafana-telem.ps1

:: Linux / macOS
scripts/setup-grafana-telem.sh
```

That script:

1. Verifies `[otel]` and `[launcher]` extras are importable.
2. Calls `scripts/install-native-observability.{ps1,sh}` to download +
   verify-sha + extract the five pinned binaries into
   [`tools/native-otel/`](../tools/native-otel/).
3. Runs `python -m cymatix_context.launcher.observability_render render-all`
   to materialize `tools/native-otel/configs/` (substitutes
   `tempo:4317` → `localhost:14317` — Tempo's OTLP receiver is
   remapped off `4317` so it doesn't collide with the collector's
   intake on bare-metal localhost; container paths → per-user state
   dirs) and copy dashboard JSON + datasource provisioning into
   Grafana's `conf/provisioning/` tree.
4. Smoke-tests Grafana `:3000` and Prometheus `:9090` if the supervisor
   is already running, otherwise prints next-step instructions.

Useful flags: `--skip-download` (binaries already on disk),
`--verify-only` (just smoke-test the running stack), `--server-only`
(render configs only — for CI).

The script does NOT start the supervisor itself — running it once is a
one-time on-disk-state prep, after which `start-helix-tray.bat` (or
`cymatix-launcher --tray`) spawns the five binaries. Re-runs are
idempotent.

Then daily-launch via:

```cmd
start-helix-tray.bat
```

What that batch file does (from
[`start-helix-tray.bat`](../start-helix-tray.bat)):

- Sets `CYMATIX_OTEL_ENABLED=1`, `CYMATIX_OTEL_ENDPOINT=localhost:4317`,
  `CYMATIX_OTEL_INSECURE=1`, `CYMATIX_OTEL_SAMPLER_RATIO=1.0`.
- Sets `CYMATIX_BUDGET_ZONE=1` (2026-04-14 spike — clamps `max_genes`
  based on caller's prompt-token-zone).
- Defaults `CYMATIX_USER=max` if unset (you should override this).
- Sets `CYMATIX_HEADROOM_ENABLED=1` and `CYMATIX_HEADROOM_AUTOSTART=1` (only
  effective if `[codec]` is installed).
- Sets `CYMATIX_HEADROOM_ROUTE_UPSTREAM_AUTO=1` so non-local upstreams
  auto-route through Headroom.
- Spawns `python -m cymatix_context.launcher.app --tray --grafana-url
  http://localhost:3000/d/helix-overview/helix-overview --prometheus-url
  http://localhost:9090/graph` in `/B` (no new window) mode.

Verify it's up:

```bash
cymatix-status
curl -s http://127.0.0.1:11437/health | python -m json.tool
```

The tray icon's right-click menu surfaces Start / Stop / Restart, "Open
Grafana", "Open Prometheus", and (when Headroom is installed and
enabled) "Open Headroom Dashboard" + Start / Restart / Stop Headroom.

**Linux / macOS variant.** No `start-helix-tray.sh` exists yet
(non-docs follow-up tracked in issue #59). For now, on Linux/macOS, run
the launcher directly:

```bash
CYMATIX_OTEL_ENABLED=1 \
CYMATIX_OTEL_ENDPOINT=localhost:4317 \
CYMATIX_OTEL_INSECURE=1 \
CYMATIX_OTEL_SAMPLER_RATIO=1.0 \
CYMATIX_BUDGET_ZONE=1 \
CYMATIX_HEADROOM_ENABLED=1 \
CYMATIX_HEADROOM_AUTOSTART=1 \
cymatix-launcher --tray \
  --grafana-url "http://localhost:3000/d/helix-overview/helix-overview" \
  --prometheus-url "http://localhost:9090/graph"
```

If `pystray` cannot bind a tray icon on your desktop environment (some
Wayland sessions, some headless macOS setups), the launcher falls back
to the native pywebview window if `[launcher-native]` is installed, or
opens the dashboard in a browser tab.

### Proxy-only flow (no tray, no observability)

This is the canonical headless deployment — a server that exposes
`/context`, `/context/packet`, `/ingest`, `/stats`, `/health`, and the
OpenAI-compatible `/v1/chat/completions` proxy on `:11437`, with no tray
icon, no supervisor, no observability sidecar. Roughly 150 MB on disk.

```bash
pip install -e ".[embeddings,cpu]"
```

Add `,mcp` if you also want `python -m cymatix_context.mcp_server` (MCP
stdio shim) on the same install. Add `,otel` if you have an external OTel
collector you want to export to.

```bash
python -m uvicorn cymatix_context._asgi:app --host 127.0.0.1 --port 11437
```

> **Note.** The internal ASGI module is `cymatix_context._asgi`. The
> `start-helix-tray.bat` flow goes through the launcher's
> `HelixSupervisor` which spawns its own uvicorn — this direct command
> is for headless servers and CI where the supervisor adds no value.
> An equivalent, also-supported entry-point is
> `python -m uvicorn cymatix_context.server:app` — that path exists in
> [`backend-with-otel.bat`](../backend-with-otel.bat) line 10.

Verify it's up:

```bash
curl -s http://127.0.0.1:11437/health | python -m json.tool
curl -s -X POST http://127.0.0.1:11437/context \
  -H "Content-Type: application/json" \
  -d '{"query":"hello"}' | python -m json.tool
```

This flow expects the knowledge store to live at the path configured in
[`cymatix.toml`](../cymatix.toml) line 127 (`[knowledge store] path =
"genomes/main/genome.db"`). Override with `CYMATIX_GENOME_PATH=/abs/path`
or by editing the TOML.

### Agent-SDK flow

When Cymatix is consumed *as a context source* by another agent (Claude
Code via MCP, Continue via OpenAI-compat, Cursor, a custom Claude
Agent SDK app, etc.), the install is the same as the proxy-only flow.
The extra step is teaching the *consuming* agent to honor the Stage 6
KnowBlock / MissBlock contract.

Install:

```bash
pip install -e ".[embeddings,cpu,mcp]"
```

Run the proxy:

```bash
python -m uvicorn cymatix_context._asgi:app --host 127.0.0.1 --port 11437
```

Then inject the `<helix:no_match/>` and `know` / `miss` contract into
the agent's system prompt. The fragment is exported from
`cymatix_context.agent_prompt.CYMATIX_NO_MATCH_FRAGMENT` for programmatic
inclusion, and documented in plain text at
[`docs/agent-sdk-fragment.md`](agent-sdk-fragment.md).

For Claude Code / MCP, register the cymatix MCP shim in
`~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "cymatix-context": {
      "command": "python",
      "args": ["-m", "cymatix_context.mcp_server"],
      "cwd": "/absolute/path/to/your/project",
      "env": {
        "CYMATIX_MCP_URL": "http://127.0.0.1:11437",
        "CYMATIX_AGENT": "your-agent-handle",
        "CYMATIX_USER": "your-handle",
        "CYMATIX_PARTY_ID": "your-machine-name"
      }
    }
  }
}
```

For an Open WebUI or other OpenAPI host that wraps MCP via mcpo:

```cmd
start-helix-mcpo.bat
```

That file (see [`start-helix-mcpo.bat`](../start-helix-mcpo.bat)) waits
for the cymatix backend to answer `/health`, then runs
`mcpo --port 8788 -- python -m cymatix_context.mcp_server`. Requires
`pip install mcpo` in the same env. The default port is `8788`,
overridable via `CYMATIX_MCPO_PORT`. Identity defaults are set defensively
so multi-agent sessions don't collide on `CYMATIX_AGENT=laude` (Claude
Code's default) — see lines 28–41 of the bat file.

The agent itself **must** import the prompt fragment. Without it, a
capable model paints over `do_not_answer_from_genome=true` and answers
from its training prior — which is scored as a hard failure in offline
eval. See [`docs/agent-sdk-fragment.md`](agent-sdk-fragment.md) for the
full contract.

#### Optional: Grafana telemetry for agent sessions

When cymatix is consumed via MCP from a headless agent process, the
proxy-only flow does **not** spawn the observability supervisor (no
tray, no launcher). If you want the four operations dashboards to light
up so you can audit agent activity — `/context` rate, know/miss ratio,
per-stage latency, calibration provenance, token spend — run the
dedicated setup script once per machine:

```cmd
:: Windows
scripts\setup-grafana-telem.ps1

:: Linux / macOS
scripts/setup-grafana-telem.sh
```

That installs the five native binaries (collector, Prometheus, Tempo,
Loki, Grafana), renders runtime configs, and wires dashboard
provisioning. After it completes, start the cymatix backend with OTel
enabled:

```bash
export CYMATIX_OTEL_ENABLED=1
export CYMATIX_OTEL_ENDPOINT=localhost:4317
python -m uvicorn cymatix_context._asgi:app --host 127.0.0.1 --port 11437
```

Bring the supervisor up the same way the tray does — the obs binaries
are spawned implicitly by the launcher; pass `--no-autostart` if the
agent host is managing the cymatix backend itself:

```bash
cymatix-launcher --no-autostart        # observability only, no cymatix backend
cymatix-launcher                        # observability + cymatix backend
```

Or, if the agent host doesn't have `[launcher]` installed, use the
docker-compose stack at [`deploy/otel/`](../deploy/otel/) — it is
bit-for-bit compatible with the rendered native configs (same datasource
UIDs, same dashboard JSON).

Smoke-test that telemetry is flowing end-to-end after the agent issues
its first `/context` call:

```bash
curl 'http://localhost:9090/api/v1/query?query=helix_context_latency_seconds_count'
```

A non-empty `data.result` array confirms the wiring is correct. The
Grafana panels at <http://localhost:3000/d/helix-overview> populate
within ~15s (one Prometheus scrape interval).

## Implicit requirements

The following are silent-failure modes — cymatix starts, `/context`
returns 200, but quality is degraded or telemetry is missing. Each one
has bitten at least one operator.

### spaCy NER model

The `[cpu]` extra installs the spaCy *library*, not the *model*. After
installing `[cpu]`:

```bash
python -m spacy download en_core_web_sm
```

Without the model, the `CpuTagger` ingest path silently falls back to a
keyword-only tagger. Tags become coarser, NER nodes in the
entity graph are absent, and `/context` retrieval quality on
entity-bearing queries degrades by 10–30 percentage points depending on
corpus.

### Ollama version + reachability

Cymatix does not ship Ollama. The default upstream is
`http://localhost:11434` (configured in [`cymatix.toml`](../cymatix.toml)
line 142, `[server] upstream`). Verify reachability:

```bash
curl -s http://localhost:11434/api/tags
```

The response must be a JSON object with a `models` array. If the server
is not reachable, `/v1/chat/completions` proxy requests fail
end-to-end, and **`/context` returns an empty `expressed_context` with
no warning** when the compressor backend is set to `"ollama"` (which is
the default placeholder, even when compressor is disabled — see
[`cymatix.toml`](../cymatix.toml) lines 21–22).

If you want to use a different upstream (e.g., a remote Ollama, a
LiteLLM proxy, an OpenAI-compatible endpoint), set
`CYMATIX_SERVER_UPSTREAM=http://...` or edit `[server] upstream` in
`cymatix.toml`.

Recommended models for the chat upstream:

```bash
ollama pull gemma4:e4b      # default for benchmarks; ~5 GB
ollama pull qwen3:4b         # alternative; ~3 GB, holds VRAM well
ollama pull gemma4:e2b       # small, fast; ~2 GB — good for ribosome backend if enabled
```

### Headroom first-run model download

If `[codec]` is installed and `CYMATIX_HEADROOM_ENABLED=1` (which
`start-helix-tray.bat` sets unconditionally on line 60), the headroom
proxy downloads its ModernBERT ONNX model on the **first**
`/v1/chat/completions` call. The download is roughly 200 MB and adds
20–60 seconds of cold-start latency to that first request, which the
caller experiences as a hung response.

Pre-warm before exposing the proxy to users:

```bash
python -c "from headroom_ai import preload; preload()"
```

The model caches under `~/.cache/headroom/`. Subsequent boots are
near-instant.

### OTel exporter

`start-helix-tray.bat` line 17 sets `CYMATIX_OTEL_ENABLED=1`. The cymatix
server reads that env var at boot and tries to construct an OTLP
exporter. If the `[otel]` extra is **not** installed, the server logs a
single WARN line on boot and continues serving — but emits no metrics,
no traces, no structured logs. Grafana dashboards will be empty.

Verify the exporter is wired:

```bash
curl -s http://127.0.0.1:11437/health | python -m json.tool
```

Look for the `observability.exporter` field. Expected values:

- `"otlp_grpc"` — `[otel]` installed and OTLP exporter built.
- `"noop"` — `CYMATIX_OTEL_ENABLED=1` but `[otel]` not installed.
  Grafana will be empty until you add the extra.
- absent / `null` — telemetry is off: `CYMATIX_OTEL_ENABLED` is unset or
  `0` AND `[telemetry] enabled` in cymatix.toml is false (the shipped
  default), and the tray launcher has not exported the enable (it does
  so once the local stack's collector port is up). This is the intended
  state for the proxy-only flow.

If the field shows `"noop"` and you want telemetry, run
`pip install -e ".[otel]"` and restart the tray.

## Post-install operator actions for the 7-stage retrieval-fix

The 7-stage retrieval-fix landed 2026-05-10 (PRs #45, #46, #47, #48,
#51, #55, #56). It introduced new schema, scripts, and config knobs
that need explicit operator action after `pip install -e .[...]`. These
actions are idempotent; running them twice is a no-op once they
converge.

### One-time backfill of full-1024-dim BGE-M3 vectors

Stage 2 promoted BGE-M3 dense retrieval from a 12-candidate re-ranker
to a parallel first-class recall source over the full corpus, and
restored the full 1024-dim Matryoshka representation (the previous
256-dim truncation collapsed random-pair cosine to ~0.6). To use the
new path, every document's `embedding_dense_v2` BLOB column must be
populated.

```bash
python scripts/backfill_bgem3_v2.py path/to/genome.db
```

If you omit the path argument, the script reads `[genome] path` from
`cymatix.toml`. The script is idempotent: rows already populated with a
non-NULL fp32 BLOB of the expected length are skipped. Runtime
estimate from
[`scripts/backfill_bgem3_v2.py`](../scripts/backfill_bgem3_v2.py) lines
22–24:

- ~30–90 minutes on CPU `sentence-transformers` BGE-M3 at 18.9k documents.
- ~5–15 minutes with `FlagEmbedding` + GPU.

Recommended runbook from the same script (lines 15–20):

1. Make a snapshot copy of `genomes/main/genome.db` first.
2. Run the script against the copy. Verify it reports `coverage=100%`.
3. Hot-swap the populated DB into place during a maintenance window.
4. Stage 4 follows: recalibrate `ann_similarity_threshold` at dim=1024.

After backfill, flip dense recall on by editing
[`cymatix.toml`](../cymatix.toml):

```toml
[retrieval]
dense_embedding_enabled = true     # was false (line 255)
dense_embedding_dim = 1024         # default; do not change
dense_pool_size = 500              # default
```

Without the backfill, `query_genes_dense_recall` logs a one-time WARN
and returns `[]`, falling back to lexical-only retrieval (degraded but
not broken). See spec
[`docs/specs/2026-05-08-stage-2-dense-recall.md`](specs/2026-05-08-stage-2-dense-recall.md)
§4 for the exact fallback contract.

### Threshold calibration (optional, recommended after backfill)

Stage 4 replaces the legacy hard-coded `TIGHT_SCORE_FLOOR=5.0`,
`FOCUSED_SCORE_FLOOR=2.5`, `abstain=2.5` constants with per-classifier
floors derived from a margin-over-random distribution. Default mode is
`"global"` (byte-identical to pre-Stage-4 behavior); to consume the
new path you run the calibration script and flip
`[abstain] mode = "per_classifier"`.

```bash
python scripts/calibrate_thresholds.py \
    --genome genomes/main/genome.db \
    --bench results/located_n1000.json
```

The script samples random document pairs from the knowledge store's
`embedding_dense_v2` BLOBs, computes a `mu + sigma_mult * sigma` ANN
cosine cutoff (margin-over-random), and segments
`agent.score_top` distributions by classifier class. It outputs:

- A TOML snippet with `[retrieval]` overrides and per-classifier
  `[abstain.<cls>]` blocks. Paste these into your `cymatix.toml`.
- `calibration_report.json` with full provenance.
- An UPSERT into the `genome_calibration` table when `--write-db`
  (default).

After pasting, flip the mode in `cymatix.toml`:

```toml
[abstain]
mode = "per_classifier"   # was "global" (line 432)

[abstain.default]         # required when mode = "per_classifier"
abstain_top = 0.40
focused_top = 0.65
tight_top = 1.10
foveated_alpha = 1.0
```

Other classes (`factual`, `multi_hop`, `arithmetic`, `procedural`) may
be omitted — runtime falls back to `[abstain.default]`. Without
`[abstain.default]`, the config loader raises `ConfigError` at boot.

Also flip the ANN threshold mode if you want margin-over-random:

```toml
[retrieval]
ann_threshold_mode = "margin_over_random"   # was "absolute" (line 267)
```

The runtime then reads the persisted threshold from `genome_calibration`
and falls back to `ann_similarity_threshold` (with a one-time WARN) if
the row is missing. See
[`docs/specs/2026-05-08-stage-4-threshold-calibration.md`](specs/2026-05-08-stage-4-threshold-calibration.md)
§3 + §6.

### Confidence calibration (optional, recommended after Stage 1 bench output exists)

Stage 6 introduces the KnowBlock / MissBlock contract on `/context` and
`/context/packet` — every retrieval emits exactly one of two top-level
blocks. The `[know]` table in [`cymatix.toml`](../cymatix.toml) lines
313–343 ships ship-time defaults that work on a generic corpus; the
calibration step refits the four logistic coefficients from a labeled
bench run.

Prerequisite: a JSONL file from a Stage 1 bench run (the output of
`benchmarks/located_n1000.py`).

```bash
python scripts/calibrate_know_confidence.py \
    --input results/located_n1000.jsonl \
    --out cymatix.toml
```

The script refits `[know].betas`, `s_ref`, `g_ref`, and `emit_floor`
to the operator's corpus. It uses `scikit-learn` for logistic
regression when the package is importable; otherwise it falls back to
the pure-Python gradient descent in
`cymatix_context.know_calibration.fit_betas_from_features`. To use the
faster path:

```bash
pip install scikit-learn
```

(There is no `[calibration]` extra in `pyproject.toml` today — sklearn
is intentionally a soft-optional. Issue #59 flagged this as a possible
future extra.)

After running, the `[know]` block in `cymatix.toml` is rewritten in place.
Spec:
[`docs/specs/2026-05-08-stage-6-know-miss-blocks.md`](specs/2026-05-08-stage-6-know-miss-blocks.md)
§3, §11.

### Idempotency and re-runs

All three calibration runs are idempotent. Re-run after corpus changes
that materially shift the document-pair cosine distribution or the
classifier-segmented score distribution. Typical triggers:

- A new ingest batch that adds more than ~20% of total document count.
- A schema migration that changes the BGE-M3 codec version.
- A bench discipline change (different `located_n1000` sampling).

A no-op re-run after no corpus change writes the same numbers and
costs a few minutes — there is no harm in including these in a
periodic pipeline.

## Environment variables

Cymatix reads roughly two dozen `CYMATIX_*` environment variables. The
table below is a one-line summary of the load-bearing ones. Full
documentation belongs in `.env.example` (cross-link below).

| Variable | Purpose |
|---|---|
| `CYMATIX_ORG` | Organization tag for 4-layer federation attribution. |
| `CYMATIX_DEVICE` | Machine identifier for CWoLa + session registry. |
| `CYMATIX_USER` | Operator handle. Defaults to `"max"` in `start-helix-tray.bat` if unset. |
| `CYMATIX_AGENT` | Persona writing documents (e.g., `laude`, `raude`, `taude`, `gemini`). If unset, ingests tag as "manual". |
| `CYMATIX_AGENT_KIND` | Tool-kind stamp (e.g., `claude-code`, `ollama-chat`). |
| `CYMATIX_PARTY_ID` | Multi-tenant party tag. Falls back to `[session] default_party_id` in `cymatix.toml`. |
| `CYMATIX_MCP_HANDLE` | Session-registry handle for the MCP host process. Disambiguates Claude Code / OpenWebUI / etc. on shared machines. |
| `CYMATIX_MCP_HOST` | MCP host kind (`ollama-chat`, `claude-desktop`, `cursor`, etc.). |
| `CYMATIX_MCP_URL` | URL where the cymatix backend is listening (default `http://127.0.0.1:11437`). |
| `CYMATIX_CONFIG` | Path to the TOML config file. Default: `cymatix.toml` in the cymatix run directory. |
| `CYMATIX_GENOME_PATH` | Override `[genome] path` from CLI / env. |
| `CYMATIX_USE_SHARDS` | Enable phase-2 shard router. |
| `CYMATIX_SERVER_UPSTREAM` | Override `[server] upstream`. Set automatically by the launcher when Headroom auto-routing is in effect. |
| `CYMATIX_SERVER_UPSTREAM_TIMEOUT` | Per-request timeout (float, seconds) for proxied calls to the upstream model server. |
| `CYMATIX_OTEL_ENABLED` | Set to `1` to enable OpenTelemetry. Set unconditionally by `start-helix-tray.bat`. Requires the `[otel]` extra. |
| `CYMATIX_OTEL_ENDPOINT` | OTLP gRPC endpoint. Default `localhost:4317`. |
| `CYMATIX_OTEL_INSECURE` | `1` for plaintext gRPC (default for local sidecar). |
| `CYMATIX_OTEL_SAMPLER_RATIO` | Trace sampling ratio (0.0–1.0). Default `1.0` in the tray flow. |
| `CYMATIX_OTEL_REDACT_QUERY` | Redact query text from emitted traces (privacy). |
| `CYMATIX_OTEL_LOGS_ENABLED` | `1` (default) ships Python log records via OTLP to the collector → Loki; `0` keeps traces + metrics on while suppressing log shipment (useful under Loki disk pressure or PII-sensitive deployments). |
| `CYMATIX_OTEL_LOGS_LEVEL` | Minimum level forwarded to OTel: `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`. Default `INFO`. Tunes log volume without touching traces/metrics. |
| `CYMATIX_OBSERVABILITY` | Set to `0` to skip the native observability sidecar. Useful when you're using the Docker compose stack at `deploy/otel/` or want no observability at all. |
| `CYMATIX_HEADROOM_ENABLED` | `1` to wire the Headroom proxy lifecycle into the launcher. |
| `CYMATIX_HEADROOM_AUTOSTART` | `1` to spawn a fresh headroom child if no existing proxy is found on the configured port. |
| `CYMATIX_HEADROOM_ROUTE_UPSTREAM_AUTO` | `1` to auto-route non-local upstreams through Headroom. |
| `CYMATIX_DISABLE_HEADROOM` | Hard kill switch. Overrides every other Headroom toggle. |
| `CYMATIX_BUDGET_ZONE` | `1` to enable the budget-zone document-cap clamp (clamps `max_genes` based on caller's prompt-token-zone). Set by `start-helix-tray.bat`. |
| `CYMATIX_DEVICE` | Device picker for hardware-aware codec/rerank: `auto \| cuda \| rocm \| mps \| cpu`. Default `auto`. |
| `CYMATIX_FILENAME_ANCHOR_ENABLED` | Override `[retrieval] filename_anchor_enabled`. |
| `CYMATIX_ABSTAIN_DISABLE` | `1` forces `[budget] abstain_enabled = false` without redeploy. |
| `CYMATIX_NO_MATCH_FRAGMENT` | Override the no-match prompt fragment text. Default lives in `cymatix_context.agent_prompt`. |
| `CYMATIX_REFRESH_FRAGMENT` | Override the refresh prompt fragment text. |
| `CYMATIX_TIMEOUT` | Default per-request timeout for the cymatix client SDK. |
| `CYMATIX_TZ` | Timezone for log timestamps. |
| `CYMATIX_URL` | Default URL for client SDK callers. |
| `CYMATIX_LAYERED_FINGERPRINTS` | Enable layered fingerprint payloads in `/fingerprint`. |
| `CYMATIX_FORMAT_VERSION` | Output format version pin (for reproducible bench runs). |
| `CYMATIX_LAUNCHER_UPDATE_CHECK` | `0` to disable the launcher's update check. |
| `CYMATIX_CODEGRAPH_PATH` | Path to a code graph database (optional). |
| `CYMATIX_EMBED_CODEGRAPH` | Enable code graph embedding generation. |
| `CYMATIX_WALKING_TIEBREAK` | Tiebreaker selection for walking retrieval (research-bench only). |

For the full list with default values and load order, see
[`.env.example`](../.env.example) (planned follow-up — issue #59
tracks adding it). Until that file lands, the canonical source is the
`os.environ.get(...)` calls in `cymatix_context/config.py`,
`cymatix_context/launcher/app.py`, and `cymatix_context/server.py`.

## Uninstall / cleanup

A clean uninstall has three steps. Each step is independent — you may
keep the Python package installed but delete the knowledge store, or vice versa.

### 1. Remove the Python package

```bash
pip uninstall cymatix-context
```

If you used an editable install (`pip install -e .`), this removes
the `.egg-link` and console scripts (`cymatix`, `cymatix-launcher`,
`cymatix-status`, `cymatix-vault`) but leaves the source tree intact.

### 2. Delete the per-user state directory

```bash
# Linux / macOS
rm -rf ~/.helix/

# Windows (PowerShell)
Remove-Item -Recurse -Force "$env:USERPROFILE\.helix"
```

This directory holds the supervisor's PID files and Unix sockets, the
tray's state JSON, the launcher's update-check cache, and any
session-registry metadata. Deleting it forces a fresh launcher boot;
no documents are lost (documents live in `genomes/`).

### 3. Delete the genome(s)

```bash
# All genomes
rm -rf genomes/

# Specific shard only
rm -rf genomes/main/
```

KnowledgeStores are SQLite `.db` files (plus their `-wal` and `-shm`
companions). They are durable data — back them up before deleting if
you might need them again.

### 4. Headroom cache (optional)

If `[codec]` is installed, the headroom proxy caches downloaded
ModernBERT ONNX weights under:

```bash
# Linux / macOS
rm -rf ~/.cache/headroom/

# Windows
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\headroom"
```

(Path may differ — `headroom-ai` uses `platformdirs` for cache
discovery. Check `python -c "import platformdirs;
print(platformdirs.user_cache_dir('headroom'))"` for your platform's
exact path.)

### 5. Native observability sidecar binaries (optional)

If you used `start-helix-tray.bat` and let it install the native OTel
binaries:

```bash
# Linux / macOS
rm -rf tools/native-otel/

# Windows
Remove-Item -Recurse -Force tools\native-otel
```

The launcher's first-run install lives there. Reinstall via
`scripts/install-native-observability.ps1` (Windows) or
`scripts/install-native-observability.sh` (Linux/macOS).

## Cross-links

- [`docs/TROUBLESHOOTING.md`](TROUBLESHOOTING.md) — recurring failure
  modes, including symptom → fix tables for empty `/context`,
  spaCy-model-missing, OTel-noop, Headroom-cold-start, and FTS5-not-linked.
- [`docs/api/context-endpoint.md`](api/context-endpoint.md) —
  `/context` request/response reference, including `caller_model_class`
  (Stage 5) and the `know` / `miss` block contract (Stage 6 / 7).
- [`docs/operator-runbooks.md`](operator-runbooks.md) — full
  calibration + backfill procedures, with timing and rollback steps.
- [`docs/config-reference.md`](config-reference.md) — every
  `cymatix.toml` section, every env-var override, every default value.
- [`docs/agent-sdk-fragment.md`](agent-sdk-fragment.md) — the
  prompt-template fragment teaching frontier agents to honor
  `do_not_answer_from_genome=true` and the `<helix:no_match/>` tag.
- [`docs/architecture/LAUNCHER.md`](architecture/LAUNCHER.md) —
  supervisor + tray + observability stack lifecycle.
- [`docs/architecture/OBSERVABILITY.md`](architecture/OBSERVABILITY.md)
  — Prometheus metrics, Grafana dashboards, alert rules.
- [`deploy/otel/README.md`](../deploy/otel/README.md) — Docker compose
  observability stack (advanced; the default native sidecar covers most
  cases).
