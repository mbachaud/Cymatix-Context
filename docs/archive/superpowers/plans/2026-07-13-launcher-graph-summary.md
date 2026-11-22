# Launcher Graph Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a cached, read-only graph-layer summary to the active genome dashboard and make the database modal honor its `hidden` attribute.

**Architecture:** A new launcher module owns SQLite aggregation and a bounded 30-second cache keyed by resolved genome path. `StateCollector` adds that mapping to the existing state payload, while a dedicated Jinja component and small CSS block render the summary without a graph library or new endpoint. The modal fix remains an isolated author-CSS correction.

**Tech Stack:** Python 3.11+, SQLite, FastAPI, Jinja2, CSS, pytest

## Global Constraints

- Do not add a graph renderer, graph dependency, browser fetch loop, or Obsidian export.
- Read the active SQLite genome only; never mutate it.
- Cache graph aggregation for 30 seconds and bound the process-local cache to eight paths.
- Missing, locked, corrupt, or incomplete graph schemas render `Graph summary unavailable` and log a warning.
- Catch `Exception`, never a bare `except`, and never silently swallow an error.
- Use native Windows `python`, not `uv run`.
- Preserve the existing dark amber launcher styling and two-second panel polling.
- Preserve unrelated worktree changes in `helix.toml` and `all_utterance.jsonl`.

---

## File Structure

- Create `helix_context/launcher/graph_summary.py`: read-only graph aggregation and bounded TTL cache.
- Create `tests/test_launcher_graph_summary.py`: aggregation, failure, and caching tests.
- Modify `helix_context/launcher/collector.py`: attach the active genome's graph summary to launcher state.
- Create `helix_context/launcher/templates/components/graph_summary_panel.html`: render the layer ledger and unavailable state.
- Modify `helix_context/launcher/templates/components/panels.html`: include the graph summary component.
- Modify `helix_context/launcher/static/launcher.css`: Genome-tab visibility, graph ledger styling, and modal hidden rule.
- Modify `tests/test_launcher_app.py`: verify server-rendered summary content.
- Modify `tests/test_launcher_collector.py`: verify active-path wiring into the graph cache.
- Modify `tests/test_launcher_devmode_startup.py`: pin the modal CSS regression.

### Task 1: Read-only graph aggregation and TTL cache

**Files:**
- Create: `helix_context/launcher/graph_summary.py`
- Create: `tests/test_launcher_graph_summary.py`

**Interfaces:**
- Consumes: an active genome `pathlib.Path` or path string.
- Produces: `GraphSummaryCache.get(path) -> Dict[str, Any]` with `available`, `path`, `total_genes`, `cover_connected_genes`, `cover_links`, `cover_rows`, `chunk_of_rows`, `harmonic_links`, `entities`, and `entity_memberships`.

- [ ] **Step 1: Write aggregation and failure tests**

Create `tests/test_launcher_graph_summary.py` with the schema and assertions below:

```python
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from helix_context.launcher.graph_summary import GraphSummaryCache


def _graph_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE genes (gene_id TEXT PRIMARY KEY);
        CREATE TABLE gene_relations (
            gene_id_a TEXT,
            gene_id_b TEXT,
            relation INTEGER
        );
        CREATE TABLE harmonic_links (
            gene_id_a TEXT,
            gene_id_b TEXT
        );
        CREATE TABLE entity_graph (entity TEXT, gene_id TEXT);

        INSERT INTO genes VALUES ('a'), ('b'), ('c'), ('d');
        INSERT INTO gene_relations VALUES
            ('a', 'b', 5),
            ('b', 'a', 5),
            ('b', 'c', 5),
            ('c', 'd', 100);
        INSERT INTO harmonic_links VALUES ('a', 'c'), ('b', 'd');
        INSERT INTO entity_graph VALUES
            ('sqlite', 'a'),
            ('sqlite', 'b'),
            ('mcp', 'c');
        """
    )
    conn.commit()
    conn.close()
    return path


def test_graph_summary_counts_distinct_undirected_cover_pairs(tmp_path):
    db = _graph_db(tmp_path / "genome.db")

    result = GraphSummaryCache().get(db)

    assert result == {
        "available": True,
        "path": str(db.resolve()),
        "total_genes": 4,
        "cover_connected_genes": 3,
        "cover_links": 2,
        "cover_rows": 3,
        "chunk_of_rows": 1,
        "harmonic_links": 2,
        "entities": 2,
        "entity_memberships": 3,
    }


def test_graph_summary_reports_incomplete_schema_without_raising(tmp_path, caplog):
    db = tmp_path / "incomplete.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE genes (gene_id TEXT PRIMARY KEY)")
    conn.close()

    result = GraphSummaryCache().get(db)

    assert result["available"] is False
    assert result["path"] == str(db.resolve())
    assert "gene_relations" in result["error"]
    assert "Graph summary unavailable" in caplog.text


def test_graph_summary_cache_reuses_path_and_switches_database(tmp_path):
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    first.touch()
    second.touch()
    clock = [100.0]
    cache = GraphSummaryCache(ttl_s=30.0, clock=lambda: clock[0])
    payload = {
        "available": True,
        "total_genes": 1,
    }

    with patch(
        "helix_context.launcher.graph_summary._read_graph_summary",
        return_value=payload,
    ) as read:
        assert cache.get(first)["total_genes"] == 1
        assert cache.get(first)["total_genes"] == 1
        assert read.call_count == 1

        cache.get(second)
        assert read.call_count == 2

        clock[0] = 131.0
        cache.get(first)
        assert read.call_count == 3
```

- [ ] **Step 2: Run the new tests and confirm the missing module failure**

Run:

```powershell
python -m pytest tests/test_launcher_graph_summary.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'helix_context.launcher.graph_summary'`.

- [ ] **Step 3: Implement aggregation and caching**

Create `helix_context/launcher/graph_summary.py`:

```python
"""Read-only graph-layer summary for the launcher Genome tab."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Dict, Union

log = logging.getLogger("helix.launcher.graph_summary")

GRAPH_SUMMARY_TTL_S = 30.0
GRAPH_SUMMARY_MAX_PATHS = 8
_REQUIRED_TABLES = {"genes", "gene_relations", "harmonic_links", "entity_graph"}


def _read_graph_summary(path: Path) -> Dict[str, Any]:
    uri = f"{path.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as conn:
        conn.execute("PRAGMA query_only = ON")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = sorted(_REQUIRED_TABLES - tables)
        if missing:
            raise sqlite3.OperationalError(
                f"missing graph tables: {', '.join(missing)}"
            )

        total_genes = conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
        cover_rows = conn.execute(
            "SELECT COUNT(*) FROM gene_relations WHERE relation = 5"
        ).fetchone()[0]
        cover_links = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT
                    MIN(gene_id_a, gene_id_b) AS gene_low,
                    MAX(gene_id_a, gene_id_b) AS gene_high
                FROM gene_relations
                WHERE relation = 5
                GROUP BY gene_low, gene_high
            )
            """
        ).fetchone()[0]
        cover_connected_genes = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT gene_id_a AS gene_id
                FROM gene_relations WHERE relation = 5
                UNION
                SELECT gene_id_b AS gene_id
                FROM gene_relations WHERE relation = 5
            )
            """
        ).fetchone()[0]
        chunk_of_rows = conn.execute(
            "SELECT COUNT(*) FROM gene_relations WHERE relation = 100"
        ).fetchone()[0]
        harmonic_links = conn.execute(
            "SELECT COUNT(*) FROM harmonic_links"
        ).fetchone()[0]
        entities = conn.execute(
            "SELECT COUNT(DISTINCT entity) FROM entity_graph"
        ).fetchone()[0]
        entity_memberships = conn.execute(
            "SELECT COUNT(*) FROM entity_graph"
        ).fetchone()[0]

    return {
        "available": True,
        "path": str(path),
        "total_genes": total_genes,
        "cover_connected_genes": cover_connected_genes,
        "cover_links": cover_links,
        "cover_rows": cover_rows,
        "chunk_of_rows": chunk_of_rows,
        "harmonic_links": harmonic_links,
        "entities": entities,
        "entity_memberships": entity_memberships,
    }


class GraphSummaryCache:
    """Bounded process-local TTL cache around graph aggregation."""

    def __init__(
        self,
        ttl_s: float = GRAPH_SUMMARY_TTL_S,
        max_paths: int = GRAPH_SUMMARY_MAX_PATHS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_s = ttl_s
        self.max_paths = max_paths
        self.clock = clock
        self._entries: Dict[str, tuple[float, Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def get(self, path: Union[str, Path]) -> Dict[str, Any]:
        resolved = Path(path).resolve()
        key = os.path.normcase(str(resolved))
        now = self.clock()
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and now - cached[0] < self.ttl_s:
                return dict(cached[1])

        try:
            result = _read_graph_summary(resolved)
        except Exception as exc:
            log.warning("Graph summary unavailable for %s: %s", resolved, exc)
            result = {
                "available": False,
                "path": str(resolved),
                "error": str(exc),
            }

        with self._lock:
            self._entries[key] = (now, result)
            while len(self._entries) > self.max_paths:
                oldest = min(self._entries, key=lambda item: self._entries[item][0])
                del self._entries[oldest]
        return dict(result)
```

- [ ] **Step 4: Run the graph summary tests**

Run:

```powershell
python -m pytest tests/test_launcher_graph_summary.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit the aggregation unit**

```powershell
git add helix_context/launcher/graph_summary.py tests/test_launcher_graph_summary.py
git commit -m "feat(launcher): collect cached graph summary"
```

### Task 2: Attach the summary to launcher state

**Files:**
- Modify: `helix_context/launcher/collector.py`
- Modify: `tests/test_launcher_collector.py`

**Interfaces:**
- Consumes: `GraphSummaryCache.get(active_path)` from Task 1 and `database.active_path` from `_database_panel()`.
- Produces: `state["graph_summary"]`, available even when Helix is stopped as long as an active path is known.

- [ ] **Step 1: Write the failing collector wiring test**

Add this test to `tests/test_launcher_collector.py`:

```python
def test_collect_attaches_graph_summary_for_active_database(fake_supervisor):
    fake_supervisor.is_running.return_value = False
    fake_supervisor.find_orphan_helix.return_value = None
    fake_supervisor.get_last_error.return_value = None
    graph_cache = MagicMock()
    graph_cache.get.return_value = {
        "available": True,
        "total_genes": 5_636,
    }
    subject = StateCollector(
        supervisor=fake_supervisor,
        graph_summary_cache=graph_cache,
    )

    with patch.object(
        subject,
        "_database_panel",
        return_value={"active_path": "F:/genomes/dogfood/genome.db", "entries": []},
    ):
        state = subject.collect()

    graph_cache.get.assert_called_once_with("F:/genomes/dogfood/genome.db")
    assert state["graph_summary"]["total_genes"] == 5_636
```

- [ ] **Step 2: Run the test and confirm the constructor failure**

Run:

```powershell
python -m pytest tests/test_launcher_collector.py::test_collect_attaches_graph_summary_for_active_database -q
```

Expected: FAIL because `StateCollector.__init__()` does not accept `graph_summary_cache`.

- [ ] **Step 3: Wire the cache into `StateCollector`**

In `helix_context/launcher/collector.py`, add the import:

```python
from .graph_summary import GraphSummaryCache
```

Extend `StateCollector.__init__` with the injected cache and default:

```python
    def __init__(
        self,
        supervisor: HelixSupervisor,
        ollama_base_url: str = "http://127.0.0.1:11434",
        http_timeout: float = 4.0,
        update_checker: Optional[Any] = None,
        graph_summary_cache: Optional[GraphSummaryCache] = None,
    ) -> None:
        self.supervisor = supervisor
        self.ollama_base_url = ollama_base_url.rstrip("/")
        self.http_timeout = http_timeout
        self.update_checker = update_checker
        self.graph_summary_cache = graph_summary_cache or GraphSummaryCache()
```

Immediately after `state["database"] = self._database_panel()` in `collect`, add:

```python
        active_path = state["database"].get("active_path")
        if active_path:
            state["graph_summary"] = self.graph_summary_cache.get(active_path)
```

- [ ] **Step 4: Run focused and collector regression tests**

Run:

```powershell
python -m pytest tests/test_launcher_collector.py tests/test_launcher_graph_summary.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the state wiring**

```powershell
git add helix_context/launcher/collector.py tests/test_launcher_collector.py
git commit -m "feat(launcher): expose active graph summary"
```

### Task 3: Render the summary on the Genome tab

**Files:**
- Create: `helix_context/launcher/templates/components/graph_summary_panel.html`
- Modify: `helix_context/launcher/templates/components/panels.html`
- Modify: `helix_context/launcher/static/launcher.css`
- Modify: `tests/test_launcher_app.py`

**Interfaces:**
- Consumes: `state.graph_summary` from Task 2.
- Produces: a full-width `.panel--graph-summary` layer ledger visible only on the Genome tab.

- [ ] **Step 1: Write the failing panel rendering test**

Add to `TestPanelsPartial` in `tests/test_launcher_app.py`:

```python
    def test_panels_partial_renders_graph_summary(self, client, fake_collector):
        fake_collector.collect.return_value = {
            "helix": {
                "running": True,
                "availability": "available",
                "port": 11437,
            },
            "graph_summary": {
                "available": True,
                "total_genes": 5_636,
                "cover_connected_genes": 4_049,
                "cover_links": 29_061,
                "cover_rows": 29_164,
                "chunk_of_rows": 4_240,
                "harmonic_links": 28,
                "entities": 14_186,
                "entity_memberships": 42_236,
            },
        }

        html = client.get("/api/state/panels").text

        assert "panel--graph-summary" in html
        assert "Graph summary" in html
        assert "29,061" in html
        assert "42,236 memberships" in html
        assert "Renderer deferred" in html
```

Also add the unavailable-state test:

```python
    def test_panels_partial_contains_graph_summary_failure(self, client, fake_collector):
        fake_collector.collect.return_value = {
            "helix": {"running": False, "port": 11437},
            "graph_summary": {
                "available": False,
                "error": "missing graph tables: harmonic_links",
            },
        }

        html = client.get("/api/state/panels").text

        assert "panel--graph-summary" in html
        assert "Graph summary unavailable" in html
        assert "missing graph tables" not in html
```

- [ ] **Step 2: Run the tests and confirm the absent panel failure**

Run:

```powershell
python -m pytest tests/test_launcher_app.py::TestPanelsPartial::test_panels_partial_renders_graph_summary tests/test_launcher_app.py::TestPanelsPartial::test_panels_partial_contains_graph_summary_failure -q
```

Expected: both fail because `panel--graph-summary` is absent.

- [ ] **Step 3: Add the graph summary component**

Create `helix_context/launcher/templates/components/graph_summary_panel.html`:

```html
<section class="panel panel--graph-summary">
  <h2 class="panel-title">
    Graph summary
    <span class="panel-count">SUMMARY</span>
    <span class="panel-count-muted">Renderer deferred</span>
  </h2>
  {% if state.graph_summary.available %}
    <dl class="graph-layers">
      <div class="graph-layer">
        <dt>Genes</dt>
        <dd><strong>{{ "{:,}".format(state.graph_summary.total_genes) }}</strong></dd>
        <dd class="muted">{{ "{:,}".format(state.graph_summary.cover_connected_genes) }} COVER-connected</dd>
      </div>
      <div class="graph-layer">
        <dt>Semantic</dt>
        <dd><strong>{{ "{:,}".format(state.graph_summary.cover_links) }}</strong> distinct COVER links</dd>
        <dd class="muted">{{ "{:,}".format(state.graph_summary.cover_rows) }} stored rows</dd>
      </div>
      <div class="graph-layer">
        <dt>Structure</dt>
        <dd><strong>{{ "{:,}".format(state.graph_summary.chunk_of_rows) }}</strong> CHUNK_OF links</dd>
        <dd class="muted">document hierarchy</dd>
      </div>
      <div class="graph-layer">
        <dt>Co-activation</dt>
        <dd><strong>{{ "{:,}".format(state.graph_summary.harmonic_links) }}</strong> harmonic links</dd>
        <dd class="muted">learned from retrieval</dd>
      </div>
      <div class="graph-layer">
        <dt>Entities</dt>
        <dd><strong>{{ "{:,}".format(state.graph_summary.entities) }}</strong> distinct entities</dd>
        <dd class="muted">{{ "{:,}".format(state.graph_summary.entity_memberships) }} memberships</dd>
      </div>
    </dl>
  {% else %}
    <p class="graph-summary-unavailable muted">Graph summary unavailable.</p>
  {% endif %}
</section>
```

In `components/panels.html`, directly after the database panel include, add:

```html
{% if state.graph_summary %}
  {% include "components/graph_summary_panel.html" %}
{% endif %}
```

- [ ] **Step 4: Add Genome-tab visibility and ledger styling**

In the full-width panel selector in `launcher.css`, add `.panel--graph-summary`. In the Genome-tab visible selector, add:

```css
.panels[data-active-tab="genome"] .panel--graph-summary,
```

Add the component styles near the other panel-specific blocks:

```css
.graph-layers {
  display: grid;
  gap: 0;
  margin: 0;
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  overflow: hidden;
}

.graph-layer {
  display: grid;
  grid-template-columns: minmax(110px, 0.7fr) minmax(220px, 1.3fr) minmax(180px, 1fr);
  gap: 12px;
  align-items: baseline;
  padding: 10px 12px;
  background: color-mix(in srgb, var(--color-elevated) 48%, transparent);
}

.graph-layer + .graph-layer {
  border-top: 1px solid var(--color-border);
}

.graph-layer dt {
  color: var(--color-accent-strong);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.graph-layer dd {
  margin: 0;
}

.graph-layer strong {
  color: var(--color-text);
  font-family: var(--font-mono);
}

.graph-summary-unavailable {
  margin: 0;
}

@media (max-width: 720px) {
  .graph-layer {
    grid-template-columns: 1fr;
    gap: 4px;
  }
}
```

- [ ] **Step 5: Run panel and launcher regression tests**

Run:

```powershell
python -m pytest tests/test_launcher_app.py tests/test_launcher_dashboard_wiring.py tests/test_launcher_graph_summary.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit the graph panel**

```powershell
git add helix_context/launcher/templates/components/graph_summary_panel.html helix_context/launcher/templates/components/panels.html helix_context/launcher/static/launcher.css tests/test_launcher_app.py
git commit -m "feat(launcher): render genome graph summary"
```

### Task 4: Make the hidden database modal stay hidden

**Files:**
- Modify: `helix_context/launcher/static/launcher.css`
- Modify: `tests/test_launcher_devmode_startup.py`

**Interfaces:**
- Consumes: the existing `hidden` attribute emitted by `dashboard.html`.
- Produces: author CSS that forces only hidden modal backdrops to `display: none`.

- [ ] **Step 1: Write the failing CSS regression test**

Add below `test_db_modal_hidden_when_not_needed` in `tests/test_launcher_devmode_startup.py`:

```python
def test_hidden_db_modal_author_css_overrides_backdrop_display():
    css_path = (
        Path(__file__).resolve().parent.parent
        / "helix_context"
        / "launcher"
        / "static"
        / "launcher.css"
    )
    normalized = " ".join(css_path.read_text(encoding="utf-8").split())

    assert ".modal-backdrop[hidden] { display: none !important; }" in normalized
```

- [ ] **Step 2: Run the test and confirm the missing rule failure**

Run:

```powershell
python -m pytest tests/test_launcher_devmode_startup.py::test_hidden_db_modal_author_css_overrides_backdrop_display -q
```

Expected: FAIL because the normalized CSS does not contain the rule.

- [ ] **Step 3: Add the narrowly scoped hidden rule**

Immediately after the existing `.modal-backdrop` block in `launcher.css`, add:

```css
.modal-backdrop[hidden] {
  display: none !important;
}
```

- [ ] **Step 4: Run first-boot and modal tests**

Run:

```powershell
python -m pytest tests/test_launcher_devmode_startup.py -q
```

Expected: all tests pass, including both the visible no-database modal and hidden active-database state.

- [ ] **Step 5: Commit the modal fix**

```powershell
git add helix_context/launcher/static/launcher.css tests/test_launcher_devmode_startup.py
git commit -m "fix(launcher): honor hidden database modal"
```

### Task 5: Verify the complete launcher change

**Files:**
- Verify only; modify files only if a failing test reveals a scoped defect.

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces: test and live-dashboard evidence for the finished launcher change.

- [ ] **Step 1: Run the focused launcher suite**

```powershell
python -m pytest tests/test_launcher_graph_summary.py tests/test_launcher_collector.py tests/test_launcher_app.py tests/test_launcher_dashboard_wiring.py tests/test_launcher_devmode_startup.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Run diff and worktree checks**

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors; only the user's pre-existing `helix.toml` and `all_utterance.jsonl` changes remain uncommitted.

- [ ] **Step 3: Verify the running dashboard after restarting only the launcher**

Open `http://127.0.0.1:11438`, select the Genome tab, and verify:

- the database-selection modal is absent;
- dogfood remains active;
- Graph summary shows the five named layers;
- COVER displays approximately 29,061 distinct links and 29,164 stored rows;
- no canvas, node layout, or attachments appear.

- [ ] **Step 4: Record verification without mixing unrelated changes**

If verification required no fixes, do not create an empty commit. If a scoped fix was necessary, run its failing test first, make the smallest change, rerun the focused suite, and commit only the affected launcher/test files.
