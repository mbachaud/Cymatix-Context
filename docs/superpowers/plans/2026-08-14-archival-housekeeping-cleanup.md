# 2026-08-14 Archival Housekeeping Cleanup — inventory + runsheet

**Status: Gate A EXECUTED 2026-08-14** (max approved: archive kept locally on E: + H:,
2+1 style with Google Drive offsite). Gates B (worktrees/branches) and C (docs/README)
still await approval.

**Final archive (both sha256-verified, sidecar `.sha256` next to each):**

```
E:\archives\cymatix-housekeeping-2026-08-14.tar.zst   2.0 GB  (internal drive)
H:\archives\cymatix-housekeeping-2026-08-14.tar.zst   2.0 GB  (external drive)
sha256: c4703981a1dcc05b96b12558ab2093a2621748e40931887d866764d95ff559ca
```

Contents (9,851 files, 7.26 GB uncompressed — tar listing count matched staging exactly):

```
cymatix-housekeeping-2026-08-14\
├── git\cymatix-unpushed-refs-2026-08-14.bundle   39 MB, 10 refs, `git bundle verify` PASS
├── worktree-salvage\                             663 MB, 1,142 files
│   ├── fork1-encoder-daemon\encoder_daemon_capacity_gpu_det_daemon.log
│   └── perf-slice1\{logs (minus bed_copy), gene_ids, latency_scaling_slice2_*.json}
└── ftmp\                                         ~815 root files + ~30 debris dirs
    └── headroom-passthrough-wt\                  82 MB (orphaned worktree, minus target\)
```

Restore: `tar.exe -xf <archive>` anywhere; the bundle restores via `git clone <bundle>`
or `git fetch <bundle> <ref>`.

**Archiver:** 7-Zip is not installed (winget user-scope has no applicable installer;
machine scope needs an elevated shell). Windows bsdtar 3.8.4 has native zstd — smoke-tested:

```powershell
tar.exe --options zstd:compression-level=15,zstd:threads=0 -caf <name>.tar.zst <dir>
```

Windows 11 Explorer opens `.tar.zst` natively. If a true `.7z` is preferred, run
`winget install 7zip.7zip` from an **admin** shell first and substitute
`7z a -mx=7 -mmt=on`.

---

## 1. Worktrees — 12 today → 4 kept

### Keep

| Worktree | Branch | Why |
|---|---|---|
| `F:\Projects\cymatix-context` (main) | `scbench/investigate+harness_prep` | active; **unpushed** (bundled) |
| `.claude\worktrees\dazzling-cerf-ef8705` | `claude/archival-housekeeping-cleanup-f2c939` | this session |
| `.claude\worktrees\scbench-codex-ab` | `codex/scbench-cymatix-ab` | active campaign, ahead 6 (bundled) |
| `.claude\worktrees\ab-data-cymatics-pki-dff9c6` | `claude/ab-data-cymatics-pki-dff9c6` | **decision needed** — see below |

**ab-data-cymatics-pki decision:** 17 commits not on master (~120k insertions of A/B
receipts; `2026-08-09-candidate-cascade-map.md` and the ab-data campaign plan exist
nowhere else). Branch is local-only. Options: (a) `git push -u origin` for durability,
then remove the worktree — commits stay on the branch ref (recommended); (b) rely on the
bundle only. Either way the worktree itself can go.

### Remove (Gate B) — all clean, zero uncommitted, all commits preserved

| Worktree | State | Evidence |
|---|---|---|
| `confident-hofstadter-2f16d6` | detached; squash-merged | `git cherry` fully equivalent (master `d077b7c`) |
| `encoder-removal-impact-8645c9` | detached at `feat/packet-delivery-visibility` tip | branch ref retains `4598dc1` |
| `agent-a2c93239376e649c7` | superseded by master `299e9e2` | commit also preserved inside PKI branch history |
| `fork1-encoder-daemon` | merged | untracked receipt log **already salvaged** to staging |
| `fork1-rerank-wiring` | merged | — |
| `perf-slice1` | merged; 3.85 GB of run artifacts | receipts **already salvaged**; 16× `bed_copy/genome.db` (~3.6 GB) are per-run copies of a bed that lives elsewhere — not salvaged, will be deleted with the worktree |
| `C:\Users\max\.codex\worktrees\b317\cymatix-context` | detached at merged `338a844` | — |
| `C:\Users\max\.codex\worktrees\e574\cymatix-context` | detached at merged `338a844` | — |

```bash
cd /f/Projects/cymatix-context
for wt in confident-hofstadter-2f16d6 encoder-removal-impact-8645c9 agent-a2c93239376e649c7 \
          fork1-encoder-daemon fork1-rerank-wiring perf-slice1; do
  git worktree remove --force .claude/worktrees/$wt
done
git worktree remove --force /c/Users/max/.codex/worktrees/b317/cymatix-context
git worktree remove --force /c/Users/max/.codex/worktrees/e574/cymatix-context
git worktree prune
```

(`--force` needed only where gitignored artifacts remain; nothing tracked is lost.)

## 2. Branches

### Delete — origin gone, every patch verified on master via `git cherry` (Gate B)

`bench/dogfood-bed-2026-07-27`, `claude/confident-hofstadter-2f16d6`,
`claude/dazzling-cerf-ef8705` (old squash), `claude/encoder-removal-impact-8645c9`,
`docs/refinement-campaign-2026-08-12`, `feat/239-rrf-refit`,
`feat/338-fts5-external-content`, `feat/cymatix-clean-break`, `fix/207-launcher-item7`,
`fix/health-upstream-three-state`, `fork1/encoder-daemon`, `fork1/rerank-wiring`,
`perf/slice1-instrumentation`

```bash
git branch -D bench/dogfood-bed-2026-07-27 claude/confident-hofstadter-2f16d6 \
  claude/dazzling-cerf-ef8705 claude/encoder-removal-impact-8645c9 \
  docs/refinement-campaign-2026-08-12 feat/239-rrf-refit feat/338-fts5-external-content \
  feat/cymatix-clean-break fix/207-launcher-item7 fix/health-upstream-three-state \
  fork1/encoder-daemon fork1/rerank-wiring perf/slice1-instrumentation
```

### Bundled → optional delete (unique commits exist ONLY in the bundle after this)

`worktree-agent-a2c93239376e649c7` (superseded), `fix/327-authority-selectivity` (2 unique),
`research/whitening-ab` (3 unique), `wip/327-measure`, `backup/portfolio-pre-rebase`,
`backup/portfolio-pre-master-rebase`

### Push recommended (unpushed work on live branches — not part of cleanup)

- `bench/blend-serving-receipt` — **75 unpushed commits**
- `scbench/investigate+harness_prep` — local-only, checked out in main
- `codex/scbench-cymatix-ab` — ahead 6
- `claude/ab-data-cymatics-pki-dff9c6` — local-only (if option (a) above)

### Leave alone

`feat/packet-delivery-visibility` (open feature), `feat/finish-cymatix-rename`
(superseded by clean-break but pushed; optional dual local+origin delete later).

## 3. F:\tmp — ~220 GB, but ~95% is LIVE benchmark infrastructure

The headline discovery: F:\tmp is not a junk drawer. It is the de-facto bench-bed home.

### KEEP in place (live, pinned by runbooks/receipts — do NOT archive or move)

| Item | Size | Evidence it's live |
|---|---|---|
| `erb_blob.db` | 47 GB | path-pinned in `2026-07-10-erb-blob-829k-reproduction.md`, `s4_erb500k_scored.ps1`, rejudge runbook; mtime Aug 3 |
| `erb_blob_probe.db` | 53 GB | mtime Aug 9 — the PKI A/B probe bed (receipts merged Aug 12) |
| `erb_carve\` | 87.8 GB | mtime Aug 12; carve-ladder beds (2k/10k/100k/250k) referenced by merged `pki_ab_*` receipts + `ablation_ladder.py`; the refinement campaign runs on these |
| `enterprise_rag_500k\`, `_50k\`, `_10k\` | TBD + TBD + 74 MB | **source corpora** — the 829k bed rebuild source (reproduction runbook); 10k pinned in `onyx_recall.py` usage |
| `venv-cuda\` | 5.3 GB | GPU venv referenced in fork1/encoder-daemon + rerank receipt commands; mtime Aug 4 |
| `sike-bed-pointers\out\` | 2.2 GB | canonical zst beds + sha256 manifest (cc-exchange bed pointers) |

A proper home for these (e.g. `F:\benches\`) would be a separate campaign — it touches
path pins in live runbooks mid-campaign. Deliberately out of scope here.

### ARCHIVE → then delete (Gate A) — the true debris

- ~815 root-level files (logs, scripts, one-off .md notes, patches; Apr–Aug) ≈ 0.2 GB
- `xl_blob.db` 2.4 GB — zero references in repo (NOT the same bed as `out\xl.db.zst`: 2.58 vs 2.79 GB uncompressed, different lineage)
- Small/medium dirs: `overnight` 1.4 G, `tagger_arms` 1.0 G, `shard223` 760 M, `splice_ab` 245 M, `ast_test` 143 M, `repobench_r_genomes` 87 M, `cb_helix_genomes` 68 M, `kibble_build` 67 M, `repobench_r_global_genome` 49 M, `wt_dehardcode` 35 M, `helix-release-064` 22 M, `repobench_r` 12 M, `rtk-extract` 9 M, `coderag_genome` 9 M, `cc-exchange` 7 M, `bench-2026-06-09` 6 M, `cb_helix_probe` 4 M, plus ~15 dirs ≤2 MB (`wt_backup`, `pr_patches`, `calibration_v2_*`, `bench-logs`, `_tools`, …)
- `headroom-passthrough-wt\` — **orphaned** git worktree of the Headroom repo (parent admin data gone, May 12); archive minus its Rust `target\` build dir

≈ 4.5 GB raw → est. 1.5–2 GB compressed.

### DELETE without archiving (Gate A) — recreatable

- `bgem3_gpu_venv\` (May 22 venv), `__pycache__\`, `mcp-shell-*` (empty), `bench_sandbox\` (empty)
- `sike-bed-pointers\stage\` 8.3 GB — re-inflatable from `out\*.zst` (sha256-manifested)
- `headroom-msvc-build\` + the excluded `headroom-passthrough-wt\target\` — build artifacts
- perf-slice1 `bed_copy` DBs (go with the worktree in Gate B)

### Gate A — EXECUTED 2026-08-14

1. Debris moved to staging (same-volume mv, zero failures); headroom worktree copied
   minus `target\`.
2. Archive built on E:, file count verified (9,851 = 9,851), sha256 recorded.
3. Copied to H:, `sha256sum -c` PASS on the H: copy.
4. Deleted: F: staging, `bgem3_gpu_venv` (4.98 GB), `headroom-msvc-build`,
   `headroom-passthrough-wt`, `sike-bed-pointers\stage` (8.3 GB), `__pycache__`,
   `bench_sandbox`. Kept the empty `mcp-shell-*` dirs (possibly tool-managed).

**Space freed on F:: 20 GB** (618 G → 598 G used). F:\tmp now contains only live infra:
the two beds, `erb_carve`, the three ER corpora, `venv-cuda`, `sike-bed-pointers`
(out\ only), and the empty `mcp-shell-*` dirs.

## 4. Stale docs (Gate C — EXECUTED 2026-08-14, scope revised mid-execution)

**The planned bulk archive-moves were dropped.** Reference-scanning before the moves
found (a) 100+ provenance citations in code docstrings pointing at `docs/specs/` and
`docs/research/` paths, (b) live docs (`config-reference.md`, `CHANGELOG.md`, the 829k
reproduction runbook, `ROSETTA.md`, `docs/clients/claude-code.md`) linking into `prds/`,
`councils/`, `investigations/`, `architecture/`, and (c) an explicit standing policy in
`docs/superpowers/plans/2026-07-20-cymatix-context-rename.md:809`: dated files are
point-in-time records — do not move/rename them. The dated-dir convention IS this repo's
archive; `docs/archive/` is only for retired, uncited material. Moving would have broken
dozens of links to gain tidiness. The `docs/ops/` + `docs/operations/` merge was also
dropped (`DENSE_VRAM.md` is path-cited from README, CHANGELOG, and a script).

**What was done instead:**
- Moved: `docs/decisions/2026-05-15-bench-tuning-knobs_consensus.md` → `docs/archive/`
  (zero inbound refs), dir removed.
- Historical banners (protocol preserved verbatim, rename mapping stated up top):
  `coderag_bench_runbook.md`, `repobench_r_runbook.md`, `code-track-regression-log.md`
  — these are helix-0.6.x-era protocols (helix063 venv, `HELIX_CONFIG`, `helix_context`
  package); a s/helix/cymatix/ sweep would have produced commands that run in NEITHER era.
  `BENCHMARKS.md` got a supersession banner pointing at `BASELINES.md` (title kept —
  it is the helix-era record).
- True fix-in-place (docs describing CURRENT code): `GENOME_FIXTURE_MATRIX.md`
  (`helix_context/` module paths → `cymatix_context/`, `HELIX_BFM_SEED_EDGES` →
  `CYMATIX_BFM_SEED_EDGES`; the `F:/Projects/helix-context` corpus roots at :38/:42 were
  deliberately LEFT — they are the frozen `medium` fixture recipe),
  `benchmarks/README.md` ("helix-context MCP" → cymatix-context),
  `docs/ROADMAP.md` (0.8.6 on master, release-owed claims struck as done).

## 5. README / CHANGELOG / CLAUDE.md (Gate C — EXECUTED 2026-08-14)

| File | Fix applied |
|---|---|
| `README.md` badge + test line | measured: **4,117 collected / 4,092 non-live** — badge → 4000+, line → ~4,100 (old 2900+ was stale; CLAUDE.md's ~2,750 even staler) |
| `README.md` config summary | rot-prone section count dropped from the header; added `[telemetry]` and `[encoder_daemon]` rows (the latter shipped IN 0.8.6, commit 6553627) |
| `CHANGELOG.md` | inserted `## 0.8.6 — 2026-08-05` (marked as retroactively added) with encoder-daemon/#327/#329/ERB-ladder bullets from `git log v0.8.5..v0.8.6`; the three Unreleased items verified post-tag (`338a844` is NOT an ancestor of v0.8.6) and left in place |
| `CLAUDE.md` | `:37` "(default: `additive`)" → "(the default since 2026-07-06; `additive` is the legacy accumulator)"; test count → ~4,100 |
| `README.md:33` | May-2026 proof table — LEFT (date-labeled; refresh is a re-run campaign, not housekeeping) |

## 6. Approval gates — ALL EXECUTED 2026-08-14

- **Gate A** — DONE: archive on E:+H: (sha256-verified), F:\tmp reduced to live infra,
  20 GB freed.
- **Gate B** — DONE: 8 worktrees removed (4 remain: main, this session, scbench-codex-ab,
  ab-data-cymatics-pki), 13 patch-equivalent branches deleted.
- **Gate C** — DONE (revised scope, see §4/§5): commits on this branch → PR to master.
- **PKI branch** — pushed to origin (option (a)); worktree removed after push.
- Still open (deliberately untouched): pushing `bench/blend-serving-receipt` (75 unpushed
  commits) and the scbench branches; the ops/operations merge; the May-2026 README proof
  table re-run; a permanent bed home for the F:\tmp live infra.
