"""rerank_ab.py — order-reversed rerank A/B (#341) with encoder-daemon proof.

Drives ``benchmarks/dogfood/erb/ablation_ladder.py`` TWICE against the same
bed, needles and gold, with the rerank knob's base value reversed between the
runs, and proves from the daemon's own access log that the cross-encoder
actually ran out-of-process on the ON side of each run.

────────────────────────────────────────────────────────────────────────
Why two runs, and what inverts in the second
────────────────────────────────────────────────────────────────────────

The ladder's baseline arm is the shipped config, untouched, and it always
runs first — it is the positive control every other arm validates against.
That makes a single run's ON-vs-OFF comparison *order-confounded*: the ON
side is always the second arm, so any drift that tracks arm order (page
cache warmth, thermal state, a leaking process-lifetime cache) is
indistinguishable from a rerank effect.

So:

  run 1  ``--config <config-off>``  ``--arms rerank_on``
         baseline = OFF (control), arm = ON (the ablation-table arm).
         paired delta ``arm - baseline`` is already ``ON - OFF``.

  run 2  ``--config <config-on>``   ``--arms rerank_off``
         baseline = ON (control), arm = OFF. **The ON side is now the
         BASELINE arm and it runs FIRST**, and the paired delta
         ``arm - baseline`` is ``OFF - ON`` — the sign is inverted.

Anything computing a gate from these receipts MUST normalise direction
first, using the wrapper's ``arm_order`` / ``on_side`` / ``delta_sign``
fields rather than assuming "arm minus baseline". ``delta_sign`` is exactly
the multiplier that turns ``arm - baseline`` into ``ON - OFF``: ``+1`` in run
1, ``-1`` in run 2. An effect that survives BOTH runs after that
normalisation is not an artefact of which arm went first.

The same identity-drop rule is what makes one arm table serve both
directions: ``arm_is_identity`` drops ``rerank_off`` when the loaded config
already has rerank off, and drops ``rerank_on`` when it is already on, so
each run selects the only arm that can actually move its config.

────────────────────────────────────────────────────────────────────────
The daemon proof (the silent-fallback trap)
────────────────────────────────────────────────────────────────────────

``encoder_client`` degrades to in-process encoding when the daemon is
unreachable. That is the right production behaviour and the wrong benchmark
behaviour: a rerank A/B that quietly fell back would still produce a
plausible receipt, and the whole GPU/daemon claim attached to it would be
false. So each run gets a FRESH daemon with its OWN log file, and after
teardown this script counts ``POST /encode/rerank`` access lines in that log:

    daemon_log_posts_rerank  >=  n_queries + 1

The ``+1`` is the ON side's untimed warmup query (``run_arm`` runs one before
the timed loop). In run 2 the ON side is the baseline arm, so the check is
stated against the RUN TOTAL in both runs — the OFF arm contributes zero
rerank POSTs either way. A shortfall means some queries encoded in-process:
this script REFUSES the run (nonzero exit, loud stderr) rather than writing a
receipt that looks fine.

A fresh daemon per run is deliberate: it keeps ``spawn_token_verified``
meaningful per run, and it stops run 1's POSTs from being counted toward run
2's floor (a cumulative log would defeat the check entirely).

``daemon_log_posts_total`` counts every ``POST /encode/*`` line, so a dense-leg
divergence BETWEEN the two runs (one run silently encoding dense in-process)
is visible in the verdict even though it does not trip the rerank floor.

────────────────────────────────────────────────────────────────────────
Config preflight
────────────────────────────────────────────────────────────────────────

``load_config`` does not merge one TOML over another: a section a file omits
falls back to DATACLASS defaults, not to the other config. A hand-written
"rerank on" toml therefore also drops ``[synonyms]``, ``[know]`` and anything
else it does not repeat — and the two runs stop being an A/B of one variable.
So before anything is spawned this script loads both configs, REFUSES if the
OFF config is not rerank-off or the ON config not rerank-on (that premise
failing would let ``arm_is_identity`` drop the only arm that matters and
still leave a receipt that looks complete), and DISCLOSES every other knob on
which the two disagree as ``config_delta_other`` in both wrappers. Build the
ON config by copying the OFF one and flipping the single knob.

Windows rules throughout (campaign lessons): kill the daemon by
``_taskkill_tree`` on ITS pid — never by name/regex — wait for the port to be
free before spawning on it, ``CREATE_NO_WINDOW`` on every subprocess, explicit
timeouts on everything.

Usage::

    python benchmarks/dogfood/erb/rerank_ab.py \
        --genome f:/tmp/erb_carve/erb_100k.db --limit 30 --k 12 \
        --config-off cymatix.toml --config-on benchmarks/dogfood/erb/rerank_on.toml \
        --daemon-python f:/tmp/venv-cuda/Scripts/python.exe \
        --out-prefix benchmarks/dogfood/erb/receipts/rerank_ab_100k
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# benchmarks/dogfood/erb/rerank_ab.py -> repo root is parents[3].
REPO_ROOT = Path(__file__).resolve().parents[3]
# Same prologue ablation_ladder.py carries, and for the same reason: run as a
# script, sys.path[0] is THIS directory, not the repo root, so
# `import cymatix_context` would resolve from the editable install — a
# plain-path .pth naming the MASTER checkout, which has no #341 rerank seams
# at all ([retrieval] rerank_enabled does not exist there). Pinning the
# ladder CHILD's PYTHONPATH (see ladder_env) does nothing for the imports
# this module makes ITSELF; both halves are needed. Must run BEFORE any
# cymatix_context import — preflight_configs imports load_config lazily, so
# "before" is satisfied by module position alone, and a source-order test
# keeps it that way.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_ERB_DIR = Path(__file__).resolve().parent
if str(_ERB_DIR) not in sys.path:
    sys.path.insert(0, str(_ERB_DIR))

# Daemon lifecycle is NOT reimplemented here: spawn-token identity, the
# token-verified /ready poll, connect_ex port-free waiting and taskkill /T /F
# tree cleanup all already exist (and are already load-tested) in the capacity
# probe. Importing them keeps one implementation of the Windows rules.
import encoder_daemon_capacity as cap  # noqa: E402

LADDER_REL = "benchmarks/dogfood/erb/ablation_ladder.py"
HOST = "127.0.0.1"
DEFAULT_PORT = cap.DEFAULT_PORT
DEFAULT_READY_TIMEOUT_S = cap.DEFAULT_READY_TIMEOUT_S
# A 30-needle ladder run is two arms x (warmup + 30 queries) of real
# retrieval; 3 h is a ceiling that only fires on a genuine hang.
DEFAULT_LADDER_TIMEOUT_S = 10800.0
DEFAULT_OUT_PREFIX = "benchmarks/dogfood/erb/receipts/rerank_ab"

# uvicorn access-log markers. The daemon logs one line per request, e.g.
#   INFO:  127.0.0.1:5001 - "POST /encode/rerank HTTP/1.1" 200 OK
MARK_POST_RERANK = "POST /encode/rerank"
MARK_POST_ANY = "POST /encode/"


# ── run plan (the order reversal, as data) ───────────────────────────────


@dataclass(frozen=True)
class RunPlan:
    """One of the two ladder invocations.

    ``delta_sign`` is the multiplier that normalises a paired
    ``arm - baseline`` delta into ``ON - OFF``: +1 when the arm is the ON
    side, -1 when the BASELINE is (run 2). Consumers must apply it before any
    gate math — see the module docstring.
    """

    run: int
    config_role: str      # "off" | "on" — which --config-* this run loads
    arms: str             # value passed to the ladder's --arms
    arm_order: str        # human-readable statement of which side ran first
    on_side: str          # "arm" | "baseline" — where rerank was ON
    on_arm: str
    off_arm: str
    delta_sign: int


RUN_PLANS = (
    RunPlan(
        run=1, config_role="off", arms="rerank_on",
        arm_order="baseline=OFF (first), arm=ON (second)",
        on_side="arm", on_arm="rerank_on", off_arm="baseline", delta_sign=1,
    ),
    RunPlan(
        run=2, config_role="on", arms="rerank_off",
        arm_order="baseline=ON (first), arm=OFF (second)",
        on_side="baseline", on_arm="baseline", off_arm="rerank_off", delta_sign=-1,
    ),
)


# ── pure helpers (unit-tested in tests/test_ablation_ladder_metrics.py) ──


def count_daemon_posts(log_path: Path, marker: str) -> int:
    """Count daemon access-log lines containing *marker*.

    Substring match on whole lines, which is what makes it robust to
    uvicorn's formatting (client host/port, HTTP version, status text all
    vary). ``marker`` carries the method, so a ``GET /ready`` poll or a prose
    log line mentioning "rerank" cannot be miscounted as a POST.

    A missing log file counts 0 rather than raising: the caller's refusal
    gate already turns 0 into a loud failure, and "the daemon wrote no log"
    is one of the failures it exists to catch.
    """
    path = Path(log_path)
    if not path.exists():
        return 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"warning: could not read daemon log {path}: {exc}", file=sys.stderr)
        return 0
    return sum(1 for line in text.splitlines() if marker in line)


def expected_min_rerank_posts(n_queries: int) -> int:
    """Coarse floor on rerank POSTs for a run: every needle plus the warmup.

    ``run_arm`` runs one untimed warmup ``build_context`` before the timed
    loop (so the lazy encoder load is not billed to p50), and that query goes
    through the same seam — so the ON side owes ``n_queries + 1`` calls, not
    ``n_queries``. Stated as a floor, not an equality: the lexical and ANN
    paths both host a rerank seam, and a needle that exercises both is not a
    fault.

    This is the FALLBACK basis, used only when the ladder receipt carries no
    per-query records. It assumes every needle reaches the seam, which is
    false for a no-match early return / abstain / empty pool — prefer
    ``rerank_eligible_needles`` whenever the records exist.
    """
    return int(n_queries) + 1


def rerank_eligible_needles(records: Sequence[Mapping[str, Any]]) -> int:
    """How many needles could have produced a rerank POST at all.

    ``gate_kept`` (from ``genome.last_rerank_diag``, written per needle by
    the ladder) is the discriminator the receipt already carries:

      * ``None`` / absent — the query never reached ``query_docs_ann``'s
        publication point (no-match early return, abstain, exception), so no
        rerank could have been attempted.
      * ``0`` — the ANN pool was empty; ``rerank_backend.score_pairs``
        returns ``[]`` on an empty candidate list WITHOUT issuing a POST.
      * anything else — the seam was reachable and owes one POST.

    Counting these instead of every needle makes the floor both tighter (a
    silent in-process fallback still trips it) and non-brittle (a legitimate
    no-match needle no longer looks like a fallback).

    Edge, on the record: a store variant that reranked over a candidate set
    whose gate kept nothing would make this one short — a LOOSER floor, never
    a false refusal. On the current gate (``apply_ann_gate`` with the
    min_genes floor) ``gate_kept == 0`` implies no scored candidates, so the
    two agree.
    """
    return sum(
        1 for r in records
        if r.get("gate_kept") is not None and r.get("gate_kept") != 0
    )


def invalid_arms_from_receipt(payload: Optional[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """Arms the LADDER itself judged invalid (``arm_valid is False``).

    The ladder exits 0 even when an arm fails self-validation, and some
    failure modes RAISE the daemon POST count rather than lowering it — a
    contaminated baseline that already fires ``xenc_rerank`` makes
    ``rerank_on`` unfalsifiable while every query still reranks. Without
    folding this verdict in, the wrapper would certify a measurement the
    ladder had already flagged as proving nothing.

    ``arm_valid is None`` (a skipped arm) is not a failure and is ignored.
    """
    out: List[Dict[str, str]] = []
    for row in ((payload or {}).get("arms") or ()):
        if row.get("arm_valid") is False:
            out.append({
                "arm": str(row.get("arm", "?")),
                "validation_detail": str(row.get("validation_detail", "")),
            })
    return out


def arm_records_from_receipt(
    payload: Optional[Mapping[str, Any]], arm_name: str,
) -> Optional[List[Mapping[str, Any]]]:
    """The named arm's ``per_query`` records, or None when unavailable.

    None means "fall back to the coarse floor" — never "zero needles".
    """
    for row in ((payload or {}).get("arms") or ()):
        if row.get("arm") == arm_name:
            records = row.get("per_query")
            return list(records) if records else None
    return None


def ladder_env(base_env: Mapping[str, str], port: int) -> Dict[str, str]:
    """Environment for the ladder subprocess: encoder URL + a PYTHONPATH
    pinned to THIS worktree. Returns a copy; *base_env* is never mutated.

    Campaign trap #1 (pin PYTHONPATH for any out-of-tree client). The
    editable install is a plain-path ``.pth`` naming the MASTER checkout, so
    which ``cymatix_context`` a child imports is decided purely by sys.path
    ORDER — and a bare ``python benchmarks/.../ablation_ladder.py`` puts the
    SCRIPT's directory at sys.path[0], not the repo root.

    Measured, so the record is honest: ``ablation_ladder.py``'s own prologue
    inserts its ``REPO_ROOT`` (= this worktree) at sys.path[0] before it
    imports anything, and a probe replicating that prologue resolves to the
    worktree even with PYTHONPATH unset — so the ladder is not, today,
    running on master. This pin is defence in depth: the runner must not
    depend on the callee's prologue surviving a refactor, and any child the
    ladder itself spawns inherits the pin for free. Master has no rerank
    seams at all, so the failure it guards is total (``set_dotted`` on
    ``retrieval.rerank_enabled`` would skip, and the arm would measure
    nothing) — and it would only surface after a full baseline arm had
    already burned.

    Prepend-and-inherit semantics copied from ``spawn_daemon`` (one idiom
    for both children): an existing PYTHONPATH is kept behind the root, and
    a root already at the head is left alone rather than doubled.
    """
    env = dict(base_env)
    env["CYMATIX_ENCODER_URL"] = f"http://{HOST}:{port}"
    root_str = str(REPO_ROOT)
    existing = env.get("PYTHONPATH", "")
    if existing:
        head = existing.split(os.pathsep, 1)[0]
        if head != root_str:
            env["PYTHONPATH"] = root_str + os.pathsep + existing
    else:
        env["PYTHONPATH"] = root_str
    return env


def config_diff_paths(a: Mapping[str, Any], b: Mapping[str, Any], prefix: str = "") -> List[str]:
    """Dotted paths where two config mappings (``dataclasses.asdict`` output)
    differ. Sorted; empty when identical.

    Exists for one specific confound: ``load_config`` does NOT merge a TOML
    over another TOML — unset sections fall back to DATACLASS defaults. So a
    hand-written "rerank on" config that omits ``[synonyms]`` or ``[know]``
    quietly changes far more than the rerank knob, and the two runs stop
    being an A/B of one variable. Reported, not guessed at.
    """
    out: List[str] = []
    for key in sorted(set(a) | set(b)):
        path = f"{prefix}{key}"
        left, right = a.get(key), b.get(key)
        if isinstance(left, dict) and isinstance(right, dict):
            out.extend(config_diff_paths(left, right, prefix=f"{path}."))
        elif left != right:
            out.append(path)
    return out


def build_wrapper(
    plan: RunPlan,
    *,
    ladder_receipt_path: str,
    daemon_log: str,
    posts_rerank: int,
    posts_total: int,
    spawn_token_verified: bool,
    n_queries: Optional[int],
    ladder_returncode: Optional[int],
    config: str,
    port: int,
    config_delta_other: Sequence[str] = (),
    on_arm_records: Optional[Sequence[Mapping[str, Any]]] = None,
    invalid_arms: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    """Build one run's wrapper receipt, including the refusal verdict.

    Pure on purpose: the refusal is the safety property this whole script
    exists for, so it is decided by a function that can be tested without a
    daemon. ``refusals`` is a list of reasons, empty iff ``daemon_proof_ok``.

    ``on_arm_records`` are the ON side's per-needle records; when present the
    POST floor counts only rerank-ELIGIBLE needles (see
    ``rerank_eligible_needles``) instead of assuming all of them reached the
    seam. ``invalid_arms`` folds the LADDER's own self-validation verdict in
    — a daemon that served every request still proves nothing about an arm
    the ladder marked unfalsifiable.
    """
    if on_arm_records is not None:
        eligible = rerank_eligible_needles(on_arm_records)
        expected_min = eligible + 1
        basis = (
            f"per-needle gate_kept: {eligible} of {len(on_arm_records)} needles "
            "were rerank-eligible (gate_kept not null and not 0), + 1 untimed "
            "warmup query"
        )
    elif n_queries is not None:
        expected_min = expected_min_rerank_posts(n_queries)
        basis = (
            f"n_queries + 1 ({n_queries} needles + 1 untimed warmup) — "
            "per-query records absent from the ladder receipt, so needles "
            "that never reached the rerank seam cannot be discounted"
        )
    else:
        expected_min = None
        basis = "unknown — the ladder receipt could not be read"

    refusals: List[str] = []
    for entry in invalid_arms:
        refusals.append(
            f"the ladder judged arm {entry.get('arm')!r} INVALID: "
            f"{entry.get('validation_detail')} — the daemon proof cannot "
            "certify a measurement the ladder itself flagged as proving "
            "nothing (the ladder exits 0 regardless, and some failure modes "
            "raise the POST count rather than lowering it)"
        )
    if ladder_returncode is None:
        refusals.append(
            "ladder did not complete — it never started (daemon lifecycle "
            "failure) or was killed on the --ladder-timeout"
        )
    elif ladder_returncode != 0:
        refusals.append(f"ladder exited rc={ladder_returncode} (expected 0)")
    if not spawn_token_verified:
        refusals.append(
            "daemon /ready was never verified against this run's spawn token — "
            "the receipt cannot claim which process served the encodes"
        )
    if expected_min is None:
        refusals.append(
            "needle count unknown (ladder receipt missing or unreadable), so "
            "the rerank-POST floor cannot be checked"
        )
    elif posts_rerank < expected_min:
        refusals.append(
            f"daemon served {posts_rerank} {MARK_POST_RERANK} requests, "
            f"expected >= {expected_min} — basis: {basis}. Needles that never "
            "reached the seam are ALREADY discounted by that basis, so the "
            "shortfall is encoder_client falling back to in-process "
            "reranking: the silent-fallback trap this A/B exists to rule out"
            if on_arm_records is not None else
            f"daemon served {posts_rerank} {MARK_POST_RERANK} requests, "
            f"expected >= {expected_min} — basis: {basis}. Two causes are "
            "possible on this basis: encoder_client falling back to "
            "in-process reranking (the silent-fallback trap), or a needle "
            "that never reached the seam (no-match / abstain / empty pool, "
            "which posts nothing). Re-run with --per-query receipts to "
            "separate them"
        )
    return {
        "kind": "rerank_ab_run",
        "run": plan.run,
        "config": config,
        "config_role": plan.config_role,
        # Every knob (other than rerank_enabled) on which the two runs'
        # configs disagree. Disclosed, never silently tolerated — but not a
        # refusal either: only the operator can say whether a given delta is
        # legitimate for their bed.
        "config_delta_other": list(config_delta_other),
        "arms_requested": plan.arms,
        "arm_order": plan.arm_order,
        "on_side": plan.on_side,
        "on_arm": plan.on_arm,
        "off_arm": plan.off_arm,
        # Multiply a paired (arm - baseline) delta by this to get (ON - OFF).
        "delta_sign": plan.delta_sign,
        "ladder_receipt_path": ladder_receipt_path,
        "ladder_returncode": ladder_returncode,
        "n_queries": n_queries,
        "daemon_log": daemon_log,
        "daemon_log_posts_rerank": posts_rerank,
        "daemon_log_posts_rerank_expected_min": expected_min,
        "rerank_expected_basis": basis,
        "daemon_log_posts_total": posts_total,
        "spawn_token_verified": bool(spawn_token_verified),
        # The ladder's own arm_valid verdicts, folded into daemon_proof_ok.
        "invalid_arms": [dict(a) for a in invalid_arms],
        "daemon_proof_ok": not refusals,
        "refusals": refusals,
        "port": port,
        "note": (
            "daemon_log_posts_* are counted from THIS run's own daemon log "
            "(a fresh daemon and a fresh log per run, so counts never "
            "accumulate across runs). daemon_log_posts_rerank is a RUN TOTAL "
            "spanning BOTH arms, not an ON-arm count — it is compared against "
            "the ON side's expectation because the OFF arm reranks nothing "
            "and therefore contributes zero. daemon_log_posts_total spans "
            "every POST /encode/* family, so a dense-leg fallback divergence "
            "between the two runs is visible even when the rerank floor "
            "holds. daemon_proof_ok also folds in the ladder's own arm_valid "
            "verdicts (see invalid_arms). Direction: multiply any paired "
            "(arm - baseline) delta by delta_sign to obtain (ON - OFF)."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (REPO_ROOT / p)


def _read_receipt(receipt_path: Path) -> Optional[Dict[str, Any]]:
    """Load a ladder receipt once (needle count, arm verdicts, per-needle
    records all come off it), or None with a warning if unreadable."""
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not read ladder receipt {receipt_path}: {exc}",
              file=sys.stderr)
        return None
    return payload if isinstance(payload, dict) else None


def _needle_count(payload: Optional[Mapping[str, Any]]) -> Optional[int]:
    """``needle_count`` from a ladder receipt payload.

    Read from the receipt rather than recomputed from ``--limit`` so the
    floor is checked against the run that actually happened.
    """
    value = (payload or {}).get("needle_count")
    return int(value) if isinstance(value, int) else None


def preflight_configs(off_path: Path, on_path: Path) -> Tuple[List[str], List[str]]:
    """Check the two configs BEFORE spawning anything.

    Returns ``(fatal_reasons, other_delta_paths)``. Fatal means the A/B as
    specified cannot happen: if the OFF config is not actually rerank-off, or
    the ON config not actually rerank-on, then run 2's "baseline = ON
    control" premise is false and ``arm_is_identity`` would silently drop the
    only arm that matters — leaving a receipt that looks complete and
    measures nothing.

    The cymatix_context import is deliberately inside the function: this
    module must stay importable (and unit-testable) without pulling the
    package in.
    """
    fatal: List[str] = []
    for label, path in (("--config-off", off_path), ("--config-on", on_path)):
        if not path.exists():
            fatal.append(
                f"{label} not found: {path}. Build the ON config by COPYING "
                "the OFF config and setting [retrieval] rerank_enabled = true "
                "— a hand-written minimal toml is not equivalent, because "
                "load_config falls back to dataclass defaults (not to the "
                "other file) for every section it omits, silently dropping "
                "[synonyms] and [know] among others"
            )
    if fatal:
        return fatal, []
    try:
        from cymatix_context.config import load_config
        off = load_config(str(off_path))
        on = load_config(str(on_path))
    except Exception as exc:  # noqa: BLE001
        return [f"could not load configs: {type(exc).__name__}: {exc}"], []

    if off.retrieval.rerank_enabled is not False:
        fatal.append(
            f"--config-off ({off_path}) has [retrieval] rerank_enabled="
            f"{off.retrieval.rerank_enabled!r}, expected false"
        )
    if on.retrieval.rerank_enabled is not True:
        fatal.append(
            f"--config-on ({on_path}) has [retrieval] rerank_enabled="
            f"{on.retrieval.rerank_enabled!r}, expected true"
        )
    # A per-class override map beats the global knob at query time
    # (context_manager resolves it into rerank_override ->
    # KnowledgeStore._rerank_effective), so a config that names any class is
    # not actually OFF (or ON) for that class. Worse, a map INHERITED
    # IDENTICALLY by both cell configs produces no config_delta_other entry
    # at all — it is invisible in the delta report while silently reranking
    # inside the control. Refuse: an A/B cell config must carry the whole
    # treatment in the one global knob.
    for label, path, cfg in (("--config-off", off_path, off), ("--config-on", on_path, on)):
        by_class = dict(getattr(cfg.retrieval, "rerank_enabled_by_class", None) or {})
        if by_class:
            fatal.append(
                f"{label} ({path}) sets [retrieval] rerank_enabled_by_class="
                f"{by_class!r}; the per-class map must be EMPTY in an A/B cell "
                "config. It overrides the global rerank_enabled per query "
                "class, so the OFF cell would still rerank for the classes it "
                "names — and because both cells inherit the same map it would "
                "not even show up in config_delta_other"
            )
    deltas = [
        p for p in config_diff_paths(asdict(off), asdict(on))
        if p != "retrieval.rerank_enabled"
    ]
    return fatal, deltas


# ── the two runs (lifecycle path — exercised by the receipts run) ────────


def _run_ladder(
    plan: RunPlan, args: argparse.Namespace, config: Path, out_path: Path, stamp: str,
) -> Optional[int]:
    """Run the ladder as a subprocess against the daemon, from THIS worktree.

    Env comes from ``ladder_env`` (encoder URL + worktree-pinned PYTHONPATH —
    see its docstring for the sys.path trap it guards). stdout/stderr are
    INHERITED (no pipes) so the operator watches progress live and no pipe
    can deadlock on a long run."""
    cmd = [
        sys.executable, LADDER_REL,
        "--genome", str(args.genome),
        "--resolved", str(args.resolved),
        "--gold", str(args.gold),
        "--limit", str(args.limit),
        "--k", str(args.k),
        "--arms", plan.arms,
        "--config", str(config),
        "--out", str(out_path),
        "--stamp", f"{stamp}-run{plan.run}",
        "--per-query",
    ]
    env = ladder_env(os.environ, args.port)
    print(f"\n[run {plan.run}] {' '.join(cmd)}\n  CYMATIX_ENCODER_URL="
          f"{env['CYMATIX_ENCODER_URL']}\n  PYTHONPATH={env['PYTHONPATH']}",
          flush=True)
    proc = subprocess.Popen(
        cmd, cwd=str(REPO_ROOT), env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        return proc.wait(timeout=args.ladder_timeout)
    except subprocess.TimeoutExpired:
        print(f"error: ladder run {plan.run} exceeded {args.ladder_timeout}s; "
              "killing its process tree", file=sys.stderr)
        cap._taskkill_tree(proc.pid)
        try:
            proc.wait(timeout=30.0)
        except subprocess.TimeoutExpired:
            print(f"warning: ladder pid={proc.pid} still alive after taskkill",
                  file=sys.stderr)
        return None


def run_one(
    plan: RunPlan, args: argparse.Namespace, stamp: str,
    config_delta_other: Sequence[str] = (),
) -> Dict[str, Any]:
    """Spawn a fresh daemon, run the ladder against it, tear down, verify."""
    prefix = _resolve(args.out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    stem = prefix.name
    ladder_out = prefix.with_name(f"{stem}_run{plan.run}_ladder.json")
    wrapper_out = prefix.with_name(f"{stem}_run{plan.run}.json")
    log_path = prefix.with_name(f"{stem}_run{plan.run}_daemon.log")
    config = _resolve(args.config_off if plan.config_role == "off" else args.config_on)

    spawn_token_verified = False
    rc: Optional[int] = None
    proc: Optional[subprocess.Popen] = None
    log_fh = None
    try:
        # spawn_daemon opens the log in APPEND mode, so a leftover log from an
        # earlier invocation would make this run's POST counts cumulative —
        # and a cumulative count is exactly what defeats the shortfall check.
        if log_path.exists():
            log_path.unlink()
        cap._wait_port_free(HOST, args.port)
        proc, spawn_token, log_fh = cap.spawn_daemon(
            args.daemon_python, args.port, log_path)
        cap.wait_ready(HOST, args.port, spawn_token, proc, log_path, args.ready_timeout)
        spawn_token_verified = True
        rc = _run_ladder(plan, args, config, ladder_out, stamp)
    except Exception as exc:  # noqa: BLE001
        # Any lifecycle failure (ProbeError, a locked log file, a spawn that
        # cannot start) still gets a written wrapper carrying its refusal —
        # teardown below runs either way, and run 2 is not skipped just
        # because run 1's daemon misbehaved.
        print(f"error: run {plan.run} daemon lifecycle failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        # Both halves of the handle's lifetime are covered: spawn_daemon
        # closes it itself if the Popen fails (so the caller never receives
        # an orphan), and this closes it for every path after a successful
        # spawn — including wait_ready raising or the ladder timing out.
        if log_fh is not None:
            log_fh.close()
        if proc is not None and proc.poll() is None:
            cap._taskkill_tree(proc.pid)
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                print(f"warning: daemon pid={proc.pid} still alive after taskkill",
                      file=sys.stderr)
        try:
            cap._wait_port_free(HOST, args.port)
        except cap.ProbeError as exc:
            print(f"warning: {exc}", file=sys.stderr)

    # Counted after teardown: uvicorn's access lines go to stderr, which
    # CPython keeps line-buffered even when redirected to a file, so every
    # served request is already on disk by the time the process is gone.
    posts_rerank = count_daemon_posts(log_path, MARK_POST_RERANK)
    posts_total = count_daemon_posts(log_path, MARK_POST_ANY)
    # One read, three uses: the needle count, the ladder's own arm_valid
    # verdicts, and the ON side's per-needle records (which set the POST
    # floor). Only trusted when the ladder actually completed.
    payload = _read_receipt(ladder_out) if rc == 0 else None
    wrapper = build_wrapper(
        plan,
        ladder_receipt_path=str(ladder_out),
        daemon_log=str(log_path),
        posts_rerank=posts_rerank,
        posts_total=posts_total,
        spawn_token_verified=spawn_token_verified,
        n_queries=_needle_count(payload),
        ladder_returncode=rc,
        config=str(config),
        port=args.port,
        config_delta_other=config_delta_other,
        on_arm_records=arm_records_from_receipt(payload, plan.on_arm),
        invalid_arms=invalid_arms_from_receipt(payload),
    )
    wrapper_out.write_text(json.dumps(wrapper, indent=2) + "\n", encoding="utf-8")
    print(f"[run {plan.run}] wrote {wrapper_out}")
    print(f"[run {plan.run}] rerank POSTs={posts_rerank} "
          f"(min {wrapper['daemon_log_posts_rerank_expected_min']}), "
          f"encode POSTs total={posts_total}, "
          f"proof_ok={wrapper['daemon_proof_ok']}")
    for reason in wrapper["refusals"]:
        print(f"[run {plan.run}] REFUSED: {reason}", file=sys.stderr)
    return wrapper


# ── CLI ──────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=("Order-reversed rerank A/B (#341): two ablation-ladder runs "
                     "with the rerank base value flipped between them, each "
                     "against its own encoder daemon, with a POST-count proof "
                     "that the cross-encoder really ran out-of-process."),
    )
    ap.add_argument("--genome", default="f:/tmp/erb_carve/erb_100k.db")
    ap.add_argument("--resolved",
                    default="benchmarks/dogfood/erb/needles_resolved_carve.json")
    ap.add_argument("--gold", default="benchmarks/dogfood/erb/gold_by_needle_carve.json")
    ap.add_argument("--limit", type=int, default=30, help="needles to run (0 = all)")
    ap.add_argument("--k", type=int, default=12, help="delivery cap + recall depth")
    ap.add_argument("--config-off", dest="config_off", default="cymatix.toml",
                    help="config whose [retrieval] rerank_enabled is FALSE; run 1 "
                         "loads it, so run 1's baseline is the OFF control")
    ap.add_argument("--config-on", dest="config_on",
                    default="benchmarks/dogfood/erb/cymatix_rerank_on.toml",
                    help="config whose [retrieval] rerank_enabled is TRUE; run 2 "
                         "loads it, so run 2's baseline is the ON control and the "
                         "paired delta sign inverts (see --help notes / module doc)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="encoder daemon port (default: %(default)s)")
    ap.add_argument("--daemon-python", dest="daemon_python", default=sys.executable,
                    help="interpreter used to SPAWN the daemon (default: this one; "
                         "GPU runs pass f:/tmp/venv-cuda/Scripts/python.exe)")
    ap.add_argument("--out-prefix", dest="out_prefix", default=DEFAULT_OUT_PREFIX,
                    help="receipts are <prefix>_run{N}.json (wrapper), "
                         "<prefix>_run{N}_ladder.json (ladder receipt) and "
                         "<prefix>_run{N}_daemon.log (default: %(default)s)")
    ap.add_argument("--stamp", default="", help="run timestamp; blank = generated")
    ap.add_argument("--ready-timeout", dest="ready_timeout", type=float,
                    default=DEFAULT_READY_TIMEOUT_S,
                    help="seconds to wait for daemon /ready (default: %(default)s)")
    ap.add_argument("--ladder-timeout", dest="ladder_timeout", type=float,
                    default=DEFAULT_LADDER_TIMEOUT_S,
                    help="seconds before a ladder run is treated as hung "
                         "(default: %(default)s)")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    stamp = args.stamp or time.strftime("%Y%m%d-%H%M%S")

    fatal, deltas = preflight_configs(
        _resolve(args.config_off), _resolve(args.config_on))
    if fatal:
        for reason in fatal:
            print(f"error: config preflight: {reason}", file=sys.stderr)
        return 2
    if deltas:
        shown = ", ".join(deltas[:12]) + (" ..." if len(deltas) > 12 else "")
        print(f"warning: the two configs differ in {len(deltas)} knob(s) beyond "
              f"[retrieval] rerank_enabled: {shown}\n"
              "  load_config does NOT merge one toml over another — an omitted "
              "section falls back to dataclass defaults, so these deltas are a "
              "confound unless deliberate. Recorded as config_delta_other in "
              "both wrappers.", file=sys.stderr)

    wrappers: List[Dict[str, Any]] = []
    for plan in RUN_PLANS:
        wrappers.append(run_one(plan, args, stamp, config_delta_other=deltas))

    bad = [w["run"] for w in wrappers if not w["daemon_proof_ok"]]
    print("\n=== rerank A/B summary ===")
    for w in wrappers:
        print(f"  run {w['run']} ({w['arm_order']}): "
              f"rerank_posts={w['daemon_log_posts_rerank']} "
              f"total_encode_posts={w['daemon_log_posts_total']} "
              f"proof_ok={w['daemon_proof_ok']}")
    if bad:
        print(f"\nREFUSING: run(s) {bad} did not prove daemon-served reranking. "
              "The receipts on disk are NOT usable as evidence — see the "
              "'refusals' field in each wrapper.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
