"""Score the v0.6.3 dense matrix on the 8-task subset (official evaluator, matched tasks).
Arms: BM25 / v0.6.3 lexical / v0.6.3 shipped-dense(cap12) / v0.6.3 densefix(cap500). cb-step0 venv."""
import json, os, subprocess, sys

CB_SRC = "F:/Projects/contextbench-src"
GOLD = "F:/Projects/helix-context/benchmarks/contextbench/gold_smoke_4repo.parquet"
RES = "F:/Projects/helix-context/benchmarks/contextbench/results"
CACHE = "F:/Projects/_cache/cb_repos"
VAL = "F:/tmp/cb_val"; os.makedirs(VAL, exist_ok=True)

iids = {t["instance_id"] for t in json.load(open("F:/tmp/cb_tasks_sub8.json", encoding="utf-8"))}
env = os.environ.copy()
env["PYTHONPATH"] = CB_SRC + os.pathsep + env.get("PYTHONPATH", "")
env["CONTEXTBENCH_TMP_ROOT"] = "F:/Projects/_cache/cb_wt"
env["GIT_LFS_SKIP_SMUDGE"] = "1"


def evalrows(pred, out):
    if os.path.isfile(out):
        os.remove(out)
    subprocess.run([sys.executable, "-m", "contextbench.evaluate", "--gold", GOLD, "--pred", pred,
                    "--cache", CACHE, "--out", out], env=env,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rows = [json.loads(l) for l in open(out, encoding="utf-8") if l.strip()] if os.path.isfile(out) else []
    return [r for r in rows if r.get("instance_id") in iids]


def micro(rows, gran):
    v = [r for r in rows if "error" not in r]
    inter = sum(r.get("final", {}).get(gran, {}).get("intersection", 0) for r in v)
    gold = sum(r.get("final", {}).get(gran, {}).get("gold_size", 0) for r in v)
    pred = sum(r.get("final", {}).get(gran, {}).get("pred_size", 0) for r in v)
    return (inter / gold if gold else 1.0), (inter / pred if pred else 1.0), len(v)


def medtok(metaf, fixed=None):
    if fixed:
        return fixed
    if not os.path.isfile(metaf):
        return 0
    m = json.load(open(metaf, encoding="utf-8"))
    xs = sorted(m[i]["injected_tokens"] for i in m if i in iids)
    return xs[len(xs) // 2] if xs else 0


# (label, pred_file, meta_file_or_None, fixed_tok_or_None)
arms = [
    ("BM25 @8k", "bm25_8k_pred.json", None, 8000),
    ("BM25 @27k", "bm25_27k_pred.json", None, 27000),
    ("v063 lexical @8k", "helix_v063lex_fingerprint_8k_pred.json", "helix_v063lex_fingerprint_8k_meta.json", None),
    ("v063 lexical @27k", "helix_v063lex_fingerprint_27k_pred.json", "helix_v063lex_fingerprint_27k_meta.json", None),
    ("v063 lexical packet", "helix_v063lex_packet_pred.json", "helix_v063lex_packet_meta.json", None),
    ("v063 SHIP dense @8k", "helix_v063ship8_fingerprint_8k_pred.json", "helix_v063ship8_fingerprint_8k_meta.json", None),
    ("v063 SHIP dense @27k", "helix_v063ship8_fingerprint_27k_pred.json", "helix_v063ship8_fingerprint_27k_meta.json", None),
    ("v063 SHIP dense packet", "helix_v063ship8_packet_pred.json", "helix_v063ship8_packet_meta.json", None),
    ("v063 DENSEFIX @8k", "helix_v063fix8_fingerprint_8k_pred.json", "helix_v063fix8_fingerprint_8k_meta.json", None),
    ("v063 DENSEFIX @27k", "helix_v063fix8_fingerprint_27k_pred.json", "helix_v063fix8_fingerprint_27k_meta.json", None),
    ("v063 DENSEFIX packet", "helix_v063fix8_packet_pred.json", "helix_v063fix8_packet_meta.json", None),
]

print(f"\nSubset = {len(iids)} tasks (2 each django/sympy/sklearn/requests)")
print(f"{'arm':<24}{'n':>3}{'file_R':>8}{'line_R':>8}{'sym_R':>8}{'med_inj':>9}{'cmprsd':>7}")
print("-" * 67)
out_rows = []
for label, pf, mf, fixed in arms:
    pp = os.path.join(RES, pf)
    if not os.path.isfile(pp):
        print(f"{label:<24}{'(missing pred)':>20}")
        continue
    rows = evalrows(pp, os.path.join(VAL, "s8_" + pf.replace(".json", ".jsonl")))
    fr, _, _ = micro(rows, "file")
    lr, _, n = micro(rows, "line")
    sr, _, _ = micro(rows, "symbol")
    mt = medtok(os.path.join(RES, mf) if mf else "", fixed)
    cz = 0
    if mf and os.path.isfile(os.path.join(RES, mf)):
        m = json.load(open(os.path.join(RES, mf), encoding="utf-8"))
        cz = sum(m[i].get("n_compressed", 0) for i in m if i in iids)
    print(f"{label:<24}{n:>3}{fr:>8.3f}{lr:>8.3f}{sr:>8.3f}{int(mt):>9}{cz:>7}")
    out_rows.append(dict(zip(["arm", "n", "file_R", "line_R", "sym_R", "med_inj", "compressed"],
                            [label, n, fr, lr, sr, int(mt), cz])))
json.dump(out_rows, open(os.path.join(RES, "sub8_table.json"), "w"), indent=2)
print("\n-> sub8_table.json")
