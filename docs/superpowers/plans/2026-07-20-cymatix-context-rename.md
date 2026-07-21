# Cymatix Context Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the project from `helix-context` to `cymatix-context` (distribution, import package, CLI, config, env vars, MCP server) with full back-compat shims, in one rebase-survivable PR.

**Architecture:** The import package `helix_context/` is `git mv`'d to `cymatix_context/` and all internal imports rewritten by a committed, idempotent codemod script (so the PR can be rebased over inflight work by re-running it). A new thin `helix_context/` shim package aliases every old import path to the *identical* new module objects via a meta-path finder. Env vars stay `HELIX_*` at call sites this PR; a one-shot mirror at package import makes `CYMATIX_*` canonical for users. Config gains `cymatix.toml` as the preferred filename with `helix.toml` fallback.

**Tech Stack:** Python 3.11+, hatchling, pytest, FastMCP.

## Global Constraints

- Work in a dedicated worktree under `.claude/worktrees/` — the main `f:\Projects\helix-context` checkout is contended by concurrent sessions (memory: `env_helix_main_checkout_contended`). Branch name: `rename/cymatix-context`, based on `master`.
- Run pytest as `rtk proxy python -m pytest ...` (bare pytest output is swallowed by the rtk hook; memory: `env_rtk_pytest_proxy`). Native `python`, never `uv run` (Windows).
- Baseline is ZERO test failures on master (memory: `test_failures_local_baseline`). Any failure after a task is caused by that task.
- Old import paths, old env vars (`HELIX_*`), old CLI names (`helix`, `helix-server`, …), old config filename (`helix.toml`), and `python -m helix_context.mcp_server` must all keep working after this PR. Removal happens in a later release, not here.
- Internal *identifier* names (`HelixConfig`, `cli/helix_status.py` module basename, biology lexicon terms) are NOT renamed in this PR — only package/distribution/user-facing surfaces. A follow-up docs+identifier sweep PR handles the rest.
- Do not touch `genomes/`, `tools/native-otel/`, `.claude/worktrees/`, `training/` fixture data.
- Version bumps `0.7.2b1` → `0.8.0` (the rename release).
- Commit messages: prefix `rename:`; end with the Claude co-author trailer per repo convention.
- The GitHub repo is ALREADY renamed (2026-07-20): `mbachaud/Cymatix-Context`, with `mbachaud/helix-context` redirecting. All new-URL references use `https://github.com/mbachaud/Cymatix-Context`. (The `SwiftWing21/helix-context` URLs currently in pyproject were stale even before the rename.)
- PyPI publishing is via a pending trusted publisher bound to: repo `mbachaud/Cymatix-Context`, workflow **`publish.yml`** (Max aligned the publisher to the existing filename 2026-07-20 — do NOT rename the workflow file), environment `pypi`, project name `Cymatix-Context` (PyPI-normalizes equal to `cymatix-context`). Task 6b switches it to OIDC auth.
- Remote `master` carries a direct commit ("Rename project from Helix Context to Cymatix Context") that edits the README title in place. Task 8 replaces README.md entirely, so on any rebase conflict in README.md take the BRANCH version.
- The PR body must state: PR will be rebased over the inflight #230/#231 fix PRs — see the Rebase Runbook section, which must be pasted into the PR body.

---

### Task 1: Worktree + branch setup

**Files:** none created in repo (environment setup).

- [ ] **Step 1: Create isolated worktree**

Use the `superpowers:using-git-worktrees` skill. Target: `.claude/worktrees/cymatix-rename` on new branch `rename/cymatix-context` from `origin/master` (fetch first — HEAD in the main checkout may have moved).

```bash
cd /f/Projects/helix-context
git fetch origin
git worktree add .claude/worktrees/cymatix-rename -b rename/cymatix-context origin/master
cd .claude/worktrees/cymatix-rename
```

- [ ] **Step 2: Verify baseline is green before touching anything**

Run: `rtk proxy python -m pytest tests/ -m "not live" -q`
Expected: 0 failures (~2,900 passed). If not zero, STOP and report — do not build on a red baseline.

- [ ] **Step 3: Editable-install the worktree into the venv you'll test with**

```bash
python -m pip install -e ".[dev]"
```

Expected: `Successfully installed helix-context-0.7.2b1` (name changes later in Task 6).

---

### Task 2: Codemod script + package move + import rewrite

**Files:**
- Create: `scripts/codemod_cymatix_rename.py`
- Rename: `helix_context/` → `cymatix_context/` (via the script, `git mv`)
- Modify: every `*.py` under `cymatix_context/`, `tests/`, `scripts/`, `benchmarks/`, `deploy/` containing the token `helix_context` (~68 + 193 + 49 + 54 files, ~1,780 occurrences — mechanical)

**Interfaces:**
- Produces: importable package `cymatix_context` with identical public API; re-runnable `python scripts/codemod_cymatix_rename.py` used again during every rebase.

- [ ] **Step 1: Write the codemod script**

```python
#!/usr/bin/env python3
"""Idempotent codemod: helix_context -> cymatix_context.

Committed to the repo on purpose: the rename PR is rebased over inflight
work by re-running this script (see the Rebase Runbook in
docs/superpowers/plans/2026-07-20-cymatix-context-rename.md). Running it
twice is a no-op.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD_PKG, NEW_PKG = "helix_context", "cymatix_context"
# NEW_PKG first: after the move, rewrites target the new tree. The
# back-compat shim dir (a later re-created helix_context/) is deliberately
# NOT listed — its references to the old name are intentional.
CODE_DIRS = [NEW_PKG, "tests", "scripts", "benchmarks", "deploy"]
SKIP_PARTS = {".claude", "tools", "node_modules", ".venv", "__pycache__", ".git", "genomes"}


def move_package() -> None:
    old_dir = ROOT / OLD_PKG
    new_dir = ROOT / NEW_PKG
    if old_dir.is_dir() and not new_dir.exists():
        subprocess.run(["git", "mv", OLD_PKG, NEW_PKG], cwd=ROOT, check=True)
        print(f"moved {OLD_PKG}/ -> {NEW_PKG}/")


def rewrite_imports() -> int:
    pat = re.compile(rf"\b{OLD_PKG}\b")
    changed = 0
    for d in CODE_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for py in base.rglob("*.py"):
            if SKIP_PARTS & set(py.parts):
                continue
            text = py.read_text(encoding="utf-8")
            new = pat.sub(NEW_PKG, text)
            if new != text:
                py.write_text(new, encoding="utf-8")
                changed += 1
    return changed


if __name__ == "__main__":
    move_package()
    n = rewrite_imports()
    print(f"codemod complete: {n} files rewritten")
    sys.exit(0)
```

- [ ] **Step 2: Run it**

Run: `python scripts/codemod_cymatix_rename.py`
Expected: `moved helix_context/ -> cymatix_context/` then `codemod complete: ~360 files rewritten`.

- [ ] **Step 3: Run it AGAIN to prove idempotence**

Run: `python scripts/codemod_cymatix_rename.py`
Expected: `codemod complete: 0 files rewritten`.

- [ ] **Step 4: Reinstall + full suite**

```bash
python -m pip install -e ".[dev]"
rtk proxy python -m pytest tests/ -m "not live" -q
```

Expected: install may still say `helix-context` (pyproject not yet touched) but must succeed — if hatchling errors with "Unable to determine which files to ship" it cannot find a package matching the project name; fix by adding NOW (this is expected — pyproject still names `helix-context` but the dir moved):

```toml
[tool.hatch.build.targets.wheel]
packages = ["cymatix_context"]
```

Then reinstall and re-run. Expected: 0 failures. Old-path imports are BROKEN at this commit (shim lands next task) — that is fine within the branch.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "rename: move helix_context -> cymatix_context via committed codemod"
```

---

### Task 3: `helix_context` back-compat shim package

**Files:**
- Create: `helix_context/__init__.py`
- Create: `helix_context/mcp_server.py`
- Test: `tests/test_cymatix_rename.py` (new)

**Interfaces:**
- Consumes: importable `cymatix_context` (Task 2).
- Produces: `import helix_context.X` yields the SAME module object as `import cymatix_context.X` for every submodule; `python -m helix_context.mcp_server` still launches the MCP stdio loop.

- [ ] **Step 1: Write the failing tests**

```python
"""Rename back-compat contract: old helix_context paths alias, not copy."""
import subprocess
import sys
import warnings


def _purge_old_modules():
    for mod in [m for m in list(sys.modules) if m.split(".")[0] == "helix_context"]:
        del sys.modules[mod]


def test_old_package_import_warns():
    _purge_old_modules()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        import helix_context  # noqa: F401
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)


def test_old_import_is_same_module_object():
    import cymatix_context.config as new_cfg
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import helix_context.config as old_cfg
    assert old_cfg is new_cfg


def test_deep_from_import_is_same_module_object():
    from cymatix_context.retrieval import freshness as new_mod
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from helix_context.retrieval import freshness as old_mod
    assert old_mod is new_mod


def test_existing_module_shims_still_work():
    # genome.py / ribosome.py / server.py re-export shims from the pre-rename
    # era must survive the second rename layer.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import helix_context.genome  # noqa: F401
        import helix_context.mcp_server as old_mcp
    import cymatix_context.mcp.mcp_server as new_mcp
    assert old_mcp is new_mcp


def test_mcp_dash_m_entry_importable_via_old_path():
    proc = subprocess.run(
        [sys.executable, "-c",
         "import helix_context.mcp_server as m; print(m.__name__)"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `rtk proxy python -m pytest tests/test_cymatix_rename.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'helix_context'`.

- [ ] **Step 3: Write `helix_context/__init__.py`**

Design notes baked into this code: the shim module is NOT replaced in `sys.modules` (it keeps its own `__path__`, so `python -m helix_context.mcp_server` resolves the real shim file below via the normal path finder). All *other* submodules are aliased by a meta-path finder to the identical `cymatix_context` module objects, so isinstance checks and module-level singletons hold across both paths. `helix_context.mcp_server` is excluded from aliasing because `python -m` must execute real file code under `__name__ == "__main__"`.

```python
"""Backward-compat namespace: ``helix_context`` -> ``cymatix_context``.

The project was renamed to cymatix-context (July 2026). Every
``helix_context[.sub]`` import resolves to the *identical*
``cymatix_context`` module object — no copies — so isinstance checks and
module singletons keep working across old and new import paths. This
package will be removed after a deprecation window.
"""
import importlib
import importlib.abc
import importlib.util
import sys
import warnings

_OLD = "helix_context"
_NEW = "cymatix_context"
# Real files shipped in this shim dir (needed for ``python -m``): let the
# normal path finder handle them instead of aliasing.
_REAL_FILES = {f"{_OLD}.mcp_server"}

warnings.warn(
    "'helix_context' has been renamed to 'cymatix_context'; the old import "
    "path will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)


class _AliasLoader(importlib.abc.Loader):
    def create_module(self, spec):
        real = importlib.import_module(_NEW + spec.name[len(_OLD):])
        sys.modules[spec.name] = real
        return real

    def exec_module(self, module):  # real module already executed
        pass


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(_OLD + ".") and fullname not in _REAL_FILES:
            return importlib.util.spec_from_loader(fullname, _AliasLoader())
        return None


if not any(type(f).__name__ == "_AliasFinder" for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())

_pkg = importlib.import_module(_NEW)


def __getattr__(name):
    return getattr(_pkg, name)


def __dir__():
    return dir(_pkg)
```

- [ ] **Step 4: Write `helix_context/mcp_server.py`**

Same pattern the repo already uses for its `-m` shim (this file replaces the one that moved to `cymatix_context/` in Task 2):

```python
"""Backward-compat shim -- real module at cymatix_context.mcp.mcp_server."""
import sys
from cymatix_context.mcp import mcp_server as _real

sys.modules[__name__] = _real

if __name__ == "__main__":
    # ``python -m helix_context.mcp_server`` runs this shim with
    # __name__ == "__main__", so the real module's own __main__ guard
    # never fires — dispatch explicitly or the documented invocation
    # exits 0 without ever starting the MCP stdio loop.
    _real.main()
```

- [ ] **Step 5: Run the new tests**

Run: `rtk proxy python -m pytest tests/test_cymatix_rename.py -q`
Expected: PASS (all 5).

- [ ] **Step 6: Full suite**

Run: `rtk proxy python -m pytest tests/ -m "not live" -q`
Expected: 0 failures.

- [ ] **Step 7: Commit**

```bash
git add helix_context/ tests/test_cymatix_rename.py
git commit -m "rename: helix_context alias shim package (identical module objects, -m entry preserved)"
```

---

### Task 4: `CYMATIX_*` env var mirror

**Files:**
- Modify: `cymatix_context/__init__.py` (insert after module docstring, before other imports)
- Test: `tests/test_cymatix_rename.py` (append)

**Interfaces:**
- Produces: `cymatix_context._mirror_env() -> None`; setting `CYMATIX_X` behaves exactly like setting `HELIX_X` for every var, without touching the ~200 internal `HELIX_*` read sites (their migration is deferred past the rebase window).

- [ ] **Step 1: Write the failing tests (append to `tests/test_cymatix_rename.py`)**

```python
import os


def test_env_mirror_copies_cymatix_to_helix(monkeypatch):
    monkeypatch.setenv("CYMATIX_RENAME_TEST_XYZ", "1")
    monkeypatch.delenv("HELIX_RENAME_TEST_XYZ", raising=False)
    from cymatix_context import _mirror_env
    _mirror_env()
    assert os.environ["HELIX_RENAME_TEST_XYZ"] == "1"


def test_env_mirror_never_overrides_explicit_helix_value(monkeypatch):
    monkeypatch.setenv("CYMATIX_RENAME_TEST_ABC", "new")
    monkeypatch.setenv("HELIX_RENAME_TEST_ABC", "old")
    from cymatix_context import _mirror_env
    _mirror_env()
    assert os.environ["HELIX_RENAME_TEST_ABC"] == "old"
```

- [ ] **Step 2: Run to verify failure**

Run: `rtk proxy python -m pytest tests/test_cymatix_rename.py -q -k env_mirror`
Expected: FAIL — `ImportError: cannot import name '_mirror_env'`.

- [ ] **Step 3: Implement in `cymatix_context/__init__.py`**

Insert directly after the module docstring (keep every existing line below it):

```python
import os as _os


def _mirror_env() -> None:
    """Accept CYMATIX_* env vars while internal reads still use HELIX_*.

    CYMATIX_* is the canonical user-facing prefix as of the 0.8.0 rename;
    each CYMATIX_X is mirrored to HELIX_X unless HELIX_X is already set
    (an explicit old-name setting wins, so existing deployments are
    untouched). Internal call sites migrate to CYMATIX_* reads in a
    follow-up PR, after this PR has survived its rebases.
    """
    for _k, _v in list(_os.environ.items()):
        if _k.startswith("CYMATIX_"):
            _os.environ.setdefault("HELIX_" + _k[len("CYMATIX_"):], _v)


_mirror_env()
```

- [ ] **Step 4: Run tests**

Run: `rtk proxy python -m pytest tests/test_cymatix_rename.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add cymatix_context/__init__.py tests/test_cymatix_rename.py
git commit -m "rename: CYMATIX_* env vars canonical via one-shot mirror to HELIX_* reads"
```

---

### Task 5: `cymatix.toml` canonical config filename

**Files:**
- Modify: `cymatix_context/config.py:1106-1119` (`load_config` discovery)
- Rename: `helix.toml` → `cymatix.toml` (repo root, `git mv`)
- Test: `tests/test_cymatix_rename.py` (append)

**Interfaces:**
- Consumes: `_mirror_env` (Task 4) — `CYMATIX_CONFIG` already lands in `HELIX_CONFIG`, so only filename defaults change here.
- Produces: discovery order = explicit `path` arg > `HELIX_CONFIG` env (mirror-fed) > `./cymatix.toml` if present > `./helix.toml`.

- [ ] **Step 1: Write the failing tests (append)**

```python
def test_config_prefers_cymatix_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HELIX_CONFIG", raising=False)
    monkeypatch.delenv("CYMATIX_CONFIG", raising=False)
    (tmp_path / "cymatix.toml").write_text("[server]\nport = 12345\n", encoding="utf-8")
    (tmp_path / "helix.toml").write_text("[server]\nport = 54321\n", encoding="utf-8")
    from cymatix_context.config import load_config
    cfg = load_config()
    assert cfg.server.port == 12345


def test_config_falls_back_to_helix_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HELIX_CONFIG", raising=False)
    monkeypatch.delenv("CYMATIX_CONFIG", raising=False)
    (tmp_path / "helix.toml").write_text("[server]\nport = 54321\n", encoding="utf-8")
    from cymatix_context.config import load_config
    cfg = load_config()
    assert cfg.server.port == 54321
```

(Verify the attribute path for port on the loaded config object — read `load_config`'s return construction around `cymatix_context/config.py:1106-1664`; if server port lives at a different attribute, e.g. `cfg.server_port`, adjust BOTH tests to the real attribute before running.)

- [ ] **Step 2: Run to verify the first test fails**

Run: `rtk proxy python -m pytest tests/test_cymatix_rename.py -q -k config`
Expected: `test_config_prefers_cymatix_toml` FAILS (port 54321 loaded — helix.toml default), fallback test PASSES.

- [ ] **Step 3: Implement discovery change in `load_config`**

Current code at `cymatix_context/config.py:1112` is `path = os.environ.get("HELIX_CONFIG", "helix.toml")`. Replace with:

```python
        path = os.environ.get("HELIX_CONFIG")  # CYMATIX_CONFIG lands here via _mirror_env
        if path is None:
            path = "cymatix.toml" if Path("cymatix.toml").exists() else "helix.toml"
```

- [ ] **Step 4: Rename the shipped config file**

```bash
git mv helix.toml cymatix.toml
```

Then grep for hardcoded loads of the repo file (NOT temp-file test fixtures, which the fallback keeps working):

Run: `rtk proxy grep -rln --include='*.py' '"helix.toml"' cymatix_context/ scripts/ benchmarks/`
For each hit that opens the repo-root file directly (rather than passing a user path), change the literal to try `cymatix.toml` first, mirroring Step 3's two-line pattern. Test-fixture writers that create their own `helix.toml` in tmp dirs stay unchanged.

- [ ] **Step 5: Run tests + full suite**

```bash
rtk proxy python -m pytest tests/test_cymatix_rename.py -q
rtk proxy python -m pytest tests/ -m "not live" -q
```

Expected: 0 failures.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "rename: cymatix.toml canonical config filename, helix.toml fallback preserved"
```

---

### Task 6: Distribution rename in pyproject (name, version, entry points, wheel contents)

**Files:**
- Modify: `pyproject.toml`
- Test: `tests/test_cymatix_rename.py` (append)

**Interfaces:**
- Produces: distribution `cymatix-context 0.8.0`; console scripts `cymatix`, `cymatix-server`, `cymatix-launcher`, `cymatix-status`, `cymatix-vault` PLUS the five old `helix*` names as aliases; wheel ships BOTH `cymatix_context/` and the `helix_context/` shim.

- [ ] **Step 1: Write the failing test (append)**

```python
def test_new_and_old_console_scripts_registered():
    from importlib.metadata import entry_points
    names = {ep.name for ep in entry_points(group="console_scripts")}
    expected = {
        "cymatix", "cymatix-server", "cymatix-launcher", "cymatix-status", "cymatix-vault",
        "helix", "helix-server", "helix-launcher", "helix-status", "helix-vault",
    }
    missing = expected - names
    assert not missing, f"missing console scripts: {missing}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `rtk proxy python -m pytest tests/test_cymatix_rename.py -q -k console_scripts`
Expected: FAIL — missing the five `cymatix*` names.

- [ ] **Step 3: Edit `pyproject.toml`**

Exact changes (leave dependencies/extras untouched):

```toml
[project]
name = "cymatix-context"
version = "0.8.0"
description = "Coordinate index layer for LLM context — Cymatix weighs, doesn't retrieve"
```

Keywords line becomes:

```toml
keywords = ["llm", "context", "compression", "retrieval", "cymatics", "rag", "mcp"]
```

URLs (repo already renamed; the old `SwiftWing21` URLs in pyproject were stale — actual origin was `mbachaud/helix-context`, which now redirects):

```toml
[project.urls]
Homepage = "https://github.com/mbachaud/Cymatix-Context"
Repository = "https://github.com/mbachaud/Cymatix-Context"
Issues = "https://github.com/mbachaud/Cymatix-Context/issues"
```

Scripts (replace the whole `[project.scripts]` table):

```toml
[project.scripts]
cymatix = "cymatix_context.cli:main"
cymatix-server = "cymatix_context.server:main"
cymatix-launcher = "cymatix_context.launcher.app:main"
cymatix-status = "cymatix_context.cli.helix_status:main"
cymatix-vault = "cymatix_context.vault.cli:main"
# Deprecated aliases from the helix-context era — identical targets, kept
# for one deprecation window.
helix = "cymatix_context.cli:main"
helix-server = "cymatix_context.server:main"
helix-launcher = "cymatix_context.launcher.app:main"
helix-status = "cymatix_context.cli.helix_status:main"
helix-vault = "cymatix_context.vault.cli:main"
```

Wheel contents — the shim package must ship (extend the table added in Task 2 Step 4):

```toml
[tool.hatch.build.targets.wheel]
packages = ["cymatix_context", "helix_context"]
```

- [ ] **Step 4: Reinstall + run the test**

```bash
python -m pip install -e ".[dev]"
rtk proxy python -m pytest tests/test_cymatix_rename.py -q
```

Expected: install reports `cymatix-context-0.8.0`; all rename tests PASS.

- [ ] **Step 5: CLI smoke both names**

```bash
cymatix --help && helix --help
cymatix-server --help 2>&1 | head -3
```

Expected: identical help output for `cymatix`/`helix`; server help prints without import errors.

- [ ] **Step 6: Wheel-content check (the shim regression trap)**

```bash
python -m pip install build 2>/dev/null; python -m build --wheel
python - <<'EOF'
import glob, zipfile
w = glob.glob("dist/cymatix_context-0.8.0-*.whl")[0]
names = zipfile.ZipFile(w).namelist()
assert any(n.startswith("helix_context/") for n in names), "shim package missing from wheel"
assert any(n.startswith("cymatix_context/") for n in names)
print("wheel ships both packages")
EOF
```

Expected: `wheel ships both packages`.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml tests/test_cymatix_rename.py
git commit -m "rename: distribution cymatix-context 0.8.0, dual console scripts, shim in wheel"
```

---

### Task 6b: Trusted-publishing workflow alignment

**Files:**
- Modify: `.github/workflows/publish.yml` (keep this exact filename — the PyPI publisher is bound to `publish.yml`; renaming it would break the binding)

**Interfaces:**
- Consumes: PyPI pending publisher configured by Max 2026-07-20 (repo `mbachaud/Cymatix-Context`, workflow `publish.yml`, environment `pypi`).
- Produces: a release-triggered publish that authenticates via OIDC trusted publishing and can create the `cymatix-context` project on PyPI.

- [ ] **Step 1: Switch the publish step from token auth to OIDC**

In `.github/workflows/publish.yml`, the final step currently reads:

```yaml
      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
        with:
          password: ${{ secrets.PYPI_SECRET_HELIX }}
```

Replace with (no `with:` block — presence of `password` forces token auth and bypasses the trusted publisher; the helix-era token cannot create the new project):

```yaml
      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
```

Leave `environment: pypi` and both `permissions: id-token: write` blocks exactly as they are — they are what the trusted publisher validates.

- [ ] **Step 3: Static-validate the workflow**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/publish.yaml')); print('yaml ok')"`
Expected: `yaml ok`. (Full end-to-end validation happens on the first `workflow_dispatch` run after merge — note this in the PR body.)

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/
git commit -m "rename: align publish workflow with PyPI trusted publisher (publish.yaml, OIDC)"
```

---

### Task 7: MCP server rename

**Files:**
- Modify: `cymatix_context/mcp/mcp_server.py:160`
- Test: `tests/test_cymatix_rename.py` (append)

**Interfaces:**
- Produces: MCP server self-identifies as `cymatix` (client tool namespace becomes `mcp__cymatix__*`). Breaking for connected clients — called out in PR body + Max's checklist (local `.mcp.json` keys and the `helix-context` Claude skill reference the old namespace).

- [ ] **Step 1: Write the failing test (append)**

```python
def test_mcp_server_identifies_as_cymatix():
    from cymatix_context.mcp.mcp_server import mcp
    assert mcp.name == "cymatix"
```

- [ ] **Step 2: Run to verify it fails**

Run: `rtk proxy python -m pytest tests/test_cymatix_rename.py -q -k identifies`
Expected: FAIL — `assert 'helix' == 'cymatix'`.

- [ ] **Step 3: Implement**

At `cymatix_context/mcp/mcp_server.py:160` change `mcp = FastMCP("helix")` to:

```python
mcp = FastMCP("cymatix")
```

- [ ] **Step 4: Run tests**

Run: `rtk proxy python -m pytest tests/test_cymatix_rename.py tests/ -m "not live" -q -k "mcp or rename"`
Expected: PASS. Then full suite: `rtk proxy python -m pytest tests/ -m "not live" -q` — 0 failures (some MCP tests may assert the old name; update those assertions to `"cymatix"`, they are part of this task).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "rename: MCP server name helix -> cymatix (client namespace mcp__cymatix__*)"
```

---

### Task 8: README + CLAUDE.md rebrand

**Files:**
- Modify: `README.md` — replace entirely with the reviewed draft at `docs/superpowers/plans/2026-07-20-cymatix-readme-draft.md` (drop that file's top provenance comment block if present)
- Modify: `CLAUDE.md`

- [ ] **Step 1: Swap README**

```bash
cp docs/superpowers/plans/2026-07-20-cymatix-readme-draft.md README.md
```

Then open `README.md` and delete the HTML comment header (`<!-- draft … -->`) if the draft carries one. Verify every relative link target exists (`docs/SETUP.md`, `docs/clients/cli.md`, etc. — they are unrenamed and must still resolve).

- [ ] **Step 2: Update CLAUDE.md**

Precise replacements (project CLAUDE.md only, `f:` worktree copy):

| Location | Old | New |
|---|---|---|
| Title line | `# Helix Context` | `# Cymatix Context` |
| Intro line | `v0.7.1` | `v0.8.0 (renamed from helix-context in July 2026 — old CLI/env/import names still work)` |
| Quick Start | `pip install helix-context` | `pip install cymatix-context` |
| Quick Start | `helix ingest` / `helix query` / `helix diag corpus` / `helix status` | `cymatix ingest` / `cymatix query` / `cymatix diag corpus` / `cymatix status` |
| Quick Start | `helix-server` | `cymatix-server` |
| Quick Start | `python -m uvicorn helix_context._asgi:app` | `python -m uvicorn cymatix_context._asgi:app` |
| Package Structure heading + text | `helix_context/` | `cymatix_context/` (add one sentence: "`helix_context` remains as an alias shim package.") |
| Configuration intro | `All config lives in helix.toml.` | `All config lives in cymatix.toml (helix.toml still honored as fallback).` |
| `[telemetry]` row | `HELIX_OTEL_*` | `CYMATIX_OTEL_* (HELIX_OTEL_* honored)` |
| Back-compat shims paragraph | append | `The whole helix_context package name is itself now a shim for cymatix_context.` |

Leave the biology-lexicon material (`genome.db` path, ROSETTA references) untouched.

- [ ] **Step 3: Verify no stale install commands**

Run: `rtk proxy grep -n "pip install helix-context" README.md CLAUDE.md`
Expected: no matches (a *migration note* mentioning the old name is fine; an instruction to install it is not).

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "rename: README + CLAUDE.md rebrand to Cymatix Context, July 2026 benchmark refresh"
```

---

### Task 8b: LICENSE + NOTICE

**Files:**
- Modify: `LICENSE` (one line), `NOTICE`

- [ ] **Step 1: LICENSE copyright line**

The Apache-2.0 text is generic; only the appendix boilerplate is project-specific. Change:

```
   Copyright 2025-2026 Michael Bachaud (SwiftWing21)
```

to:

```
   Copyright 2025-2026 Michael Bachaud (mbachaud)
```

(Handle update only — the copyright holder is the person, not the product, so no other LICENSE change is needed or appropriate.)

- [ ] **Step 2: NOTICE rebrand**

Apply exactly these changes to `NOTICE`:

| Old | New |
|---|---|
| `Helix Context` (line 1, product name) | `Cymatix Context` — and add a second line: `(formerly Helix Context)` |
| `Copyright 2025-2026 Michael Bachaud and Helix Context contributors` | `Copyright 2025-2026 Michael Bachaud and Cymatix Context contributors` |
| `https://github.com/SwiftWing21/helix-context` | `https://github.com/mbachaud/Cymatix-Context` |
| `Helix Context optionally uses the following` | `Cymatix Context optionally uses the following` |
| `` installed via `helix-context[codec]` `` | `` installed via `cymatix-context[codec]` `` |
| `Helix Context uses Headroom's` | `Cymatix Context uses Headroom's` |
| `helix_context/headroom_bridge.py` | `cymatix_context/encoding/headroom_bridge.py` (the NOTICE path was already stale — the bridge lives under `encoding/`) |

- [ ] **Step 3: Verify nothing helix-branded remains**

Run: `rtk proxy grep -in "helix\|swiftwing" LICENSE NOTICE`
Expected: only the single deliberate `(formerly Helix Context)` line in NOTICE.

- [ ] **Step 4: Commit**

```bash
git add LICENSE NOTICE
git commit -m "rename: LICENSE handle + NOTICE rebrand (fixes stale headroom_bridge path)"
```

---

### Task 8c: Live-docs sweep (historical docs stay untouched)

**Files:**
- Modify: the 31 live docs below.
- NEVER touch: `docs/benchmarks/`, `docs/archive/`, `docs/superpowers/`, `docs/investigations/`, any file with a `YYYY-MM-DD` date in its name or path. These are point-in-time records (bench runbooks, council verdicts, plans) — renaming them would falsify history. This is policy, not deferral.

Live-doc list (enumerated 2026-07-20 via `grep -rli helix docs/ --include='*.md'` minus historical paths — re-run at execution time to catch docs added since):

```
docs/DESIGN_TARGET.md            docs/architecture/DIMENSIONS.md
docs/INTEGRATING_WITH_EXISTING_RAG.md  docs/architecture/FEDERATION_LOCAL.md
docs/MISSION.md                  docs/architecture/KNOWLEDGE_GRAPH.md
docs/ROADMAP.md                  docs/architecture/LAUNCHER.md
docs/ROSETTA.md                  docs/architecture/OBSERVABILITY.md
docs/SETUP.md                    docs/architecture/PIPELINE_LANES.md
docs/TROUBLESHOOTING.md          docs/architecture/SESSION_REGISTRY.md
docs/agent-sdk-fragment.md       docs/architecture/raude_antigravity_persona.md
docs/api/context-endpoint.md     docs/clients/claude-code.md
docs/api/endpoints.md            docs/clients/cli.md
docs/api/mcp-tools.md            docs/config-reference.md
docs/hardware/grace-blackwell.md docs/operations/DENSE_VRAM.md
docs/operator-runbooks.md        docs/ops/OBSIDIAN_VAULT_STATUS.md
docs/ops/RESTART_PROTOCOL.md     docs/ops/SKILLS_BUNDLE.md
docs/ops/dense-ingest-on-12gb-rigs.md  docs/research/oss-semantic-retrieval-vs-helix.md
```

(`docs/research/oss-semantic-retrieval-vs-helix.md`: update content, keep the filename — inbound links.)

- [ ] **Step 1: Mechanical replacements across the live list**

Run this exact script (bash) — word-boundary, ordered longest-first so `helix-context` doesn't get half-replaced by a bare `helix` rule:

```bash
FILES=$(grep -rliE 'helix' docs/ --include='*.md' \
  | grep -vE 'docs/(benchmarks|archive|superpowers|investigations)/' \
  | grep -vE '20[0-9]{2}-[0-9]{2}-[0-9]{2}')
for f in $FILES; do
  sed -i \
    -e 's/helix-context/cymatix-context/g' \
    -e 's/helix_context/cymatix_context/g' \
    -e 's/helix\.toml/cymatix.toml/g' \
    -e 's/HELIX_/CYMATIX_/g' \
    -e 's/helix-server/cymatix-server/g' \
    -e 's/helix-launcher/cymatix-launcher/g' \
    -e 's/helix-status/cymatix-status/g' \
    -e 's/helix-vault/cymatix-vault/g' \
    -e 's/Helix Context/Cymatix Context/g' \
    -e 's/\bhelix \(query\|packet\|ingest\|gene\|neighbors\|refresh-targets\|status\|diag\|config\)/cymatix \1/g' \
    "$f"
done
```

- [ ] **Step 2: Judgment pass on remaining bare "Helix"/"helix" mentions**

Run: `rtk proxy grep -rn "[Hh]elix" $FILES`
For each remaining hit: prose references to the product → `Cymatix`; references to the *old era* ("since the helix days", changelog-style lines, `formerly helix`) → leave; the Grafana dashboard slug `helix-overview` → leave (Task 9 decides the dashboard, docs must match reality).

- [ ] **Step 2b: In-repo skill bundle**

```bash
git mv skills/helix skills/cymatix
```

Then apply the same Step-1 sed replacements + Step-2 judgment pass to `skills/cymatix/SKILL.md` (31 helix references: MCP tool namespace becomes `mcp__cymatix__*`, commands become `cymatix ...`). If `docs/ops/SKILLS_BUNDLE.md` references the `skills/helix` path, update it to `skills/cymatix`.

- [ ] **Step 3: Anchor the second rename layer in the lexicon**

Add to the top of `docs/ROSETTA.md`, directly under its title:

```markdown
> **July 2026:** the project itself was renamed **helix-context → cymatix-context**
> (package `helix_context` → `cymatix_context`, env `HELIX_*` → `CYMATIX_*`,
> config `helix.toml` → `cymatix.toml`, CLI `helix` → `cymatix`). Old names
> remain as working aliases for a deprecation window. Historical docs
> (benchmarks, council verdicts, dated plans) intentionally keep the helix
> vocabulary — they are point-in-time records. This table's biology↔software
> mapping is unchanged by the rename.
```

- [ ] **Step 4: Doc snippets must still be true**

Docs now show `CYMATIX_*` env vars and `cymatix` commands — both work only because of Tasks 4 and 6. Spot-check three snippets end-to-end:

```bash
CYMATIX_CONFIG=cymatix.toml python -c "from cymatix_context.config import load_config; load_config()"
cymatix status
cymatix diag corpus
```

Expected: all exit 0.

- [ ] **Step 5: Commit**

```bash
git add docs/
git commit -m "rename: live-docs sweep to cymatix (historical/dated docs intentionally untouched)"
```

---

### Task 9: Residual user-facing brand-string sweep

**Files:** determined by grep; expected: `cymatix_context/launcher/` (tray window title), `cymatix_context/telemetry/` (OTel `service.name`), `cymatix_context/cli/` help epilogs.

- [ ] **Step 1: Enumerate candidates**

```bash
rtk proxy grep -rn --include='*.py' -iE '"helix[ -]?context"|service\.name|service_name|app_name|window_title|title="Helix|Helix Context' cymatix_context/ | grep -viv 'genome\|helix_context'
```

- [ ] **Step 2: Apply the decision rule to each hit**

- User-visible product strings (tray tooltip/window title, CLI `--help` prog/description, OTel `service.name`, FastAPI `title=`) → change to `cymatix-context` / `Cymatix Context`. For OTel `service.name` specifically: change the default but honor any explicit env/toml override unchanged (Grafana dashboards filter on it — note the dashboard update in Max's checklist).
- Internal identifiers, comments, biology-lexicon terms, log messages that tests assert on → leave.
- Anything ambiguous → leave, and list it in the PR body under "deferred to docs/identifier sweep".

- [ ] **Step 3: Full suite**

Run: `rtk proxy python -m pytest tests/ -m "not live" -q`
Expected: 0 failures (if a test asserts an old display string, updating that assertion is in-scope here).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "rename: user-facing brand strings (tray, CLI help, OTel service.name)"
```

---

### Task 10: Final verification + PR

- [ ] **Step 1: Full suite, clean venv install check**

```bash
rtk proxy python -m pytest tests/ -m "not live" -q
python -m build --wheel
```

Expected: 0 failures; wheel builds.

- [ ] **Step 2: Old-surface smoke matrix (all must work)**

```bash
helix --help                                        # old CLI alias
python -c "import helix_context; import helix_context.config"   # old import (warns)
python -c "import helix_context.mcp_server"         # old -m target importable
HELIX_CONFIG=cymatix.toml python -c "from cymatix_context.config import load_config; load_config()"
CYMATIX_CONFIG=cymatix.toml python -c "import cymatix_context; from cymatix_context.config import load_config; load_config()"
```

Expected: each exits 0; the import prints one DeprecationWarning.

- [ ] **Step 3: Use superpowers:verification-before-completion, then requesting-code-review**

Drive one real flow end-to-end (`cymatix query` against a scratch genome or `cymatix-server` boot + `curl /health`) before claiming done.

- [ ] **Step 4: Open PR**

Base `master`, title `rename: helix-context -> cymatix-context (0.8.0)`. PR body must include: the back-compat contract table (old surface → status), the MCP client-namespace breaking note, the deferred-work list (env call-site migration, docs sweep, identifier sweep, `helix_status.py` basename), and the Rebase Runbook below verbatim. Note: **do not merge until #230/#231 land.** The GitHub repo rename already happened (2026-07-20); after merge, publishing 0.8.0 = create a GitHub release → `publish.yaml` fires → trusted publisher creates `cymatix-context` on PyPI.

---

## Rebase Runbook (paste into PR body)

This PR moves `helix_context/` → `cymatix_context/` with a committed idempotent codemod, so rebasing over inflight PRs (#230, #231, or anything else touching `helix_context/`) is mechanical:

```bash
git fetch origin
git rebase origin/master
# For each conflict: if the conflict is inside cymatix_context/* against
# master's helix_context/* counterpart, take MASTER's content into the
# cymatix path:
#   git checkout --theirs <path> || cp <master version> <cymatix path>
# If master ADDED a brand-new file under helix_context/, git mv it to the
# same relative path under cymatix_context/ (do NOT leave it beside the
# shim __init__.py / mcp_server.py).
python scripts/codemod_cymatix_rename.py     # rewrites any helix_context tokens the rebase brought in
rtk proxy python -m pytest tests/ -m "not live" -q
git add -A && git rebase --continue
```

The codemod is a no-op on already-converted files, so re-running it after every `--continue` is always safe.

## Explicitly deferred (follow-up PRs, not this one)

1. **Env var call-site migration** — flip ~200 internal `os.environ` reads from `HELIX_*` to `CYMATIX_*`-first; delete `_mirror_env`. Do after the rebase window closes.
2. **Identifier sweep** — `HelixConfig`, `cli/helix_status.py` basename, `helix-overview` Grafana dashboard slug.
3. **Biology-lexicon retirement (decided 2026-07-21, staged behind the rename):** 0.8.x additive dual-naming at every seam — responses emit `document_id` alongside `gene_id` (rail laid by PR #288), `/documents/{id}` aliases `/genes/{id}`, `[store]`/`[compressor]` config aliases for `[genome]`/`[ribosome]`, `cymatix doc get` aliases `gene get`. v0.9 flips software terms to documented defaults. SQLite DDL names are NEVER migrated (invisible to users; would force multi-GB store migrations and break the no-re-ingest promise). NOT in this PR: `gene_id` is on the wire contract that the in-flight ERB §12 re-capture and external harnesses parse.
4. **Old-surface removal** — shim package, `helix*` console scripts, `helix.toml` fallback: earliest v0.9, following the repo's condition-gated (not calendar) deprecation convention.

## Appendix: Max's external checklist

Kept in the session log + memory (`project-rename-cymatix`). Status as of 2026-07-20: GitHub repo renamed to `mbachaud/Cymatix-Context` DONE; PyPI pending trusted publisher DONE (project `Cymatix-Context`, workflow `publish.yaml`, env `pypi`); `BrickWallStudio` GitHub org created DONE. Remaining: domains (cymatixcontext.com/.app, cymatix.dev), `helix-context` final pointer release on PyPI, name squats (`cymatix`, `ccmx` on PyPI; `cymatix` on npm), local `.mcp.json`/skill/env updates on gandalf+red70, ERB leaderboard rename note for the August re-run. **On the eventual transfer to the BrickWallStudio org:** GitHub redirects survive the transfer, but the PyPI trusted publisher does NOT follow — add a publisher for `BrickWallStudio/Cymatix-Context` in the PyPI project's publishing settings before the first post-transfer release, then remove the `mbachaud` one.
