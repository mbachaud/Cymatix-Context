"""A5 attack 6: independent audit of the r-loss scorer (P0's substrate)."""
import json, random, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
ERB = Path(r"F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab/benchmarks/dogfood/erb")
sys.path.insert(0, str(ERB))
import rloss

BASE = ERB / "receipts/ladder_v09x_w24_min_delivered_2026-08-28.json"
pq = rloss.load_arm(str(BASE))
res = {}

# ---- independent reimplementation (no shared code with rloss.py) ----
def mine_score(a, b, field="delivered_gold"):
    A = {r["needle"]: (0 if r.get("error") else (r.get(field) or 0)) for r in a}
    B = {r["needle"]: (0 if r.get("error") else (r.get(field) or 0)) for r in b}
    sh = set(A) & set(B)
    lost = sorted(n for n in sh if A[n] > 0 and B[n] <= 0)
    won = sorted(n for n in sh if A[n] <= 0 and B[n] > 0)
    return {"n": len(sh), "hurt": len(lost), "helped": len(won),
            "lost": lost, "won": won,
            "base_rate": sum(1 for n in sh if A[n] > 0) / len(sh),
            "arm_rate": sum(1 for n in sh if B[n] > 0) / len(sh)}

# 1. identity
m = mine_score(pq, pq)
r = rloss.robustness_index(pq, pq)
res["identity"] = {"independent": {k: m[k] for k in ("n", "hurt", "helped", "base_rate")},
                   "rloss": {"n": r["n"], "hurt": r["hurt"], "helped": r["helped"],
                             "baseline_rate": r["baseline_rate"]},
                   "agree": m["hurt"] == r["hurt"] and m["helped"] == r["helped"]
                            and abs(m["base_rate"] - r["baseline_rate"]) < 1e-12}
res["baseline_delivered_gold_314_of_470"] = {
    "hits": sum(1 for x in pq if (x.get("delivered_gold") or 0) > 0),
    "n": len(pq),
    "rate": round(sum(1 for x in pq if (x.get("delivered_gold") or 0) > 0) / len(pq), 6),
}

# 2. RANDOMISED negative control, 200 trials with random flip counts
rng = random.Random(4242)
hits = [x["needle"] for x in pq if (x.get("delivered_gold") or 0) > 0]
miss = [x["needle"] for x in pq if not (x.get("delivered_gold") or 0) > 0]
mismatches = 0
for _ in range(200):
    nb = rng.randrange(0, 30)
    nf = rng.randrange(0, 30)
    broke = set(rng.sample(hits, nb))
    fixed = set(rng.sample(miss, nf))
    mut = []
    for x in pq:
        y = dict(x)
        if y["needle"] in broke:
            y["delivered_gold"] = 0
        if y["needle"] in fixed:
            y["delivered_gold"] = 3
        mut.append(y)
    a = rloss.r_loss_at_k(pq, mut)
    b = rloss.rescued_at_k(pq, mut)
    if a["count"] != nb or b["count"] != nf or set(a["needles"]) != broke or set(b["needles"]) != fixed:
        mismatches += 1
res["randomised_negative_control_200_trials"] = {
    "mismatches": mismatches,
    "note": "random numbers of hits broken and misses fixed; scorer must name exactly those sets",
}

# 3. adversarial: does the scorer survive a shuffled arm with DIFFERENT objects?
sh = [dict(x) for x in pq]
random.Random(7).shuffle(sh)
res["shuffle_with_copied_objects"] = rloss.robustness_index(pq, sh)["churn"]

# 4. THE GAP: an arm that silently drops needles
short = [dict(x) for x in pq if x["needle"] not in set(hits[:40])]
ri = rloss.robustness_index(pq, short)
rl = rloss.r_loss_at_k(pq, short)
res["dropped_needle_hazard"] = {
    "arm_has_n_needles": len(short),
    "reported_n": ri["n"],
    "reported_hurt": rl["count"],
    "reported_baseline_rate": round(ri["baseline_rate"], 6),
    "true_baseline_rate_over_470": round(sum(1 for x in pq if (x.get("delivered_gold") or 0) > 0) / len(pq), 6),
    "baseline_only_len": len(rl["baseline_only"]),
    "finding": ("40 baseline HITS removed from the arm are scored as ZERO r-loss and the reported "
                "rates are computed over the surviving 430. The dropped names ARE disclosed in "
                "baseline_only/arm_only, but nothing raises or warns, so a caller reading only "
                "count / robustness_index / delta_rate would see a clean arm. Recommend an explicit "
                "strict=True that raises when baseline_only or arm_only is non-empty."),
}

# 5. basis field sensitivity
res["basis_fields"] = {b: rloss.robustness_index(pq, pq, b)["baseline_rate"] for b in ("delivered", "pool", "final")}

# 6. does _hit treat a boolean / float field correctly?
probe = [{"needle": "a", "delivered_gold": True}, {"needle": "b", "delivered_gold": False},
         {"needle": "c", "delivered_gold": 0.5}, {"needle": "d"}, {"needle": "e", "error": "x", "delivered_gold": 5}]
res["hit_semantics"] = {r["needle"]: rloss._hit(r) for r in probe}

# 7. duplicate / missing-key guards
try:
    rloss.r_loss_at_k(pq + [dict(pq[0])], pq)
    dup = "NOT RAISED"
except ValueError as e:
    dup = "raised: %s" % e
res["duplicate_guard"] = dup
try:
    rloss.r_loss_at_k([{"delivered_gold": 1}], pq)
    nk = "NOT RAISED"
except ValueError as e:
    nk = "raised: %s" % e
res["missing_needle_key_guard"] = nk
try:
    rloss.r_loss_at_k(pq, pq, "nonsense")
    bb = "NOT RAISED"
except ValueError as e:
    bb = "raised: %s" % e
res["bad_basis_guard"] = bb

print(json.dumps(res, indent=1, default=str))
json.dump(res, open(Path(r"C:/Users/max/AppData/Local/Temp/claude/F--Projects-cymatix-context--claude-worktrees-wave-1-semantic-ranking-5f1dab/c81df3a0-0a0c-4160-81ee-e68db3b02372/scratchpad/attack6.json"), "w"), indent=1, default=str)
