# helix-context → cymatix-context

**This project was renamed to
[cymatix-context](https://pypi.org/project/cymatix-context/) in v0.8.0
(July 2026).**

`pip install helix-context` now installs `cymatix-context`, which includes:

- the `helix_context` import shim — old imports keep working and emit a
  `DeprecationWarning`;
- the deprecated `helix` / `helix-server` / `helix-launcher` /
  `helix-status` / `helix-vault` console-script aliases;
- `HELIX_*` environment variables honored alongside the canonical
  `CYMATIX_*` names;
- `helix.toml` still loading as a config fallback.

Migrate by switching to `pip install cymatix-context` and the `cymatix*`
names. The knowledge-store format is unchanged — no re-ingest needed.

---

## Publishing this tombstone (maintainer notes)

This directory builds a metadata-only distribution — no packages ship
(the real `helix_context` shim comes from cymatix-context's wheel; a
second copy here would collide on install).

```bash
cd deploy/pypi-tombstone
python -m build
twine upload dist/*
```

Publishing to the existing `helix-context` PyPI project needs its
credentials (an API token scoped to helix-context, or a trusted
publisher added to that project — the Cymatix-Context publisher does
not cover it).
