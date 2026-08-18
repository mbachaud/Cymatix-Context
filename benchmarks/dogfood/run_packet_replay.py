"""run_packet_replay.py — /context/packet DeliveryBlock replay + served-vs-
in-process agreement gate (#377 Phase 0.1; PR #363's DeliveryBlock).

PR #363 shipped a delivery-visibility block (``pool_size``,
``delivered_count``, ``delivered_gene_ids``, ``cap_binding`` — vocabulary
``schemas.CAP_BINDINGS``) with **zero receipts**. The #377 gate reads
"ladder and served values agree on a needle replay". This harness boots a
real uvicorn on the bed, replays the resolved needles over HTTP, then
replays the SAME needles in-process and reports whether the served
delivered sets are the ones the in-process bases would have claimed.

**The block lives on two different code paths** — a fact this harness
measures instead of papering over:

* ``/context`` serves the PIPELINE block (``window.metadata["delivery"]``,
  context_manager.py ~2287): ``delivered_gene_ids`` **is**
  ``window.expressed_gene_ids`` — the exact basis
  ``erb/ablation_ladder.py::delivered_gold_fields`` computes
  delivered-gold from. This is the surface where the ladder-agreement
  question is well-posed, and the only surface that can emit
  ``cap_binding = "token_budget"``.
* ``/context/packet`` serves the PACKET block (context_packet.py ~636):
  ``delivered_gene_ids`` is the packet's verified+stale_risk item order
  from ``build_context_packet`` — a separate retrieval path that applies
  no classifier cap and no token budget, so it never emits
  ``"token_budget"``. Its honest in-process counterpart is
  ``build_context_packet`` itself, NOT the ladder's ``build_context``.

So the agreement check runs three comparisons per needle:

1. **context surface** (the ladder gate): served ``/context``
   ``delivery.delivered_gene_ids`` vs in-process
   ``build_context(...).expressed_gene_ids`` — same call parameters the
   route uses (``ignore_delivered=True``, ``read_only=True``, no
   per-request max_genes).
2. **packet surface** (the replay gate for the packet path): served
   ``/context/packet`` ``delivery.delivered_gene_ids`` vs in-process
   ``build_context_packet`` on the same bed/config/max_genes.
3. **cross-path** (informational, the literal #377 sentence): ladder
   basis vs served packet ids, gold-delivery agreement only. The two
   paths retrieve differently by construction, so this number is a
   measurement of path divergence, not a harness defect.

Honesty guardrails, same spirit as the rest of the bed:

* **Set match gates, order is reported.** The packet path orders items by
  ``live_truth_score`` which decays with ``now_ts``, so served-vs-inproc
  calls minutes apart can legally permute near-ties. Exact-order match is
  recorded; the verdict gates on set match and on gold agreement.
* **No concurrent bed access.** HTTP phase first (server owns the bed),
  server torn down (issue #127 tree-kill), then the in-process phase
  opens the bed itself. Two writers never overlap.
* **Hash-seed pin on BOTH sides.** BenchServer pins ``PYTHONHASHSEED=0``
  in the spawned uvicorn; the harness re-execs itself once with the same
  pin so in-process iteration order cannot drift for a reason the server
  was defended against.
* **Session elision cannot pollute the replay.** /context is called with
  ``ignore_delivered=true`` AND a unique ``session_id`` per needle.
  /context/packet has no such knob — ``build_context_packet`` performs no
  session-delivery elision at all (checked, not assumed).
* **Read-only intent, known residue.** Fixture + per-request
  ``read_only=true`` guard gene content, but the /context route's CWoLa
  logger appends telemetry rows to the served bed regardless (same
  residue run_server_scaling.py documents). ``CYMATIX_DISABLE_LEARN=1``
  is set on the server env and in this process.
* **Served shape drift is a receipt fact.** The served block's keys are
  diffed against the schema's four fields; extra/missing keys land in
  the receipt instead of being silently accepted.

Usage::

    python benchmarks/dogfood/run_packet_replay.py              # all needles
    python benchmarks/dogfood/run_packet_replay.py --limit 5    # smoke
    python benchmarks/dogfood/run_packet_replay.py \
        --ladder-receipt benchmarks/dogfood/erb/receipts/some_ladder.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "benchmarks"))
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "erb"))

# Retrieval-only probe: never let a query mutate the bed's learning state.
# Must be set BEFORE any manager is constructed (in-process phase).
os.environ.setdefault("CYMATIX_DISABLE_LEARN", "1")

from bench_orchestrator import BenchServer, Fixture  # noqa: E402
from ablation_ladder import delivered_gold_fields  # noqa: E402

log = logging.getLogger("bench.packet_replay")

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Torch imports alone can eat tens of seconds on a cold filesystem cache
# (same rationale as run_cold_start.py).
HEALTH_TIMEOUT_S = 120.0

# The schema's contract for the served block (schemas.DeliveryBlock,
# extra="forbid"). Any drift between this tuple and what actually arrives
# over HTTP is recorded per needle instead of silently accepted.
DELIVERY_KEYS = ("pool_size", "delivered_count", "delivered_gene_ids",
                 "cap_binding")


# ------------------------------------------------------------------ http ----

def _http_json(method: str, url: str, body: dict | None = None,
               timeout: float = 30.0):
    """Stdlib JSON request with an explicit timeout (repo rule: no hanging
    requests). Raises urllib.error.HTTPError / URLError on failure."""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def _git_sha() -> str | None:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=10.0,
            creationflags=NO_WINDOW,
        )
        return res.stdout.strip() or None if res.returncode == 0 else None
    except Exception:  # noqa: BLE001 — receipt tolerates a missing sha
        return None


# ---------------------------------------------------------------- shapes ----

def _delivery_shape_drift(block: dict | None) -> dict | None:
    """Diff a served delivery block's keys against the schema contract.

    Returns None when the shape matches exactly; otherwise a small dict
    naming the drift — the #377 review explicitly wants field mismatches
    reported, not silently adapted to."""
    if block is None:
        return None
    got = set(block.keys())
    want = set(DELIVERY_KEYS)
    if got == want:
        return None
    return {
        "missing_keys": sorted(want - got),
        "unexpected_keys": sorted(got - want),
    }


def _gold_facts(ids: list[str] | None, gold: set) -> dict:
    """Ladder-identical delivered-gold math (erb/ablation_ladder.py#335)."""
    return delivered_gold_fields(list(ids or []), gold)


def _compare(served_ids: list[str] | None, inproc_ids: list[str] | None,
             gold: set) -> dict:
    """Per-needle agreement row between a served and an in-process basis."""
    s = list(served_ids or [])
    i = list(inproc_ids or [])
    sg = _gold_facts(s, gold)
    ig = _gold_facts(i, gold)
    return {
        "exact_order_match": s == i,
        "set_match": set(s) == set(i),
        "gold_match": (sg["delivered_gold"] == ig["delivered_gold"]
                       and sg["delivered_gold_rank"] == ig["delivered_gold_rank"]),
        "served_only": sorted(set(s) - set(i)),
        "inproc_only": sorted(set(i) - set(s)),
    }


def _rate(rows: list[dict], surface: str, key: str) -> float | None:
    vals = [r["agreement"][surface][key] for r in rows
            if r.get("agreement", {}).get(surface) is not None]
    if not vals:
        return None
    return round(sum(1 for v in vals if v) / len(vals), 4)


# ------------------------------------------------------------ http phase ----

def _run_http_phase(server_url: str, needles: list[dict],
                    gold_by_needle: dict, args: argparse.Namespace) -> list[dict]:
    """POST /context/packet and /context per needle; return per-needle rows."""
    rows: list[dict] = []
    stamp = int(time.time() * 1000)
    for idx, nd in enumerate(needles):
        name = nd["name"]
        gold = gold_by_needle.get(name, set())
        row: dict = {"needle": name, "gold_count": len(gold)}

        # -- /context/packet (the zero-receipts surface) ---------------
        try:
            t0 = time.perf_counter()
            pkt = _http_json("POST", f"{server_url}/context/packet", {
                "query": nd["query"],
                "task_type": args.task_type,
                "max_genes": args.packet_max_genes,
                "read_only": True,
            }, timeout=args.request_timeout) or {}
            pkt_ms = round((time.perf_counter() - t0) * 1000, 1)
            delivery = pkt.get("delivery")
            ids = (delivery or {}).get("delivered_gene_ids")
            row["packet"] = {
                "latency_ms": pkt_ms,
                "delivery": delivery,
                "delivery_present": delivery is not None,
                "delivery_shape_drift": _delivery_shape_drift(delivery),
                "know": pkt.get("know"),
                "miss": pkt.get("miss"),
                "plr_confidence": pkt.get("plr_confidence"),
                "verified_count": len(pkt.get("verified") or []),
                "stale_risk_count": len(pkt.get("stale_risk") or []),
                "refresh_targets_count": len(pkt.get("refresh_targets") or []),
                **_gold_facts(ids, gold),
            }
        except Exception as exc:  # noqa: BLE001 — a failing needle is a datapoint
            log.warning("%s: /context/packet failed", name, exc_info=True)
            row["packet"] = {"error": repr(exc)}

        # -- /context (the pipeline surface, the ladder's counterpart) -
        try:
            t0 = time.perf_counter()
            payload = _http_json("POST", f"{server_url}/context", {
                "query": nd["query"],
                # Unique session per needle AND ignore_delivered: the
                # session working-set register can neither elide nor leak
                # across the replay.
                "session_id": f"packet-replay-{stamp}-{idx}",
                "ignore_delivered": True,
                "read_only": True,
            }, timeout=args.request_timeout)
            ctx_ms = round((time.perf_counter() - t0) * 1000, 1)
            # routes_context.context_endpoint returns [response]; unwrap.
            resp = (payload[0] if isinstance(payload, list) and payload
                    else (payload or {}))
            delivery = resp.get("delivery")
            ids = (delivery or {}).get("delivered_gene_ids")
            citations = ((resp.get("agent") or {}).get("citations") or [])
            cite_ids = [c.get("gene_id") for c in citations]
            row["context"] = {
                "latency_ms": ctx_ms,
                "delivery": delivery,
                "delivery_present": delivery is not None,
                "delivery_shape_drift": _delivery_shape_drift(delivery),
                "know": resp.get("know"),
                "miss": resp.get("miss"),
                # Route-internal consistency: citations are built from the
                # same expressed ids but SKIP genes whose citation row is
                # missing — so citations must be a subset, in order.
                "citations_subset_of_delivery": (
                    None if ids is None
                    else all(g in set(ids) for g in cite_ids)
                ),
                **_gold_facts(ids, gold),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: /context failed", name, exc_info=True)
            row["context"] = {"error": repr(exc)}

        p = row["packet"]
        c = row["context"]
        print(f"  [{idx + 1}/{len(needles)}] {name}: "
              f"packet {p.get('latency_ms', '-')}ms "
              f"gold_rank {p.get('delivered_gold_rank')}  "
              f"context {c.get('latency_ms', '-')}ms "
              f"gold_rank {c.get('delivered_gold_rank')}", flush=True)
        rows.append(row)
    return rows


# --------------------------------------------------------- inproc phase ----

def _run_inproc_phase(rows: list[dict], needles: list[dict],
                      gold_by_needle: dict, bed: Path,
                      config_path: Path | None,
                      args: argparse.Namespace) -> None:
    """Replay every needle in-process on the same bed/config.

    Two bases per needle, matching each served surface's true code path:
    ``build_context`` (pipeline / ladder basis) and
    ``build_context_packet`` (packet basis). Mutates *rows* in place.
    """
    from cymatix_context.config import load_config
    from cymatix_context.context_manager import CymatixContextManager
    from cymatix_context.context_packet import build_context_packet

    cfg = load_config(str(config_path)) if config_path else load_config()
    cfg.genome.path = str(bed)
    manager = CymatixContextManager(cfg)
    try:
        # Untimed warmup, erb convention: first call pays lazy imports /
        # cache priming so per-needle wall ms stays comparable.
        try:
            manager.build_context(needles[0]["query"], read_only=True,
                                  ignore_delivered=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("inproc warmup failed: %r", exc)

        by_name = {r["needle"]: r for r in rows}
        for idx, nd in enumerate(needles):
            name = nd["name"]
            gold = gold_by_needle.get(name, set())
            row = by_name[name]
            inproc: dict = {}

            # Pipeline basis — same effective parameters the /context
            # route passes to build_context_async (no per-request
            # max_genes in continue mode; config cap applies).
            try:
                t0 = time.perf_counter()
                window = manager.build_context(
                    nd["query"],
                    party_id=cfg.session.default_party_id,
                    session_id=f"packet-replay-inproc-{idx}",
                    ignore_delivered=True,
                    read_only=True,
                )
                inproc["pipeline_wall_ms"] = round(
                    (time.perf_counter() - t0) * 1000, 1)
                expressed = list(getattr(window, "expressed_gene_ids", None) or ())
                inproc["expressed_gene_ids"] = expressed
                inproc["pipeline_delivery"] = (
                    (getattr(window, "metadata", None) or {}).get("delivery"))
                inproc["pipeline_gold"] = _gold_facts(expressed, gold)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: inproc build_context failed", name,
                            exc_info=True)
                inproc["pipeline_error"] = repr(exc)

            # Packet basis — same call the /context/packet route makes.
            try:
                t0 = time.perf_counter()
                packet = build_context_packet(
                    nd["query"],
                    task_type=args.task_type,
                    genome=manager.genome,
                    max_genes=args.packet_max_genes,
                    now_ts=time.time(),
                    read_only=True,
                )
                inproc["packet_wall_ms"] = round(
                    (time.perf_counter() - t0) * 1000, 1)
                pkt_delivery = (packet.delivery.model_dump()
                                if packet.delivery is not None else None)
                inproc["packet_delivery"] = pkt_delivery
                inproc["packet_gold"] = _gold_facts(
                    (pkt_delivery or {}).get("delivered_gene_ids"), gold)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: inproc build_context_packet failed", name,
                            exc_info=True)
                inproc["packet_error"] = repr(exc)

            row["inproc"] = inproc
            print(f"  [{idx + 1}/{len(needles)}] {name}: inproc pipeline "
                  f"{inproc.get('pipeline_wall_ms', '-')}ms  packet "
                  f"{inproc.get('packet_wall_ms', '-')}ms", flush=True)
    finally:
        try:
            manager.close()
        except Exception:  # noqa: BLE001
            log.warning("manager.close failed", exc_info=True)


# ------------------------------------------------------------- agreement ----

def _attach_agreement(rows: list[dict], gold_by_needle: dict) -> None:
    """Per-needle served-vs-inproc comparisons (mutates rows)."""
    for row in rows:
        gold = gold_by_needle.get(row["needle"], set())
        inproc = row.get("inproc") or {}
        agreement: dict = {"context": None, "packet": None,
                           "ladder_vs_packet": None}

        ctx = row.get("context") or {}
        if "error" not in ctx and "pipeline_error" not in inproc \
                and "expressed_gene_ids" in inproc:
            served_ids = (ctx.get("delivery") or {}).get("delivered_gene_ids")
            cmp_row = _compare(served_ids, inproc["expressed_gene_ids"], gold)
            cmp_row["delivery_present_match"] = (
                ctx.get("delivery_present") is True
                and inproc.get("pipeline_delivery") is not None
            ) or (
                ctx.get("delivery_present") is False
                and inproc.get("pipeline_delivery") is None
            )
            srv_blk = ctx.get("delivery") or {}
            inp_blk = inproc.get("pipeline_delivery") or {}
            cmp_row["pool_size_match"] = (
                srv_blk.get("pool_size") == inp_blk.get("pool_size"))
            cmp_row["cap_binding_match"] = (
                srv_blk.get("cap_binding") == inp_blk.get("cap_binding"))
            agreement["context"] = cmp_row

        pkt = row.get("packet") or {}
        if "error" not in pkt and "packet_error" not in inproc \
                and "packet_delivery" in inproc:
            served_ids = (pkt.get("delivery") or {}).get("delivered_gene_ids")
            inp_blk = inproc.get("packet_delivery") or {}
            cmp_row = _compare(served_ids,
                               inp_blk.get("delivered_gene_ids"), gold)
            cmp_row["delivery_present_match"] = (
                pkt.get("delivery_present") is True
                and inproc.get("packet_delivery") is not None
            ) or (
                pkt.get("delivery_present") is False
                and inproc.get("packet_delivery") is None
            )
            srv_blk = pkt.get("delivery") or {}
            cmp_row["pool_size_match"] = (
                srv_blk.get("pool_size") == inp_blk.get("pool_size"))
            cmp_row["cap_binding_match"] = (
                srv_blk.get("cap_binding") == inp_blk.get("cap_binding"))
            agreement["packet"] = cmp_row

        # Cross-path (the literal #377 sentence): ladder basis vs the
        # served packet ids — gold agreement only, because the two code
        # paths retrieve differently by construction and a set diff here
        # measures path divergence, not serve drift.
        if "error" not in pkt and "expressed_gene_ids" in inproc:
            ladder_g = _gold_facts(inproc["expressed_gene_ids"], gold)
            agreement["ladder_vs_packet"] = {
                "gold_match": (
                    pkt.get("delivered_gold") == ladder_g["delivered_gold"]
                    and pkt.get("delivered_gold_rank")
                    == ladder_g["delivered_gold_rank"]),
                "ladder_delivered_gold": ladder_g["delivered_gold"],
                "packet_delivered_gold": pkt.get("delivered_gold"),
            }

        row["agreement"] = agreement


# ------------------------------------------------------- ladder receipt ----

def _compare_ladder_receipt(rows: list[dict], receipt_path: Path,
                            arm_name: str | None) -> dict:
    """Optional mode (a): join a prior ladder receipt's per-needle rows.

    Ladder receipts carry ``delivered_gold`` / ``delivered_gold_rank`` /
    ``delivered_count`` per needle (``--per-query`` arms) but NOT
    ``delivered_gene_ids``, so only the gold basis is comparable. The
    comparison is informational: the prior receipt's bed/config may have
    drifted from this run's, and that confound belongs to the reader."""
    data = json.loads(receipt_path.read_text(encoding="utf-8"))
    arms = [a for a in (data.get("arms") or []) if a.get("per_query")]
    if arm_name:
        arms = [a for a in arms if a.get("arm") == arm_name]
    if not arms:
        return {
            "receipt": str(receipt_path),
            "error": ("no arm with per_query rows"
                      + (f" named {arm_name!r}" if arm_name else "")
                      + " — re-run the ladder with --per-query"),
        }
    arm = arms[0]
    ladder_rows = {r.get("needle"): r for r in arm["per_query"]}

    per_needle, gold_matches = [], []
    for row in rows:
        lr = ladder_rows.get(row["needle"])
        if lr is None:
            continue
        ctx = row.get("context") or {}
        match = (lr.get("delivered_gold") == ctx.get("delivered_gold")
                 and lr.get("delivered_gold_rank")
                 == ctx.get("delivered_gold_rank"))
        gold_matches.append(match)
        per_needle.append({
            "needle": row["needle"],
            "ladder_delivered_gold": lr.get("delivered_gold"),
            "ladder_delivered_gold_rank": lr.get("delivered_gold_rank"),
            "served_context_delivered_gold": ctx.get("delivered_gold"),
            "served_context_delivered_gold_rank": ctx.get("delivered_gold_rank"),
            "gold_match": match,
        })
    return {
        "receipt": str(receipt_path),
        "arm": arm.get("arm"),
        "ladder_git_sha": data.get("git_sha"),
        "ladder_bed": data.get("bed"),
        "needles_matched": len(per_needle),
        "gold_match_rate": (round(sum(gold_matches) / len(gold_matches), 4)
                            if gold_matches else None),
        "note": ("gold-basis only: ladder receipts carry no "
                 "delivered_gene_ids; bed/config drift vs this run is the "
                 "reader's confound"),
        "per_needle": per_needle,
    }


# ---------------------------------------------------------------- verdict ----

def _summarize(rows: list[dict], inproc_ran: bool) -> tuple[dict, str]:
    errors = []
    for r in rows:
        for side in ("packet", "context"):
            if "error" in (r.get(side) or {}):
                errors.append(f"{r['needle']}:{side}")
        inp = r.get("inproc") or {}
        for key in ("pipeline_error", "packet_error"):
            if key in inp:
                errors.append(f"{r['needle']}:inproc.{key}")

    pkt_lat = [r["packet"]["latency_ms"] for r in rows
               if "latency_ms" in (r.get("packet") or {})]
    ctx_lat = [r["context"]["latency_ms"] for r in rows
               if "latency_ms" in (r.get("context") or {})]

    summary: dict = {
        "needles": len(rows),
        "errors": errors,
        "packet_latency_ms_median": (round(statistics.median(pkt_lat), 1)
                                     if pkt_lat else None),
        "context_latency_ms_median": (round(statistics.median(ctx_lat), 1)
                                      if ctx_lat else None),
        "packet_delivered_gold_rate": (
            round(sum(1 for r in rows
                      if (r.get("packet") or {}).get("delivered_gold"))
                  / len(rows), 4) if rows else None),
        "context_delivered_gold_rate": (
            round(sum(1 for r in rows
                      if (r.get("context") or {}).get("delivered_gold"))
                  / len(rows), 4) if rows else None),
        "delivery_present_packet": sum(
            1 for r in rows if (r.get("packet") or {}).get("delivery_present")),
        "delivery_present_context": sum(
            1 for r in rows if (r.get("context") or {}).get("delivery_present")),
        "shape_drift_needles": sorted(
            r["needle"] for r in rows
            if (r.get("packet") or {}).get("delivery_shape_drift")
            or (r.get("context") or {}).get("delivery_shape_drift")),
    }

    if inproc_ran:
        for surface in ("context", "packet"):
            summary[f"{surface}_agreement"] = {
                "exact_order_rate": _rate(rows, surface, "exact_order_match"),
                "set_match_rate": _rate(rows, surface, "set_match"),
                "gold_match_rate": _rate(rows, surface, "gold_match"),
                "pool_size_match_rate": _rate(rows, surface, "pool_size_match"),
                "cap_binding_match_rate": _rate(rows, surface,
                                                "cap_binding_match"),
            }
        summary["ladder_vs_packet_gold_match_rate"] = _rate(
            rows, "ladder_vs_packet", "gold_match")
        # Headline: fraction of needles where BOTH served surfaces
        # set-match their in-process counterpart.
        both = [
            (r["agreement"]["context"] or {}).get("set_match") is True
            and (r["agreement"]["packet"] or {}).get("set_match") is True
            for r in rows if r.get("agreement")
        ]
        summary["agreement_rate"] = (round(sum(both) / len(both), 4)
                                     if both else None)

    # Verdict: FAIL on any error or on served-vs-inproc GOLD disagreement
    # (the #377 gate's substance — the served block claiming a different
    # delivered-gold outcome than the in-process basis would). WARN when
    # gold agrees everywhere but sets/order/pool/cap drift somewhere.
    # PASS only on exact order + pool + cap agreement on both surfaces.
    if errors or not rows:
        return summary, "FAIL"
    if not inproc_ran:
        return summary, "INCONCLUSIVE"
    gold_ok = all(
        (r["agreement"][s] or {}).get("gold_match", True) is True
        for r in rows for s in ("context", "packet")
    )
    if not gold_ok:
        return summary, "FAIL"
    exact_ok = all(
        (r["agreement"][s] or {}).get(k) is True
        for r in rows for s in ("context", "packet")
        for k in ("exact_order_match", "pool_size_match", "cap_binding_match",
                  "delivery_present_match")
    )
    return summary, ("PASS" if exact_ok else "WARN")


# ---------------------------------------------------------------- parent ----

def main() -> int:
    # Determinism parity with the spawned server: BenchServer pins
    # PYTHONHASHSEED=0 in the uvicorn env; re-exec once so the in-process
    # phase runs under the same pin (set/dict iteration order).
    if os.environ.get("PYTHONHASHSEED") != "0":
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = "0"
        print("re-exec with PYTHONHASHSEED=0 (server-parity determinism pin)",
              flush=True)
        return subprocess.call(
            [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
            env=env, creationflags=NO_WINDOW,
        )

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--genome",
                    default="F:/Projects/cymatix-context/genomes/dogfood/genome.db",
                    help="bed genome (READ-ONLY intent; worktrees don't carry "
                         "it, so the default is absolute)")
    ap.add_argument("--resolved", default="benchmarks/dogfood/needles_resolved.json")
    ap.add_argument("--gold",
                    default="F:/Projects/cymatix-context/benchmarks/dogfood/"
                            "gene_ids/gold_by_needle.json")
    ap.add_argument("--config", default="",
                    help="cymatix.toml for BOTH sides (default: repo-root "
                         "cymatix.toml, which the spawned uvicorn also loads "
                         "via its repo_root cwd)")
    ap.add_argument("--k", type=int, default=12,
                    help="recall cutoff for gold facts (default 12)")
    ap.add_argument("--packet-max-genes", type=int, default=12,
                    help="max_genes for /context/packet AND the in-process "
                         "packet basis (default 12 = k = the config's "
                         "max_genes_per_turn, so all surfaces run at one "
                         "cap; the route's own default is 8)")
    ap.add_argument("--task-type", default="explain",
                    help="packet task_type (route default 'explain')")
    ap.add_argument("--limit", type=int, default=0,
                    help="use only the first N needles (0 = all; for smoke)")
    ap.add_argument("--port", type=int, default=11493,
                    help="non-default port; 11491/11492 belong to the other "
                         "dogfood harnesses, 11437/11439 to user services")
    ap.add_argument("--no-inproc", action="store_true",
                    help="skip the in-process phase (verdict INCONCLUSIVE "
                         "unless a --ladder-receipt is supplied)")
    ap.add_argument("--ladder-receipt", default="",
                    help="optional prior ladder receipt (--per-query arms) "
                         "to join on needle names, gold basis only")
    ap.add_argument("--ladder-arm", default="",
                    help="arm name inside --ladder-receipt (default: first "
                         "arm carrying per_query rows)")
    ap.add_argument("--request-timeout", type=float, default=120.0)
    ap.add_argument("--warmup-timeout", type=float, default=300.0,
                    help="untimed first-hit timeout; covers lazy model load")
    ap.add_argument("--out", default="benchmarks/dogfood/packet_replay.json")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    bed = Path(args.genome).resolve()
    if not bed.exists():
        print(f"genome not found: {bed}", file=sys.stderr)
        return 2

    config_path: Path | None
    if args.config:
        config_path = Path(args.config).resolve()
        if not config_path.exists():
            print(f"config not found: {config_path}", file=sys.stderr)
            return 2
    else:
        default_cfg = REPO_ROOT / "cymatix.toml"
        config_path = default_cfg if default_cfg.exists() else None

    resolved = json.loads((REPO_ROOT / args.resolved).read_text(encoding="utf-8"))
    needles = resolved["needles"][: args.limit] if args.limit else resolved["needles"]
    if not needles:
        print("no needles after --limit; nothing to measure", file=sys.stderr)
        return 2
    gold_path = Path(args.gold)
    if not gold_path.is_absolute():
        gold_path = REPO_ROOT / gold_path
    gold_by_needle = {k: set(v) for k, v in json.loads(
        gold_path.read_text(encoding="utf-8")).items()}

    # Bed identity lives next to the gold pack (both gitignored, absolute).
    summary_path = gold_path.parent / "summary.json"
    identity = (json.loads(summary_path.read_text(encoding="utf-8"))
                .get("identity_sha256") if summary_path.exists() else None)

    run_dir = REPO_ROOT / "benchmarks/dogfood/logs/packet_replay" / \
        time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"packet replay: {len(needles)} needles, port {args.port}, "
          f"packet_max_genes {args.packet_max_genes}, bed "
          f"{identity[:16] if identity else 'UNKNOWN'}\n")

    # ── Phase 1: HTTP replay (server owns the bed) ────────────────────
    server = BenchServer(port=args.port, repo_root=REPO_ROOT,
                         health_timeout_s=HEALTH_TIMEOUT_S,
                         log_to=run_dir / "server.log")
    boot: dict = {}
    try:
        swap = server.start(Fixture(
            name="dogfood-packet-replay", db=str(bed), read_only=True,
            extra_env={"CYMATIX_DISABLE_LEARN": "1"},
        ))
        boot = {"boot_s": swap.elapsed_s, "mechanism": swap.mechanism,
                "genes": swap.genes}
        print(f"server up in {swap.elapsed_s}s ({swap.genes} genes); "
              f"untimed warmup...", flush=True)
        # Untimed warmup: the first query pays lazy imports / caches.
        try:
            _http_json("POST", f"{server.url}/context", {
                "query": needles[0]["query"],
                "session_id": "packet-replay-warmup",
                "ignore_delivered": True,
                "read_only": True,
            }, timeout=args.warmup_timeout)
        except Exception as exc:  # noqa: BLE001 — replay rows will re-surface it
            log.warning("warmup /context failed: %r", exc)

        print("HTTP phase:", flush=True)
        rows = _run_http_phase(server.url, needles, gold_by_needle, args)
    finally:
        server.stop()  # tree-kill (issue #127) BEFORE the in-process open

    # ── Phase 2: in-process replay (harness owns the bed) ─────────────
    inproc_ran = False
    if not args.no_inproc:
        print("\nin-process phase:", flush=True)
        _run_inproc_phase(rows, needles, gold_by_needle, bed, config_path, args)
        inproc_ran = True
        _attach_agreement(rows, gold_by_needle)

    summary, verdict = _summarize(rows, inproc_ran)

    ladder_cmp = None
    if args.ladder_receipt:
        try:
            ladder_cmp = _compare_ladder_receipt(
                rows, Path(args.ladder_receipt), args.ladder_arm or None)
        except Exception as exc:  # noqa: BLE001 — informational join, not the gate
            log.warning("ladder receipt comparison failed", exc_info=True)
            ladder_cmp = {"receipt": args.ladder_receipt, "error": repr(exc)}

    payload = {
        "tool": "benchmarks/dogfood/run_packet_replay.py",
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "bed": str(bed),
        "bed_bytes": bed.stat().st_size,
        "bed_identity_sha256": identity,
        "config": str(config_path) if config_path else None,
        "git_sha": _git_sha(),
        "needles_file": str((REPO_ROOT / args.resolved).resolve()),
        "gold_file": str(gold_path),
        "needle_count": len(needles),
        "k": args.k,
        "packet_max_genes": args.packet_max_genes,
        "task_type": args.task_type,
        "port": args.port,
        "server_boot": boot,
        "env": {
            "CYMATIX_DISABLE_LEARN": os.environ.get("CYMATIX_DISABLE_LEARN"),
            "CYMATIX_ENCODER_URL": os.environ.get("CYMATIX_ENCODER_URL"),
            "CYMATIX_ENCODER_URL_set": "CYMATIX_ENCODER_URL" in os.environ,
            "PYTHONHASHSEED": os.environ.get("PYTHONHASHSEED"),
        },
        "worker_model": "one BenchServer uvicorn for the whole HTTP phase "
                        "(read-only fixture, CYMATIX_DISABLE_LEARN=1, "
                        "untimed warmup, sequential stdlib POSTs), torn down "
                        "before the in-process phase opens the same bed",
        "note": "DeliveryBlock rides two code paths: /context serves the "
                "pipeline block (delivered_gene_ids == expressed_gene_ids, "
                "the ablation ladder's delivered-gold basis; the only "
                "surface that can emit cap_binding=token_budget) while "
                "/context/packet serves build_context_packet's own block "
                "(verified+stale_risk order; never token_budget). Each "
                "surface is compared against ITS in-process counterpart; "
                "ladder_vs_packet is the cross-path gold comparison and "
                "measures path divergence, not serve drift. Verdict gates "
                "on gold agreement (FAIL) and exact order/pool/cap "
                "agreement (PASS vs WARN); packet item order depends on "
                "now_ts-decayed live_truth_score, so near-tie permutations "
                "land as WARN, not FAIL. agreement_rate = fraction of "
                "needles where BOTH surfaces set-match their in-process "
                "basis. Known residue: /context's CWoLa logger appends "
                "telemetry rows to the served bed even under read_only.",
        "per_needle": rows,
        "summary": summary,
        "ladder_receipt_comparison": ladder_cmp,
        "verdict": verdict,
        "worker_logs": str(run_dir),
    }
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"\nsummary: agreement_rate {summary.get('agreement_rate')}  "
          f"context set/gold "
          f"{(summary.get('context_agreement') or {}).get('set_match_rate')}/"
          f"{(summary.get('context_agreement') or {}).get('gold_match_rate')}  "
          f"packet set/gold "
          f"{(summary.get('packet_agreement') or {}).get('set_match_rate')}/"
          f"{(summary.get('packet_agreement') or {}).get('gold_match_rate')}  "
          f"cross-path gold "
          f"{summary.get('ladder_vs_packet_gold_match_rate')}  "
          f"verdict {verdict}")
    print(f"\nwrote {out}")
    return 0 if verdict != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
