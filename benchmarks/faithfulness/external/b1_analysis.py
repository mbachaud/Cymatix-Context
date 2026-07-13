"""B1 operating-point repair — offline analysis on the #239 beds.

Read-only. Recomputes know-confidence under candidate (betas, s_ref, g_ref, floor)
and reports recall (delivered-and-used should fire) vs false-fire (heldout/non-causal
should not) on the sec.3 (48-needle) and sec.4 (72-needle) beds.

Formula mirrors helix_context/scoring/know_calibration.py:267-280 exactly.
"""
import json, math

NP = "F:/Projects/np-graph"
S3 = f"{NP}/needles_239_stage1.json"
S3F = f"{NP}/needles_239_faith.json"
S4 = f"{NP}/needles_239b_stage1.json"
S4F = f"{NP}/needles_239b_faith.json"

SHIPPED = dict(betas=[-2.1222, -1.1442, 0.8794, 0.9407, 0.2999, 0.7979],
               s_ref=4.2503, g_ref=0.4386, floor=0.45)

def clamp01(x): return max(0.0, min(1.0, x))
def sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z); return e / (1.0 + e)

def confidence(row, betas, s_ref, g_ref):
    b = betas
    s_ref = s_ref if s_ref > 0 else 1.0
    g_ref = g_ref if g_ref > 0 else 0.5
    z = b[0]
    z += b[1] * math.tanh(row["top_score"] / s_ref)
    z += b[2] * math.tanh(row["score_gap"] / g_ref)
    z += b[3] * (1.0 if row["lexical_dense_agree"] else 0.0)
    z += b[4] * clamp01(row["coordinate_confidence"])
    fm = row.get("freshness_min", None)
    if fm is not None and len(b) >= 6:
        z += b[5] * clamp01(fm)
    return sigmoid(z)

def load(stage, faith):
    s = {r["id"]: r for r in json.load(open(stage, encoding="utf-8"))}
    f = {r["id"]: r for r in json.load(open(faith, encoding="utf-8"))}
    rows = []
    for i, sr in s.items():
        fr = f[i]
        rows.append({**sr, "causal_use": fr.get("causal_use"),
                     "cell": sr.get("cell", None),
                     "used_competitor": fr.get("used_competitor_not_gold", False)})
    return rows

def verify(rows, name):
    """Confirm we reproduce stored raw_confidence under shipped params."""
    err = 0.0
    for r in rows:
        c = confidence(r, SHIPPED["betas"], SHIPPED["s_ref"], SHIPPED["g_ref"])
        err = max(err, abs(c - r["raw_confidence"]))
    print(f"  [{name}] max|recompute-stored| = {err:.2e}  (n={len(rows)})")

def evaluate(rows3, rows4, betas, s_ref, g_ref, floor, label=""):
    def fires(r): return confidence(r, betas, s_ref, g_ref) >= floor
    # sec3: recall on causal-used (45 pos), false-fire on 3 non-causal
    pos3 = [r for r in rows3 if r["causal_use"] is True]
    neg3 = [r for r in rows3 if r["causal_use"] is False]
    rec3 = sum(fires(r) for r in pos3) / len(pos3)
    ff3 = sum(fires(r) for r in neg3) / len(neg3) if neg3 else float("nan")
    # sec4 by cell
    answ = [r for r in rows4 if r["cell"] == "answerable"]
    held = [r for r in rows4 if r["cell"] == "heldout"]
    comp = [r for r in rows4 if r["cell"] == "competition"]
    comp_causal = [r for r in comp if r["causal_use"] is True]        # 5 used gold
    comp_noncausal = [r for r in comp if r["causal_use"] is False]    # 7 didn't
    rec_answ = sum(fires(r) for r in answ) / len(answ)
    ff_held = sum(fires(r) for r in held) / len(held)
    rec_compc = sum(fires(r) for r in comp_causal) / len(comp_causal)
    ff_compn = sum(fires(r) for r in comp_noncausal) / len(comp_noncausal)
    maxc3 = max(confidence(r, betas, s_ref, g_ref) for r in rows3)
    maxc4 = max(confidence(r, betas, s_ref, g_ref) for r in rows4)
    print(f"\n### {label}")
    print(f"  betas={[round(x,3) for x in betas]} s_ref={s_ref} g_ref={g_ref} floor={floor}")
    print(f"  conf range: sec3 max={maxc3:.3f}  sec4 max={maxc4:.3f}")
    print(f"  sec3  RECALL(causal,n={len(pos3)})={rec3:.2f}   false-fire(non-causal,n={len(neg3)})={ff3:.2f}")
    print(f"  sec4  answerable-recall(n={len(answ)})={rec_answ:.2f}   HELDOUT false-fire(n={len(held)})={ff_held:.2f}")
    print(f"  sec4  competition: fire@used-gold(n={len(comp_causal)})={rec_compc:.2f}  fire@ignored(n={len(comp_noncausal)})={ff_compn:.2f}")
    return dict(rec3=rec3, ff3=ff3, rec_answ=rec_answ, ff_held=ff_held,
                rec_compc=rec_compc, ff_compn=ff_compn, maxc3=maxc3, maxc4=maxc4)

def main():
    rows3 = load(S3, S3F)
    rows4 = load(S4, S4F)
    print("Recompute check (should match stored to ~1e-4):")
    verify(rows3, "sec3"); verify(rows4, "sec4")

    b = SHIPPED
    evaluate(rows3, rows4, b["betas"], b["s_ref"], b["g_ref"], b["floor"], "SHIPPED (baseline)")

    # Candidate A: flip b1 sign only (minimal). Keep floor 0.45.
    bA = list(b["betas"]); bA[1] = abs(bA[1])
    evaluate(rows3, rows4, bA, b["s_ref"], b["g_ref"], 0.45, "A: un-invert b1 only, floor 0.45")

    # Candidate B: flip b1 + raise intercept so answerable clears floor.
    for db0 in (0.5, 1.0, 1.5):
        bB = list(b["betas"]); bB[1] = abs(bB[1]); bB[0] = b["betas"][0] + db0
        evaluate(rows3, rows4, bB, b["s_ref"], b["g_ref"], 0.5,
                 f"B: un-invert b1 + intercept+{db0}, floor 0.5")

    # Candidate C: flip b1 only, lower floor to a reachable operating point.
    bC = list(b["betas"]); bC[1] = abs(bC[1])
    for fl in (0.45, 0.5, 0.55):
        evaluate(rows3, rows4, bC, b["s_ref"], b["g_ref"], fl,
                 f"C: un-invert b1, floor {fl}")

    # Candidate D: full principled refit against a DELIVERY label (answerable/causal vs heldout),
    # NOT the circular imputed-causal refit. Fit on sec4 answerable+heldout (clean delivery contrast),
    # validate recall on sec3. Uses simple logistic GD.
    fit_rows = [r for r in rows4 if r["cell"] in ("answerable", "heldout")]
    y = [1 if r["cell"] == "answerable" else 0 for r in fit_rows]
    def feats(r, s_ref, g_ref):
        fm = r.get("freshness_min", None)
        return [1.0,
                math.tanh(r["top_score"]/s_ref),
                math.tanh(r["score_gap"]/g_ref),
                1.0 if r["lexical_dense_agree"] else 0.0,
                clamp01(r["coordinate_confidence"]),
                clamp01(fm) if fm is not None else 0.0]
    import statistics
    s_ref = max(1e-3, statistics.median(r["top_score"] for r in fit_rows))
    g_ref = max(1e-3, statistics.median(r["score_gap"] for r in fit_rows))
    X = [feats(r, s_ref, g_ref) for r in fit_rows]
    w = [0.0]*6
    lr, l2, epochs = 0.3, 1e-3, 4000
    n = len(X)
    for _ in range(epochs):
        grad = [0.0]*6
        for xi, yi in zip(X, y):
            z = sum(wj*xj for wj, xj in zip(w, xi))
            p = sigmoid(z)
            e = p - yi
            for j in range(6):
                grad[j] += e*xi[j]
        for j in range(6):
            g = grad[j]/n + (l2*w[j] if j > 0 else 0.0)
            w[j] -= lr*g
    print(f"\n[Candidate D refit] s_ref={s_ref:.4f} g_ref={g_ref:.4f} betas={[round(x,3) for x in w]}")
    # choose floor = value achieving precision>=0.8 on heldout-vs-answerable test-ish (in-sample here, flagged)
    for fl in (0.5, 0.6, 0.7):
        evaluate(rows3, rows4, w, s_ref, g_ref, fl, f"D: delivery-refit, floor {fl}")

    # Candidate E: revert to CODE DEFAULT_BETAS (which already have correct b1 sign +2.0, coord 1.8),
    # but keep the corpus-scale s_ref/g_ref (default 1.0/0.5 saturate tanh on this corpus).
    DEFAULT = [-2.0, 2.0, 1.5, 0.7, 1.8, 1.5]
    for fl in (0.5, 0.6, 0.7):
        evaluate(rows3, rows4, DEFAULT, b["s_ref"], b["g_ref"], fl, f"E: DEFAULT_BETAS + shipped s/g_ref, floor {fl}")
    # E' with default s_ref/g_ref for reference
    evaluate(rows3, rows4, DEFAULT, 1.0, 0.5, 0.6, "E': DEFAULT_BETAS + default s/g_ref (tanh-saturating), floor 0.6")

    # Candidate F: targeted conceptual correction — un-invert b1, up-weight coord to 1.8 (its
    # delivery-signal importance, = the code default), keep everything else; sweep floor.
    bF = list(b["betas"]); bF[1] = abs(bF[1]); bF[4] = 1.8
    for fl in (0.5, 0.55, 0.6, 0.65):
        evaluate(rows3, rows4, bF, b["s_ref"], b["g_ref"], fl, f"F: un-invert b1 + coord->1.8, floor {fl}")

if __name__ == "__main__":
    main()
