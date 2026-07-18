"""
Sprint 4 - Seeded co-activation edges with Hebbian evidence decay.

Solves the "cold-start graph-topology" gap flagged in Sprint 2:
brand-new OPEN documents have no co_activated_with history, so the Tier 5
harmonic boost and Tier 5.5 SR propagation cannot surface them on the
first few retrievals after ingest. Seeding edges at ingest time - when
multi-signal agreement suggests two documents are semantically linked -
gives new documents a "social standing" in the graph before the first
retrieval lands.

Design
------
Three edge classes, provenance-tagged on harmonic_links.source:
  seeded            weight_mul 0.3   - admitted on multi-signal agreement
  co_retrieved      weight_mul 0.7   - observed co-retrieval in retrieval
  cwola_validated   weight_mul 1.0   - classifier-confirmed (Sprint 3)

Each edge carries Laplace-smoothed evidence counters:
  co_count          - # co-retrievals (both endpoints in retrieved set)
  miss_count        - sum of dense-rank weighted miss events

Effective weight during retrieval scoring:
  ratio = (co_count + 1) / (co_count + miss_count + 2)
  weight = raw_weight * source_mul * ratio

This gives new seeds Optimistic Neutrality (ratio=0.5 at no evidence),
promoting with sustained co-retrieval and attriting under contradiction.

Promotion thresholds:
  seeded -> co_retrieved         co_count >= 3 AND ratio >= 0.4
  co_retrieved -> cwola_validated (deferred to Sprint 3 trainer)

Pruning: effective weight < PRUNE_FLOOR drops the edge entirely.

Miss counting uses dense-rank so tied documents at the cutoff share the
same miss_weight, preventing stable-sort artifacts from creating
"persistent bad-luck" seeds.

See STATISTICAL_FUSION.md + SUCCESSOR_REPRESENTATION.md for context.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Dict, Iterable, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .genome import Genome

log = logging.getLogger("helix.seeded_edges")


SOURCE_SEEDED = "seeded"
SOURCE_CO_RETRIEVED = "co_retrieved"
SOURCE_CWOLA = "cwola_validated"

SOURCE_WEIGHT_MULTIPLIER = {
    SOURCE_SEEDED: 0.3,
    SOURCE_CO_RETRIEVED: 0.7,
    SOURCE_CWOLA: 1.0,
}

CO_PROMOTE_MIN_COUNT = 3      # co_count floor for seeded -> co_retrieved
CO_PROMOTE_MIN_RATIO = 0.4    # Laplace ratio floor for seeded -> co_retrieved
PRUNE_FLOOR = 0.05            # effective-weight floor below which edge is deleted
SEEDING_CAP = 200             # max documents per seed_edges call — O(n²) pair loop
                              # blows up beyond this; matches FRONTIER_CAP pattern
                              # in sr.py. Caller gets a warning + truncation.

# ── Cross-shard seeding (issue #223 fixture gap) ─────────────────────────
#
# seed_edges() below only ever sees ONE Genome connection, so every edge it
# writes is intra-shard by construction. Sharded bench fixtures
# (scripts/build_fixture_matrix.py) historically shipped ZERO harmonic_links
# rows at all -- neither intra- nor cross-shard -- which left
# ShardRouter._expand_cross_shard_coactivation (shard_router.py #120/#223)
# permanently unreachable in every sharded receipt. seed_cross_shard_edges()
# is the cross-connection sibling: same 2-of-4 multi-signal gate, bucketed
# by shared domain/entity token so the comparison stays sub-quadratic.
CROSS_SHARD_SEEDING_CAP = 400          # total cross-shard edges per pass
CROSS_SHARD_PER_SHARD_SAMPLE = 2000    # genes sampled per shard for bucketing
CROSS_SHARD_BUCKET_CAP = 40            # skip buckets larger than this (too
                                       # generic a token to be a real signal --
                                       # empirically, a single overly-generic
                                       # domain token (e.g. "completion") can
                                       # otherwise monopolize the entire cap
                                       # against one repeatedly-hit doc; 40
                                       # was picked by surveying a real medium
                                       # fixture's actual token-bucket sizes)


def _laplace_ratio(co: int, miss: float) -> float:
    """(co + 1) / (co + miss + 2). No evidence -> 0.5; strong co -> ~1.0;
    strong miss -> ~0.0. Prevents one-off events from flipping the edge."""
    return (co + 1) / (co + miss + 2)


def effective_weight(raw_weight: float, source: str, co: int, miss: float) -> float:
    """Retrieval-time weight factoring in provenance + Hebbian evidence."""
    mul = SOURCE_WEIGHT_MULTIPLIER.get(source, 1.0)
    return raw_weight * mul * _laplace_ratio(co, miss)


def dense_rank(sorted_scores: List[float]) -> Dict[int, int]:
    """Return {index_in_sorted_list: dense_rank}, rank 1 for the top.

    Tied scores share a rank. sorted_scores must already be sorted
    descending. Used for miss-weight computation so documents tied at the
    cutoff share the same penalty rather than having it arbitrarily
    pinned to whoever stable-sort happened to put first.
    """
    ranks: Dict[int, int] = {}
    current_rank = 0
    last_score: Optional[float] = None
    for i, s in enumerate(sorted_scores):
        if last_score is None or s != last_score:
            current_rank += 1
            last_score = s
        ranks[i] = current_rank
    return ranks


def miss_weight(rank: int, max_genes: int) -> float:
    """Dense-rank miss weight. Y ranked just below the cut -> near 1.0;
    Y ranked deep in the candidate pool -> near 0.0.

    Dense ranks start at 1, so rank <= 0 indicates a caller bug — raise
    rather than silently returning 0.0 (which would hide a programming
    error under the guise of "no miss penalty")."""
    if rank <= 0:
        raise ValueError(f"rank must be >= 1 (dense_rank 1-based); got {rank}")
    if rank <= max_genes:
        return 0.0
    return min(1.0, max_genes / rank)


def seed_edges(
    genome: "Genome",
    gene_ids: Iterable[str],
    *,
    overlap_fn=None,
    weight: float = 1.0,
) -> int:
    """Ingest-batch seeding pass over OPEN documents only.

    For each unordered pair in gene_ids, check if overlap_fn(knowledge store, a, b)
    returns True (multi-signal agreement gate); if so insert or
    refresh a 'seeded' edge. Returns the number of edges written.

    overlap_fn defaults to multi_signal_overlap below. Caller can pass
    a stub in tests.
    """
    if overlap_fn is None:
        overlap_fn = multi_signal_overlap
    gene_ids = list(gene_ids)
    if len(gene_ids) < 2:
        return 0
    if len(gene_ids) > SEEDING_CAP:
        log.warning(
            "seed_edges called with %d genes; capping to %d",
            len(gene_ids), SEEDING_CAP,
        )
        gene_ids = gene_ids[:SEEDING_CAP]
    import time as _time

    cur = genome.conn.cursor()
    now = _time.time()
    written = 0
    for i in range(len(gene_ids)):
        for j in range(i + 1, len(gene_ids)):
            a, b = gene_ids[i], gene_ids[j]
            if not overlap_fn(genome, a, b):
                continue
            try:
                cur.execute(
                    """INSERT INTO harmonic_links
                       (gene_id_a, gene_id_b, weight, updated_at,
                        source, co_count, miss_count, created_at)
                       VALUES (?, ?, ?, ?, 'seeded', 0, 0.0, ?)
                       ON CONFLICT(gene_id_a, gene_id_b) DO NOTHING""",
                    (a, b, weight, now, now),
                )
                if cur.rowcount:
                    written += 1
            except Exception:
                log.warning("seed_edges insert failed for (%s,%s)", a, b, exc_info=True)
    if written:
        genome.conn.commit()
    return written


def _extract_signals(
    promoter_json: Optional[str],
    key_values_json: Optional[str],
    chromatin: Optional[int],
) -> Dict[str, object]:
    """Parse one ``genes`` row's tag columns into the comparison sets used
    by the 2-of-4 multi-signal gate.

    Shared by :func:`multi_signal_overlap` (single-connection, exactly 2
    rows) and :func:`seed_cross_shard_edges` (many connections, bucketed)
    so the two can never drift on what counts as "overlap".
    """
    import json
    try:
        p = json.loads(promoter_json) if promoter_json else {}
    except Exception:
        p = {}
    try:
        kv = json.loads(key_values_json) if key_values_json else []
    except Exception:
        kv = []
    return {
        "domains": set(p.get("domains") or []),
        "entities": set(p.get("entities") or []),
        "kv_keys": {pair.split("=", 1)[0] for pair in kv if "=" in pair},
        "is_open": chromatin == 0,
    }


def _signal_overlap_count(sig_a: Dict[str, object], sig_b: Dict[str, object]) -> int:
    """Count of the 4 agreement signals two :func:`_extract_signals` dicts
    share: shared domain tag, shared entity tag, shared KV key (ignores
    value), both OPEN lifecycle tier. Same-directory proximity alone is
    NOT a signal - the gate's whole purpose is to avoid file-system
    coincidence edges."""
    signals = 0
    if sig_a["domains"] & sig_b["domains"]:
        signals += 1
    if sig_a["entities"] & sig_b["entities"]:
        signals += 1
    if sig_a["kv_keys"] & sig_b["kv_keys"]:
        signals += 1
    if sig_a["is_open"] and sig_b["is_open"]:
        signals += 1
    return signals


def multi_signal_overlap(genome: "Genome", gene_id_a: str, gene_id_b: str) -> bool:
    """Two-of-N signal agreement gate for ingest-batch seeding.

    Requires at least two of:
      - shared domain tag
      - shared entity tag
      - shared KV key (ignores value)
      - both OPEN lifecycle tier
    Same-directory proximity alone is NOT enough - the gate's whole
    purpose is to avoid file-system coincidence edges.
    """
    cur = genome.read_conn.cursor()
    rows = cur.execute(
        "SELECT gene_id, promoter, key_values, chromatin "
        "FROM genes WHERE gene_id IN (?, ?)", (gene_id_a, gene_id_b),
    ).fetchall()
    if len(rows) != 2:
        return False

    sig_a = _extract_signals(rows[0]["promoter"], rows[0]["key_values"], rows[0]["chromatin"])
    sig_b = _extract_signals(rows[1]["promoter"], rows[1]["key_values"], rows[1]["chromatin"])
    return _signal_overlap_count(sig_a, sig_b) >= 2


def seed_cross_shard_edges(
    shard_conns: Dict[str, sqlite3.Connection],
    *,
    weight: float = 1.0,
    cap: int = CROSS_SHARD_SEEDING_CAP,
    per_shard_sample: int = CROSS_SHARD_PER_SHARD_SAMPLE,
    bucket_cap: int = CROSS_SHARD_BUCKET_CAP,
) -> int:
    """Seed CROSS-shard ``harmonic_links`` edges (issue #223 fixture gap).

    :func:`seed_edges` only ever sees one ``Genome`` connection, so every
    edge it writes is intra-shard. ``ShardRouter._expand_cross_shard_coactivation``
    (helix_context/shard_router.py) needs at least some edges whose
    ``gene_id_b`` endpoint lives in a DIFFERENT shard's ``genes`` table to
    have anything to promote across a shard boundary. ``harmonic_links``
    carries no FOREIGN KEY on ``gene_id_b`` (storage/ddl.py) so a dangling
    cross-shard reference is schema-legal - the router already expects it,
    resolving the far endpoint via main.db's ``fingerprint_index``.

    Every sharded fixture ``scripts/build_fixture_matrix.py`` built before
    this fix shipped ZERO ``harmonic_links`` rows at all, which left the
    cross-shard co-activation path permanently unreachable in every
    sharded receipt (#223 re-scope).

    Uses the SAME 2-of-4 multi-signal gate as :func:`seed_edges`
    (:func:`_signal_overlap_count`), bucketed by shared domain/entity
    token so the comparison stays sub-quadratic in total gene count: only
    genes that already share a domain or entity token are ever compared
    pairwise. Buckets larger than ``bucket_cap`` are skipped outright (a
    token that generic isn't a meaningful signal, e.g. a domain tag
    shared by half the corpus).

    Each qualifying cross-shard pair (``a`` in shard X, ``b`` in shard Y,
    X != Y) gets edges written in BOTH directions (``gene_id_a=a`` in X
    AND ``gene_id_a=b`` in Y) since ``fetch_forward_neighbors`` only reads
    the ``gene_id_a`` direction - a one-directional edge would only be
    exploitable when that specific endpoint is the one a query ranks
    highly.

    Returns the total number of edge rows written (both directions
    counted), capped at ``cap``. Read failures on an individual shard are
    logged and that shard is skipped; insert failures on an individual
    pair are logged and that pair is skipped - neither is fatal to the
    overall pass.
    """
    import time as _time
    now = _time.time()

    tagged: List[Tuple[str, str, Dict[str, object]]] = []
    for shard_name, conn in shard_conns.items():
        try:
            rows = conn.execute(
                "SELECT gene_id, promoter, key_values, chromatin "
                "FROM genes ORDER BY gene_id LIMIT ?",
                (per_shard_sample,),
            ).fetchall()
        except Exception:
            log.warning(
                "seed_cross_shard_edges: genes read failed for shard %s",
                shard_name, exc_info=True,
            )
            continue
        for r in rows:
            gene_id, promoter, kv, chromatin = r[0], r[1], r[2], r[3]
            sig = _extract_signals(promoter, kv, chromatin)
            if sig["domains"] or sig["entities"]:
                tagged.append((shard_name, gene_id, sig))

    buckets: Dict[str, List[int]] = {}
    for idx, (_shard, _gid, sig) in enumerate(tagged):
        for tok in sig["domains"]:
            buckets.setdefault(f"d:{tok}", []).append(idx)
        for tok in sig["entities"]:
            buckets.setdefault(f"e:{tok}", []).append(idx)

    written = 0
    seen_pairs: set = set()
    # Smallest buckets first: when the cap binds, this spends the budget
    # across many distinct, specific tokens (diverse pairs) rather than
    # exhausting it on whichever merely-under-``bucket_cap`` generic token
    # happens to be processed first (dict order == first-seen order).
    for idxs in sorted(buckets.values(), key=len):
        if written >= cap:
            break
        if len(idxs) > bucket_cap:
            continue  # too generic a token to be a meaningful signal
        for i_pos in range(len(idxs)):
            if written >= cap:
                break
            i = idxs[i_pos]
            shard_i, gid_i, sig_i = tagged[i]
            for j_pos in range(i_pos + 1, len(idxs)):
                if written >= cap:
                    break
                j = idxs[j_pos]
                shard_j, gid_j, sig_j = tagged[j]
                if shard_i == shard_j:
                    continue  # intra-shard -- seed_edges()'s job, not ours
                pair_key = tuple(sorted(((shard_i, gid_i), (shard_j, gid_j))))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                if _signal_overlap_count(sig_i, sig_j) < 2:
                    continue
                for (s_from, g_from), (_s_to, g_to) in (
                    ((shard_i, gid_i), (shard_j, gid_j)),
                    ((shard_j, gid_j), (shard_i, gid_i)),
                ):
                    if written >= cap:
                        break
                    try:
                        cur = shard_conns[s_from].cursor()
                        cur.execute(
                            """INSERT INTO harmonic_links
                               (gene_id_a, gene_id_b, weight, updated_at,
                                source, co_count, miss_count, created_at)
                               VALUES (?, ?, ?, ?, 'seeded', 0, 0.0, ?)
                               ON CONFLICT(gene_id_a, gene_id_b) DO NOTHING""",
                            (g_from, g_to, weight, now, now),
                        )
                        if cur.rowcount:
                            written += 1
                    except Exception:
                        log.warning(
                            "seed_cross_shard_edges insert failed for (%s,%s)",
                            g_from, g_to, exc_info=True,
                        )
    for conn in shard_conns.values():
        try:
            conn.commit()
        except Exception:
            log.debug("seed_cross_shard_edges commit failed", exc_info=True)
    return written


def update_edge_evidence(
    genome: "Genome",
    gene_scores: Dict[str, float],
    expressed_ids: List[str],
    *,
    max_genes: int,
) -> int:
    """Walk seeded / co_retrieved edges anchored at retrieved documents,
    increment co_count or miss_count per dense-rank miss weighting.

    Returns count of edges updated. Intended to be called once per
    retrieval after candidates are finalised. Caller owns the commit
    cadence (we commit here; override via transaction-control if that
    becomes a contention bottleneck on very high QPS).
    """
    if not expressed_ids or not gene_scores:
        return 0

    sorted_ids = sorted(gene_scores.keys(), key=lambda g: gene_scores[g], reverse=True)
    sorted_scores = [gene_scores[g] for g in sorted_ids]
    ranks = dense_rank(sorted_scores)
    id_to_rank = {g: ranks[i] for i, g in enumerate(sorted_ids)}
    expressed_set = set(expressed_ids)

    cur = genome.conn.cursor()
    # Dedupe: an edge (a,b) must update exactly once even if both a and b
    # are in expressed_ids. Collect unique edges first, then walk.
    placeholders = ",".join("?" * len(expressed_ids))
    try:
        edges = cur.execute(
            f"""SELECT gene_id_a, gene_id_b, weight, co_count, miss_count, source
                FROM harmonic_links
                WHERE (gene_id_a IN ({placeholders}) OR gene_id_b IN ({placeholders}))
                  AND source IN ('seeded', 'co_retrieved')""",
            (*expressed_ids, *expressed_ids),
        ).fetchall()
    except Exception:
        log.debug("update_edge_evidence read failed", exc_info=True)
        return 0

    updates = 0
    for row in edges:
        a, b = row["gene_id_a"], row["gene_id_b"]
        if a == b:
            continue
        # Decide which endpoint anchors this event. If both are
        # retrieved it's a co_count event (order doesn't matter). If
        # one is retrieved, the other is either a candidate miss or
        # outside the pool (candidacy gate via id_to_rank).
        if a in expressed_set and b in expressed_set:
            new_co = row["co_count"] + 1
            new_miss = row["miss_count"]
            new_source = row["source"]
            if (
                new_source == SOURCE_SEEDED
                and new_co >= CO_PROMOTE_MIN_COUNT
                and _laplace_ratio(new_co, new_miss) >= CO_PROMOTE_MIN_RATIO
            ):
                new_source = SOURCE_CO_RETRIEVED
            cur.execute(
                """UPDATE harmonic_links SET co_count=?, source=?, updated_at=strftime('%s','now')
                   WHERE gene_id_a=? AND gene_id_b=?""",
                (new_co, new_source, a, b),
            )
            updates += 1
            continue

        # Exactly one endpoint is in expressed_set - the other is the
        # candidate to weigh for miss. Candidacy gate: must also appear
        # in gene_scores (i.e. scored nonzero on some tier).
        other = b if a in expressed_set else a
        rank = id_to_rank.get(other)
        if rank is None:
            continue
        mw = miss_weight(rank, max_genes)
        if mw <= 0:
            continue
        new_miss = row["miss_count"] + mw
        new_co = row["co_count"]
        eff = effective_weight(row["weight"], row["source"], new_co, new_miss)
        if eff < PRUNE_FLOOR:
            cur.execute(
                "DELETE FROM harmonic_links WHERE gene_id_a=? AND gene_id_b=?",
                (a, b),
            )
        else:
            cur.execute(
                """UPDATE harmonic_links SET miss_count=?, updated_at=strftime('%s','now')
                   WHERE gene_id_a=? AND gene_id_b=?""",
                (new_miss, a, b),
            )
        updates += 1
    if updates:
        genome.conn.commit()
    return updates
