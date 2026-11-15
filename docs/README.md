# docs/ — map

Start here instead of grepping. The **wiki** (`wiki/`, published at
<https://cymatixcontext.com/wiki/>) is the maintained user-facing reference:
configuration defaults, HTTP API, CLI, pipeline. Most files under `docs/` are
**dated, point-in-time records**: a filename starting with a date describes the
code as it was on that date. Check the code or the wiki before acting on one.

## Current reference (kept up to date)

| Path | What |
|---|---|
| `SETUP.md`, `TROUBLESHOOTING.md` | Install and fix-it guides |
| `config-reference.md` | Config keys (the wiki `Configuration` page is the primary copy) |
| `RELEASING.md` | Branch model (`beta` → `release/vX.Y.Z` → `master`) and the release steps |
| `clients/` | Per-host MCP and Agent Skill guides (Claude Code, Codex, Gemini CLI, Antigravity) and the CLI reference |
| `api/` | HTTP endpoint and MCP tool reference |
| `architecture/` | Subsystem write-ups: launcher, lanes, session registry, observability, knowledge graph; ADRs in `architecture/adr/` |
| `benchmarks/INDEX.md` | Where each benchmark lives: corpora, needles, profiles, runners, judges |
| `benchmarks/BASELINES.md` | Current baseline numbers per bed, and the ingest-concurrency pin |
| `operator-runbooks.md`, `ops/`, `operations/` | Operator procedures (`ops/` and `operations/` overlap and are candidates to merge) |

## Dated records (read as history)

| Path | What |
|---|---|
| `benchmarks/` | Bench write-ups and verdicts, `YYYY-MM-DD-*.md`. The receipts they cite live in the `mbachaud/cymatix-receipts` repo |
| `research/` | Experiments, refutations, and consensus notes |
| `design/`, `specs/` | Design proposals and specs; check whether they shipped |
| `reviews/` | Code, security and test audits |
| `archive/` | Finished plans, PRDs, councils, investigations and audits, plus superseded docs; not maintained |

`MISSION.md`, `DESIGN_TARGET.md`, and `ROADMAP.md` are positioning documents.
`ROADMAP.md` stops at 0.9.0; the wiki `Roadmap and Releases` page is current.
`ROSETTA.md` is a stub that points to the wiki Lexicon page.

Retired scripts, harnesses and uncited bench data live in the repo-root
`archive/` at their original relative path (`scripts/x.py` becomes
`archive/scripts/x.py`); see `archive/README.md`.
