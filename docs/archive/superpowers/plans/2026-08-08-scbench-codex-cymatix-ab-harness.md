# SCBench Codex/Cymatix A/B Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible, subscription-backed SCBench A/B harness that compares stock Codex with a deterministic Cymatix closed loop and reports paired regression-resistance outcomes.

**Architecture:** Reusable treatment logic lives under `cymatix_context.integrations.scbench`; it owns workspace synchronization, a sidecar Cymatix server, packet rendering, receipts, orchestration, and analysis. A thin fork of SCBench registers `CymatixCodexAgent`, a host-side subclass of SCBench's existing `CodexAgent`, so the same Docker image, ChatGPT subscription mount, CLI invocation, model, and evaluation pipeline remain intact. The adapter wraps each checkpoint immediately before and after `CodexAgent.run()`, before SCBench creates or evaluates the snapshot, preventing evaluation data from entering Cymatix.

**Tech Stack:** Python 3.11+, Pydantic 2, httpx, FastAPI/Cymatix HTTP APIs, pytest, tiktoken `o200k_base`, SCBench at `06b5c0687d4c05ee502e9696a4d0c22fc1eec5e0`, Codex CLI, Docker Desktop, JSON/JSONL receipts.

## Global Constraints

- Execute this plan only after rebasing the benchmark worktree on the merged results of Cymatix Context #338 and #204.
- Use the fork `mbachaud/slop-code-bench` on branch `cymatix/codex-ab`, created from upstream commit `06b5c0687d4c05ee502e9696a4d0c22fc1eec5e0`; never modify the upstream checkout in place.
- Both arms must use the same ChatGPT subscription login, Codex CLI version, model, reasoning effort, Docker image, prompt template, problem revision, and sequential host execution.
- Arm A uses SCBench's unmodified `CodexAgent`; Arm B subclasses that class but may only change the prompt and perform Cymatix side effects around `super().run()`.
- Checkpoint 1 receives no packet. Checkpoints 2+ receive one deterministic packet with `max_genes=8`, `include_raw=true`, `max_item_chars=2000`, and at most 4,000 `o200k_base` tokens.
- Arm B uses `read_only=false`, `ignore_delivered=true`, a unique problem-run `session_id`, SPLADE disabled, ribosome disabled, BGE-M3 dense encoding disabled (`[retrieval] dense_embedding_enabled = false` and `[ingestion] dense_embed_on_ingest = false` — both default on and must be pinned off), and model-free Cymatix retrieval.
- Only agent-visible checkpoint prompts, final Codex agent messages, and allowlisted workspace files may be ingested. Hidden tests and hidden tests' paths, grader output, evaluation artifacts, tool traces, reasoning items, credentials, benchmark receipts, and genomes are prohibited.
- Every HTTP request must have an explicit 10–30 second timeout. Every caught broad exception must use `except Exception:` and log at warning level or higher.
- Every Python `subprocess.Popen` and `subprocess.run` call must pass `creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)` on Windows.
- A Cymatix miss is a valid treatment observation. An ingest, packet, rendering, receipt, version, or integrity failure invalidates the pair and must never fall back to Arm A behavior.
- Scored runs are sequential with SCBench `--num-workers 1` and concurrent evaluation disabled.
- Keep the Claude-authored preparation note `docs/benchmarks/2026-08-08-scbench-ab-harness-prep.md` untouched unless the user separately asks to commit or revise it.

---

## File and ownership map

### Cymatix Context repository

- `cymatix_context/integrations/scbench/config.py` — frozen campaign, treatment, problem, and pair models.
- `cymatix_context/integrations/scbench/workspace.py` — allowlisted scanning, stable source IDs, hashes, manifest deltas, and path safety.
- `cymatix_context/integrations/scbench/client.py` — typed Cymatix HTTP operations with response verification.
- `cymatix_context/integrations/scbench/server_process.py` — fresh-genome sidecar lifecycle and model-free config generation.
- `cymatix_context/integrations/scbench/packet.py` — deterministic query construction, live-source filtering, rendering, and token ceiling.
- `cymatix_context/integrations/scbench/receipts.py` — receipt models, secret checks, content hashes, and atomic persistence.
- `cymatix_context/integrations/scbench/coordinator.py` — checkpoint state machine joining sync, retrieval, learning, and receipts.
- `cymatix_context/integrations/scbench/agent.py` — optional SCBench subclass loaded only when `slop_code` is installed.
- `cymatix_context/integrations/scbench/runner.py` — paired AB/BA SCBench subprocess orchestration and clean resume.
- `cymatix_context/integrations/scbench/analysis.py` — SCBench result loading, paired estimands, clustered bootstrap, and gates.
- `cymatix_context/integrations/scbench/cli.py` — `preflight`, `run-pair`, `analyze`, and `verify-receipts` commands.
- `benchmarks/scbench/campaigns/2026-08-08-codex-pilot/manifest.json` — frozen eight-problem pilot.
- `benchmarks/scbench/contracts/` — pinned upstream/API contract receipts.
- `benchmarks/scbench/fixtures/` — non-scored qualification workspaces and packet goldens.
- `benchmarks/scbench/README.md` — operator runbook.

### SCBench fork

- `src/slop_code/agent_runner/agents/codex_cymatix/__init__.py` — re-export and registration import.
- `src/slop_code/agent_runner/agents/__init__.py` — import the optional Cymatix adapter before building `AgentConfigType`.
- `configs/agents/codex_cymatix.yaml` — treatment agent config using the same Codex version and limits as control.
- `tests/agent_runner/agents/codex_cymatix_agent_test.py` — adapter lifecycle and prompt-symmetry tests.

---

### Task 1: Rebase audit and frozen dependency contracts

**Files:**
- Create: `benchmarks/scbench/contracts/cymatix-contract.json`
- Create: `benchmarks/scbench/contracts/scbench-06b5c068.json`
- Create: `tests/test_scbench_contracts.py`
- Modify: `docs/benchmarks/2026-08-08-scbench-codex-cymatix-ab-design.md`

**Interfaces:**
- Consumes: rebased Cymatix worktree; upstream SCBench commit `06b5c0687d4c05ee502e9696a4d0c22fc1eec5e0`.
- Produces: machine-checked route and lifecycle contracts consumed by all later tasks.

- [ ] **Step 1: Rebase and record the exact Cymatix base**

Run in the isolated benchmark worktree:

```bash
git fetch origin
git rebase origin/master
git merge-base --is-ancestor f675439 HEAD
git log --oneline --grep="#338" HEAD
git rev-parse HEAD
```

Expected: the #204 commit (`f675439`, the SPLADE scale-curve bench commit) is an ancestor, and the #338 merge appears in HEAD's own history — the `--grep` search is deliberately scoped to `HEAD` because an `--all` search matches unmerged refs and proves nothing about the pinned base; confirm the matched merge with `git merge-base --is-ancestor <338-merge-sha> HEAD`. `git rev-parse HEAD` returns the commit to store as `cymatix_commit`.

- [ ] **Step 2: Write the failing contract test**

```python
def test_scbench_contracts_pin_required_surfaces():
    cymatix = json.loads(CYMATIX_CONTRACT.read_text(encoding="utf-8"))
    scbench = json.loads(SCBENCH_CONTRACT.read_text(encoding="utf-8"))
    assert cymatix["routes"]["ingest"] == "/ingest"
    assert cymatix["routes"]["packet"] == "/context/packet"
    assert cymatix["routes"]["tombstone"] == "/admin/genes/tombstone"
    assert cymatix["health_ready_path"] == ["checks", "genome_ready"]
    assert scbench["commit"] == "06b5c0687d4c05ee502e9696a4d0c22fc1eec5e0"
    assert scbench["checkpoint_reset"] == "finish_checkpoint(reset_context=True)"
    assert scbench["strict_field"] == "strict_pass_rate"
    assert scbench["isolated_field"] == "isolated_pass_rate"
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/test_scbench_contracts.py -q`

Expected: FAIL because both contract JSON files are absent.

- [ ] **Step 4: Create the contract receipts from inspected source**

`cymatix-contract.json` must contain the rebased commit, route paths, accepted packet fields, health readiness path, packet item fields, session-delivery behavior, tombstone semantics, and the config keys that disable SPLADE, ribosome, and BGE-M3 dense encoding (`dense_embedding_enabled`, `dense_embed_on_ingest`). `scbench-06b5c068.json` must contain the upstream commit, Codex adapter class path, `run()`/`finish_checkpoint()`/`save_artifacts()` hooks, output filenames, and official strict/isolated fields.

If rebased `/context/packet` still ignores `session_id` and `ignore_delivered`, set `packet_session_delivery` to `false` and retain Task 2. If #338 already implements and tests both fields, set it to `true`, record the proving test names, and mark Task 2 as satisfied by those exact tests rather than duplicating the implementation.

- [ ] **Step 5: Add a short contract-reconciliation note to the design**

Add a paragraph under “Cymatix treatment settings” stating that packet session delivery is an implementation prerequisite, and that scored execution is blocked until the contract receipt says `packet_session_delivery=true`.

- [ ] **Step 6: Run the contract and affected server tests**

Run:

```bash
python -m pytest tests/test_scbench_contracts.py tests/test_context_packet.py tests/test_server.py -q -k "packet or health or ingest or tombstone or contracts"
```

Expected: PASS with no collection errors.

- [ ] **Step 7: Commit**

```bash
git add benchmarks/scbench/contracts tests/test_scbench_contracts.py docs/benchmarks/2026-08-08-scbench-codex-cymatix-ab-design.md
git commit -m "test(bench): pin SCBench and Cymatix contracts"
```

### Task 2: Packet session-delivery support

**Files:**
- Modify: `cymatix_context/context_packet.py`
- Modify: `cymatix_context/server/routes_context.py`
- Modify: `tests/test_context_packet.py`
- Modify: `tests/test_server.py`

**Interfaces:**
- Consumes: `session_delivery.already_delivered()`, `session_delivery.log_delivery()`, and `session_delivery.content_hash()`.
- Produces: `build_context_packet(..., session_id: str | None = None, ignore_delivered: bool = False)` and matching `/context/packet` JSON fields.

- [ ] **Step 1: Write failing direct-builder tests**

```python
def test_packet_ignore_delivered_true_repeats_content_and_logs_delivery():
    genome = Genome(":memory:")
    gene = make_gene("stable constraint alpha", domains=["constraint"])
    genome.upsert_gene(gene, apply_gate=False)
    first = build_context_packet(
        "constraint", genome=genome, session_id="run-1",
        ignore_delivered=True, read_only=False,
    )
    second = build_context_packet(
        "constraint", genome=genome, session_id="run-1",
        ignore_delivered=True, read_only=False,
    )
    assert first.verified[0].content == second.verified[0].content
    assert session_delivery.already_delivered(
        genome.conn, session_id="run-1", gene_id=gene.gene_id
    ) is not None


def test_packet_read_only_does_not_log_delivery():
    genome = Genome(":memory:")
    try:
        gene = make_gene("stable constraint beta", domains=["constraint"])
        genome.upsert_gene(gene, apply_gate=False)
        packet = build_context_packet(
            "constraint", genome=genome, session_id="run-ro",
            ignore_delivered=True, read_only=True,
        )
        assert packet.verified
        assert session_delivery.already_delivered(
            genome.conn, session_id="run-ro", gene_id=gene.gene_id
        ) is None
    finally:
        genome.close()
```

- [ ] **Step 2: Run the direct tests to verify they fail**

Run: `python -m pytest tests/test_context_packet.py -q -k "packet_ignore_delivered or packet_read_only"`

Expected: FAIL because `build_context_packet` does not accept the session arguments.

- [ ] **Step 3: Implement packet delivery tracking**

Add the two keyword arguments, use the expressed `ContextItem.content` hash, preserve packet ordering, and write `mode="context_packet"` only when `session_id` is non-empty and `read_only` is false. When `ignore_delivered=false` and the same gene/content hash was already delivered, replace only that item's content with `session_delivery.format_elision_stub(...)`; `ignore_delivered=true` must always retain full content while still logging the delivery.

- [ ] **Step 4: Add failing endpoint plumbing tests**

```python
def test_context_packet_endpoint_forwards_session_delivery(client, monkeypatch):
    captured = {}
    def fake_build(query, **kwargs):
        captured.update(kwargs)
        return ContextPacket(task_type="edit", query=query)
    monkeypatch.setattr(routes_context, "build_context_packet", fake_build)
    response = client.post("/context/packet", json={
        "query": "preserve behavior",
        "session_id": "campaign:pair:replicate",
        "ignore_delivered": True,
        "read_only": False,
    })
    assert response.status_code == 200
    assert captured["session_id"] == "campaign:pair:replicate"
    assert captured["ignore_delivered"] is True
    assert captured["read_only"] is False
```

- [ ] **Step 5: Implement route plumbing and validation**

Normalize `session_id` to a stripped string or `None`, reject non-string values with HTTP 400, parse `ignore_delivered` as a boolean, and pass both fields into `build_context_packet`.

- [ ] **Step 6: Run focused and regression tests**

Run:

```bash
python -m pytest tests/test_context_packet.py tests/test_server.py tests/test_session_delivery.py -q -k "packet or delivered"
```

Expected: PASS.

- [ ] **Step 7: Update the contract receipt and commit**

Set `packet_session_delivery=true` and record the proving test node IDs.

```bash
git add cymatix_context/context_packet.py cymatix_context/server/routes_context.py tests/test_context_packet.py tests/test_server.py benchmarks/scbench/contracts/cymatix-contract.json
git commit -m "feat(context): track packet delivery sessions"
```

### Task 3: Campaign configuration and manifest validation

**Files:**
- Create: `cymatix_context/integrations/scbench/__init__.py`
- Create: `cymatix_context/integrations/scbench/config.py`
- Create: `tests/test_scbench_config.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: contract JSON from Task 1.
- Produces: `CampaignConfig.load(path: Path)`, `PairSpec`, `TreatmentConfig`, `SourcePolicy`, and canonical `config_hash()`.

- [ ] **Step 1: Write failing model tests**

```python
def test_campaign_rejects_non_balanced_orders(tmp_path):
    data = valid_campaign_dict()
    data["replicates"] = [{"id": 1, "order": ["control", "cymatix"]}]
    with pytest.raises(ValueError, match="AB and BA"):
        CampaignConfig.model_validate(data)


def test_campaign_hash_ignores_manifest_location(tmp_path):
    left = CampaignConfig.model_validate(valid_campaign_dict())
    right = CampaignConfig.model_validate(valid_campaign_dict())
    assert left.config_hash() == right.config_hash()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scbench_config.py -q`

Expected: FAIL because the integration package does not exist.

- [ ] **Step 3: Implement the frozen models**

Use Pydantic models with `extra="forbid"`. Define `Arm = Literal["control", "cymatix"]`; require exactly the two replicate orders `("control", "cymatix")` and `("cymatix", "control")`; require unique problem names; require `num_workers == 1`, `concurrent_evaluation is False`, `max_genes == 8`, `max_item_chars == 2000`, and `max_packet_tokens == 4000`. Serialize with sorted keys and compact separators before SHA-256 hashing.

- [ ] **Step 4: Add the benchmark tokenizer dependency**

Add an optional extra without changing core installs:

```toml
scbench = ["tiktoken>=0.9,<1"]
```

- [ ] **Step 5: Run tests and packaging checks**

Run:

```bash
python -m pytest tests/test_scbench_config.py tests/test_packaging.py -q
python -m build --wheel
```

Expected: tests PASS and the wheel contains `cymatix_context/integrations/scbench/config.py`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml cymatix_context/integrations/scbench tests/test_scbench_config.py
git commit -m "feat(bench): define frozen SCBench campaign config"
```

### Task 4: Safe workspace manifest and delta engine

**Files:**
- Create: `cymatix_context/integrations/scbench/workspace.py`
- Create: `tests/test_scbench_workspace.py`

**Interfaces:**
- Consumes: `SourcePolicy` from Task 3.
- Produces: `scan_workspace(root, problem, policy) -> WorkspaceManifest`, `diff_workspace(previous, current) -> WorkspaceDelta`, and `resolve_source_id(root, source_id) -> Path | None`.

- [ ] **Step 1: Write failing allowlist and path-safety tests**

```python
def test_scan_excludes_eval_secrets_and_symlink_escape(tmp_path):
    root = make_workspace(tmp_path)
    (root / "src" / "app.py").write_text("VALUE = 1", encoding="utf-8")
    (root / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (root / "evaluation.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("LEAK = True", encoding="utf-8")
    try_make_symlink(root / "src" / "escape.py", outside)
    manifest = scan_workspace(root, "file-backup", SourcePolicy.default())
    assert set(manifest.sources) == {"repo://file-backup/src/app.py"}


def test_diff_reports_changed_new_and_deleted(tmp_path):
    before = manifest_for({"a.py": "one", "gone.py": "old"})
    after = manifest_for({"a.py": "two", "new.py": "new"})
    delta = diff_workspace(before, after)
    assert delta.changed == ("repo://p/a.py",)
    assert delta.created == ("repo://p/new.py",)
    assert delta.deleted == ("repo://p/gone.py",)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scbench_workspace.py -q`

Expected: FAIL because `workspace.py` does not exist.

- [ ] **Step 3: Implement stable scanning**

Use `Path.rglob`, resolve every candidate, require `candidate.is_relative_to(root.resolve())`, cap files at the configured byte limit, reject NUL bytes, normalize relative paths to forward slashes, and generate `repo://{problem}/{relative}` source IDs. Hash raw bytes with SHA-256, decode UTF-8 strictly, infer `content_type="code"` from the same extension set used by `cymatix_context.cli.cmd_ingest._content_type_for`, and sort all records by source ID.

- [ ] **Step 4: Implement deterministic deltas and live-source resolution**

`WorkspaceDelta.created`, `.changed`, `.deleted`, and `.unchanged` are sorted tuples. `resolve_source_id` accepts only the current problem's `repo://` prefix, normalizes separators, and returns `None` for missing paths, directories, traversal, or symlink escape.

- [ ] **Step 5: Run focused and property tests**

Run: `python -m pytest tests/test_scbench_workspace.py -q`

Expected: PASS, including Windows path cases.

- [ ] **Step 6: Commit**

```bash
git add cymatix_context/integrations/scbench/workspace.py tests/test_scbench_workspace.py
git commit -m "feat(bench): add safe workspace synchronization manifest"
```

### Task 5: Verified HTTP client and fresh-genome server lifecycle

**Files:**
- Create: `cymatix_context/integrations/scbench/client.py`
- Create: `cymatix_context/integrations/scbench/server_process.py`
- Create: `tests/test_scbench_client.py`
- Create: `tests/test_scbench_server_process.py`

**Interfaces:**
- Consumes: Cymatix contracts from Task 1 and `SourceRecord` from Task 4.
- Produces: `CymatixClient`, `IngestVerification`, `PacketResponse`, and `CymatixServerProcess` context manager.

- [ ] **Step 1: Write failing client verification tests**

```python
def test_ingest_rejects_success_without_gene_ids():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ingest"
        return httpx.Response(200, json={"gene_ids": [], "count": 0})
    transport = httpx.MockTransport(respond)
    with pytest.raises(CymatixProtocolError, match="no gene ids"):
        CymatixClient("http://x", timeout_s=20, transport=transport).ingest_source(
            SourceRecord(
                source_id="repo://p/app.py", relative_path="app.py",
                sha256="a" * 64, size_bytes=9, content_type="code",
                content="VALUE = 1",
            )
        )


def test_wait_ready_requires_genome_ready():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "status": "ok", "checks": {"genome_ready": False}, "genes": 0,
    }))
    with pytest.raises(CymatixProtocolError, match="genome_ready"):
        CymatixClient("http://x", timeout_s=10, transport=transport).wait_ready(
            deadline_s=0.01
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scbench_client.py -q`

Expected: FAIL because `client.py` does not exist.

- [ ] **Step 3: Implement the typed client**

Use one `httpx.Client(timeout=httpx.Timeout(timeout_s), transport=transport)`, where `transport: httpx.BaseTransport | None = None` exists solely for deterministic tests. Implement `health()`, `stats()`, `ingest_source()`, `ingest_conversation()`, `tombstone()`, `context_packet()`, and `wait_ready()`. Every response must call `raise_for_status()`, validate its JSON shape, and raise `CymatixProtocolError` on missing fields. Source ingest metadata must set **both** `path` and `source_id` to the canonical `repo://{problem}/{relative}` ID — the server resolves provenance as `metadata.get("path") or metadata.get("source_id")` (`context_manager.py`, `encoding/fragments.py`), so a filesystem value in `path` would shadow the canonical ID, silently break tombstone matching (`WHERE source_id = ?` returns zero rows yet HTTP 200), and fail the renderer's `repo://` scheme filter on every packet. Record the filesystem path under a separate `fs_path` key. Metadata must also contain `problem`, `content_sha256`, and `source_kind="code"`; conversation source IDs use `conversation://{campaign}/{pair}/{replicate}/{checkpoint}`.

- [ ] **Step 4: Write failing sidecar lifecycle tests**

```python
def test_server_process_pins_model_free_config(tmp_path):
    process = CymatixServerProcess.for_run(tmp_path, port=19191)
    text = process.render_config()
    cfg = tomllib.loads(text)
    assert cfg["ingestion"]["splade_enabled"] is False
    assert cfg["ribosome"]["enabled"] is False
    assert cfg["retrieval"]["dense_embedding_enabled"] is False
    assert cfg["ingestion"]["dense_embed_on_ingest"] is False
    assert str(process.genome_path).replace("\\", "/") in text.replace("\\", "/")


def test_spawn_hides_windows_console(monkeypatch, tmp_path):
    popen = Mock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    process = CymatixServerProcess.for_run(tmp_path, port=19191)
    process.start(wait_ready=False)
    assert popen.call_args.kwargs["creationflags"] == getattr(
        subprocess, "CREATE_NO_WINDOW", 0
    )
```

- [ ] **Step 5: Implement sidecar lifecycle**

Create the genome and generated TOML inside the arm receipt directory, reserve a localhost port, set `CYMATIX_CONFIG` and `CYMATIX_GENOME_PATH`, spawn `sys.executable -m uvicorn cymatix_context._asgi:app`, wait for the exact PID and `checks.genome_ready=true`, capture stdout/stderr to files, and terminate/reap the whole process tree on Windows. A stale responder or a genome path that already contains scored data must fail startup.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/test_scbench_client.py tests/test_scbench_server_process.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add cymatix_context/integrations/scbench/client.py cymatix_context/integrations/scbench/server_process.py tests/test_scbench_client.py tests/test_scbench_server_process.py
git commit -m "feat(bench): manage verified Cymatix sidecars"
```

### Task 6: Deterministic query and bounded packet renderer

**Files:**
- Create: `cymatix_context/integrations/scbench/packet.py`
- Create: `tests/test_scbench_packet.py`
- Create: `benchmarks/scbench/fixtures/packet-golden.json`

**Interfaces:**
- Consumes: raw packet JSON, `WorkspaceManifest`, and `TreatmentConfig`.
- Produces: `build_retrieval_query() -> str` and `render_packet() -> RenderedPacket` with text, SHA-256, token count, included IDs, and filtered IDs.

- [ ] **Step 1: Write failing deterministic and contamination tests**

```python
def test_renderer_filters_deleted_source_and_rejects_unknown_scheme():
    live = manifest_for({"src/live.py": "print('live')"})
    packet = packet_with_sources(
        "repo://p/src/live.py", "repo://p/src/deleted.py"
    )
    rendered = render_packet(packet, live, treatment_config())
    assert "live.py" in rendered.text
    assert "deleted.py" not in rendered.text
    assert rendered.deleted_source_filtered == ("repo://p/src/deleted.py",)
    with pytest.raises(PacketIntegrityError, match="unsupported source"):
        render_packet(packet_with_sources("file:///hidden/tests.py"), live,
                      treatment_config())


def test_renderer_never_exceeds_o200k_ceiling():
    rendered = render_packet(oversized_packet(), live_manifest(), treatment_config())
    assert rendered.tokenizer == "o200k_base"
    assert rendered.token_count <= 4000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scbench_packet.py -q`

Expected: FAIL because `packet.py` does not exist.

- [ ] **Step 3: Implement query construction**

Return exactly:

```text
Problem: {problem}
Checkpoint: {checkpoint_index}
Current request:
{original_prompt}

Retrieve prior constraints, decisions, affected code, and behavior that must remain working.
```

Reject prompts containing configured evaluation-root paths before hashing or transmission.

- [ ] **Step 4: Implement rendering and token trimming**

Use `tiktoken.get_encoding("o200k_base")`. Preserve server category and item order, filter absent `repo://` sources, allow the current run's `conversation://` sources, reject every other scheme, render fixed XML delimiters from the design, and drop complete lowest-priority items from the end until the token count is at most 4,000. Record `delivered_item_count` and `budget_dropped_item_count` on `RenderedPacket` and in receipts: at `max_genes=8 × max_item_chars=2000` (~16,000 chars) the 4,000-token ceiling binds before `max_genes` on code-heavy packets, so drops are expected behavior that must be observable per checkpoint, never silent. Never truncate the fixed instruction header or split a UTF-8 code point. A header that alone exceeds the ceiling raises `PacketIntegrityError`.

- [ ] **Step 5: Freeze the golden**

Write a fixture containing one verified source, one conversation item, one stale-risk source, one deleted source, notes, and refresh targets. Assert exact rendered text, item order, filtered IDs, SHA-256, and token count.

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_scbench_packet.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add cymatix_context/integrations/scbench/packet.py tests/test_scbench_packet.py benchmarks/scbench/fixtures/packet-golden.json
git commit -m "feat(bench): render deterministic bounded context packets"
```

### Task 7: Receipt schemas and atomic integrity checks

**Files:**
- Create: `cymatix_context/integrations/scbench/receipts.py`
- Create: `tests/test_scbench_receipts.py`

**Interfaces:**
- Consumes: config, sync, packet, Codex usage, and SCBench result values.
- Produces: `CheckpointReceipt`, `ArmReceipt`, `PairReceipt`, `atomic_write_receipt()`, `verify_receipt_tree()`, and `redact_secrets()`.

- [ ] **Step 1: Write failing receipt tests**

```python
def test_receipt_rejects_credentials_and_auth_contents(tmp_path):
    receipt = valid_checkpoint_receipt()
    receipt.codex["OPENAI_API_KEY"] = "sk-secret"
    with pytest.raises(ReceiptIntegrityError, match="credential"):
        atomic_write_receipt(tmp_path / "checkpoint.json", receipt)


def test_atomic_write_leaves_no_partial_file(monkeypatch, tmp_path):
    target = tmp_path / "receipt.json"
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        atomic_write_receipt(target, valid_checkpoint_receipt())
    assert not target.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scbench_receipts.py -q`

Expected: FAIL because `receipts.py` does not exist.

- [ ] **Step 3: Implement strict Pydantic receipt models**

Include every field listed in the design's receipt section. Use explicit `valid`, `invalidation_reason`, and `retry_of` fields. Store auth only as `auth_type="chatgpt_subscription"`. Store raw prompt/packet/output as content-addressed artifacts with SHA-256 references; never place secret-bearing environment mappings in a model.

- [ ] **Step 4: Implement atomic persistence and verification**

Write UTF-8 JSON to a sibling temporary file, flush and `os.fsync`, then `os.replace`. `verify_receipt_tree()` recalculates all artifact hashes, verifies pair symmetry on the checkpoint-1 initial-workspace hash and the pre-injection base-prompt hashes only (post-checkpoint-1 workspaces and the injected treatment prompt legitimately diverge), checks that every checkpoint has exactly one terminal state, and ensures no Cymatix fields appear in control prompts.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_scbench_receipts.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add cymatix_context/integrations/scbench/receipts.py tests/test_scbench_receipts.py
git commit -m "feat(bench): add tamper-evident SCBench receipts"
```

### Task 8: Closed-loop treatment coordinator

**Files:**
- Create: `cymatix_context/integrations/scbench/coordinator.py`
- Create: `tests/test_scbench_coordinator.py`

**Interfaces:**
- Consumes: `CampaignConfig`, `CymatixClient`, workspace functions, packet renderer, and receipt writer.
- Produces: `TreatmentCoordinator.initialize()`, `.before_checkpoint() -> PreparedPrompt`, `.after_checkpoint()`, `.finish_checkpoint()`, and `.close()`.

- [ ] **Step 1: Write failing checkpoint-state tests**

```python
def test_checkpoint_one_is_aa_sentinel(fake_client, workspace):
    coordinator = coordinator_for(fake_client, workspace)
    coordinator.initialize()
    prepared = coordinator.before_checkpoint("build feature one")
    assert prepared.prompt == "build feature one"
    assert prepared.packet is None
    assert fake_client.packet_calls == []


def test_checkpoint_two_injects_after_verified_learning(fake_client, workspace):
    coordinator = coordinator_for(fake_client, workspace)
    coordinator.initialize()
    coordinator.before_checkpoint("first")
    coordinator.after_checkpoint("first", "implemented first")
    coordinator.finish_checkpoint()
    prepared = coordinator.before_checkpoint("second")
    assert "<cymatix_context version=\"1\">" in prepared.prompt
    assert fake_client.packet_calls[0]["session_id"] == coordinator.session_id
    assert fake_client.packet_calls[0]["ignore_delivered"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scbench_coordinator.py -q`

Expected: FAIL because `coordinator.py` does not exist.

- [ ] **Step 3: Implement initialization and warm-up**

Create a fresh genome/server, wait for readiness, scan and ingest the entire allowlisted workspace, assert initial gene growth (dense encoding is pinned off in the sidecar config, so gene-count checks are sufficient; a configuration with dense enabled would additionally need a non-NULL `embedding_dense_v2` coverage assertion, because ingest encoding soft-fails to NULL), and make one unscored packet call. Record warm-up separately. Any failed verification transitions the coordinator to `INVALID` and makes later calls raise `TreatmentInvalidError`.

- [ ] **Step 4: Implement before-checkpoint behavior**

For checkpoint 1 return the original prompt byte-for-byte. For checkpoint 2+, require the prior sync receipt to be complete, construct/query/render one packet, append exactly two newlines plus the rendered block, and persist prompt/query/packet artifacts before invoking Codex.

- [ ] **Step 5: Implement after-checkpoint learning and sync**

Require a non-empty final `agent_message`, ingest `User:\n{original_prompt}\n\nAssistant:\n{final_response}` as `content_type="conversation"`, rescan the workspace, ingest created/changed files, call `/admin/genes/tombstone` for deleted source IDs, keep the deletion exclusion set even after tombstoning, verify health/count monotonicity, and atomically complete the sync receipt. Do not accept thinking or tool-output text.

- [ ] **Step 6: Add fail-closed tests**

```python
@pytest.mark.parametrize("failure", ["ingest", "packet", "receipt"])
def test_treatment_failure_never_returns_bare_prompt(failure, coordinator):
    coordinator.inject_failure(failure)
    with pytest.raises(TreatmentInvalidError):
        coordinator.before_checkpoint("checkpoint two")
```

- [ ] **Step 7: Run tests**

Run: `python -m pytest tests/test_scbench_coordinator.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add cymatix_context/integrations/scbench/coordinator.py tests/test_scbench_coordinator.py
git commit -m "feat(bench): coordinate closed-loop Cymatix checkpoints"
```

### Task 9: Thin SCBench Codex adapter and fork seam

**Files (Cymatix):**
- Create: `cymatix_context/integrations/scbench/agent.py`
- Create: `tests/test_scbench_agent.py`

**Files (SCBench fork):**
- Create: `src/slop_code/agent_runner/agents/codex_cymatix/__init__.py`
- Modify: `src/slop_code/agent_runner/agents/__init__.py`
- Create: `configs/agents/codex_cymatix.yaml`
- Create: `tests/agent_runner/agents/codex_cymatix_agent_test.py`

**Interfaces:**
- Consumes: SCBench `CodexConfig`, `CodexAgent`, and `AgentCommandResult`; Task 8 coordinator.
- Produces: registered agent type `codex_cymatix` with prompt/CLI symmetry outside the treatment block.

- [ ] **Step 1: Create the fork and branch**

```bash
gh repo fork SprocketLab/slop-code-bench --clone=false --remote=false
git clone https://github.com/mbachaud/slop-code-bench.git ../slop-code-bench-cymatix
cd ../slop-code-bench-cymatix
git checkout -b cymatix/codex-ab 06b5c0687d4c05ee502e9696a4d0c22fc1eec5e0
```

Expected: `origin` is `mbachaud/slop-code-bench`; add upstream only for fetches, and push only to origin.

- [ ] **Step 2: Write failing Cymatix adapter tests**

```python
def test_adapter_calls_super_with_prepared_prompt(monkeypatch, adapter):
    run = Mock()
    monkeypatch.setattr(CodexAgent, "run", run)
    adapter.coordinator.before_checkpoint.return_value = PreparedPrompt(
        original_prompt="cp2", prompt="cp2\n\n<cymatix_context version=\"1\">x</cymatix_context>",
        packet=rendered_packet(),
    )
    adapter.run("cp2")
    # a class-attribute Mock is not a descriptor: super().run(prompt) invokes it
    # unbound, so only the prompt argument is recorded — never self
    run.assert_called_once_with(adapter.coordinator.before_checkpoint.return_value.prompt)


def test_adapter_learns_only_last_agent_message(adapter):
    adapter._last_command = command_with_codex_jsonl(
        thinking="private reasoning", agent_messages=["draft", "final answer"]
    )
    assert adapter.extract_final_agent_message() == "final answer"
```

- [ ] **Step 3: Implement the optional adapter module**

Define `CymatixCodexConfig(CodexConfig)` with `type: Literal["codex_cymatix"]`, `campaign_manifest`, `pair_id`, `replicate`, and `receipt_root`. Define `CymatixCodexAgent(CodexAgent)` with matching constructor fields and override `_from_config`, `setup`, `run`, `finish_checkpoint`, `save_artifacts`, and `cleanup`. `run` must call the coordinator before `super().run()`, parse only `item.completed/item.type="agent_message"` events from `_last_command.stdout`, then call `after_checkpoint` before returning — passing `PreparedPrompt.original_prompt`, never `.prompt`, which contains the injected packet. `save_artifacts` calls `super()` first and adds only Cymatix receipt references. End the module with `register_agent("codex_cymatix", CymatixCodexAgent)`; the config subclass registers itself through `AgentConfigBase.__init_subclass__`.

- [ ] **Step 4: Run Cymatix adapter tests**

Run: `python -m pytest tests/test_scbench_agent.py -q`

Expected: PASS with SCBench installed from the pinned fork in the execution environment; otherwise the entire module test uses `pytest.importorskip("slop_code")` and the separate fork tests remain mandatory.

- [ ] **Step 5: Add the fork registration seam**

The fork package file must contain only:

```python
from cymatix_context.integrations.scbench.agent import CymatixCodexAgent
from cymatix_context.integrations.scbench.agent import CymatixCodexConfig

__all__ = ["CymatixCodexAgent", "CymatixCodexConfig"]
```

Import those names in `agents/__init__.py` before `_build_agent_config_type()` runs. The YAML must copy the pinned control Codex version, timeout, args, environment, and limits; it adds only the manifest/pair/replicate/receipt fields.

- [ ] **Step 6: Run fork adapter and dry-run tests**

Run in the SCBench fork:

```bash
python -m pytest tests/agent_runner/agents/codex_agent_test.py tests/agent_runner/agents/codex_cymatix_agent_test.py -q
slop-code run --agent configs/agents/codex_cymatix.yaml --problem file-backup --dry-run --num-workers 1
```

Expected: all tests PASS; dry-run resolves `codex_cymatix` without starting Codex.

- [ ] **Step 7: Commit both repositories**

Cymatix:

```bash
git add cymatix_context/integrations/scbench/agent.py tests/test_scbench_agent.py
git commit -m "feat(bench): expose SCBench Cymatix Codex adapter"
```

SCBench fork:

```bash
git add src/slop_code/agent_runner/agents configs/agents/codex_cymatix.yaml tests/agent_runner/agents/codex_cymatix_agent_test.py
git commit -m "feat(agent): register Cymatix Codex treatment"
git push origin cymatix/codex-ab
```

### Task 10: Paired AB/BA campaign runner and clean resume

**Files:**
- Create: `cymatix_context/integrations/scbench/runner.py`
- Create: `cymatix_context/integrations/scbench/cli.py`
- Create: `tests/test_scbench_runner.py`
- Create: `tests/test_scbench_cli.py`

**Interfaces:**
- Consumes: campaign/pair models, SCBench checkout, stock and treatment agent configs.
- Produces: `CampaignRunner.preflight()`, `.run_pair()`, `.resume_pair()`, and module CLI.

- [ ] **Step 1: Write failing order and subprocess tests**

```python
def test_pair_runner_inverts_arm_order_by_replicate(fake_subprocess, campaign):
    runner = CampaignRunner(campaign, run_command=fake_subprocess)
    runner.run_pair(PairSpec(problem="file-backup", replicate=1))
    runner.run_pair(PairSpec(problem="file-backup", replicate=2))
    assert fake_subprocess.arm_order == [
        "control", "cymatix", "cymatix", "control"
    ]


def test_scbench_command_is_sequential_and_disables_concurrent_eval(campaign):
    command = build_scbench_command(campaign, pair_spec(), "control")
    assert command[command.index("--num-workers") + 1] == "1"
    assert "concurrent_evaluation=false" in command
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scbench_runner.py tests/test_scbench_cli.py -q`

Expected: FAIL because runner and CLI modules do not exist.

- [ ] **Step 3: Implement preflight**

Check exact Cymatix and SCBench commits, clean fork state, Codex version and `codex login status`, Docker availability, auth file existence without reading its contents, problem/checkpoint counts, model/reasoning symmetry, emptiness of the target `{problem}-r{replicate}` pair directory only (never the campaign root — completed pairs and preserved failed attempts from other pairs must not block execution, and resume validates its preserved attempts instead), contract hashes, and free disk. Emit a machine-readable preflight receipt and refuse scored execution on any mismatch.

- [ ] **Step 4: Implement paired execution**

Invoke `slop-code run` separately for each problem/arm with an argument list, explicit cwd, captured log files, `check=False`, explicit timeout, and the Windows no-window creation flag. Agent configs are passed to `--agent` as YAML paths, matching the documented dry-run commands: control uses the stock `configs/agents/codex.yaml`, treatment uses `configs/agents/codex_cymatix.yaml` (the registered type names inside them are `codex` and `codex_cymatix`). Pin output directories to `{campaign}/pairs/{problem}-r{replicate}/{arm}/scbench`. After each arm, verify terminal artifacts before starting its mate.

- [ ] **Step 5: Implement clean resume**

Resume only at an arm boundary. A partial arm is marked invalid and rerun into a new attempt directory linked by `retry_of`; never use SCBench checkpoint resume for a treatment arm because genome/workspace synchronization cannot be proven from SCBench artifacts alone. Preserve failed attempts.

- [ ] **Step 6: Implement CLI commands**

```text
python -m cymatix_context.integrations.scbench.cli preflight --manifest PATH
python -m cymatix_context.integrations.scbench.cli run-pair --manifest PATH --problem NAME --replicate N
python -m cymatix_context.integrations.scbench.cli verify-receipts --manifest PATH
python -m cymatix_context.integrations.scbench.cli analyze --manifest PATH
```

Return 0 only for complete valid operations, 2 for manifest/preflight errors, 3 for invalidated pairs, and 4 for analysis gate failure.

- [ ] **Step 7: Run tests**

Run: `python -m pytest tests/test_scbench_runner.py tests/test_scbench_cli.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add cymatix_context/integrations/scbench/runner.py cymatix_context/integrations/scbench/cli.py tests/test_scbench_runner.py tests/test_scbench_cli.py
git commit -m "feat(bench): orchestrate paired SCBench arms"
```

### Task 11: Official-result loader and paired regression analysis

**Files:**
- Create: `cymatix_context/integrations/scbench/analysis.py`
- Create: `tests/test_scbench_analysis.py`
- Create: `benchmarks/scbench/fixtures/checkpoint-results-control.jsonl`
- Create: `benchmarks/scbench/fixtures/checkpoint-results-cymatix.jsonl`

**Interfaces:**
- Consumes: SCBench `checkpoint_results.jsonl` fields `strict_pass_rate`, `isolated_pass_rate`, `erosion`, `verbosity`, `input`, and `elapsed` plus pair receipts.
- Produces: `load_arm_results()`, `pair_checkpoints()`, `compute_pilot_summary()`, `clustered_bootstrap()`, and JSON/Markdown summaries.

- [ ] **Step 1: Write failing estimand tests**

```python
def test_regression_event_requires_isolated_pass_and_strict_failure():
    assert regression_event({"isolated_pass_rate": 1.0, "strict_pass_rate": 0.75})
    assert not regression_event({"isolated_pass_rate": 0.5, "strict_pass_rate": 0.0})
    assert not regression_event({"isolated_pass_rate": 1.0, "strict_pass_rate": 1.0})


def test_primary_effect_is_control_minus_cymatix():
    summary = compute_pilot_summary(synthetic_pairs(control_events=4,
                                                    cymatix_events=2,
                                                    isolated_each=8))
    assert summary.control_regression_rate == 0.5
    assert summary.cymatix_regression_rate == 0.25
    assert summary.treatment_effect == 0.25


def test_primary_rates_share_paired_denominator():
    # treatment isolated-solves four extra checkpoints; those discordant cells
    # must not enter the primary denominator and dilute its regression rate
    summary = compute_pilot_summary(synthetic_pairs(control_events=4,
                                                    cymatix_events=4,
                                                    isolated_control=8,
                                                    isolated_cymatix=12,
                                                    isolated_both=8))
    assert summary.paired_denominator == 8
    assert summary.treatment_effect == 0.0
    assert summary.isolated_discordant == 4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scbench_analysis.py -q`

Expected: FAIL because `analysis.py` does not exist.

- [ ] **Step 3: Implement official-field loading and pairing**

Require exact `(problem, checkpoint, replicate)` matches; discard checkpoint 1 only from the primary population, not from secondary reporting. Restrict the primary regression-rate denominators to matched checkpoints where both arms isolated-passed (the design's shared paired denominator); count and report isolated-discordant checkpoints separately. Define pass as `math.isclose(rate, 1.0)`. Missing checkpoints count as operational invalidations, not failures or passes. Validate SCBench commit and metric-version symmetry from receipts.

- [ ] **Step 4: Implement pilot gates**

Calculate net prevented events, relative regression reduction, isolated-solve difference, median paired erosion/verbosity deltas, median token/time ratios, and integrity-failure count. Encode the eight design gates verbatim and report each as `pass`, `fail`, or `not_evaluable`; `not_evaluable` prevents graduation.

- [ ] **Step 5: Implement the full-suite interval**

Use a deterministic seeded paired bootstrap that resamples problems with replacement and retains every checkpoint/replicate inside the sampled cluster. Return percentile 95% bounds for `regression_rate_control - regression_rate_cymatix`; reject samples with no isolated passes and report the rejected count.

- [ ] **Step 6: Run fixture and determinism tests**

Run: `python -m pytest tests/test_scbench_analysis.py -q`

Expected: PASS, including a fixed bootstrap interval snapshot for seed `20260808`.

- [ ] **Step 7: Commit**

```bash
git add cymatix_context/integrations/scbench/analysis.py tests/test_scbench_analysis.py benchmarks/scbench/fixtures/checkpoint-results-*.jsonl
git commit -m "feat(bench): analyze paired regression resistance"
```

### Task 12: Non-scored qualification ladder

**Files:**
- Create: `benchmarks/scbench/fixtures/qualification/src/state.py`
- Create: `benchmarks/scbench/fixtures/qualification/README.md`
- Create: `benchmarks/scbench/campaigns/qualification/manifest.json`
- Create: `tests/test_scbench_e2e.py`
- Modify: `benchmarks/scbench/README.md`

**Interfaces:**
- Consumes: all harness components and the pinned SCBench fork.
- Produces: a local qualification receipt proving the Phase 0 properties without spending scored pilot observations.

- [ ] **Step 1: Add an in-process closed-loop integration test**

```python
def test_closed_loop_remembers_prior_constraint_without_eval_data(tmp_path):
    with live_test_server(tmp_path) as server:
        coordinator = qualification_coordinator(server, tmp_path)
        coordinator.initialize()
        coordinator.before_checkpoint("Keep VALUE compatible")
        coordinator.after_checkpoint("Keep VALUE compatible",
                                     "I preserved VALUE = 1 (decision token QUAL-CONV-7Q4)")
        coordinator.finish_checkpoint()
        prepared = coordinator.before_checkpoint("Add a new accessor")
        # the token exists only in the assistant message, never in any workspace
        # file, so source retrieval alone cannot satisfy this assertion
        assert "QUAL-CONV-7Q4" in prepared.prompt
        assert "evaluation.json" not in prepared.prompt
        assert "hidden" not in prepared.prompt.lower()
```

- [ ] **Step 2: Run the test**

Run: `python -m pytest tests/test_scbench_e2e.py -q -k closed_loop`

Expected: PASS with a real temporary genome and in-process FastAPI server.

- [ ] **Step 3: Add qualification tests for all Phase 0 gates**

Cover subscription preflight with mocked command output, checkpoint reset/workspace persistence from the SCBench contract, initial and incremental ingest, conversation retrieval, failed-write detection, untimed warm-up, deleted-source filtering, hidden-data exclusion, deterministic rendering, genome/session isolation, receipt replay, and rate-limit invalidation.

- [ ] **Step 4: Run the complete local harness suite**

Run:

```bash
python -m pytest tests/test_scbench_contracts.py tests/test_scbench_config.py tests/test_scbench_workspace.py tests/test_scbench_client.py tests/test_scbench_server_process.py tests/test_scbench_packet.py tests/test_scbench_receipts.py tests/test_scbench_coordinator.py tests/test_scbench_agent.py tests/test_scbench_runner.py tests/test_scbench_cli.py tests/test_scbench_analysis.py tests/test_scbench_e2e.py -q
```

Expected: all tests PASS.

- [ ] **Step 5: Run one real, explicitly non-scored Codex qualification pair**

First write `benchmarks/scbench/campaigns/qualification/manifest.json`: a single-problem, single-replicate campaign (problem `file-backup`, order control→cymatix) marked `scored=false`, reusing the pilot's treatment limits verbatim. It exists solely for this gate and is never analyzed with pilot data.

With Docker Desktop running and the fork installed:

```bash
python -m cymatix_context.integrations.scbench.cli preflight --manifest benchmarks/scbench/campaigns/qualification/manifest.json
python -m cymatix_context.integrations.scbench.cli run-pair --manifest benchmarks/scbench/campaigns/qualification/manifest.json --problem file-backup --replicate 1
python -m cymatix_context.integrations.scbench.cli verify-receipts --manifest benchmarks/scbench/campaigns/qualification/manifest.json
```

Expected: control and treatment complete; checkpoint 1 prompts hash-identically; checkpoint 2 differs only by the Cymatix block; no evaluation path or content hash appears in treatment ingest receipts.

- [ ] **Step 6: Document measured warm-up and operational behavior**

Add the qualification command, receipt location, durations, packet ceiling, deletion-filter count, and any rate-limit pause to `benchmarks/scbench/README.md`. Do not copy auth contents or raw credentials.

- [ ] **Step 7: Commit**

```bash
git add benchmarks/scbench/fixtures/qualification benchmarks/scbench/README.md tests/test_scbench_e2e.py
git commit -m "test(bench): qualify the SCBench Cymatix closed loop"
```

### Task 13: Freeze the pilot manifest and tracking surface

**Files:**
- Create: `benchmarks/scbench/campaigns/2026-08-08-codex-pilot/manifest.json`
- Create: `benchmarks/scbench/campaigns/2026-08-08-codex-pilot/README.md`
- Modify: `benchmarks/scbench/README.md`

**Interfaces:**
- Consumes: qualification receipts, exact post-rebase Cymatix commit, pushed SCBench fork commit, installed Codex version, Docker image digest, and verified problem checkpoint counts.
- Produces: immutable pilot inputs and one GitHub campaign issue; no GitHub Project.

- [ ] **Step 1: Generate and validate the manifest**

Set the exact commits and versions from preflight; list:

```json
{
  "easy": ["file-backup", "code-search", "execution-server"],
  "medium": ["database-migration", "trajectory-api", "layered-config-synthesizer"],
  "hard": ["dag-execution", "recli"]
}
```

Include AB/BA replicates, `expected_checkpoints=40`, `primary_checkpoints=32`, all treatment limits, retry rules, bootstrap seed `20260808`, and every graduation threshold from the design. The validation command must independently count checkpoints from the pinned SCBench problem catalog and fail if the manifest's recorded `expected_checkpoints`/`primary_checkpoints` do not match the verified catalog count — on mismatch, update and re-freeze the manifest with the verified totals (per the design, a mismatch invalidates the manifest, not the benchmark). Catalog counts are per single pass; with the AB/BA replicates each arm scores twice these totals (80/64 observations per arm at the expected counts).

- [ ] **Step 2: Run final pre-pilot verification**

Run:

```bash
python -m cymatix_context.integrations.scbench.cli preflight --manifest benchmarks/scbench/campaigns/2026-08-08-codex-pilot/manifest.json
python -m cymatix_context.integrations.scbench.cli verify-receipts --manifest benchmarks/scbench/campaigns/qualification/manifest.json
git diff --check
python -m pytest tests/test_scbench_contracts.py tests/test_scbench_config.py tests/test_scbench_e2e.py -q
```

Expected: every command exits 0.

- [ ] **Step 3: Create one GitHub campaign issue**

Use a body file containing links to the design, this plan, qualification receipt hashes, fork branch, manifest, implementation commits, and the pilot execution checklist.

```bash
gh issue create --repo mbachaud/Cymatix-Context --title "SCBench: run Codex/Cymatix regression-resistance pilot" --body-file benchmarks/scbench/campaigns/2026-08-08-codex-pilot/README.md
```

Do not create a GitHub Project. Add the issue URL to the campaign README and manifest metadata in a follow-up commit.

- [ ] **Step 4: Commit the frozen campaign**

```bash
git add benchmarks/scbench/campaigns/2026-08-08-codex-pilot benchmarks/scbench/README.md
git commit -m "bench: freeze SCBench Codex pilot campaign"
```

- [ ] **Step 5: Stop before scored execution for operator confirmation**

Report the exact campaign commit, fork commit, issue URL, Docker digest, Codex version, expected subscription usage, and estimated runtime. Scored `run-pair` commands begin only after the operator confirms the frozen manifest and Docker Desktop is running.

---

## Post-graduation scope split

This plan intentionally ends with a qualified, frozen deterministic pilot harness. If the pilot graduates, do not expand this implementation in place without two focused follow-on plans:

1. **Full suite plus retrieval-only B0:** reuse the runner, receipts, and analysis modules; add a `retrieval_only` treatment mode that retains source synchronization and packet injection while disabling conversation ingestion, CWoLa mutation, and delivery-tail writes. Freeze the full SCBench problem list before execution and treat B0 as a secondary attribution study, never as part of the primary two-arm pilot estimate.
2. **Live MCP confirmation:** add a separate `codex_cymatix_mcp` agent configuration that exposes Cymatix through the intended MCP tool workflow instead of deterministic prompt injection. Measure tool selection, call compliance, and MCP failures separately; do not pool those results with the deterministic A/B campaign.

Each follow-on starts from the same pinned campaign receipts, receives its own design amendment and test-first implementation plan, and is linked from the campaign issue. This preserves the approved retrieval-only and live MCP phases without adding unqualified branches to the pilot state machine.

---

## Full verification before scored runs

Run in the Cymatix worktree:

```bash
python -m pytest tests/test_context_packet.py tests/test_server.py tests/test_session_delivery.py -q -k "packet or delivered or tombstone"
python -m pytest tests/test_scbench_contracts.py tests/test_scbench_config.py tests/test_scbench_workspace.py tests/test_scbench_client.py tests/test_scbench_server_process.py tests/test_scbench_packet.py tests/test_scbench_receipts.py tests/test_scbench_coordinator.py tests/test_scbench_agent.py tests/test_scbench_runner.py tests/test_scbench_cli.py tests/test_scbench_analysis.py tests/test_scbench_e2e.py -q
python -m cymatix_context.integrations.scbench.cli preflight --manifest benchmarks/scbench/campaigns/2026-08-08-codex-pilot/manifest.json
git diff --check
```

Run in the SCBench fork:

```bash
python -m pytest tests/agent_runner/agents/codex_agent_test.py tests/agent_runner/agents/codex_cymatix_agent_test.py -q
slop-code run --agent configs/agents/codex_cymatix.yaml --problem file-backup --dry-run --num-workers 1
git diff --check
```

The implementation is ready for pilot execution only when both repositories are clean, every command above exits 0, the qualification pair verifies, the contract says `packet_session_delivery=true`, and the campaign manifest contains exact non-empty commit and image digests.
