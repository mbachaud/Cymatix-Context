"""
Benchmark Suite for AA (Artificial Analysis) and Hard Reasoning Tasks.

Supports "On" (Helix-augmented) and "Off" (Baseline) modes for comparison.
Mocked datasets are used for initial functional validation.

Usage:
    # Run Baseline
    python benchmarks/bench_aa_suite.py --benchmark scicode --mode off --output benchmarks/scicode_off.json
    
    # Run Treatment (Helix On)
    python benchmarks/bench_aa_suite.py --benchmark scicode --mode on --output benchmarks/scicode_on.json
"""

import argparse
import json
import os
import sys
import time
import re
import subprocess
import tempfile
import statistics
from typing import Dict, List, Optional

try:
    from datasets import load_dataset
    HAS_DATASETS = True
except ImportError:
    HAS_DATASETS = False

import httpx

HELIX_URL = os.environ.get("HELIX_URL", "http://127.0.0.1:11437")

# --- Mock Datasets ---
MOCK_DATASETS = {
    "scicode": [
        {
            "id": "sci_001",
            "category": "physics",
            "background": "The Navier-Stokes equations describe the motion of viscous fluid substances.",
            "question": "Which equations describe the motion of viscous fluid substances?",
            "expected": "Navier-Stokes",
            "accept": ["navier-stokes", "navier stokes"]
        },
        {
            "id": "sci_002",
            "category": "chemistry",
            "background": "The Arrhenius equation is a formula for the temperature dependence of reaction rates.",
            "question": "What formula describes the temperature dependence of reaction rates?",
            "expected": "Arrhenius",
            "accept": ["arrhenius"]
        },
        {
            "id": "sci_003",
            "category": "biology",
            "background": "The Michaelis-Menten kinetics model describes the rate of enzymatic reactions.",
            "question": "Which kinetics model describes the rate of enzymatic reactions?",
            "expected": "Michaelis-Menten",
            "accept": ["michaelis-menten", "michaelis menten"]
        }
    ],
    "aa-lcr": [
         {
            "id": "lcr_001",
            "category": "multi-hop",
            "background": "In the year 2042, the UN passed the Lunar Extraction Treaty. Five years later, the first commercial helium-3 mine opened. The mine was operated by a company called HelioCorp.",
            "question": "What year did the first commercial helium-3 mine open?",
            "expected": "2047",
            "accept": ["2047"]
        },
        {
            "id": "lcr_002",
            "category": "multi-hop",
            "background": "Project Genesis was initiated by Dr. Aris Thorne. The project aimed to create a self-sustaining biosphere. Thorne's colleague, Dr. Elena Rostova, later took over the project after Thorne retired in 2055.",
            "question": "Who took over Project Genesis after Dr. Thorne?",
            "expected": "Elena Rostova",
            "accept": ["elena rostova", "rostova"]
        }
    ],
    "terminal-bench": [
        {
            "id": "tb_001",
            "category": "logs",
            "background": "[2026-04-29T10:15:22Z] ERROR: Connection refused on port 8080.\n[2026-04-29T10:15:23Z] INFO: Retrying connection to database...\n[2026-04-29T10:15:25Z] FATAL: Database credentials expired for user 'admin_svc'.\n[2026-04-29T10:15:26Z] WARN: Falling back to read-only replica at 10.0.0.54.",
            "question": "Which user had expired credentials?",
            "expected": "admin_svc",
            "accept": ["admin_svc", "'admin_svc'"]
        },
        {
            "id": "tb_002",
            "category": "logs",
            "background": "Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)\nCPU: 0 PID: 1 Comm: swapper/0 Not tainted 5.15.0-generic\nHardware name: QEMU Standard PC (i440FX + PIIX, 1996)\nCall Trace:\n dump_stack+0x18/0x1a\n panic+0xe8/0x241\n mount_block_root+0x21a/0x256",
            "question": "What function was called immediately after dump_stack?",
            "expected": "panic",
            "accept": ["panic", "panic+"]
        }
    ],
    "ifbench": [
        {
            "id": "ifb_001",
            "category": "instruction-following",
            "background": "You are in a dark room. To the north is a door locked with a silver key. To the east is a chest. Inside the chest is a silver key and a potion. You have a sword in your inventory.",
            "question": "What sequence of two actions should you take to open the door to the north?",
            "expected": "Take silver key from chest, unlock door",
            "accept": ["take key", "open chest"]
        }
    ],
    "aa-omniscience": [
        {
            "id": "omn_001",
            "category": "knowledge",
            "background": "The capital of Australia is Canberra. The largest city is Sydney. The currency is the Australian Dollar (AUD).",
            "question": "What is the capital city of Australia?",
            "expected": "Canberra",
            "accept": ["canberra"]
        }
    ],
    "critpt": [
        {
            "id": "cpt_001",
            "category": "planning",
            "background": "Project Alpha has 3 tasks. Task A takes 2 days. Task B depends on A and takes 5 days. Task C depends on A and takes 1 day.",
            "question": "How many days is the critical path for Project Alpha?",
            "expected": "7",
            "accept": ["7", "seven"]
        }
    ]
}

# --- Real Dataset Loaders ---
def load_hf_dataset(benchmark: str, limit: Optional[int] = None) -> List[Dict]:
    if not HAS_DATASETS:
        raise ImportError("The 'datasets' package is required for real benchmarks. Run: pip install datasets")
        
    problems = []
    if benchmark == "gpqa":
        try:
            ds = load_dataset("Idavidrein/gpqa", "gpqa_diamond", split="train")
            for i, row in enumerate(ds):
                if limit and i >= limit:
                    break
                
                # Format GPQA
                q = row['Question']
                corr = row['Correct Answer']
                inc = [row['Incorrect Answer 1'], row['Incorrect Answer 2'], row['Incorrect Answer 3']]
                
                # GPQA is multiple choice, but for generative reasoning we often just ask the question
                # and provide the choices.
                background = "You are an expert scientist. Answer the following multiple-choice question."
                prompt = f"{q}\nChoices:\nA) {corr}\nB) {inc[0]}\nC) {inc[1]}\nD) {inc[2]}"
                
                problems.append({
                    "id": f"gpqa_{i}",
                    "category": "science",
                    "background": background,
                    "question": prompt,
                    "expected": corr,
                    "accept": [corr]
                })
        except Exception as e:
            print(f"Failed to load GPQA dataset: {e}")
            print("\nNOTE: GPQA is a gated dataset on HuggingFace.")
            print("Please run `huggingface-cli login` and provide an access token with permissions to read Idavidrein/gpqa.")
            sys.exit(1)
            
    elif benchmark == "scicode":
        # Example SciCode integration (using dummy if not standard HF path)
        print("Warning: SciCode dataset path might need updating based on access.")
        try:
            ds = load_dataset("SciCode1/SciCode", split="test")
            added = 0
            for i, row in enumerate(ds):
                if limit and added >= limit:
                    break
                    
                bg = row.get('problem_background_main', '')
                if not bg or not bg.strip():
                    continue # Skip problems without a background
                    
                tests = row.get('general_tests', [])
                test_code = "\n".join(tests) if isinstance(tests, list) else str(tests)
                
                problems.append({
                    "id": f"scicode_{i}",
                    "category": "coding",
                    "background": bg.strip(),
                    "question": row.get('problem_description_main', ''),
                    "expected": "code",
                    "accept": [],
                    "test_code": test_code
                })
                added += 1
        except Exception as e:
            print(f"Failed to load scicode: {e}")
            print("Falling back to mock dataset for scicode")
            return MOCK_DATASETS["scicode"][:limit] if limit else MOCK_DATASETS["scicode"]
            
    # Add other benchmarks here as they are acquired
    else:
        # Fallback to mock for unimplemented ones
        if benchmark in MOCK_DATASETS:
            print(f"Using mock dataset for {benchmark}")
            return MOCK_DATASETS[benchmark][:limit] if limit else MOCK_DATASETS[benchmark]
        raise ValueError(f"Unknown benchmark: {benchmark}")
        
    return problems


def evaluate_answer(problem: Dict, answer_text: str) -> bool:
    if problem["category"] == "coding" and problem.get("test_code"):
        # Extract Python code
        match = re.search(r'```(?:python)?\s*(.*?)\s*```', answer_text, re.DOTALL)
        code = match.group(1) if match else answer_text
            
        full_code = code + "\n\n" + problem["test_code"]
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
            f.write(full_code)
            temp_path = f.name
            
        try:
            result = subprocess.run([sys.executable, temp_path], capture_output=True, timeout=10)
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            return False
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
                
    if not problem.get("accept"):
        return False
        
    return any(a.lower() in answer_text.lower() for a in problem["accept"])



def ingest_backgrounds(client: httpx.Client, benchmark: str, problems: List[Dict]) -> int:
    """Ingest the background information for 'on' mode testing."""
    ingested = 0
    for p in problems:
        try:
            metadata = {
                "source_id": f"{benchmark}_{p['id']}",
                "benchmark": benchmark
            }
            resp = client.post(f"{HELIX_URL}/ingest", json={
                "content": p["background"],
                "content_type": "text",
                "metadata": metadata,
            })
            if resp.status_code == 200:
                ingested += 1
        except Exception as e:
            print(f"Failed to ingest {p['id']}: {e}")
    return ingested


def run_problem(client: httpx.Client, problem: Dict, model: str, mode: str) -> Dict:
    t0 = time.time()
    
    found_in_context = False
    context_latency = 0.0
    proxy_latency = 0.0
    answer_correct = False
    answer_text = ""
    error_msg = None
    
    # Mode: OFF (Baseline) - provide background directly in prompt
    if mode == "off":
        prompt = f"Background:\n{problem['background']}\n\nQuestion:\n{problem['question']}"
        t1 = time.time()
        try:
            proxy_resp = client.post(f"{HELIX_URL}/v1/chat/completions", json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0, "num_predict": 64},
            })
            proxy_latency = time.time() - t1
            
            if proxy_resp.status_code == 200:
                choices = proxy_resp.json().get("choices", [])
                if choices:
                    answer_text = choices[0].get("message", {}).get("content", "")
                    answer_correct = evaluate_answer(problem, answer_text)
            else:
                error_msg = f"HTTP {proxy_resp.status_code}"
                
        except Exception as e:
            proxy_latency = time.time() - t1
            error_msg = str(e)
            
    # Mode: ON (Treatment) - rely on Helix context
    elif mode == "on":
        try:
            resp = client.post(f"{HELIX_URL}/context", json={
                "query": problem["question"],
                "decoder_mode": "condensed",
            })
            context_latency = time.time() - t0
            
            if resp.status_code == 200:
                data = resp.json()
                entry = data[0] if data else {}
                content = entry.get("content", "").lower()
                found_in_context = any(a.lower() in content for a in problem["accept"])
                
            else:
                error_msg = f"Context HTTP {resp.status_code}"
                
        except Exception as e:
            context_latency = time.time() - t0
            error_msg = f"Context Error: {e}"

        t1 = time.time()
        try:
            proxy_resp = client.post(f"{HELIX_URL}/v1/chat/completions", json={
                "model": model,
                "messages": [{"role": "user", "content": problem["question"]}],
                "stream": False,
                "options": {"temperature": 0, "num_predict": 64},
            })
            proxy_latency = time.time() - t1
            
            if proxy_resp.status_code == 200:
                choices = proxy_resp.json().get("choices", [])
                if choices:
                    answer_text = choices[0].get("message", {}).get("content", "")
                    answer_correct = evaluate_answer(problem, answer_text)
            else:
                error_msg = f"Proxy HTTP {proxy_resp.status_code}"
                
        except Exception as e:
            proxy_latency = time.time() - t1
            error_msg = f"Proxy Error: {e}"


    return {
        "id": problem["id"],
        "category": problem["category"],
        "found_in_context": found_in_context,
        "answer_correct": answer_correct,
        "context_latency_s": context_latency,
        "proxy_latency_s": proxy_latency,
        "error": error_msg
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", choices=["gpqa", "scicode", "aa-lcr", "tau-bench", "terminal-bench", "ifbench", "aa-omniscience", "critpt"], required=True)
    parser.add_argument("--mode", choices=["on", "off"], required=True)
    parser.add_argument("--model", default=os.environ.get("HELIX_MODEL", "qwen3:8b"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=None, help="Limit number of problems for a smoke test.")
    parser.add_argument("--mock", action="store_true", help="Force use of mock datasets.")
    parser.add_argument("--timeout", type=float, default=120.0, help="httpx client timeout per request (seconds).")
    parser.add_argument("--ids", type=str, default=None, help="Comma-separated problem IDs to keep (filter applied after dataset load).")
    args = parser.parse_args()

    client = httpx.Client(timeout=args.timeout)
    
    # Server check
    try:
        stats = client.get(f"{HELIX_URL}/stats").json()
        genome_genes = stats['total_genes']
        print(f"Helix server: {genome_genes} genes, mode={args.mode}")
    except Exception as e:
        print(f"ERROR: Helix server unreachable at {HELIX_URL}: {e}")
        # Allow offline running if mode is OFF and testing direct completion?
        # Actually, completions run through Helix proxy, so it must be up.
        sys.exit(1)

    if args.mock:
        problems = MOCK_DATASETS.get(args.benchmark, [])
        if args.limit: problems = problems[:args.limit]
    else:
        problems = load_hf_dataset(args.benchmark, limit=args.limit)
        
    if not problems:
        print("No problems loaded.")
        sys.exit(1)

    if args.ids:
        wanted = {s.strip() for s in args.ids.split(",") if s.strip()}
        problems = [p for p in problems if p.get("id") in wanted]
        print(f"Filtered to {len(problems)} problem(s) by --ids.")
        if not problems:
            print("No problems matched --ids filter.")
            sys.exit(1)

    if args.mode == "on":
        print(f"Ingesting {len(problems)} background contexts for {args.benchmark}...")
        ingested = ingest_backgrounds(client, args.benchmark, problems)
        print(f"Ingested {ingested}/{len(problems)} contexts.")
        time.sleep(2) # Give it a moment

    print(f"\nRunning {args.benchmark} ({args.mode.upper()})")
    
    results = []
    start_time = time.time()
    
    for p in problems:
        res = run_problem(client, p, args.model, args.mode)
        results.append(res)
        
        ctx_mark = "[+]" if res["found_in_context"] else "[-]"
        ans_mark = "[+]" if res["answer_correct"] else "[-]"
        lat = res["context_latency_s"] + res["proxy_latency_s"]
        
        err_str = f" ERR: {res['error']}" if res["error"] else ""
        print(f"  ctx{ctx_mark} ans{ans_mark} {lat:.2f}s | {p['id']}{err_str}")

    total_time_min = (time.time() - start_time) / 60.0

    # Calculate metrics
    n = len(results)
    retrieved_count = sum(1 for r in results if r["found_in_context"])
    answered_count = sum(1 for r in results if r["answer_correct"])
    error_count = sum(1 for r in results if r["error"] is not None)
    
    proxy_latencies = [r["proxy_latency_s"] for r in results if r["proxy_latency_s"] > 0]
    context_latencies = [r["context_latency_s"] for r in results if r["context_latency_s"] > 0]
    
    p50_proxy = statistics.median(proxy_latencies) if proxy_latencies else 0.0
    
    # simple p95 approx
    proxy_latencies.sort()
    context_latencies.sort()
    
    p95_proxy = proxy_latencies[int(len(proxy_latencies)*0.95)] if proxy_latencies else 0.0
    p95_context = context_latencies[int(len(context_latencies)*0.95)] if context_latencies else 0.0
    
    # Calculate by category
    categories = {}
    for r in results:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = {"n": 0, "retrieved": 0, "answered": 0}
        categories[cat]["n"] += 1
        if r["found_in_context"]: categories[cat]["retrieved"] += 1
        if r["answer_correct"]: categories[cat]["answered"] += 1
        
    for cat in categories:
        categories[cat]["retrieval_rate"] = categories[cat]["retrieved"] / categories[cat]["n"]
        categories[cat]["answer_rate"] = categories[cat]["answered"] / categories[cat]["n"]

    summary = {
        "n": n,
        "retrieval_rate": retrieved_count / max(n, 1),
        "answer_accuracy_rate": answered_count / max(n, 1),
        "retrieved": retrieved_count,
        "answered": answered_count,
        "errors": error_count,
        "by_category": categories,
        "latency": {
            "proxy_p50_s": round(p50_proxy, 3),
            "proxy_p95_s": round(p95_proxy, 3),
            "context_p95_s": round(p95_context, 3)
        },
        "total_time_min": round(total_time_min, 3)
    }

    output = {
        "benchmark": args.benchmark,
        "mode": args.mode,
        "model": args.model,
        "genome_genes": genome_genes,
        "summary": summary,
        "results": results
    }

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
        
    print(f"\nSaved results to {args.output}")
    print(f"Summary: Ans={answered_count}/{n} ({summary['answer_accuracy_rate']:.1%}), "
          f"Ret={retrieved_count}/{n} ({summary['retrieval_rate']:.1%})")


if __name__ == "__main__":
    main()
