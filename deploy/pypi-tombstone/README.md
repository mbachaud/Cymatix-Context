# helix-context → cymatix-context

**This project was renamed to
[cymatix-context](https://pypi.org/project/cymatix-context/) in v0.8.0
(July 2026).**

`pip install helix-context` only installs `cymatix-context`; it contains no
code, packages, or entry points of its own. Current Cymatix (0.8.5 and later)
does not provide the removed aliases:

- the `helix_context` import;
- the `helix*` console-script names;
- `HELIX_*` environment variables; or
- the `helix.toml` configuration name.

Migrate by switching to `pip install cymatix-context` and the `cymatix*`
names. The knowledge-store format is unchanged — no re-ingest needed.

---

## Publishing this tombstone (maintainer notes)

This directory builds a metadata-only distribution — no packages, modules, or
compatibility aliases ship here (a second implementation would collide on
install).

```bash
cd deploy/pypi-tombstone
python -m build
twine upload dist/*
```

Publishing to the existing `helix-context` PyPI project needs its
credentials (an API token scoped to helix-context, or a trusted
publisher added to that project — the Cymatix-Context publisher does
not cover it).
