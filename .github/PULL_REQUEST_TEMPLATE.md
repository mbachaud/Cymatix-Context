<!--
Base branch: `beta` for everything except a release cut (`release/vX.Y.Z`) or
a hotfix (`hotfix/*`), which target `master`. See docs/RELEASING.md.
-->

## What

<!-- One paragraph: what changes and why. Link the issue it closes. -->

## Checklist

- [ ] Base branch is `beta` (or this is a `release/**` / `hotfix/**` PR to `master`)
- [ ] `CHANGELOG.md` has an entry under `## Unreleased` (skip only for pure bench or CI changes that ship nothing)
- [ ] Any shipped-default change carries its paired receipt path and a `docs/benchmarks/BASELINES.md` row; the knob and its flip are separate PRs
- [ ] Tests ship with the change; `python -m pytest tests/ -m "not live"` passes locally
- [ ] Docs and wiki updated where the change is user-visible (`wiki/`, `docs/config-reference.md`, `CLAUDE.md`)

## Contributor sign-up

First PR? The **Contributor sign-up** check asks you to post one sentence as a
comment; the exact wording is in `CONTRIBUTING.md` under "Pull requests".
