"""Score all arms on the ContextBench smoke gold (official evaluator) -> one combined table.
BM25 pulled from step0_summary.json; every helix_*_pred.json scored fresh. cb-step0 venv."""
import glob, json, os, subprocess, sys

CB_SRC = "F:/Projects/contextbench-src"
GOLD = "F:/Projects/helix-context/benchmarks/contextbench/gold_smoke_4repo.parquet"
RES = "F:/Projects/helix-context/benchmarks/contextbench/results"
CACHE = "F:/Projects/_cache/cb_repos"

env = os.environ.copy()
env["PYTHONPATH"] = CB_SRC + os.pathsep + env.get("PYTHONPATH", "")
env["CONTEXTBENCH_TMP_ROOT"] = "F:/Projects/_cache/cb_wt"
env["GIT_LFS_SKIP_SMUDGE"] = "1"


def run_eval(pred, out):
    if os.path.isfile(out):
        os.remove(out)
    r = subprocess.run([sys.executable, "-m", "contextbench.evaluate", "--gold", GOLD,
                        "--pred", pred, "--cache", CACHE, "--out", out], env=env,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rows = [json.loads(l) for l in open(out, encoding="utf-8") if l.strip()] if os.path.isfile(out) else []
    return rows, r.returncode


def micro(rows, gran):
    v = [r for r in rows if "error" not in r]
    inter = sum(r.get("final", {}).get(gran, {}).get("intersection", 0) for r in v)
    gold = sum(r.get("final", {}).get(gran, {}).get("gold_size", 0) for r in v)
    pred = sum(r.get("final", {}).get(gran, {}).get("pred_size", 0) for r in v)
    return (inter / gold if gold else 1.0), (inter / pred if pred else 1.0), len(v)


def med(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


rows_out = []  # (arm, n, file_R, line_R, line_P, sym_R, med_inj, comp)

# BM25 from summary
summ = os.path.join(RES, "step0_summary.json")
if os.path.isfile(summ):
    s = json.load(open(summ, encoding="utf-8"))
    for arm, d in s.get("arms", {}).items():
        if "granularity" in d:
            g = d["granularity"]
            rows_out.append((arm, d["n_scored"], g["file"]["recall"], g["line"]["recall"],
                             g["line"]["precision"], g["symbol"]["recall"], d["median_injected_tokens"], 0))

# every helix pred
for pred in sorted(glob.glob(os.path.join(RES, "helix_*_pred.json"))):
    name = os.path.basename(pred).replace("_pred.json", "")
    metaf = pred.replace("_pred.json", "_meta.json")
    rows, rc = run_eval(pred, os.path.join(RES, name + "_eval.jsonl"))
    fr, _, _ = micro(rows, "file")
    lr, lp, n = micro(rows, "line")
    sr, _, _ = micro(rows, "symbol")
    inj, comp = [], 0
    if os.path.isfile(metaf):
        m = json.load(open(metaf, encoding="utf-8"))
        inj = [v.get("injected_tokens", 0) for v in m.values()]
        comp = sum(v.get("n_compressed", 0) for v in m.values())
    rows_out.append((name.replace("helix_", ""), n, fr, lr, lp, sr, med(inj), comp))

print(f"\n{'arm':<26}{'n':>3}{'file_R':>8}{'line_R':>8}{'line_P':>8}{'sym_R':>8}{'med_inj':>9}{'cmprsd':>7}")
print("-" * 77)
for a, n, fr, lr, lp, sr, mi, cz in rows_out:
    print(f"{a:<26}{n:>3}{fr:>8.3f}{lr:>8.3f}{lp:>8.3f}{sr:>8.3f}{int(mi):>9}{cz:>7}")
json.dump([dict(zip(["arm", "n", "file_R", "line_R", "line_P", "sym_R", "med_inj", "compressed"], r))
           for r in rows_out], open(os.path.join(RES, "combined_table.json"), "w"), indent=2)
print("\n-> combined_table.json")
