"""
Session registry tests — DAL + FastAPI endpoints.

Covers the first slice of the session registry (see docs/SESSION_REGISTRY.md):
    - Schema migration runs on genome init
    - Registry DAL: register, heartbeat, list, get, attribute, recent, sweep
    - FastAPI endpoints: /sessions/register, /sessions/{id}/heartbeat,
      /sessions, /sessions/{handle}/recent
    - /ingest extension: participant_id -> automatic attribution

All tests run against in-memory SQLite — no touching of the live genome.db
at F:\\Projects\\helix-context\\genome.db. Safe to run while the real server
is live.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

try:
    import pytest_asyncio  # noqa: F401
    _PYTEST_ASYNCIO_AVAILABLE = True
except ImportError:
    _PYTEST_ASYNCIO_AVAILABLE = False


async def await_until(condition, timeout: float = 2.0, interval: float = 0.02) -> bool:
    """Await ``condition()`` returning truthy, polling every ``interval``
    seconds until ``timeout`` elapses. ``condition`` may be a plain
    callable or an async callable.

    Replaces ``await asyncio.sleep(N); assert count >= K`` patterns that
    race background tasks on slow CI runners.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return True
        await asyncio.sleep(interval)
    result = condition()
    if asyncio.iscoroutine(result):
        result = await result
    return bool(result)

from helix_context.config import (
    BudgetConfig,
    GenomeConfig,
    HelixConfig,
    RibosomeConfig,
    ServerConfig,
)
from helix_context.identity.registry import (
    DEFAULT_TTL_S,
    IDLE_TTL_S,
    STALE_TTL_S,
    Registry,
    _status_from_last_heartbeat,
)
from helix_context.server import create_app

from tests.conftest import make_gene


# ═══ DAL unit tests ═══════════════════════════════════════════════════


@pytest.fixture
def registry(genome):
    """Registry bound to the in-memory genome fixture from conftest."""
    return Registry(genome)


class TestSchemaMigration:
    def test_registry_tables_created_on_genome_init(self, genome):
        cur = genome.conn.cursor()
        tables = {
            row[0] for row in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "parties" in tables
        assert "participants" in tables
        assert "gene_attribution" in tables
        assert "hitl_events" in tables

    def test_registry_indexes_created(self, genome):
        cur = genome.conn.cursor()
        indexes = {
            row[0] for row in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_participants_handle" in indexes
        assert "idx_attribution_party_time" in indexes
        assert "idx_attribution_participant_time" in indexes
        # HITL event logger (added 2026-04-11 per HITL observation handoff)
        assert "idx_hitl_party_time" in indexes
        assert "idx_hitl_participant_time" in indexes
        assert "idx_hitl_pause_type" in indexes

    def test_hitl_events_schema_has_expected_columns(self, genome):
        """Guard against accidental column renames — the chat-channel
        signal columns are load-bearing per the M1 finding."""
        cur = genome.conn.cursor()
        rows = cur.execute("PRAGMA table_info(hitl_events)").fetchall()
        columns = {row[1] for row in rows}  # row[1] is the column name
        expected = {
            "event_id", "party_id", "participant_id", "ts",
            "pause_type", "task_context", "resolved_without_operator",
            "operator_tone_uncertainty", "operator_risk_keywords",
            "time_since_last_risk_event", "recoverability_signal",
            "genome_total_genes", "genome_hetero_count", "cold_cache_size",
            "metadata",
        }
        missing = expected - columns
        assert not missing, f"hitl_events missing columns: {missing}"

    def test_migration_is_idempotent(self, genome):
        """Running the migration twice should not raise."""
        cur = genome.conn.cursor()
        genome._ensure_registry_schema(cur)
        genome._ensure_registry_schema(cur)
        genome.conn.commit()

    def test_participants_table_has_agent_kind_and_mcp_host_columns(self, genome):
        """Schema migration adds agent_kind and mcp_host columns idempotently."""
        cur = genome.conn.cursor()
        cols = {r[1] for r in cur.execute("PRAGMA table_info(participants)").fetchall()}
        assert "agent_kind" in cols, f"agent_kind missing; got {cols}"
        assert "mcp_host" in cols, f"mcp_host missing; got {cols}"

    def test_schema_migration_is_idempotent(self, genome):
        """Re-running _ensure_registry_schema does not raise on existing columns."""
        cur = genome.conn.cursor()
        # Should not raise even though columns already exist.
        genome._ensure_registry_schema(cur)
        genome.conn.commit()

    def test_participants_table_has_announce_columns(self, genome):
        """Schema migration adds ide_detected, ide_detection_via, model_id columns."""
        cur = genome.conn.cursor()
        cols = {r[1] for r in cur.execute("PRAGMA table_info(participants)").fetchall()}
        assert "ide_detected" in cols, f"ide_detected missing; got {cols}"
        assert "ide_detection_via" in cols, f"ide_detection_via missing; got {cols}"
        assert "model_id" in cols, f"model_id missing; got {cols}"


class TestRegisterParticipant:
    def test_register_creates_party_on_first_use(self, registry, genome):
        p = registry.register_participant(
            party_id="max@local",
            handle="taude",
            workspace="/f/Projects/Education",
        )
        assert p.participant_id
        assert p.party_id == "max@local"
        assert p.handle == "taude"
        assert p.status == "active"

        row = genome.conn.execute(
            "SELECT party_id, display_name, trust_domain FROM parties WHERE party_id = ?",
            ("max@local",),
        ).fetchone()
        assert row is not None
        assert row["trust_domain"] == "local"

    def test_second_participant_reuses_existing_party(self, registry, genome):
        registry.register_participant(party_id="max@local", handle="taude")
        registry.register_participant(party_id="max@local", handle="laude")

        party_count = genome.conn.execute(
            "SELECT COUNT(*) FROM parties WHERE party_id = ?",
            ("max@local",),
        ).fetchone()[0]
        assert party_count == 1

        participant_count = genome.conn.execute(
            "SELECT COUNT(*) FROM participants WHERE party_id = ?",
            ("max@local",),
        ).fetchone()[0]
        assert participant_count == 2

    def test_capabilities_round_trip(self, registry):
        p = registry.register_participant(
            party_id="max@local",
            handle="taude",
            capabilities=["ingest", "query"],
        )
        got = registry.get_participant(p.participant_id)
        assert got is not None
        assert got.capabilities == ["ingest", "query"]


class TestHeartbeat:
    def test_heartbeat_refreshes_last_seen(self, registry, genome):
        p = registry.register_participant(party_id="max@local", handle="taude")
        # Rewind last_heartbeat so the refresh is visible.
        genome.conn.execute(
            "UPDATE participants SET last_heartbeat = ? WHERE participant_id = ?",
            (time.time() - 60, p.participant_id),
        )
        genome.conn.commit()

        result = registry.heartbeat(p.participant_id)
        assert result is not None
        ttl, status = result
        assert ttl == DEFAULT_TTL_S
        assert status == "active"

        row = genome.conn.execute(
            "SELECT last_heartbeat FROM participants WHERE participant_id = ?",
            (p.participant_id,),
        ).fetchone()
        assert row["last_heartbeat"] > time.time() - 5

    def test_heartbeat_unknown_returns_none(self, registry):
        assert registry.heartbeat("nonexistent-id") is None


class TestPresenceGene:
    """Team-affordance: heartbeat-emitted participant presence gene."""

    def test_upsert_presence_gene_creates_retrievable_gene(self, registry, genome):
        p = registry.register_participant(party_id="swift_wing21", handle="laude")
        gene_id = registry.upsert_presence_gene(
            p.participant_id,
            handle="laude",
            party_id="swift_wing21",
            current_focus="PWPC Phase 1 follow-up",
            blocked_on=["batman access"],
            in_flight=["heartbeat endpoint", "lockstep test"],
            last_commit_hash="aeb1f45",
        )
        assert gene_id == f"presence:{p.participant_id}"
        gene = genome.get_gene(gene_id)
        assert gene is not None
        assert "laude" in gene.content
        assert "PWPC Phase 1 follow-up" in gene.content
        assert "batman access" in gene.content
        assert "aeb1f45" in gene.content

    def test_upsert_presence_gene_stable_id_updates_in_place(self, registry, genome):
        """Re-heartbeating the same participant must REPLACE, not duplicate."""
        p = registry.register_participant(party_id="swift_wing21", handle="laude")
        gene_id_1 = registry.upsert_presence_gene(
            p.participant_id, handle="laude", current_focus="first focus",
        )
        gene_id_2 = registry.upsert_presence_gene(
            p.participant_id, handle="laude", current_focus="second focus",
        )
        assert gene_id_1 == gene_id_2
        gene = genome.get_gene(gene_id_1)
        assert "second focus" in gene.content
        assert "first focus" not in gene.content

    def test_upsert_presence_gene_minimal_inputs(self, registry, genome):
        """All state fields optional — presence with just the participant_id still works."""
        p = registry.register_participant(party_id="swift_wing21", handle="laude")
        gene_id = registry.upsert_presence_gene(p.participant_id)
        gene = genome.get_gene(gene_id)
        assert gene is not None
        assert p.participant_id in gene.content or "unknown" in gene.content.lower()

    def test_upsert_presence_gene_tags_key_values(self, registry, genome):
        """Key-values carry the participant identity for downstream tier scoring."""
        p = registry.register_participant(party_id="swift_wing21", handle="laude")
        gene_id = registry.upsert_presence_gene(
            p.participant_id, handle="laude", party_id="swift_wing21",
            last_commit_hash="abc1234",
        )
        gene = genome.get_gene(gene_id)
        joined = "\n".join(gene.key_values)
        assert "presence=true" in joined
        assert "handle=laude" in joined
        assert "party=swift_wing21" in joined
        assert "last_commit=abc1234" in joined

    def test_upsert_presence_gene_bypasses_density_gate(self, registry, genome):
        """Presence genes must always land OPEN — a stale-at-birth presence
        gene is useless, and the density gate's monotonic access_count logic
        is orthogonal to presence semantics."""
        from helix_context.schemas import ChromatinState
        p = registry.register_participant(party_id="swift_wing21", handle="laude")
        gene_id = registry.upsert_presence_gene(p.participant_id, handle="laude")
        gene = genome.get_gene(gene_id)
        assert gene.chromatin == ChromatinState.OPEN


class TestListParticipants:
    def test_filter_by_party(self, registry):
        registry.register_participant(party_id="max@local", handle="taude")
        registry.register_participant(party_id="max@local", handle="laude")
        registry.register_participant(party_id="other@remote", handle="guest")

        max_participants = registry.list_participants(party_id="max@local")
        assert len(max_participants) == 2
        assert {p.handle for p in max_participants} == {"taude", "laude"}

        other = registry.list_participants(party_id="other@remote")
        assert len(other) == 1
        assert other[0].handle == "guest"

    def test_status_filter_all_returns_everyone(self, registry, genome):
        p = registry.register_participant(party_id="max@local", handle="taude")
        # Age one participant into "stale".
        genome.conn.execute(
            "UPDATE participants SET last_heartbeat = ? WHERE participant_id = ?",
            (time.time() - IDLE_TTL_S - 10, p.participant_id),
        )
        genome.conn.commit()

        active_only = registry.list_participants(party_id="max@local", status_filter="active")
        assert len(active_only) == 0

        all_statuses = registry.list_participants(party_id="max@local", status_filter="all")
        assert len(all_statuses) == 1
        assert all_statuses[0].status == "stale"

    def test_workspace_prefix_filter(self, registry):
        registry.register_participant(
            party_id="max@local", handle="taude",
            workspace="/f/Projects/Education",
        )
        registry.register_participant(
            party_id="max@local", handle="other",
            workspace="/f/Projects/Unrelated",
        )
        result = registry.list_participants(
            party_id="max@local",
            workspace_prefix="/f/Projects/Education",
        )
        assert len(result) == 1
        assert result[0].handle == "taude"


class TestStatusFromHeartbeat:
    def test_fresh_is_active(self):
        now = time.time()
        assert _status_from_last_heartbeat(now, now) == "active"

    def test_within_ttl_is_active(self):
        now = time.time()
        assert _status_from_last_heartbeat(now - DEFAULT_TTL_S + 1, now) == "active"

    def test_past_ttl_is_idle(self):
        now = time.time()
        assert _status_from_last_heartbeat(now - DEFAULT_TTL_S - 1, now) == "idle"

    def test_past_idle_is_stale(self):
        now = time.time()
        assert _status_from_last_heartbeat(now - IDLE_TTL_S - 1, now) == "stale"

    def test_past_stale_is_gone(self):
        now = time.time()
        assert _status_from_last_heartbeat(now - STALE_TTL_S - 1, now) == "gone"


class TestAttribution:
    def test_attribute_gene_writes_row(self, registry, genome):
        p = registry.register_participant(party_id="max@local", handle="taude")
        gene = make_gene(content="VS Code 1.115 shipped with agents app")
        genome.upsert_gene(gene)

        result = registry.attribute_gene(
            gene_id=gene.gene_id,
            participant_id=p.participant_id,
        )
        assert result is not None
        assert result.gene_id == gene.gene_id
        assert result.party_id == "max@local"
        assert result.participant_id == p.participant_id

        got = registry.get_attribution(gene.gene_id)
        assert got is not None
        assert got.party_id == "max@local"

    def test_attribute_unknown_participant_returns_none(self, registry, genome):
        gene = make_gene(content="orphan gene")
        genome.upsert_gene(gene)
        result = registry.attribute_gene(
            gene_id=gene.gene_id,
            participant_id="bogus-id",
        )
        assert result is None
        assert registry.get_attribution(gene.gene_id) is None

    def test_attribute_by_party_only(self, registry, genome):
        """Server-side ingests may know the party but not a specific participant."""
        # Create the party manually (no participant registered).
        genome.conn.execute(
            "INSERT INTO parties (party_id, display_name, trust_domain, created_at) "
            "VALUES ('server@local', 'server', 'local', ?)",
            (time.time(),),
        )
        genome.conn.commit()

        gene = make_gene(content="server-authored note")
        genome.upsert_gene(gene)

        result = registry.attribute_gene(
            gene_id=gene.gene_id,
            party_id="server@local",
        )
        assert result is not None
        assert result.party_id == "server@local"
        assert result.participant_id is None

    def test_attribute_implicit_heartbeat(self, registry, genome):
        p = registry.register_participant(party_id="max@local", handle="taude")
        # Rewind heartbeat.
        genome.conn.execute(
            "UPDATE participants SET last_heartbeat = ? WHERE participant_id = ?",
            (time.time() - 60, p.participant_id),
        )
        genome.conn.commit()

        gene = make_gene(content="something")
        genome.upsert_gene(gene)
        registry.attribute_gene(gene_id=gene.gene_id, participant_id=p.participant_id)

        row = genome.conn.execute(
            "SELECT last_heartbeat FROM participants WHERE participant_id = ?",
            (p.participant_id,),
        ).fetchone()
        assert row["last_heartbeat"] > time.time() - 5


class TestGetRecentByHandle:
    def test_returns_chronological_order(self, registry, genome):
        p = registry.register_participant(party_id="max@local", handle="taude")

        gene_a = make_gene(content="oldest note")
        gene_b = make_gene(content="middle note")
        gene_c = make_gene(content="newest note")
        genome.upsert_gene(gene_a)
        genome.upsert_gene(gene_b)
        genome.upsert_gene(gene_c)

        t0 = time.time()
        registry.attribute_gene(gene_id=gene_a.gene_id, participant_id=p.participant_id, authored_at=t0 - 100)
        registry.attribute_gene(gene_id=gene_b.gene_id, participant_id=p.participant_id, authored_at=t0 - 50)
        registry.attribute_gene(gene_id=gene_c.gene_id, participant_id=p.participant_id, authored_at=t0)

        recent = registry.get_recent_by_handle("taude", limit=5)
        assert len(recent) == 3
        assert recent[0]["gene_id"] == gene_c.gene_id
        assert recent[1]["gene_id"] == gene_b.gene_id
        assert recent[2]["gene_id"] == gene_a.gene_id

    def test_limit_honored(self, registry, genome):
        p = registry.register_participant(party_id="max@local", handle="taude")
        for i in range(5):
            g = make_gene(content=f"note {i}")
            genome.upsert_gene(g)
            registry.attribute_gene(gene_id=g.gene_id, participant_id=p.participant_id)

        recent = registry.get_recent_by_handle("taude", limit=2)
        assert len(recent) == 2

    def test_filters_by_handle(self, registry, genome):
        p_taude = registry.register_participant(party_id="max@local", handle="taude")
        p_laude = registry.register_participant(party_id="max@local", handle="laude")

        g1 = make_gene(content="taude note")
        g2 = make_gene(content="laude note")
        genome.upsert_gene(g1)
        genome.upsert_gene(g2)

        registry.attribute_gene(gene_id=g1.gene_id, participant_id=p_taude.participant_id)
        registry.attribute_gene(gene_id=g2.gene_id, participant_id=p_laude.participant_id)

        taude_genes = registry.get_recent_by_handle("taude")
        assert len(taude_genes) == 1
        assert "taude note" in taude_genes[0]["content_preview"]

        laude_genes = registry.get_recent_by_handle("laude")
        assert len(laude_genes) == 1
        assert "laude note" in laude_genes[0]["content_preview"]

    def test_bm25_bypass_short_text_surfaces(self, registry, genome):
        """Regression for the VS Code 1.115 broadcast failure: short notes
        must surface via the recent endpoint even when the genome has no
        other content, proving this is not a retrieval-quality path."""
        p = registry.register_participant(party_id="max@local", handle="taude")
        short_note = make_gene(content="VS Code 1.115 shipped 2026-04-08.")
        genome.upsert_gene(short_note)
        registry.attribute_gene(gene_id=short_note.gene_id, participant_id=p.participant_id)

        recent = registry.get_recent_by_handle("taude")
        assert len(recent) == 1
        assert "VS Code 1.115" in recent[0]["content_preview"]


class TestSweep:
    def test_sweep_updates_status_column(self, registry, genome):
        p = registry.register_participant(party_id="max@local", handle="taude")
        genome.conn.execute(
            "UPDATE participants SET last_heartbeat = ? WHERE participant_id = ?",
            (time.time() - IDLE_TTL_S - 10, p.participant_id),
        )
        genome.conn.commit()

        counts = registry.sweep()
        assert counts["stale"] >= 1

        row = genome.conn.execute(
            "SELECT status FROM participants WHERE participant_id = ?",
            (p.participant_id,),
        ).fetchone()
        assert row["status"] == "stale"


@pytest.mark.skipif(
    not _PYTEST_ASYNCIO_AVAILABLE,
    reason="async tests require pytest-asyncio (install via pip install -e .[dev])",
)
class TestBackgroundSweepTask:
    """Item 7 — _background_registry_sweep async helper.

    The lifespan integration is hard to unit-test in isolation, but
    the loop body itself is just `sweep() + log + sleep`. We verify
    the function exists, calls sweep(), and survives sweep() raising.
    """

    @pytest.mark.asyncio
    async def test_sweep_called_at_least_once_within_interval(self, registry, monkeypatch):
        import asyncio
        from helix_context import server as server_mod

        # Shrink the interval so the test runs in <1s
        monkeypatch.setattr(server_mod, "_REGISTRY_SWEEP_INTERVAL", 0.05)

        call_count = {"n": 0}
        original_sweep = registry.sweep

        def counting_sweep(*args, **kwargs):
            call_count["n"] += 1
            return original_sweep(*args, **kwargs)

        registry.sweep = counting_sweep  # type: ignore[method-assign]

        task = asyncio.create_task(server_mod._background_registry_sweep(registry))
        try:
            # Poll the call counter with a 2s ceiling instead of sleeping a
            # fixed duration — the sweep task is cooperative and may not
            # schedule in 0.2s on a loaded runner.
            fired = await await_until(lambda: call_count["n"] >= 1, timeout=2.0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert fired, "background sweep never ran within 2s"
        assert call_count["n"] >= 1

    @pytest.mark.asyncio
    async def test_sweep_loop_survives_sweep_exception(self, registry, monkeypatch):
        import asyncio
        from helix_context import server as server_mod

        monkeypatch.setattr(server_mod, "_REGISTRY_SWEEP_INTERVAL", 0.05)

        call_count = {"n": 0}

        def angry_sweep(*args, **kwargs):
            call_count["n"] += 1
            raise RuntimeError("simulated sweep failure")

        registry.sweep = angry_sweep  # type: ignore[method-assign]

        task = asyncio.create_task(server_mod._background_registry_sweep(registry))
        try:
            # Wait for at least two ticks to prove the loop survives an
            # exception on each pass. 2s ceiling keeps CI honest.
            fired = await await_until(lambda: call_count["n"] >= 2, timeout=2.0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Loop should have called sweep multiple times despite each raising
        assert fired, f"sweep only called {call_count['n']} times in 2s"
        assert call_count["n"] >= 2


class TestGetAttributionsForGenes:
    """Item 6 — batch attribution lookup for /context citation enrichment."""

    def test_empty_input_returns_empty_dict(self, registry):
        assert registry.get_attributions_for_genes([]) == {}

    def test_returns_party_participant_handle_for_attributed(self, registry, genome):
        p = registry.register_participant(party_id="max@local", handle="taude")
        gene = make_gene(content="cite me")
        genome.upsert_gene(gene)
        registry.attribute_gene(gene_id=gene.gene_id, participant_id=p.participant_id)

        out = registry.get_attributions_for_genes([gene.gene_id])
        assert gene.gene_id in out
        assert out[gene.gene_id]["party_id"] == "max@local"
        assert out[gene.gene_id]["participant_id"] == p.participant_id
        assert out[gene.gene_id]["handle"] == "taude"

    def test_unattributed_genes_omitted(self, registry, genome):
        gene = make_gene(content="orphan")
        genome.upsert_gene(gene)
        out = registry.get_attributions_for_genes([gene.gene_id, "nonexistent-id"])
        assert gene.gene_id not in out
        assert "nonexistent-id" not in out
        assert out == {}

    def test_party_only_attribution_returns_null_handle(self, registry, genome):
        # Server-side ingest with party_id but no participant
        genome.conn.execute(
            "INSERT INTO parties (party_id, display_name, trust_domain, created_at) "
            "VALUES ('server@local', 'server', 'local', ?)",
            (time.time(),),
        )
        genome.conn.commit()
        gene = make_gene(content="server-authored")
        genome.upsert_gene(gene)
        registry.attribute_gene(gene_id=gene.gene_id, party_id="server@local")

        out = registry.get_attributions_for_genes([gene.gene_id])
        assert out[gene.gene_id]["party_id"] == "server@local"
        assert out[gene.gene_id]["participant_id"] is None
        assert out[gene.gene_id]["handle"] is None  # LEFT JOIN, no participant row

    def test_batch_lookup_handles_mixed(self, registry, genome):
        p = registry.register_participant(party_id="max@local", handle="taude")
        attributed = make_gene(content="attributed gene")
        orphan = make_gene(content="orphan gene")
        genome.upsert_gene(attributed)
        genome.upsert_gene(orphan)
        registry.attribute_gene(gene_id=attributed.gene_id, participant_id=p.participant_id)

        out = registry.get_attributions_for_genes([attributed.gene_id, orphan.gene_id])
        assert attributed.gene_id in out
        assert orphan.gene_id not in out


class TestHITLEvents:
    """HITL event logger DAL — Item HITL-1 of the 8D dimensional roadmap.

    Motivated by laude's 2026-04-11 HITL observation handoff and raude's
    M1 discriminating test (which established the mechanism is non-genome-
    mediated, hence the chat-channel signal columns).

    Covers:
      - emit (success / unknown participant / no-participant-no-party /
        with chat signals / with genome snapshot / implicit heartbeat)
      - get (by party / by participant / by time window / by pause type /
        limit / ordering)
      - hitl_rate helper (windowed count-per-second, mirrors access_rate)
      - hitl_stats aggregate (total, by_pause_type, resolved, mean_gap_s)
      - Hard-delete semantics: events survive with participant_id NULLed
    """

    # ── emit_hitl_event ──

    def test_emit_returns_event_id_on_success(self, registry):
        p = registry.register_participant(party_id="max@local", handle="raude")
        eid = registry.emit_hitl_event(
            participant_id=p.participant_id,
            pause_type="permission_request",
            task_context="about to run destructive sweep",
        )
        assert eid is not None
        assert isinstance(eid, str)
        assert len(eid) > 0

    def test_emit_unknown_participant_returns_none(self, registry):
        eid = registry.emit_hitl_event(
            participant_id="does-not-exist",
            pause_type="uncertainty_check",
        )
        assert eid is None

    def test_emit_without_participant_or_party_returns_none(self, registry):
        eid = registry.emit_hitl_event(
            participant_id=None,
            pause_type="other",
        )
        assert eid is None

    def test_emit_with_only_party_id_succeeds(self, registry, genome):
        """Server-side emit path: caller knows party but no specific participant."""
        genome.conn.execute(
            "INSERT INTO parties (party_id, display_name, trust_domain, created_at) "
            "VALUES ('server@local', 'server', 'local', ?)",
            (time.time(),),
        )
        genome.conn.commit()
        eid = registry.emit_hitl_event(
            participant_id=None,
            party_id="server@local",
            pause_type="rollback_confirm",
        )
        assert eid is not None

    def test_emit_unknown_pause_type_coerces_to_other(self, registry):
        """Unknown pause types must not fail the emit — instrumentation
        should be tolerant of new event types being introduced in client
        code before the schema catches up."""
        p = registry.register_participant(party_id="max@local", handle="laude")
        eid = registry.emit_hitl_event(
            participant_id=p.participant_id,
            pause_type="bogus_not_in_enum",
        )
        assert eid is not None
        rows = registry.get_hitl_events(participant_id=p.participant_id)
        assert rows[0]["pause_type"] == "other"

    def test_emit_with_chat_signals(self, registry):
        """Chat-channel signals are the load-bearing addition per the M1 finding.
        Verify all four optional signal fields round-trip through the DAL."""
        p = registry.register_participant(party_id="max@local", handle="raude")
        eid = registry.emit_hitl_event(
            participant_id=p.participant_id,
            pause_type="uncertainty_check",
            chat_signals={
                "tone_uncertainty": 0.85,
                "risk_keywords": ["backup", "damage", "recovery"],
                "time_since_last_risk": 42.0,
                "recoverability": "uncertain",
            },
        )
        assert eid is not None
        rows = registry.get_hitl_events(participant_id=p.participant_id)
        row = rows[0]
        assert row["operator_tone_uncertainty"] == 0.85
        assert row["operator_risk_keywords"] == ["backup", "damage", "recovery"]
        assert row["time_since_last_risk_event"] == 42.0
        assert row["recoverability_signal"] == "uncertain"

    def test_emit_with_genome_snapshot(self, registry):
        """Genome snapshot fields are the M3 correlation substrate."""
        p = registry.register_participant(party_id="max@local", handle="raude")
        eid = registry.emit_hitl_event(
            participant_id=p.participant_id,
            pause_type="permission_request",
            genome_snapshot={
                "total_genes": 8133,
                "hetero_count": 1370,
                "cold_cache_size": 754,
            },
        )
        assert eid is not None
        rows = registry.get_hitl_events(participant_id=p.participant_id)
        row = rows[0]
        assert row["genome_total_genes"] == 8133
        assert row["genome_hetero_count"] == 1370
        assert row["cold_cache_size"] == 754

    def test_emit_with_metadata(self, registry):
        p = registry.register_participant(party_id="max@local", handle="raude")
        eid = registry.emit_hitl_event(
            participant_id=p.participant_id,
            pause_type="other",
            metadata={"turn_index": 42, "tool_name": "Bash"},
        )
        assert eid is not None
        rows = registry.get_hitl_events(participant_id=p.participant_id)
        assert rows[0]["metadata"] == {"turn_index": 42, "tool_name": "Bash"}

    def test_emit_touches_heartbeat(self, registry):
        """Emitting a HITL event is session activity — must refresh heartbeat."""
        p = registry.register_participant(party_id="max@local", handle="raude")
        # Age the heartbeat artificially
        registry.genome.conn.execute(
            "UPDATE participants SET last_heartbeat = ? WHERE participant_id = ?",
            (time.time() - 1000, p.participant_id),
        )
        registry.genome.conn.commit()

        registry.emit_hitl_event(
            participant_id=p.participant_id,
            pause_type="uncertainty_check",
        )
        fetched = registry.get_participant(p.participant_id)
        assert fetched.last_heartbeat > time.time() - 5.0  # recent

    # ── get_hitl_events ──

    def test_get_filters_by_participant(self, registry):
        p1 = registry.register_participant(party_id="max@local", handle="raude")
        p2 = registry.register_participant(party_id="max@local", handle="laude")
        registry.emit_hitl_event(participant_id=p1.participant_id, pause_type="other")
        registry.emit_hitl_event(participant_id=p2.participant_id, pause_type="other")
        registry.emit_hitl_event(participant_id=p1.participant_id, pause_type="other")

        p1_events = registry.get_hitl_events(participant_id=p1.participant_id)
        assert len(p1_events) == 2
        assert all(e["participant_id"] == p1.participant_id for e in p1_events)

    def test_get_filters_by_party(self, registry):
        registry.register_participant(party_id="max@local", handle="raude")
        registry.register_participant(party_id="other@local", handle="other")

        max_p = registry.register_participant(party_id="max@local", handle="laude")
        other_p = registry.register_participant(party_id="other@local", handle="other2")
        registry.emit_hitl_event(participant_id=max_p.participant_id, pause_type="other")
        registry.emit_hitl_event(participant_id=other_p.participant_id, pause_type="other")

        max_events = registry.get_hitl_events(party_id="max@local")
        assert len(max_events) == 1
        assert max_events[0]["party_id"] == "max@local"

    def test_get_filters_by_time_window(self, registry):
        p = registry.register_participant(party_id="max@local", handle="raude")
        registry.emit_hitl_event(participant_id=p.participant_id, pause_type="other")
        now = time.time()

        windowed = registry.get_hitl_events(
            participant_id=p.participant_id,
            since=now - 5.0,
            until=now + 5.0,
        )
        assert len(windowed) == 1

        empty = registry.get_hitl_events(
            participant_id=p.participant_id,
            since=now + 3600.0,  # future window
        )
        assert empty == []

    def test_get_filters_by_pause_type(self, registry):
        p = registry.register_participant(party_id="max@local", handle="raude")
        registry.emit_hitl_event(participant_id=p.participant_id, pause_type="permission_request")
        registry.emit_hitl_event(participant_id=p.participant_id, pause_type="uncertainty_check")
        registry.emit_hitl_event(participant_id=p.participant_id, pause_type="permission_request")

        perms = registry.get_hitl_events(
            participant_id=p.participant_id,
            pause_type="permission_request",
        )
        assert len(perms) == 2
        assert all(e["pause_type"] == "permission_request" for e in perms)

    def test_get_orders_by_ts_desc(self, registry):
        """Most recent first — matches get_recent_by_handle convention."""
        p = registry.register_participant(party_id="max@local", handle="raude")
        for i in range(5):
            registry.emit_hitl_event(
                participant_id=p.participant_id,
                pause_type="other",
                task_context=f"event {i}",
            )
            time.sleep(0.001)  # ensure distinct timestamps

        events = registry.get_hitl_events(participant_id=p.participant_id)
        timestamps = [e["ts"] for e in events]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_get_respects_limit(self, registry):
        p = registry.register_participant(party_id="max@local", handle="raude")
        for _ in range(10):
            registry.emit_hitl_event(participant_id=p.participant_id, pause_type="other")

        events = registry.get_hitl_events(participant_id=p.participant_id, limit=3)
        assert len(events) == 3

    def test_get_empty_result_returns_empty_list(self, registry):
        assert registry.get_hitl_events(participant_id="never-existed") == []

    # ── hitl_rate ──

    def test_hitl_rate_empty_participant_returns_zero(self, registry):
        assert registry.hitl_rate("does-not-exist") == 0.0

    def test_hitl_rate_zero_window_returns_zero(self, registry):
        p = registry.register_participant(party_id="max@local", handle="raude")
        registry.emit_hitl_event(participant_id=p.participant_id, pause_type="other")
        assert registry.hitl_rate(p.participant_id, window_seconds=0) == 0.0

    def test_hitl_rate_counts_events_in_window(self, registry):
        p = registry.register_participant(party_id="max@local", handle="raude")
        for _ in range(5):
            registry.emit_hitl_event(participant_id=p.participant_id, pause_type="other")

        rate = registry.hitl_rate(p.participant_id, window_seconds=3600.0)
        assert rate == pytest.approx(5 / 3600.0, rel=1e-9)

    # ── hitl_stats ──

    def test_hitl_stats_total_and_breakdown(self, registry):
        p = registry.register_participant(party_id="max@local", handle="raude")
        registry.emit_hitl_event(participant_id=p.participant_id, pause_type="permission_request")
        registry.emit_hitl_event(participant_id=p.participant_id, pause_type="permission_request")
        registry.emit_hitl_event(participant_id=p.participant_id, pause_type="uncertainty_check")
        registry.emit_hitl_event(
            participant_id=p.participant_id,
            pause_type="rollback_confirm",
            resolved_without_operator=True,
        )

        stats = registry.hitl_stats(party_id="max@local")
        assert stats["total"] == 4
        assert stats["by_pause_type"]["permission_request"] == 2
        assert stats["by_pause_type"]["uncertainty_check"] == 1
        assert stats["by_pause_type"]["rollback_confirm"] == 1
        assert stats["resolved_without_operator"] == 1

    def test_hitl_stats_mean_gap(self, registry, monkeypatch):
        p = registry.register_participant(party_id="max@local", handle="raude")

        # Deterministic timestamps — no wall-clock sleeps. Monkeypatch the
        # registry module's ``time.time`` with an incrementing stub so the
        # three events land at distinct, known timestamps. Previous version
        # used ``time.sleep(0.02)`` which is flaky on slow CI runners and
        # slows the suite for no reason.
        from helix_context.identity import registry as registry_mod
        # Each emit_hitl_event() call consumes two ticks: one for the event
        # timestamp (now = time.time()) and one inside touch_heartbeat().
        # Provide 6 ticks — event ticks at 100.0 / 100.05 / 100.10 interleaved
        # with heartbeat ticks — so future tick-count drift does not re-break
        # this test.  Use itertools.cycle as an open-ended fallback guard.
        import itertools
        ticks = itertools.cycle([100.0, 100.0, 100.05, 100.05, 100.10, 100.10])
        monkeypatch.setattr(registry_mod.time, "time", lambda: next(ticks))

        registry.emit_hitl_event(participant_id=p.participant_id, pause_type="other")
        registry.emit_hitl_event(participant_id=p.participant_id, pause_type="other")
        registry.emit_hitl_event(participant_id=p.participant_id, pause_type="other")

        stats = registry.hitl_stats(party_id="max@local")
        # Gaps are 0.05s and 0.05s → mean 0.05s.
        assert stats["mean_gap_s"] is not None
        assert stats["mean_gap_s"] == pytest.approx(0.05, abs=1e-6)
        assert stats["mean_gap_s"] > 0

    def test_hitl_stats_fewer_than_two_events_has_none_mean_gap(self, registry):
        p = registry.register_participant(party_id="max@local", handle="raude")
        registry.emit_hitl_event(participant_id=p.participant_id, pause_type="other")
        stats = registry.hitl_stats(party_id="max@local")
        assert stats["mean_gap_s"] is None

    # ── hard-delete semantics ──

    def test_events_survive_participant_hard_delete(self, registry):
        """When a participant is hard-deleted by sweep, their HITL events
        must survive with participant_id NULLed — matching gene_attribution."""
        p = registry.register_participant(party_id="max@local", handle="raude")
        eid = registry.emit_hitl_event(
            participant_id=p.participant_id,
            pause_type="permission_request",
        )
        assert eid is not None

        # Force participant into gone state past hard-delete cutoff
        from helix_context.identity.registry import HARD_DELETE_AFTER_S
        registry.genome.conn.execute(
            "UPDATE participants SET last_heartbeat = ?, status = 'gone' "
            "WHERE participant_id = ?",
            (time.time() - HARD_DELETE_AFTER_S - 100, p.participant_id),
        )
        registry.genome.conn.commit()

        counts = registry.sweep()
        assert counts["hard_deleted"] >= 1

        # Event should survive with participant_id NULL
        events = registry.get_hitl_events(party_id="max@local")
        assert len(events) == 1
        assert events[0]["event_id"] == eid
        assert events[0]["participant_id"] is None
        assert events[0]["party_id"] == "max@local"


# ═══ Endpoint integration tests ═══════════════════════════════════════


class _ServerMockBackend:
    """Minimal ribosome mock matching the test_server.py pattern."""

    def complete(self, prompt: str, system: str = "", temperature: float = 0.0) -> str:
        if "compression engine" in system:
            return json.dumps({
                "codons": [{"meaning": "test_codon", "weight": 0.8, "is_exon": True}],
                "complement": "Compressed test content.",
                "promoter": {
                    "domains": ["test"],
                    "entities": ["TestEntity"],
                    "intent": "test",
                    "summary": "Test content for registry tests",
                },
            })
        return "{}"


@pytest.fixture
def client():
    config = HelixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=4),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        server=ServerConfig(upstream="http://localhost:11434"),
    )
    app = create_app(config)
    app.state.helix.ribosome.backend = _ServerMockBackend()
    with TestClient(app) as c:
        yield c


class TestRegisterEndpoint:
    def test_register_happy_path(self, client):
        resp = client.post("/sessions/register", json={
            "party_id": "max@local",
            "handle": "taude",
            "workspace": "/f/Projects/Education",
            "capabilities": ["ingest", "query"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["party_id"] == "max@local"
        assert data["participant_id"]
        assert data["heartbeat_interval_s"] > 0
        assert data["ttl_s"] > data["heartbeat_interval_s"]

    def test_register_missing_fields_returns_400(self, client):
        resp = client.post("/sessions/register", json={"party_id": "max@local"})
        assert resp.status_code == 400


class TestHeartbeatEndpoint:
    def test_heartbeat_happy_path(self, client):
        reg = client.post("/sessions/register", json={
            "party_id": "max@local",
            "handle": "taude",
        }).json()
        pid = reg["participant_id"]

        resp = client.post(f"/sessions/{pid}/heartbeat")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "active"

    def test_heartbeat_unknown_returns_404(self, client):
        resp = client.post("/sessions/bogus-id/heartbeat")
        assert resp.status_code == 404


class TestListEndpoint:
    def test_list_sees_registered_participant(self, client):
        client.post("/sessions/register", json={
            "party_id": "max@local",
            "handle": "taude",
        })
        client.post("/sessions/register", json={
            "party_id": "max@local",
            "handle": "laude",
        })

        resp = client.get("/sessions", params={"party_id": "max@local"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        handles = {p["handle"] for p in data["participants"]}
        assert handles == {"taude", "laude"}


class TestIngestAttribution:
    def test_ingest_with_participant_id_writes_attribution(self, client):
        reg = client.post("/sessions/register", json={
            "party_id": "max@local",
            "handle": "taude",
        }).json()
        pid = reg["participant_id"]

        resp = client.post("/ingest", json={
            "content": "VS Code 1.115 shipped with Agents companion app",
            "content_type": "text",
            "participant_id": pid,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        assert data.get("attributed", 0) >= 1

    def test_ingest_without_participant_id_skips_attribution(self, client):
        # local_federation=False opts out of trust-on-first-use env-based
        # attribution (default since the 4-layer federation work landed —
        # see _local_attribution_defaults in server.py). With the opt-out
        # + no explicit id, the server resolves no participant/party,
        # so the `attributed` field is suppressed from the response per
        # the server.py:580-583 guard.
        resp = client.post("/ingest", json={
            "content": "untagged note",
            "content_type": "text",
            "local_federation": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "attributed" not in data


class TestRecentEndpoint:
    def test_recent_returns_tagged_genes(self, client):
        reg = client.post("/sessions/register", json={
            "party_id": "max@local",
            "handle": "taude",
        }).json()
        pid = reg["participant_id"]

        client.post("/ingest", json={
            "content": "VS Code 1.115 released 2026-04-08 with Agents companion app",
            "content_type": "text",
            "participant_id": pid,
        })

        resp = client.get("/sessions/taude/recent")
        assert resp.status_code == 200
        data = resp.json()
        assert data["handle"] == "taude"
        assert data["count"] >= 1
        assert any("VS Code 1.115" in g["content_preview"] for g in data["genes"])

    def test_recent_unknown_handle_returns_empty(self, client):
        resp = client.get("/sessions/nobody/recent")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0


def test_participant_model_accepts_vendor_host_fields():
    from helix_context.schemas import Participant
    p = Participant(
        participant_id="abc",
        party_id="party",
        handle="laude",
        agent_kind="claude-code",
        mcp_host="vscode",
    )
    assert p.agent_kind == "claude-code"
    assert p.mcp_host == "vscode"


def test_participant_info_accepts_vendor_host_fields():
    from helix_context.schemas import ParticipantInfo
    p = ParticipantInfo(
        participant_id="abc",
        party_id="party",
        handle="laude",
        status="active",
        last_seen_s_ago=0.0,
        started_at=0.0,
        agent_kind="codex",
        mcp_host="cursor",
    )
    assert p.agent_kind == "codex"
    assert p.mcp_host == "cursor"


def test_participant_info_defaults_vendor_host_to_none():
    from helix_context.schemas import ParticipantInfo
    p = ParticipantInfo(
        participant_id="abc",
        party_id="party",
        handle="laude",
        status="active",
        last_seen_s_ago=0.0,
        started_at=0.0,
    )
    assert p.agent_kind is None
    assert p.mcp_host is None


def test_register_participant_persists_vendor_host(registry, genome):
    """register_participant stores agent_kind and mcp_host on the row."""
    p = registry.register_participant(
        party_id="party_x",
        handle="laude",
        agent_kind="claude-code",
        mcp_host="vscode",
    )
    assert p.agent_kind == "claude-code"
    assert p.mcp_host == "vscode"

    cur = genome.conn.cursor()
    row = cur.execute(
        "SELECT agent_kind, mcp_host FROM participants WHERE participant_id = ?",
        (p.participant_id,),
    ).fetchone()
    assert row[0] == "claude-code"
    assert row[1] == "vscode"


def test_register_participant_omitting_vendor_host_stores_null(registry, genome):
    """Backwards-compat: callers that don't pass the new fields get NULL."""
    p = registry.register_participant(party_id="party_y", handle="taude")
    assert p.agent_kind is None
    assert p.mcp_host is None


def test_list_participants_projects_vendor_host(registry):
    registry.register_participant(
        party_id="party_z",
        handle="laude",
        agent_kind="codex",
        mcp_host="cursor",
    )
    rows = registry.list_participants(party_id="party_z", status_filter="all")
    assert len(rows) == 1
    assert rows[0].agent_kind == "codex"
    assert rows[0].mcp_host == "cursor"


def test_get_participant_projects_vendor_host(registry):
    p = registry.register_participant(
        party_id="party_w",
        handle="laude",
        agent_kind="gemini",
        mcp_host="antigravity",
    )
    fetched = registry.get_participant(p.participant_id)
    assert fetched is not None
    assert fetched.agent_kind == "gemini"
    assert fetched.mcp_host == "antigravity"


def test_participant_model_accepts_announce_fields():
    from helix_context.schemas import Participant
    p = Participant(
        participant_id="abc",
        party_id="party",
        handle="laude",
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
        model_id="claude-opus-4-7",
    )
    assert p.ide_detected == "vscode"
    assert p.ide_detection_via == "env:VSCODE_PID"
    assert p.model_id == "claude-opus-4-7"


def test_participant_info_accepts_announce_fields():
    from helix_context.schemas import ParticipantInfo
    p = ParticipantInfo(
        participant_id="abc",
        party_id="party",
        handle="laude",
        status="active",
        last_seen_s_ago=0.0,
        started_at=0.0,
        ide_detected="cursor",
        ide_detection_via="env:CURSOR_TRACE_ID",
        model_id="gpt-5",
    )
    assert p.ide_detected == "cursor"
    assert p.ide_detection_via == "env:CURSOR_TRACE_ID"
    assert p.model_id == "gpt-5"


def test_participant_info_defaults_announce_fields_to_none():
    from helix_context.schemas import ParticipantInfo
    p = ParticipantInfo(
        participant_id="abc",
        party_id="party",
        handle="laude",
        status="active",
        last_seen_s_ago=0.0,
        started_at=0.0,
    )
    assert p.ide_detected is None
    assert p.ide_detection_via is None
    assert p.model_id is None


def test_register_participant_persists_announce_fields(registry, genome):
    p = registry.register_participant(
        party_id="party_a",
        handle="laude",
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
    )
    assert p.ide_detected == "vscode"
    assert p.ide_detection_via == "env:VSCODE_PID"
    assert p.model_id is None  # not yet announced

    cur = genome.conn.cursor()
    row = cur.execute(
        "SELECT ide_detected, ide_detection_via, model_id "
        "FROM participants WHERE participant_id = ?",
        (p.participant_id,),
    ).fetchone()
    assert row[0] == "vscode"
    assert row[1] == "env:VSCODE_PID"
    assert row[2] is None


def test_register_participant_omitting_announce_fields_stores_null(registry):
    p = registry.register_participant(party_id="party_b", handle="taude")
    assert p.ide_detected is None
    assert p.ide_detection_via is None
    assert p.model_id is None


def test_list_participants_projects_announce_fields(registry):
    registry.register_participant(
        party_id="party_c",
        handle="laude",
        ide_detected="cursor",
        ide_detection_via="env:CURSOR_TRACE_ID",
    )
    rows = registry.list_participants(party_id="party_c", status_filter="all")
    assert len(rows) == 1
    assert rows[0].ide_detected == "cursor"
    assert rows[0].ide_detection_via == "env:CURSOR_TRACE_ID"
    assert rows[0].model_id is None


def test_get_participant_projects_announce_fields(registry):
    p = registry.register_participant(
        party_id="party_d",
        handle="laude",
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
    )
    fetched = registry.get_participant(p.participant_id)
    assert fetched is not None
    assert fetched.ide_detected == "vscode"
    assert fetched.model_id is None


def test_update_announcement_sets_model_id(registry):
    p = registry.register_participant(
        party_id="party_e",
        handle="laude",
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
    )
    registry.update_announcement(
        participant_id=p.participant_id,
        model_id="claude-opus-4-7",
    )
    fetched = registry.get_participant(p.participant_id)
    assert fetched.model_id == "claude-opus-4-7"
    # IDE should be unchanged when no override is supplied
    assert fetched.ide_detected == "vscode"
    assert fetched.ide_detection_via == "env:VSCODE_PID"


def test_update_announcement_with_ide_override_sets_via_to_agent_override(registry):
    p = registry.register_participant(
        party_id="party_f",
        handle="laude",
        ide_detected="vscode",
        ide_detection_via="env:VSCODE_PID",
    )
    registry.update_announcement(
        participant_id=p.participant_id,
        model_id="gpt-5",
        ide_override="cursor",
    )
    fetched = registry.get_participant(p.participant_id)
    assert fetched.ide_detected == "cursor"
    assert fetched.ide_detection_via == "agent_override"
    assert fetched.model_id == "gpt-5"


def test_update_announcement_is_idempotent(registry):
    """Multiple calls overwrite — last write wins."""
    p = registry.register_participant(party_id="party_g", handle="laude")
    registry.update_announcement(participant_id=p.participant_id, model_id="claude-opus-4-7")
    registry.update_announcement(participant_id=p.participant_id, model_id="claude-sonnet-4-6")
    fetched = registry.get_participant(p.participant_id)
    assert fetched.model_id == "claude-sonnet-4-6"


def test_update_announcement_unknown_participant_id_silent_no_op(registry):
    """Calling update_announcement on an unknown id should not raise.
    The registry's existing heartbeat() updates silently no-op on unknown
    ids, and update_announcement should match that contract."""
    registry.update_announcement(
        participant_id="does-not-exist",
        model_id="claude-opus-4-7",
    )
    # Should not raise. No further assertion needed.
