"""ablation_ladder.py — in-process per-layer ablation probe for the ERB beds.

One retrieval layer is disabled per arm against a ``baseline`` arm that runs
the shipped config untouched. The point is a cost/impact re-probe: what does
each layer buy in rank quality, and what does it cost in wall time?

Every arm is built from a FRESH ``load_config()`` object mutated for that arm
and a FRESH ``CymatixContextManager`` (config is per-manager, and several
gates are read once at construction into ``KnowledgeStore`` attributes — a
mid-run mutation would not take).

────────────────────────────────────────────────────────────────────────
Verified query-time gates (file:line, this worktree)
────────────────────────────────────────────────────────────────────────

Every knob below was traced from ``config.py`` through manager construction to
the *branch that actually runs at query time*. Arms whose candidate knob had
no such branch are DROPPED, with the evidence recorded in the receipt under
``dropped_arms`` (and repeated below).

KEPT
  no_splade      [ingestion] splade_enabled
                 config.py:271 -> context_manager.py:965 (splade_enabled=)
                 -> knowledge_store.py:667 (self._splade_enabled)
                 -> knowledge_store.py:2783 `if self._splade_enabled:` (Tier 3.5)
                 signal key: 'splade' (knowledge_store.py:2842)

  no_dense       [retrieval] dense_embedding_enabled
                 config.py:478 -> context_manager.py:986
                 -> knowledge_store.py:710 (self._dense_embedding_enabled)
                 -> context_manager.py:2946 (ANN vs plain query_docs branch)
                 -> knowledge_store.py:2972 (Tier 6 dense) and :3757 (dense recall)
                 signal keys: 'dense' (knowledge_store.py:3040),
                              'ann_dense_leg' (knowledge_store.py:3906)

  no_sema        [ingestion] sema_embed_on_ingest  (#227 semantics: this is the
                 GLOBAL sema switch, not merely an ingest-write flag — it gates
                 codec *materialisation*, so the query side dies with it)
                 config.py:291 -> context_manager.py:903-935 (self._sema_codec
                 stays None when false) -> context_manager.py:953 (sema_codec=)
                 -> knowledge_store.py:665 -> knowledge_store.py:2848
                 `if self._sema_codec is not None:` (Tier 4 A/B)
                 signal key: 'sema_boost' (knowledge_store.py:2894)

  no_cymatics    [cymatics] enabled
                 config.py:390 -> context_manager.py:1216 (self._use_cymatics)
                 -> context_manager.py:3119 (cymatics_enabled=)
                 -> scoring/blend.py:212 `if use_cymatics and cymatics_enabled ...`
                 NO signal key (the blend runs in the manager, not the store).
                 Validated by a call-probe on scoring.cymatics.query_spectrum,
                 which blend.py imports at call time (blend.py:214).

  no_synonyms    [synonyms] map emptied (config.synonym_map)
                 config.py:1097 -> context_manager.py:952 (synonym_map=)
                 -> knowledge_store.py:664 (self.synonym_map)
                 -> knowledge_store.py:1773-1784 `_expand_terms`, called first
                 thing in query_genes (knowledge_store.py:2305-2306).
                 NO signal key. Validated by probing ``_expand_terms`` directly
                 with the baseline map's own keys.

  no_classifier  [classifier] enabled
                 config.py:886 -> context_manager.py:1801-1807 (classify_query
                 only called when enabled) -> assembly cap at :2101-2113 and
                 decoder hint at :1829.
                 NO signal key. Validated by the absence of
                 ``window.metadata["classifier"]`` (written at
                 context_manager.py:2316 only when classifier_result is not None).

CONDITIONAL — kept in the arm table, dropped at RUNTIME when the effective
config already sits at the ablated value (an ablation that changes nothing
measures nothing, and reporting it as a null result would be a lie):
  no_cold_tier   [context] cold_tier_enabled -> context_manager.py:3013-3018
  no_sr          [retrieval] sr_enabled -> knowledge_store.py:3162
  no_seeded_edges [retrieval] seeded_edges_enabled -> knowledge_store.py:3462
On the shipped cymatix.toml all three are already ``false`` (cymatix.toml:311,
:349, :359), so all three drop. The check is done against the LOADED config,
not against my reading of the toml, so a future flip re-arms them for free.

DROPPED (no query-time gate — see ``dropped_arms`` in the receipt)
  no_plr         [plr] enabled is consumed ONLY by the HTTP layer:
                 server/routes_context.py:635 -> server/helpers.py:444
                 ``_compute_plr_confidence``. retrieval/fusion_plr.py:31 says it
                 "leaves document ranking untouched" — it decorates the
                 /context/packet response with a query-quality log-odds. It is
                 not on the ``build_context`` path at all, so an in-process arm
                 would measure exactly zero by construction.

  no_harmonic    ``use_harmonic`` is a hardcoded ``True`` default on
                 context_manager.py:2895 (``_retrieve``) and
                 knowledge_store.py:2259 (``query_docs``), and NEITHER call site
                 (context_manager.py:1945, :1965) passes it — so it is a dead
                 parameter, never fed from config. The Tier-5 harmonic block
                 (knowledge_store.py:3119) and the co-activation pull-forward
                 (``_expand_coactivated``, knowledge_store.py:3494) are
                 therefore unconditional. ``[cymatics] harmonic_links`` looks
                 like the gate but is not: context_manager.py:2353 shows it
                 gates the ingest/expression-time edge WRITE, inside a
                 ``if not read_only`` block, not the query-time read.
                 No clean off-switch => dropped.

  no_tag_*       Tier 1 (tag_exact, knowledge_store.py:2697) and Tier 2
                 (tag_prefix, knowledge_store.py:2720) run unconditionally.
                 The #327 knobs are DISCIPLINE knobs whose ablated state is
                 already the default — ``tag_idf_enabled`` (config.py:660,
                 default False) and ``tag_df_cap`` (config.py:668, default 0.0)
                 turn discipline ON, so flipping them is an intervention, not
                 an ablation. The only mute available is weight-zeroing
                 (``tag_prefix_weight = 0.0``), which silences the tier's
                 contribution while still paying its SQL cost — that is not a
                 layer ablation and would misreport the cost axis. Dropped.

────────────────────────────────────────────────────────────────────────
Self-validation
────────────────────────────────────────────────────────────────────────

An ablation that silently failed to apply is worse than no measurement: it
reports the layer as worthless. So every arm carries a positive observable and
is checked twice —

  1. The arm's expected observable must be ABSENT in the arm.
  2. The same observable must be PRESENT in the baseline arm. Without this the
     arm is *unfalsifiable* (e.g. 'hebbian' never fires under read_only=True,
     so "no hebbian" proves nothing), and it is recorded arm_valid=false with
     reason ``unfalsifiable`` rather than counted as a clean ablation.

Baseline therefore always runs, even if ``--arms`` omits it.

────────────────────────────────────────────────────────────────────────
Measurement caveats (on the record)
────────────────────────────────────────────────────────────────────────

* Ranks come from ``manager.genome.last_query_scores`` — the same source
  ``benchmarks/dogfood/run_ladder.py`` uses. That map is POST-blend: the blend
  layer republishes it (scoring/blend.py:233, :281), so cymatics / harmonic-bin
  mutations ARE reflected. It is not, however, the delivered slice, so the
  receipt also records ``delivered_gold`` from ``window.expressed_gene_ids``.
* Ties are broken by ``gene_id`` ascending, not dict insertion order, so ranks
  are reproducible across arms and processes.
* Timings are wall ms on a shared box. Treat them as within-run relatives; no
  absolute-ms claim is made or asserted anywhere.
* The bed is opened read-write by ``KnowledgeStore`` construction (index/table
  DDL is idempotent-on-open), and queries pass ``read_only=True`` so
  ``touch_genes`` / ``link_coactivated`` / the Hebbian edge update never run
  (context_manager.py:2348, knowledge_store.py:3462). ``cwola_log`` is written
  by the HTTP route, not by ``build_context``, so an in-process run appends
  nothing there.
* The encoder daemon is the CALLER's business. This script never starts or
  stops one; it records ``encoder_client.active_url()`` and the dense codec's
  concrete class per arm so the receipt says which side the encode ran on.

Usage::

    python benchmarks/dogfood/erb/ablation_ladder.py --stamp 20260805-1200
    python benchmarks/dogfood/erb/ablation_ladder.py --arms baseline,no_dense
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Retrieval-only probe: never let a query mutate the bed's learning state.
# Must be set BEFORE any manager is constructed.
os.environ.setdefault("CYMATIX_DISABLE_LEARN", "1")


# ── Arm table ────────────────────────────────────────────────────────────

VALIDATION_CONTROL = "control"
VALIDATION_SIGNAL = "signal_absent"
VALIDATION_TIER = "tier_contrib_absent"
VALIDATION_METADATA = "classifier_metadata_absent"
VALIDATION_EXPANSION = "synonym_expansion_absent"
VALIDATION_CALL_PROBE = "cymatics_call_probe"
VALIDATION_COLD = "cold_tier_flag_absent"


@dataclass(frozen=True)
class Arm:
    """One ablation arm.

    ``knobs`` maps dotted config paths to the ablated value. ``gate`` is the
    verified file:line of the query-time branch the knob controls — recorded
    in the receipt so a reader can re-check the claim without re-deriving it.
    """

    name: str
    knobs: Mapping[str, Any] = field(default_factory=dict)
    gate: str = ""
    validation: str = VALIDATION_SIGNAL
    # Observables expected to vanish. Interpreted per ``validation`` kind.
    expect_absent: Tuple[str, ...] = ()


ARMS: Tuple[Arm, ...] = (
    Arm(
        name="baseline",
        knobs={},
        gate="—  (shipped config, untouched)",
        validation=VALIDATION_CONTROL,
    ),
    Arm(
        name="no_splade",
        knobs={"ingestion.splade_enabled": False},
        gate="knowledge_store.py:2783 `if self._splade_enabled:`",
        validation=VALIDATION_SIGNAL,
        expect_absent=("splade",),
    ),
    Arm(
        name="no_dense",
        knobs={"retrieval.dense_embedding_enabled": False},
        gate=("knowledge_store.py:2972 / :3757 `if self._dense_embedding_enabled` "
              "+ context_manager.py:2946 ANN-branch selector"),
        validation=VALIDATION_SIGNAL,
        expect_absent=("dense", "ann_dense_leg"),
    ),
    Arm(
        name="no_sema",
        knobs={"ingestion.sema_embed_on_ingest": False},
        gate=("context_manager.py:903-935 (codec never materialised) -> "
              "knowledge_store.py:2848 `if self._sema_codec is not None:`"),
        validation=VALIDATION_SIGNAL,
        expect_absent=("sema_boost",),
    ),
    Arm(
        name="no_cold_tier",
        knobs={"context.cold_tier_enabled": False},
        gate="context_manager.py:3013-3018 cold_enabled gate",
        validation=VALIDATION_COLD,
        expect_absent=("cold_tier_used",),
    ),
    Arm(
        name="no_sr",
        knobs={"retrieval.sr_enabled": False},
        gate="knowledge_store.py:3162 `sr_enabled = self._sr_enabled if use_sr is None`",
        validation=VALIDATION_TIER,
        expect_absent=("sr",),
    ),
    Arm(
        name="no_seeded_edges",
        knobs={"retrieval.seeded_edges_enabled": False},
        gate=("knowledge_store.py:3462 `if self._seeded_edges_enabled and "
              "ranked_ids and not read_only:`"),
        validation=VALIDATION_SIGNAL,
        expect_absent=("hebbian",),
    ),
    Arm(
        name="no_cymatics",
        knobs={"cymatics.enabled": False},
        gate=("context_manager.py:1216 -> :3119 -> scoring/blend.py:212 "
              "`if use_cymatics and cymatics_enabled ...`"),
        validation=VALIDATION_CALL_PROBE,
        expect_absent=("scoring.cymatics.query_spectrum",),
    ),
    Arm(
        name="no_synonyms",
        knobs={"synonym_map": {}},
        gate="knowledge_store.py:1773-1784 `_expand_terms` (called at :2305-2306)",
        validation=VALIDATION_EXPANSION,
        expect_absent=("synonym_expansion",),
    ),
    Arm(
        name="no_classifier",
        knobs={"classifier.enabled": False},
        gate=("context_manager.py:1801-1807 classify_query gate; metadata at "
              ":2316; assembly cap at :2101-2113"),
        validation=VALIDATION_METADATA,
        expect_absent=("classifier",),
    ),
)

# Arms considered and rejected at authoring time, with the evidence. Copied
# into the receipt verbatim so the null-result question ("did you even try
# PLR?") has an answer that does not require reading this file.
DROPPED_ARMS: Tuple[Dict[str, str], ...] = (
    {
        "arm": "no_plr",
        "knob": "[plr] enabled",
        "reason": (
            "Not on the build_context path. [plr] enabled is read only by "
            "server/routes_context.py:635 -> server/helpers.py:444 "
            "_compute_plr_confidence, which decorates the /context/packet "
            "response; retrieval/fusion_plr.py:31 states it 'leaves document "
            "ranking untouched'. An in-process arm would measure zero by "
            "construction, not by evidence."
        ),
    },
    {
        "arm": "no_harmonic",
        "knob": "use_harmonic / [cymatics] harmonic_links",
        "reason": (
            "No query-time off-switch. `use_harmonic` defaults True at "
            "context_manager.py:2895 and knowledge_store.py:2259 and is passed "
            "by NO call site (context_manager.py:1945, :1965), so Tier 5 "
            "(knowledge_store.py:3119) and _expand_coactivated "
            "(knowledge_store.py:3494) are unconditional. [cymatics] "
            "harmonic_links gates only the ingest/expression-time edge WRITE "
            "inside the `if not read_only` block at context_manager.py:2353."
        ),
    },
    {
        "arm": "no_tag_prefix / tag discipline",
        "knob": "[retrieval] tag_idf_enabled, tag_df_cap, tag_prefix_weight",
        "reason": (
            "Tier 1 (knowledge_store.py:2697) and Tier 2 (:2720) are "
            "unconditional. The #327 knobs default to OFF (config.py:660, "
            ":668), so flipping them is an intervention, not an ablation. The "
            "only mute is weight-zeroing (tag_prefix_weight=0.0), which "
            "silences the contribution while still paying the SQL cost — that "
            "would misreport the cost axis, so no arm is offered."
        ),
    },
)


# ── Pure helpers (unit-tested in tests/test_ablation_tools.py) ───────────


def set_dotted(cfg: Any, path: str, value: Any) -> bool:
    """Set ``cfg.<section>.<key> = value`` (or ``cfg.<key>`` for a bare path).

    Returns False when the path does not exist, so a renamed/removed knob is a
    loud SKIP rather than a silently-null arm (the run_ladder.py lesson).
    """
    section, sep, key = path.partition(".")
    if not sep:
        if not hasattr(cfg, section):
            return False
        setattr(cfg, section, value)
        return True
    obj = getattr(cfg, section, None)
    if obj is None or not hasattr(obj, key):
        return False
    setattr(obj, key, value)
    return True


_MISSING = object()


def get_dotted(cfg: Any, path: str) -> Any:
    """Read ``cfg.<section>.<key>``; returns the ``_MISSING`` sentinel if absent."""
    section, sep, key = path.partition(".")
    if not sep:
        return getattr(cfg, section, _MISSING)
    obj = getattr(cfg, section, None)
    if obj is None:
        return _MISSING
    return getattr(obj, key, _MISSING)


def arm_is_identity(cfg: Any, arm: Arm) -> Optional[str]:
    """Return a reason string when *arm* would not change *cfg*, else None.

    An ablation whose knob already sits at the ablated value in the effective
    config is an identity transform: running it produces a "no effect" number
    that says nothing about the layer. Caught here rather than published.
    """
    if not arm.knobs:
        return None
    unchanged: List[str] = []
    for path, value in arm.knobs.items():
        current = get_dotted(cfg, path)
        if current is _MISSING:
            return f"unknown config path: {path}"
        if current == value:
            unchanged.append(f"{path} already {value!r}")
    if len(unchanged) == len(arm.knobs):
        return "identity vs baseline — " + "; ".join(unchanged)
    return None


def rank_gold(
    scores: Mapping[str, float], gold: Sequence[str] | set, depth: Optional[int] = None,
) -> Tuple[Optional[int], List[int]]:
    """Rank the gold ids inside a ``{gene_id: score}`` map.

    Returns ``(rank_of_first_gold, [all gold ranks])``, 1-indexed, sorted by
    score descending with ``gene_id`` ascending as the tiebreak so the ordering
    is reproducible across arms and processes (dict insertion order is not).
    ``depth`` truncates the considered list; None considers the whole map.
    """
    gold_set = set(gold)
    ordered = [gid for gid, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]
    if depth is not None:
        ordered = ordered[:depth]
    ranks = [i for i, gid in enumerate(ordered, 1) if gid in gold_set]
    return (ranks[0] if ranks else None, ranks)


def recall_at_k(first_ranks: Sequence[Optional[int]], k: int) -> float:
    """Fraction of queries whose first gold landed at rank <= k. Empty -> 0.0."""
    if not first_ranks:
        return 0.0
    hit = sum(1 for r in first_ranks if r is not None and r <= k)
    return round(hit / len(first_ranks), 4)


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile (no interpolation). Empty -> None.

    Nearest-rank keeps p95 of a 30-query arm an actually-observed value rather
    than an interpolated fiction between two samples.
    """
    if not values:
        return None
    ordered = sorted(values)
    idx = int(round((pct / 100.0) * len(ordered) + 0.5)) - 1
    idx = max(0, min(idx, len(ordered) - 1))
    return round(float(ordered[idx]), 3)


def median_or_none(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return round(float(statistics.median(values)), 3)


def validate_arm(
    arm: Arm,
    observed: Mapping[str, Any],
    baseline: Optional[Mapping[str, Any]],
) -> Tuple[bool, str]:
    """Decide arm_valid for *arm* from its observables and the baseline's.

    *observed* / *baseline* are the per-arm observable bundles built by
    ``_observables``: signal keys, tier-contribution keys, classifier-metadata
    query count, synonym-expansion count, cymatics call count, cold-tier flag.

    Two conditions, both required:
      * the ablated observable is gone in this arm, and
      * it was present in the baseline (otherwise the arm proves nothing and is
        reported as ``unfalsifiable``, not as a clean ablation).
    """
    if arm.validation == VALIDATION_CONTROL:
        return True, "control arm — shipped config, no ablation to validate"

    if arm.validation in (VALIDATION_SIGNAL, VALIDATION_TIER):
        field_name = "signal_keys" if arm.validation == VALIDATION_SIGNAL else "tier_keys"
        arm_keys = set(observed.get(field_name) or ())
        base_keys = set((baseline or {}).get(field_name) or ())
        expected = list(arm.expect_absent)
        fired_at_baseline = [s for s in expected if s in base_keys]
        if not fired_at_baseline:
            return False, (
                f"unfalsifiable: none of {expected} appear in the baseline "
                f"{field_name} — the layer never ran at baseline, so its "
                "absence here is not evidence of an ablation"
            )
        still_present = [s for s in expected if s in arm_keys]
        if still_present:
            return False, (
                f"ablation did not take: {still_present} still present in "
                f"{field_name}"
            )
        return True, (
            f"{field_name}: {sorted(fired_at_baseline)} present at baseline, "
            f"absent in this arm"
        )

    if arm.validation == VALIDATION_METADATA:
        arm_n = int(observed.get("classifier_metadata_queries") or 0)
        base_n = int((baseline or {}).get("classifier_metadata_queries") or 0)
        if base_n == 0:
            return False, (
                "unfalsifiable: window.metadata['classifier'] absent at "
                "baseline too (context_manager.py:2316 never fired)"
            )
        if arm_n:
            return False, (
                f"ablation did not take: window.metadata['classifier'] still "
                f"written on {arm_n} queries"
            )
        return True, (
            f"window.metadata['classifier'] written on {base_n} baseline "
            "queries, 0 here"
        )

    if arm.validation == VALIDATION_EXPANSION:
        arm_n = int(observed.get("synonym_expansions") or 0)
        base_n = int((baseline or {}).get("synonym_expansions") or 0)
        if base_n == 0:
            return False, (
                "unfalsifiable: baseline synonym map expanded 0 probe terms "
                "(empty or non-matching [synonyms] section)"
            )
        if arm_n:
            return False, (
                f"ablation did not take: _expand_terms still expanded {arm_n} "
                "probe terms"
            )
        return True, (
            f"_expand_terms expanded {base_n} probe terms at baseline, 0 here"
        )

    if arm.validation == VALIDATION_CALL_PROBE:
        arm_n = int(observed.get("cymatics_calls") or 0)
        base_n = int((baseline or {}).get("cymatics_calls") or 0)
        if base_n == 0:
            return False, (
                "unfalsifiable: scoring.cymatics.query_spectrum was never "
                "called at baseline (blend.py:212 needs >1 candidate)"
            )
        if arm_n:
            return False, (
                f"ablation did not take: query_spectrum still called {arm_n} times"
            )
        return True, (
            f"scoring.cymatics.query_spectrum called {base_n}x at baseline, 0 here"
        )

    if arm.validation == VALIDATION_COLD:
        arm_n = int(observed.get("cold_tier_queries") or 0)
        base_n = int((baseline or {}).get("cold_tier_queries") or 0)
        if base_n == 0:
            return False, (
                "unfalsifiable: cold tier never fired at baseline "
                "(_last_cold_tier_used False on every query)"
            )
        if arm_n:
            return False, f"ablation did not take: cold tier fired on {arm_n} queries"
        return True, f"cold tier fired on {base_n} baseline queries, 0 here"

    return False, f"validation='{arm.validation}': no observable implemented"


def layer_signal_medians(per_query_signals: Sequence[Mapping[str, float]]) -> Dict[str, float]:
    """Median ms per signal across a query set. Signals missing from a query
    are OMITTED from that signal's sample, not counted as zero — a tier that
    ran on 3 of 30 queries should report its own cost, not a diluted one."""
    buckets: Dict[str, List[float]] = {}
    for sig in per_query_signals:
        for name, ms in (sig or {}).items():
            buckets.setdefault(name, []).append(float(ms))
    return {
        name: round(float(statistics.median(vals)), 3)
        for name, vals in sorted(buckets.items())
    }


# ── Runtime plumbing ────────────────────────────────────────────────────


class _CallProbe:
    """Count calls to ``module.attr`` for the duration of the context.

    Used where a layer has no timing signal of its own. Restores the original
    attribute unconditionally so one arm cannot leak instrumentation into the
    next.
    """

    def __init__(self, module: Any, attr: str) -> None:
        self._module = module
        self._attr = attr
        self._orig: Any = None
        self.count = 0

    def __enter__(self) -> "_CallProbe":
        self._orig = getattr(self._module, self._attr)
        orig = self._orig
        probe = self

        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            probe.count += 1
            return orig(*args, **kwargs)

        setattr(self._module, self._attr, _wrapper)
        return self

    def __exit__(self, *exc: Any) -> bool:
        setattr(self._module, self._attr, self._orig)
        return False


def _git_sha(repo_root: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (REPO_ROOT / p)


def _probe_synonym_expansions(genome: Any, probe_terms: Sequence[str]) -> int:
    """How many probe terms ``_expand_terms`` actually widened.

    Direct observable for the synonyms layer: feed each probe term in alone and
    count the ones that came back with more than themselves.
    """
    expanded = 0
    for term in probe_terms:
        try:
            out = genome._expand_terms([term])
        except Exception:
            continue
        if len(set(out)) > 1:
            expanded += 1
    return expanded


def _dense_codec_name(genome: Any) -> Optional[str]:
    codec = getattr(genome, "_dense_codec", None)
    return type(codec).__name__ if codec is not None else None


def run_arm(
    arm: Arm,
    *,
    genome_path: str,
    config_path: Optional[str],
    needles: Sequence[Mapping[str, str]],
    gold_by_needle: Mapping[str, set],
    k: int,
) -> Dict[str, Any]:
    """Build a manager for *arm*, run every needle, return the receipt row."""
    from cymatix_context.backends import encoder_client
    from cymatix_context.config import load_config
    from cymatix_context.context_manager import CymatixContextManager
    from cymatix_context.scoring import cymatics as cymatics_mod

    cfg = load_config(config_path) if config_path else load_config()
    cfg.genome.path = genome_path

    unapplied = [p for p, v in arm.knobs.items() if not set_dotted(cfg, p, v)]
    if unapplied:
        return {
            "arm": arm.name,
            "knobs_changed": dict(arm.knobs),
            "arm_valid": False,
            "validation": f"unknown config path(s): {unapplied}",
            "skipped": True,
            "errors": [f"config paths not found: {unapplied}"],
        }

    # Probe terms are the BASELINE synonym keys — taken from the freshly-loaded
    # config before the arm may have blanked the map, so the no_synonyms arm
    # probes the same terms the baseline did.
    probe_terms = sorted(load_config(config_path).synonym_map if config_path
                         else load_config().synonym_map)[:12]

    errors: List[str] = []
    first_ranks: List[Optional[int]] = []
    all_ranks: List[int] = []
    latencies: List[float] = []
    per_query_signals: List[Dict[str, float]] = []
    signal_keys: set = set()
    tier_keys: set = set()
    classifier_metadata_queries = 0
    cold_tier_queries = 0
    delivered_gold_hits = 0
    n_scored = 0

    manager = CymatixContextManager(cfg)
    warmed = False
    try:
        # Untimed warmup (erb receipt convention). [hardware] lazy_encoders is
        # on by default, so the FIRST query of an arm pays the ~2 GB BGE-M3
        # load plus SPLADE/SEMA construction — measured at ~40 s vs ~20 s for
        # a warm query on the 100K carve. Folding that into p50 would make
        # every arm's cost a function of how many needles it ran. The warmup
        # is outside the cymatics call-probe so it cannot inflate that count.
        if needles:
            try:
                manager.build_context(
                    needles[0]["query"], read_only=True,
                    ignore_delivered=True, max_genes=k,
                )
                warmed = True
            except Exception as exc:  # noqa: BLE001
                errors.append(f"warmup: {type(exc).__name__}: {exc}")

        with _CallProbe(cymatics_mod, "query_spectrum") as cymatics_probe:
            for needle in needles:
                name = needle["name"]
                gold = gold_by_needle.get(name, set())
                t0 = time.perf_counter()
                try:
                    window = manager.build_context(
                        needle["query"],
                        read_only=True,
                        ignore_delivered=True,
                        max_genes=k,
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{name}: {type(exc).__name__}: {exc}")
                    latencies.append((time.perf_counter() - t0) * 1000.0)
                    first_ranks.append(None)
                    continue
                latencies.append((time.perf_counter() - t0) * 1000.0)

                scores = dict(manager.genome.last_query_scores or {})
                if scores:
                    n_scored += 1
                first, ranks = rank_gold(scores, gold)
                first_ranks.append(first)
                all_ranks.extend(ranks)

                sig = dict(getattr(manager.genome, "last_signal_timings", None) or {})
                per_query_signals.append(sig)
                signal_keys.update(sig.keys())

                contribs = getattr(manager.genome, "last_tier_contributions", None) or {}
                for per_gene in contribs.values():
                    tier_keys.update(per_gene.keys())

                meta = getattr(window, "metadata", None) or {}
                if "classifier" in meta:
                    classifier_metadata_queries += 1
                # build_context resets this per turn (context_manager.py:1844),
                # so reading it after the call is a per-query observation.
                if getattr(manager, "_last_cold_tier_used", False):
                    cold_tier_queries += 1

                delivered = set(getattr(window, "expressed_gene_ids", None) or ())
                if delivered & set(gold):
                    delivered_gold_hits += 1

        observables = {
            "signal_keys": sorted(signal_keys),
            "tier_keys": sorted(tier_keys),
            "classifier_metadata_queries": classifier_metadata_queries,
            "synonym_expansions": _probe_synonym_expansions(manager.genome, probe_terms),
            "cymatics_calls": cymatics_probe.count,
            "cold_tier_queries": cold_tier_queries,
        }
        remote_url = ""
        try:
            remote_url = encoder_client.active_url()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"encoder_client.active_url failed: {exc}")
        dense_codec = _dense_codec_name(manager.genome)
    finally:
        # Drop the store's connections so the next arm opens the bed clean.
        try:
            manager.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"manager.close failed: {exc}")

    hit_ranks = [r for r in first_ranks if r is not None]
    row: Dict[str, Any] = {
        "arm": arm.name,
        "knobs_changed": dict(arm.knobs),
        "gate": arm.gate,
        "arm_valid": None,          # filled by the caller (needs baseline)
        "validation": arm.validation,
        "validation_detail": "",    # filled by the caller
        "recall_at_k": recall_at_k(first_ranks, k),
        "mean_rank_of_gold": (round(statistics.mean(hit_ranks), 2) if hit_ranks else None),
        "median_rank_of_gold": median_or_none(hit_ranks),
        "gold_found_queries": len(hit_ranks),
        "gold_ranks_total": len(all_ranks),
        "delivered_gold_queries": delivered_gold_hits,
        "queries_with_scores": n_scored,
        "latency_ms": {
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
        },
        "signal_ms_median": layer_signal_medians(per_query_signals),
        "n_queries": len(needles),
        "warmup_ran": warmed,
        "encoder_url": remote_url,
        "dense_codec_class": dense_codec,
        "remote_dense": bool(dense_codec and dense_codec.startswith("Remote")),
        "observables": observables,
        "errors": errors,
    }
    return row


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="In-process per-layer ablation probe (ERB beds).",
    )
    ap.add_argument("--genome", default="f:/tmp/erb_carve/erb_100k.db")
    ap.add_argument("--resolved", default="benchmarks/dogfood/erb/needles_resolved_carve.json")
    ap.add_argument("--gold", default="benchmarks/dogfood/erb/gold_by_needle_carve.json")
    ap.add_argument("--limit", type=int, default=30, help="needles to run (0 = all)")
    ap.add_argument("--k", type=int, default=12, help="delivery cap + recall depth")
    ap.add_argument("--arms", default="", help="comma list; empty = all")
    ap.add_argument("--config", default="cymatix.toml",
                    help="cymatix.toml to load (repo-relative ok); '' = auto-discover")
    ap.add_argument("--out", default="benchmarks/dogfood/erb/receipts/ablation_ladder.json")
    ap.add_argument("--stamp", default="", help="caller-supplied run timestamp")
    args = ap.parse_args(argv)

    resolved_path = _resolve(args.resolved)
    gold_path = _resolve(args.gold)
    needles = json.loads(resolved_path.read_text(encoding="utf-8"))["needles"]
    if args.limit:
        needles = needles[: args.limit]
    gold_by_needle = {
        key: set(val)
        for key, val in json.loads(gold_path.read_text(encoding="utf-8")).items()
    }

    genome_path = str(_resolve(args.genome))
    if not Path(genome_path).exists():
        print(f"genome not found: {genome_path}", file=sys.stderr)
        return 2
    os.environ["CYMATIX_GENOME_PATH"] = genome_path

    config_path: Optional[str] = None
    if args.config:
        candidate = _resolve(args.config)
        if not candidate.exists():
            print(f"config not found: {candidate}", file=sys.stderr)
            return 2
        config_path = str(candidate)

    from cymatix_context.config import load_config

    effective_cfg = load_config(config_path) if config_path else load_config()

    wanted = {a.strip() for a in args.arms.split(",") if a.strip()}
    selected: List[Arm] = []
    dropped: List[Dict[str, str]] = [dict(d) for d in DROPPED_ARMS]
    for arm in ARMS:
        if wanted and arm.name not in wanted and arm.name != "baseline":
            continue
        reason = arm_is_identity(effective_cfg, arm)
        if reason:
            dropped.append({
                "arm": arm.name,
                "knob": ", ".join(arm.knobs),
                "reason": (
                    f"{reason} (gate exists at {arm.gate}, but the effective "
                    "config already sits at the ablated value, so this arm "
                    "would measure an identity transform)"
                ),
            })
            continue
        selected.append(arm)
    if wanted and "baseline" not in wanted:
        print("note: baseline is always run — it is the positive control every "
              "ablation arm validates against", file=sys.stderr)

    print(f"ablation ladder: {len(selected)} arms x {len(needles)} needles, "
          f"k={args.k}, bed={genome_path}", flush=True)
    for entry in dropped:
        print(f"  DROP {entry['arm']}: {entry['reason'].splitlines()[0]}", file=sys.stderr)

    rows: List[Dict[str, Any]] = []
    baseline_observables: Optional[Dict[str, Any]] = None
    for arm in selected:
        row = run_arm(
            arm,
            genome_path=genome_path,
            config_path=config_path,
            needles=needles,
            gold_by_needle=gold_by_needle,
            k=args.k,
        )
        if row.get("skipped"):
            rows.append(row)
            print(f"  SKIP {arm.name}: {row['validation']}", file=sys.stderr)
            continue
        if arm.name == "baseline":
            baseline_observables = row["observables"]
        valid, detail = validate_arm(arm, row["observables"], baseline_observables)
        row["arm_valid"] = valid
        row["validation_detail"] = detail
        rows.append(row)

        flag = "ok " if valid else "BAD"
        print(
            f"  [{flag}] {arm.name:16s} r@{args.k}={row['recall_at_k']:.3f} "
            f"med_rank={row['median_rank_of_gold']} "
            f"p50={row['latency_ms']['p50']}ms p95={row['latency_ms']['p95']}ms "
            f"-- {detail}",
            flush=True,
        )

    payload = {
        "tool": "benchmarks/dogfood/erb/ablation_ladder.py",
        "stamp": args.stamp,
        "bed": genome_path,
        "bed_bytes": Path(genome_path).stat().st_size,
        "config": config_path or "(auto-discovered)",
        "git_sha": _git_sha(REPO_ROOT),
        "needles_file": str(resolved_path),
        "gold_file": str(gold_path),
        "needle_count": len(needles),
        "k": args.k,
        "env": {
            "CYMATIX_ENCODER_URL": os.environ.get("CYMATIX_ENCODER_URL", "") or None,
            "CYMATIX_ENCODER_URL_set": bool(os.environ.get("CYMATIX_ENCODER_URL", "").strip()),
            "CYMATIX_DISABLE_HEADROOM": os.environ.get("CYMATIX_DISABLE_HEADROOM"),
            "CYMATIX_DISABLE_LEARN": os.environ.get("CYMATIX_DISABLE_LEARN"),
            "CYMATIX_GENOME_PATH": os.environ.get("CYMATIX_GENOME_PATH"),
        },
        "note": (
            "Ranks read from genome.last_query_scores (post-blend; blend "
            "republishes the map at scoring/blend.py:233/:281), ties broken by "
            "gene_id ascending. delivered_gold_queries reads "
            "window.expressed_gene_ids. Each arm runs one untimed warmup query "
            "first so the lazy encoder load is not billed to p50. Latencies are "
            "wall ms on a shared box — within-run relatives only."
        ),
        "dropped_arms": dropped,
        "arms": rows,
    }

    out = _resolve(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")

    invalid = [r["arm"] for r in rows if r.get("arm_valid") is False]
    if invalid:
        print(f"arms failing self-validation: {invalid}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
