"""FinanceBench query-side lexical A/B (2026-10-26).

Tests whether the metrics-generated delivery blackout (0/50 at the 2026-08-31
baseline) is a query-vocabulary problem rather than a table-chunking problem.
Three query-side levers, each isolated, then stacked:

  A0 base    shipped cymatix.toml, original needles        (paired comparator)
  A1 fy      FY-token normalization: FY2018 -> 2018, FY22 -> 2022
  A2 strip   instruction-boilerplate word removal
  A3 syn     shipped toml + a finance [synonyms] block (capex, ppne, cogs, ...)
  A4 all     fy + strip needles under the syn toml

A1/A2 rewrite the needle query text (offline proxy for a query normalizer);
A3 is a pure config arm through the product's own [synonyms] path. Gold, bed,
k, and every other knob are identical across arms. Arms run sequentially
through ablation_ladder.py --arms baseline, PYTHONHASHSEED pinned.

IN-SAMPLE CAVEAT: the boilerplate word list and the synonym rows were written
after reading these 150 questions. Treat any lift as a ceiling for the lever,
not a generalization estimate.

Usage:
  python benchmarks/dogfood/sweeps/run_financebench_lexical_ab.py build
  python benchmarks/dogfood/sweeps/run_financebench_lexical_ab.py smoke
  python benchmarks/dogfood/sweeps/run_financebench_lexical_ab.py run
  python benchmarks/dogfood/sweeps/run_financebench_lexical_ab.py pair
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
D = ROOT / "benchmarks" / "dogfood" / "financebench"
LADDER = ROOT / "benchmarks" / "dogfood" / "erb" / "ablation_ladder.py"
SHIPPED = ROOT / "cymatix.toml"
BED = "F:/tmp/financebench_bed/financebench.db"
RESOLVED = D / "needles_resolved_financebench_full.json"
GOLD = D / "gold_by_needle_financebench.json"
STAMP = "2026-10-26"
RUN = D / "receipts" / f"lexical_ab_{STAMP}"
REFERENCE = Path("F:/Projects/cymatix-receipts/mirror/benchmarks/dogfood/financebench/"
                 "receipts/ladder_financebench_beta_seed0_2026-09-04.json")
SEED = "0"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RUN_TIMEOUT_S = 3 * 3600

log = logging.getLogger("fb_lexical_ab")

# ── levers ──────────────────────────────────────────────────────────────

_FY4 = re.compile(r"\bFY\s?'?(\d{4})(?!\d)")
_FY2 = re.compile(r"\bFY\s?'?(\d{2})(?!\d)")


def normalize_fy(q: str) -> str:
    q = _FY4.sub(r"\1", q)
    return _FY2.sub(r"20\1", q)


# Unambiguous instruction / answer-format vocabulary only. Finance words that
# double as boilerplate in some templates (equity, investment, income,
# millions) are deliberately kept.
BOILERPLATE = frozenset("""
answer answers question following respond response give provide solution
assume assuming perspective standpoint analyst banker equities research lost
internet connection providers please help compute calculate utilize utilizing
using use basing base based judgment judgments primarily information details
clearly plainly outlined shown displayed stated provided referencing
considering according agreement taking drawing conclusions within need want
reasonable approximation exact possible here asked round decimal places place
units percents defined define abilities approach address adopting relying
resource resources
""".split())
_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def strip_boilerplate(q: str) -> str:
    out = _WORD.sub(lambda m: "" if m.group(0).lower() in BOILERPLATE else m.group(0), q)
    out = re.sub(r"\s+([,.;:?)])", r"\1", out)
    out = re.sub(r"([,.;:])(?:\s*[,.;:])+", r"\1", out)
    return re.sub(r"\s{2,}", " ", out).strip()


# Standard accounting equivalences (question vocabulary -> statement line-item
# vocabulary). Single tokens only: Tier-3 FTS quotes each term as a phrase.
FINANCE_SYNONYMS = {
    "capex": ["purchases", "property", "plant", "equipment", "capital", "expenditures", "additions"],
    "expenditure": ["purchases", "property", "plant", "equipment", "additions"],
    "expenditures": ["purchases", "property", "plant", "equipment", "additions"],
    "ppne": ["property", "plant", "equipment", "net"],
    "cogs": ["cost", "sales", "goods", "sold", "revenues"],
    "dpo": ["accounts", "payable", "cost", "sales"],
    "dso": ["accounts", "receivable"],
    "dio": ["inventories", "cost", "sales"],
    "roa": ["net", "income", "total", "assets"],
    "roe": ["net", "income", "equity"],
    "ebitda": ["operating", "income", "depreciation", "amortization"],
    "ebit": ["operating", "income"],
    "eps": ["earnings", "per", "share", "diluted", "basic"],
    "fcf": ["operating", "activities", "purchases", "property", "plant", "equipment"],
    "revenue": ["revenues", "sales", "net"],
    "revenues": ["revenue", "sales", "net"],
    "inventory": ["inventories"],
    "position": ["balance", "sheet", "assets", "liabilities"],
    "liquidity": ["current", "assets", "liabilities", "cash"],
    "working": ["current", "assets", "liabilities"],
    "operations": ["operating", "activities"],
    "dividends": ["paid", "cash", "dividend"],
    "dividend": ["paid", "cash", "dividends"],
    "payout": ["dividends", "paid", "net", "income"],
    "retention": ["dividends", "paid", "net", "income"],
    "cashflow": ["cash", "flows"],
}

ARMS = {
    "A0_base": {"needles": "base", "config": "shipped"},
    "A1_fy": {"needles": "fy", "config": "shipped"},
    "A2_strip": {"needles": "strip", "config": "shipped"},
    "A3_syn": {"needles": "base", "config": "syn"},
    "A4_all": {"needles": "fystrip", "config": "syn"},
}
VARIANTS = {
    "base": lambda q: q,
    "fy": normalize_fy,
    "strip": strip_boilerplate,
    "fystrip": lambda q: strip_boilerplate(normalize_fy(q)),
}


def sha256_file(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                       text=True, timeout=30, creationflags=NO_WINDOW)
    return r.stdout.strip()


def needles_path(variant: str) -> Path:
    return RESOLVED if variant == "base" else RUN / f"needles_resolved_financebench_{variant}.json"


def config_path(name: str) -> Path:
    return SHIPPED if name == "shipped" else RUN / "cymatix_finance_synonyms.toml"


def receipt_path(arm: str, smoke: bool = False) -> Path:
    return RUN / f"{'smoke_' if smoke else ''}ladder_financebench_{arm}_seed{SEED}_{STAMP}.json"


# ── build ───────────────────────────────────────────────────────────────

def build() -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    src = json.loads(RESOLVED.read_text(encoding="utf-8"))
    changed = {}
    for variant, fn in VARIANTS.items():
        if variant == "base":
            continue
        out = dict(src)
        out["needles"] = [dict(n, query=fn(n["query"])) for n in src["needles"]]
        out["lexical_variant"] = variant
        needles_path(variant).write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
        changed[variant] = sum(a["query"] != b["query"] for a, b in zip(src["needles"], out["needles"]))

    # Arm TOML = shipped text with the finance rows inserted directly under
    # the existing [synonyms] header. tomllib rejects duplicate keys, so a
    # collision with a shipped row fails the load below.
    text = SHIPPED.read_text(encoding="utf-8")
    rows = "\n".join(f"{k} = {json.dumps(v)}" for k, v in FINANCE_SYNONYMS.items())
    block = ("[synonyms]\n# FinanceBench lexical A/B 2026-10-26 (arm-only rows)\n" + rows + "\n")
    assert text.count("\n[synonyms]\n") == 1
    config_path("syn").write_text(text.replace("\n[synonyms]\n", "\n" + block, 1), encoding="utf-8")

    sys.path.insert(0, str(ROOT))
    from dataclasses import asdict
    from cymatix_context.config import load_config

    shipped = load_config(str(SHIPPED))
    syn = load_config(str(config_path("syn")))
    a, b = asdict(shipped), asdict(syn)
    diff_keys = sorted(k for k in a if a[k] != b[k])
    assert diff_keys == ["synonym_map"], diff_keys
    added = sorted(set(syn.synonym_map) - set(shipped.synonym_map))
    assert added == sorted(FINANCE_SYNONYMS), added
    assert all(syn.synonym_map[k] == shipped.synonym_map[k] for k in shipped.synonym_map)

    manifest = {
        "stamp": STAMP,
        "runner": "benchmarks/dogfood/sweeps/run_financebench_lexical_ab.py",
        "head": git("rev-parse", "HEAD"),
        "behind_origin_master": git("rev-list", "--count", "HEAD..origin/master"),
        "behind_origin_beta": git("rev-list", "--count", "HEAD..origin/beta"),
        "dirty": [ln for ln in git("status", "--porcelain").splitlines() if ln],
        "bed": BED, "bed_bytes": Path(BED).stat().st_size,
        "bed_ingest_c": "parallel, workers=6 (bed manifest.json 2026-08-31)",
        "resolved": str(RESOLVED.relative_to(ROOT)), "resolved_sha256": sha256_file(RESOLVED),
        "gold": str(GOLD.relative_to(ROOT)), "gold_sha256": sha256_file(GOLD),
        "shipped_config_sha256": sha256_file(SHIPPED),
        "syn_config_sha256": sha256_file(config_path("syn")),
        "loaded_config_diff_shipped_vs_syn": diff_keys,
        "synonym_rows_added": added,
        "variant_queries_changed": changed,
        "variant_sha256": {v: sha256_file(needles_path(v)) for v in VARIANTS if v != "base"},
        "arms": ARMS,
        "in_sample_caveat": "boilerplate list + synonym rows written after reading these 150 questions",
    }
    (RUN / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    log.info("build ok: %s", json.dumps({k: manifest[k] for k in
             ("head", "behind_origin_master", "bed_bytes", "variant_queries_changed",
              "loaded_config_diff_shipped_vs_syn")}))
    for variant in ("fy", "strip", "fystrip"):
        ex = json.loads(needles_path(variant).read_text(encoding="utf-8"))["needles"]
        for i in (95, 98, 126):
            log.info("%s | %s", variant, ex[i]["query"][:220])


# ── run ─────────────────────────────────────────────────────────────────

def env_for_run() -> dict:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = SEED
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    env["CYMATIX_DISABLE_LEARN"] = "1"
    env["CYMATIX_SQLITE_CACHE_SIZE"] = "-12582912"
    env.pop("CYMATIX_ENCODER_URL", None)
    env.pop("CYMATIX_CONFIG", None)
    return env


def bed_mtimes() -> dict:
    return {s or "db": (Path(BED + s).stat().st_mtime if Path(BED + s).exists() else None)
            for s in ("", "-wal", "-shm")}


def run_arm(arm: str, smoke: bool) -> dict:
    spec = ARMS[arm]
    out = receipt_path(arm, smoke)
    if out.exists():
        log.info("SKIP %s: receipt exists %s", arm, out)
        return {"arm": arm, "out": str(out), "skipped_existing": True}
    cmd = [sys.executable, "-P", str(LADDER),
           "--genome", BED,
           "--resolved", str(needles_path(spec["needles"])),
           "--gold", str(GOLD),
           "--limit", "1" if smoke else "0", "--k", "12", "--arms", "baseline",
           "--config", str(config_path(spec["config"])), "--per-query",
           "--stamp", STAMP, "--out", str(out)]
    before = bed_mtimes()
    log_path = RUN / f"{'smoke_' if smoke else ''}stage_{arm}.log"
    started = time.time()
    log.info("START %s: %s", arm, " ".join(cmd))
    with open(log_path, "w", encoding="utf-8") as fh:
        r = subprocess.run(cmd, cwd=str(ROOT), env=env_for_run(), stdout=fh,
                           stderr=subprocess.STDOUT, timeout=RUN_TIMEOUT_S,
                           creationflags=NO_WINDOW)
    ended = time.time()
    log.info("END %s rc=%s wall=%.0fs", arm, r.returncode, ended - started)
    row = {"arm": arm, "rc": r.returncode, "out": str(out), "wall_s": round(ended - started, 1)}
    if r.returncode == 0 and out.exists():
        d = json.loads(out.read_text(encoding="utf-8"))
        d["lexical_ab"] = {
            "arm": arm, **spec, "PYTHONHASHSEED": SEED, "python": sys.version,
            "sqlite": sqlite3.sqlite_version, "head": git("rev-parse", "HEAD"),
            "needles_sha256": sha256_file(needles_path(spec["needles"])),
            "config_sha256": sha256_file(config_path(spec["config"])),
            "bed_mtimes_before": before, "bed_mtimes_after": bed_mtimes(),
            "note": "latency not comparable to cold-gated references (arms back-to-back, warm page cache)",
        }
        out.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        base = d["arms"][0]
        row.update({k: base.get(k) for k in ("delivered_gold_rate", "recall_at_k", "final_recall_at_k")})
        row["observables"] = base.get("observables")
    return row


def run(smoke: bool) -> None:
    lock = RUN / ("smoke.lock" if smoke else "run.lock")
    if lock.exists():
        raise SystemExit(f"lock held: {lock} ({lock.read_text()}) — check for a live runner")
    lock.write_text(str(os.getpid()))
    try:
        rows = []
        for arm in (["A0_base", "A3_syn"] if smoke else list(ARMS)):
            rows.append(run_arm(arm, smoke))
            log.info("row: %s", json.dumps(rows[-1], default=str))
        (RUN / f"{'smoke_' if smoke else ''}run_rows.json").write_text(
            json.dumps(rows, indent=1, default=str), encoding="utf-8")
    finally:
        lock.unlink(missing_ok=True)


# ── pair ────────────────────────────────────────────────────────────────

def _per_query(path: Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    pq = d["arms"][0].get("per_query") or d.get("per_query")
    return {r["needle"]: r for r in pq}


def pair() -> None:
    types = {n["name"]: n["question_type"].replace("financebench_", "")
             for n in json.loads(RESOLVED.read_text(encoding="utf-8"))["needles"]}
    base = _per_query(receipt_path("A0_base"))
    out = {"stamp": STAMP, "n": len(base), "arms": {}}

    ref = _per_query(REFERENCE) if REFERENCE.exists() else None
    if ref is not None:
        diffs = [k for k in base if base[k].get("delivered_gold") != ref.get(k, {}).get("delivered_gold")
                 or base[k].get("rank_of_first_gold") != ref.get(k, {}).get("rank_of_first_gold")]
        out["A0_vs_reference_beta_seed0_2026-09-04"] = {
            "reference": str(REFERENCE), "delivered_ref": sum(r.get("delivered_gold", 0) for r in ref.values()),
            "delivered_A0": sum(r.get("delivered_gold", 0) for r in base.values()),
            "needles_differing_delivered_or_rank": len(diffs), "examples": diffs[:10],
        }

    for arm in ARMS:
        p = receipt_path(arm)
        if not p.exists():
            continue
        pq = _per_query(p)
        assert set(pq) == set(base), f"{arm}: needle sets differ"
        by_type = Counter()
        pool = Counter()
        n_type = Counter(types[k] for k in pq)
        gained, lost = [], []
        for k, r in pq.items():
            t = types[k]
            by_type[t] += int(r.get("delivered_gold") or 0)
            pool[t] += int(r.get("rank_of_first_gold") is not None)
            a, b = int(base[k].get("delivered_gold") or 0), int(r.get("delivered_gold") or 0)
            if b > a:
                gained.append(k)
            elif b < a:
                lost.append(k)
        out["arms"][arm] = {
            "delivered": sum(by_type.values()), "delivered_rate": round(sum(by_type.values()) / len(pq), 4),
            "delivered_by_type": {t: f"{by_type[t]}/{n_type[t]}" for t in sorted(n_type)},
            "gold_in_ranked_pool_by_type": {t: f"{pool[t]}/{n_type[t]}" for t in sorted(n_type)},
            "vs_A0": f"+{len(gained)}/-{len(lost)}", "gained": gained, "lost": lost,
        }
    (RUN / "paired_summary.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("stage", choices=["build", "smoke", "run", "pair"])
    args = ap.parse_args()
    if args.stage == "build":
        build()
    elif args.stage == "smoke":
        run(smoke=True)
    elif args.stage == "run":
        run(smoke=False)
    else:
        pair()
    return 0


if __name__ == "__main__":
    sys.exit(main())
