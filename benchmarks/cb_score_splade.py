"""SPLADE gate scorer: isolate SPLADE's contribution on the requests subset.
Same 2 requests tasks, official evaluator. The ONLY config delta between densefix and
SPLADE is splade_enabled -> this table is the clean +SPLADE-on-code measurement.
Run in cb-step0 venv (contextbench via PYTHONPATH, tree-sitter 0.20.4)."""
import json, os, subprocess, sys

CB_SRC = "F:/Projects/contextbench-src"
GOLD = "F:/Projects/helix-context/benchmarks/contextbench/gold_smoke_4repo.parquet"
RES = "F:/Projects/helix-context/benchmarks/contextbench/results"
CACHE = "F:/Projects/_cache/cb_repos"
VAL = "F:/tmp/cb_val"; os.makedirs(VAL, exist_ok=True)

# the 2 requests tasks (the clean dense isolation set)
IIDS = {"88e1ffd3", "e989ba2d"}  # short forms; match on suffix

env = os.environ.copy()
env["PYTHONPATH"] = CB_SRC + os.pathsep + env.get("PYTHONPATH", "")
env["CONTEXTBENCH_TMP_ROOT"] = "F:/Projects/_cache/cb_wt"
env["GIT_LFS_SKIP_SMUDGE"] = "1"


def _match(iid):
    return any(iid.endswith(s) or iid == s for s in IIDS)


def evalrows(pred, out):
    if os.path.isfile(out):
        os.remove(out)
    rc = subprocess.run([sys.executable, "-m", "contextbench.evaluate", "--gold", GOLD,
                         "--pred", pred, "--cache", CACHE, "--out", out], env=env,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    rows = [json.loads(l) for l in open(out, encoding="utf-8") if l.strip()] if os.path.isfile(out) else []
    return [r for r in rows if _match(r.get("instance_id", ""))]


def micro(rows, gran):
    v = [r for r in rows if "error" not in r]
    inter = sum(r.get("final", {}).get(gran, {}).get("intersection", 0) for r in v)
    gold = sum(r.get("final", {}).get(gran, {}).get("gold_size", 0) for r in v)
    pred = sum(r.get("final", {}).get(gran, {}).get("pred_size", 0) for r in v)
    return (inter / gold if gold else 1.0), (inter / pred if pred else 1.0), len(v)


def inj(metaf, fixed=None):
    if fixed:
        return fixed
    if not os.path.isfile(metaf):
        return 0
    m = json.load(open(metaf, encoding="utf-8"))
    xs = [m[i]["injected_tokens"] for i in m if _match(i)]
    return sum(xs) // len(xs) if xs else 0


# (label, pred_file, meta_file_or_None, fixed_tok_or_None)
arms = [
    ("BM25 @8k", "bm25_8k_pred.json", None, 8000),
    ("BM25 @27k", "bm25_27k_pred.json", None, 27000),
    ("v063 lexical @8k", "helix_v063lex_fingerprint_8k_pred.json", "helix_v063lex_fingerprint_8k_meta.json", None),
    ("v063 lexical @27k", "helix_v063lex_fingerprint_27k_pred.json", "helix_v063lex_fingerprint_27k_meta.json", None),
    ("r063 SHIP dense(cap12) @8k", "helix_r063ship_fingerprint_8k_pred.json", "helix_r063ship_fingerprint_8k_meta.json", None),
    ("r063 SHIP dense(cap12) @27k", "helix_r063ship_fingerprint_27k_pred.json", "helix_r063ship_fingerprint_27k_meta.json", None),
    ("r063 DENSEFIX(cap500) @8k", "helix_r063fix_fingerprint_8k_pred.json", "helix_r063fix_fingerprint_8k_meta.json", None),
    ("r063 DENSEFIX(cap500) @27k", "helix_r063fix_fingerprint_27k_pred.json", "helix_r063fix_fingerprint_27k_meta.json", None),
    ("r063 +SPLADE @8k", "helix_r063splade_fingerprint_8k_pred.json", "helix_r063splade_fingerprint_8k_meta.json", None),
    ("r063 +SPLADE @27k", "helix_r063splade_fingerprint_27k_pred.json", "helix_r063splade_fingerprint_27k_meta.json", None),
    ("v063 lexical packet", "helix_v063lex_packet_pred.json", "helix_v063lex_packet_meta.json", None),
    ("r063 DENSEFIX packet", "helix_r063fix_packet_pred.json", "helix_r063fix_packet_meta.json", None),
    ("r063 +SPLADE packet", "helix_r063splade_packet_pred.json", "helix_r063splade_packet_meta.json", None),
]

print(f"\nrequests isolation subset = {sorted(IIDS)} (N=2)")
print(f"{'arm':<30}{'n':>3}{'file_R':>8}{'line_R':>8}{'sym_R':>8}{'inj':>9}")
print("-" * 66)
for label, pf, mf, fixed in arms:
    pp = os.path.join(RES, pf)
    if not os.path.isfile(pp):
        print(f"{label:<30}{'(missing pred)':>20}")
        continue
    rows = evalrows(pp, os.path.join(VAL, "spl_" + pf.replace(".json", ".jsonl")))
    fr, _, _ = micro(rows, "file")
    lr, _, n = micro(rows, "line")
    sr, _, _ = micro(rows, "symbol")
    it = inj(os.path.join(RES, mf) if mf else "", fixed)
    print(f"{label:<30}{n:>3}{fr:>8.3f}{lr:>8.3f}{sr:>8.3f}{int(it):>9}")
print("\n(densefix vs +SPLADE @27k = the clean SPLADE contribution; same dense, same cap)")
