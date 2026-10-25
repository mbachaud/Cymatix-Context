"""sonnet_crater_search.py — can Sonnet 5.5 search its way to crater gold, with Jev?

EXPERIMENTAL. Follow-up to ``sonnet_answer_ab.py`` (PR #523), where Sonnet 5.5
answered the 50 crater needles one-shot from the delivered c4000 packet:
0 correct, 40 NOT_FOUND, 10 wrong. Here the same 50 needles get a search
loop. Sonnet issues one command per turn:

  SEARCH: <query>   cymatix build_context (c4000 classcap profile, k=12); Jev
                    judges every chunk against the ORIGINAL question
                    (judge_p) and the whole packet (packet_p); the 4 highest
                    judge_p chunks are shown in full, the rest as one-liners
  GREP: <words>     BM25 over genes_fts, any word (the 09-02 findability CLI)
  ALL: <words>      BM25 over genes_fts, every word
  READ: <doc_id>    one chunk in full, with its Jev judge_p
  ANSWER: <text> | NOT_FOUND   final

Turn 0 is harness-run: SEARCH with the original question, which must deliver
the same gene ids as the Jev receipt (parity check). Jev runs as the shipped
no-incoming judge (PR #522): no cymatix ranks or scores, packet order
shuffled per (needle, query). Gold gene ids are an ORACLE only: recorded per
turn (seen in an observation / shown in full) and never shown to the model.

Per turn: command, observation size, gold seen, model input / cache / output
tokens, CLI cost, wall split (model, retrieval, Jev). Final answers are graded
by Opus 5.5 with the same grader prompt as ``sonnet_answer_ab.py``.

Model calls: ``claude -p`` multi-turn via ``--resume`` in a clean room (empty
cwd, --setting-sources local, no tools/MCP/slash commands, CLAUDE.md and
auto-memory disabled). Sessions persist under ~/.claude/projects for the
clean-room cwd. One request at a time, >= 300 ms apart; Jev likewise.

Resumable per needle (``<run-dir>/needles.jsonl``; Jev calls cached in
``<run-dir>/jev_cache.jsonl``); ``<run-dir>/run.lock`` holds the runner PID.

Usage::

    $env:PYTHONHASHSEED=0
    python benchmarks/dogfood/jev/sonnet_crater_search.py --run-dir F:/tmp/sonnet_crater_search_2026-10-23 \\
        --claude-bin <claude.exe 2.1.280+> [--limit 3] [--stage search|grade|report|all] \\
        [--answer-model claude-opus-5-5]

``--answer-model`` swaps the searcher (default Sonnet 5.5); the prompts, tools,
budget and Opus grader stay fixed. A usage-limit reply from ``claude -p`` is
waited out (10-minute polls, up to 6 h) rather than recorded as an error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(REPO_ROOT))     # this worktree's cymatix_context, not the venv's
sys.path.insert(0, str(HERE))
from jev_239b_packet_know import chunk_questions, git, jev_call, packet_questions  # noqa: E402

ERB = REPO_ROOT / "benchmarks" / "dogfood" / "erb"
CONFIG = REPO_ROOT / "benchmarks" / "dogfood" / "grain_ladder" / "configs" / "grain_c4000_classcap.toml"
BED = Path(r"F:\tmp\grain_erb_full_c4000_2026-10-18\erb_full_c4000_lean.db")
EPOCH = "1792397919"   # the c4000 ladder receipt's pinned scoring clock
JEV_RECEIPT = HERE / "jev_erb_packet_know_c4000_classcap_2026-10-23.json"
GOLD = ERB / "gold_by_needle_grain4000.json"
# The 50 crater needles + ERB gold answers, and their one-shot labels (PR #523).
ONESHOT_DIR = Path(r"F:\tmp\sonnet_jev_answer_2026-10-23")
K = 12
FULL_DOCS = 4            # SEARCH: chunks shown in full, highest judge_p first
MAX_DOC_CHARS = 6000
GREP_LIMIT = 10
SNIPPET = 240
MAX_COMMANDS = 15
ANSWER_MODEL = "claude-sonnet-5-5"
GRADER_MODEL = "claude-opus-5-5"
EFFORT = "high"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CLAUDE_BIN = ["claude"]
# "You've hit your session limit · resets ..." from claude -p on a subscription plan.
LIMIT_HIT = re.compile(r"hit your .{0,40}limit", re.I)
LIMIT_POLL_S = 600
LIMIT_WAIT_MAX_S = 6 * 3600

SYSTEM = (
    "You answer questions about a company's internal documents (email, chat, tickets, "
    "docs, code reviews) using a search toolkit. Each reply may contain brief notes, but "
    "its LAST line must be exactly one command, written as plain text (not a tool call, "
    "not in a code block):\n"
    "SEARCH: <query> - the retrieval system returns up to 12 documents. A relevance judge "
    "scores each with judge_p (its probability that the document answers the original "
    "question) and the whole result with packet_p. The 4 highest-judge_p documents are "
    "shown in full, the rest as one-line summaries.\n"
    "GREP: <words> - keyword search (BM25, any of the words), top 10 hits with snippets.\n"
    "ALL: <words> - keyword search requiring every word.\n"
    "READ: <doc_id> - the full text of one document, with its judge_p.\n"
    "ANSWER: <concise answer with the specific values, names or facts asked for> - final.\n"
    "NOT_FOUND - final, when you conclude the documents do not contain the answer.\n"
    "The corpus holds many near-duplicate documents about other projects, teams or "
    "customers; answer only from a document that matches the question's specifics. The "
    "judge scores are evidence, not ground truth. Documents use their own vocabulary, "
    "which may differ from the question's: rephrase, and reuse names and terms you find."
)
GRADER_SYSTEM = (   # identical to sonnet_answer_ab.py
    "You are a strict grader. You get a question, the reference gold answer, its key "
    "answer facts, and a candidate answer. Label the candidate: 'correct' = states the "
    "key facts the question asks for (wording may differ) and contradicts none; "
    "'partial' = some key facts right but others missing or wrong; 'incorrect' = a "
    "substantive answer that is wrong, unrelated, or contradicts the facts; 'not_found' "
    "= the candidate declines or says the information was not found. Reply with exactly "
    "one line: LABEL: <correct|partial|incorrect|not_found> | <reason, at most 20 words>"
)
COMMAND = re.compile(r"^\s*(SEARCH|GREP|ALL|READ|ANSWER)\s*:\s*(.*)$|^\s*(NOT_FOUND)\s*\.?\s*$", re.I)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def append_jsonl(path: Path, rec: dict) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------- claude -p

def claude_turn(run: Path, system: str, prompt: str, model: str, last_call: list,
                session: str | None = None, persist: bool = True) -> dict:
    """One clean-room ``claude -p`` request; ``session`` continues a conversation."""
    room = run / "cleanroom"
    room.mkdir(exist_ok=True)
    env = {**os.environ, "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    cmd = [CLAUDE_BIN[0], "-p", "--tools", "", "--strict-mcp-config", "--setting-sources", "local",
           "--disable-slash-commands", "--system-prompt", system, "--model", model,
           "--effort", EFFORT, "--output-format", "json"]
    if session:
        cmd += ["--resume", session]
    elif not persist:
        cmd += ["--no-session-persistence"]
    wait = 0.3 - (time.monotonic() - last_call[0])
    if wait > 0:
        time.sleep(wait)
    rec, attempt, limit_wait = {}, 0, 0.0
    while attempt < 3:
        t0 = time.perf_counter()
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                               cwd=room, env=env, timeout=600, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            rec = {"status": "error", "error": "timeout 600 s"}
        else:
            try:
                j = json.loads(p.stdout)
            except json.JSONDecodeError:
                rec = {"status": "error", "error": f"rc={p.returncode} {p.stdout[:200]!r} {p.stderr[:200]!r}"}
            else:
                u = j.get("usage") or {}
                rec = {"status": "error" if j.get("is_error") else "succeeded",
                       "error": j.get("result") if j.get("is_error") else None,
                       "session_id": j.get("session_id"),
                       "models": sorted((j.get("modelUsage") or {}).keys()),
                       "text": (j.get("result") or "").strip(),
                       "input_tokens": u.get("input_tokens") or 0,
                       "cache_read": u.get("cache_read_input_tokens") or 0,
                       "cache_write": u.get("cache_creation_input_tokens") or 0,
                       "output_tokens": u.get("output_tokens") or 0,
                       "cost_usd": j.get("total_cost_usd") or 0.0}
        rec["wall_s"] = round(time.perf_counter() - t0, 1)
        rec["limit_wait_s"] = round(limit_wait)
        last_call[0] = time.monotonic()
        if rec["status"] == "succeeded":
            return rec
        if LIMIT_HIT.search(str(rec.get("error"))) and limit_wait < LIMIT_WAIT_MAX_S:
            # The plan's usage window ran out: wait for the reset instead of spending
            # an attempt, so a long run spans windows without recording errors.
            print(f"  usage limit, waiting {LIMIT_POLL_S // 60} min: {str(rec.get('error'))[:120]}", flush=True)
            time.sleep(LIMIT_POLL_S)
            limit_wait += LIMIT_POLL_S
            continue
        attempt += 1
        print(f"  claude attempt {attempt} failed: {str(rec.get('error'))[:200]}", flush=True)
        time.sleep(5.0 * attempt)
    return rec


def parse_command(text: str):
    """Last command line in the reply. Tolerates the tool-call-style wrappers the
    pilot saw (``<invoke>READ: x</invoke>``, code fences); an ANSWER keeps the
    lines that follow it."""
    clean = re.sub(r"</?invoke[^>]*>", "\n", text or "").replace("```", "\n")
    lines = [l.strip().strip("`*").strip() for l in clean.splitlines() if l.strip()]
    for i in range(len(lines) - 1, -1, -1):
        m = COMMAND.match(lines[i])
        if not m:
            continue
        if m.group(3):
            return "NOT_FOUND", ""
        cmd, arg = m.group(1).upper(), m.group(2).strip()
        if cmd == "ANSWER":
            arg = " ".join([arg] + lines[i + 1:]).strip()
        return cmd, arg
    return None, ""


def one_line(text: str, n: int) -> str:
    return re.sub(r"\s+", " ", text or "")[:n]


# ---------------------------------------------------------------- tools

class Tools:
    def __init__(self, run: Path, api_key: str):
        os.environ["CYMATIX_SCORING_EPOCH"] = EPOCH
        os.environ["CYMATIX_LEAN_INDEXES"] = "1"
        os.environ["CYMATIX_DISABLE_LEARN"] = "1"
        os.environ["CYMATIX_GENOME_PATH"] = str(BED)
        os.environ.pop("CYMATIX_ENCODER_URL", None)
        import cymatix_context
        from cymatix_context.config import load_config
        from cymatix_context.context_manager import CymatixContextManager

        self.cymatix_file = cymatix_context.__file__
        cfg = load_config(str(CONFIG))
        cfg.genome.path = str(BED)
        self.loaded_config = {
            "profile": str(CONFIG.relative_to(REPO_ROOT)), "genome.path": cfg.genome.path,
            "retrieval.fusion_mode": cfg.retrieval.fusion_mode, "retrieval.rrf_k": cfg.retrieval.rrf_k,
            "retrieval.dense_embedding_enabled": cfg.retrieval.dense_embedding_enabled,
            "budget.max_genes_per_turn": cfg.budget.max_genes_per_turn,
            "budget.expression_tokens": cfg.budget.expression_tokens,
            "budget.min_delivered_docs": cfg.budget.min_delivered_docs,
            "budget.classifier_cap_ref_chars": getattr(cfg.budget, "classifier_cap_ref_chars", None),
            "env.CYMATIX_SCORING_EPOCH": os.environ.get("CYMATIX_SCORING_EPOCH"),
            "env.CYMATIX_LEAN_INDEXES": os.environ.get("CYMATIX_LEAN_INDEXES"), "k": K}
        self.mgr = CymatixContextManager(cfg)
        self.ro = sqlite3.connect(f"file:{BED}?mode=ro", uri=True)
        self.api_key = api_key
        self.jev_last = [0.0]
        self.jev_path = run / "jev_cache.jsonl"
        self.jev_cache = {r["key"]: r["resp"] for r in read_jsonl(self.jev_path)}
        self.jev_calls = 0

    def close(self):
        self.mgr.close()
        self.ro.close()

    def doc(self, gid: str):
        r = self.ro.execute("SELECT source_id, content FROM genes WHERE gene_id=?", (gid,)).fetchone()
        return (r[0] or "", r[1] or "") if r else None

    def jev(self, key: str, state: dict, questions: dict) -> dict:
        if key not in self.jev_cache:
            resp = jev_call(self.api_key, state, questions, self.jev_last)
            self.jev_cache[key] = resp
            self.jev_calls += 1
            append_jsonl(self.jev_path, {"key": key, "resp": resp})
        return self.jev_cache[key]["answers"]

    def judge_chunk(self, nd: dict, gid: str, src: str, text: str) -> float:
        state = {"question": nd["question"], "question_type": nd["qtype"],
                 "chunk": {"source": src, "text": text}}
        a = self.jev(f"{nd['needle']}::{gid}::{nd['qtype']}", state, chunk_questions(nd["qtype"]))
        return a["answers"]["noul"]

    def search(self, nd: dict, query: str):
        t0 = time.perf_counter()
        w = self.mgr.build_context(query, read_only=True, ignore_delivered=True, max_genes=K)
        ids = list(w.expressed_gene_ids or ())
        t_ret = time.perf_counter() - t0
        t1 = time.perf_counter()
        docs = []
        for gid in ids:
            d = self.doc(gid)
            if d is None:
                continue
            src, text = d[0], d[1][:MAX_DOC_CHARS]
            docs.append({"id": gid, "source": src, "text": text,
                         "judge_p": self.judge_chunk(nd, gid, src, text)})
        shuffled = list(docs)   # no-incoming judge: packet order carries no cymatix rank
        random.Random(f"{nd['needle']}::{query}").shuffle(shuffled)
        pkey = f"{nd['needle']}::packet::{sha256_bytes(query.encode())[:16]}::{nd['qtype']}"
        packet_p = self.jev(pkey, {"question": nd["question"], "question_type": nd["qtype"],
                                   "packet": [{"source": d["source"], "text": d["text"]} for d in shuffled]},
                            packet_questions())["packet_answers"]["noul"] if docs else 0.0
        t_jev = time.perf_counter() - t1
        order = sorted(docs, key=lambda d: -d["judge_p"])
        full, rest = order[:FULL_DOCS], order[FULL_DOCS:]
        out = [f"SEARCH results for {query!r}: {len(docs)} documents, packet_p = {packet_p:.2f}"]
        for d in full:
            out.append(f'<document id="{d["id"]}" source="{Path(d["source"]).name}" '
                       f'judge_p="{d["judge_p"]:.2f}">\n{d["text"]}\n</document>')
        if rest:
            out.append("Also returned (READ <doc_id> for the full text):")
            out += [f"- {d['id']} judge_p={d['judge_p']:.2f} [{Path(d['source']).name}] "
                    f"{one_line(d['text'], 160)}" for d in rest]
        return "\n\n".join(out), {
            "delivered": ids, "shown_full": [d["id"] for d in full], "packet_p": packet_p,
            "judge_p": {d["id"]: d["judge_p"] for d in docs},
            "retrieval_s": round(t_ret, 1), "jev_s": round(t_jev, 1)}

    def grep(self, words: str, mode: str):
        terms = [t for t in re.findall(r"[A-Za-z0-9_\-\.]+", words.lower()) if len(t) >= 3]
        if not terms:
            return "(no usable terms; use words of 3+ characters)", {"hits": []}
        match = (" AND " if mode == "ALL" else " OR ").join(f'"{t}"' for t in terms)
        t0 = time.perf_counter()
        try:
            rows = self.ro.execute("SELECT gene_id FROM genes_fts WHERE genes_fts MATCH ? "
                                   "ORDER BY rank LIMIT ?", (match, GREP_LIMIT)).fetchall()
        except sqlite3.OperationalError as exc:
            return f"query error: {exc}", {"hits": []}
        hits = [r[0] for r in rows]
        out = [f"{mode} {' '.join(terms)!r}: {len(hits)} hits"]
        for i, gid in enumerate(hits, 1):
            src, text = self.doc(gid) or ("", "")
            out.append(f"{i}. {gid} [{Path(src).name}] {one_line(text, SNIPPET)}")
        return "\n".join(out) if hits else f"{mode} {' '.join(terms)!r}: no matches", {
            "hits": hits, "retrieval_s": round(time.perf_counter() - t0, 1)}

    def read(self, nd: dict, gid: str):
        gid = gid.strip().strip("`\"'<>")
        d = self.doc(gid)
        if d is None:
            return f"{gid}: no such document", {"read": None}
        src, text = d[0], d[1][:MAX_DOC_CHARS]
        t0 = time.perf_counter()
        p = self.judge_chunk(nd, gid, src, text)
        return (f'<document id="{gid}" source="{Path(src).name}" judge_p="{p:.2f}">\n{text}\n</document>',
                {"read": gid, "judge_p": p, "jev_s": round(time.perf_counter() - t0, 1)})


# ---------------------------------------------------------------- search loop

def run_needle(run: Path, tools: Tools, nd: dict, last_call: list) -> dict:
    gold = nd["gold"]
    obs, info = tools.search(nd, nd["question"])
    parity = info["delivered"] == nd["receipt_delivered"]
    turns = [{"turn": 0, "cmd": "SEARCH", "arg": nd["question"], "by": "harness", "obs_chars": len(obs),
              "gold_seen": sorted(gold & set(info["delivered"])),
              "gold_full": sorted(gold & set(info["shown_full"])), **info}]
    prompt = (f"Question: {nd['question']}\n\nThe first search used the question itself. "
              f"({MAX_COMMANDS} commands left)\n\n{obs}")
    session, final, n_cmd, n_bad = None, None, 0, 0
    while True:
        if n_cmd >= MAX_COMMANDS:
            prompt += ("\n\nNo commands left. Reply with your final command: ANSWER: <answer> "
                       "or NOT_FOUND.")
        m = claude_turn(run, SYSTEM, prompt, ANSWER_MODEL, last_call, session)
        session = session or m.get("session_id")
        cmd, arg = parse_command(m.get("text", ""))
        t = {"turn": len(turns), "by": "model", "cmd": cmd, "arg": arg, "reply": m.get("text"),
             **{k: m.get(k) for k in ("status", "error", "models", "input_tokens", "cache_read",
                                      "cache_write", "output_tokens", "cost_usd", "wall_s", "limit_wait_s")}}
        if m["status"] != "succeeded":
            turns.append(t)
            final = {"cmd": "ERROR", "text": m.get("error")}
            break
        if cmd in ("ANSWER", "NOT_FOUND"):
            turns.append(t)
            final = {"cmd": cmd, "text": arg}
            break
        if n_cmd >= MAX_COMMANDS:            # asked for a final answer, got a tool call
            turns.append(t)
            final = {"cmd": "BUDGET", "text": m.get("text")}
            break
        if cmd is None:                      # unparseable: a turn, but not a command
            n_bad += 1
            if n_bad >= 3:
                turns.append(t)
                final = {"cmd": "INVALID", "text": m.get("text")}
                break
        else:
            n_bad = 0
            n_cmd += 1
        if cmd == "SEARCH":
            obs, info = tools.search(nd, arg)
            seen, full = set(info["delivered"]), set(info["shown_full"])
        elif cmd in ("GREP", "ALL"):
            obs, info = tools.grep(arg, cmd)
            seen, full = set(info["hits"]), set()
        elif cmd == "READ":
            obs, info = tools.read(nd, arg)
            seen = full = {info["read"]} if info["read"] else set()
        else:
            obs, info, seen, full = ("Unrecognized reply. The last line must be one command: "
                                     "SEARCH:, GREP:, ALL:, READ:, ANSWER: or NOT_FOUND."), {}, set(), set()
        remaining = MAX_COMMANDS - n_cmd
        prompt = f"{obs}\n\n({remaining} command{'s' if remaining != 1 else ''} left)"
        t.update(obs_chars=len(obs), gold_seen=sorted(gold & seen), gold_full=sorted(gold & full), **info)
        turns.append(t)
        print(f"   t{t['turn']} {cmd} {arg[:70]!r} gold_seen={bool(t['gold_seen'])} "
              f"in={t['input_tokens']}+{t['cache_read']}c out={t['output_tokens']} {t['wall_s']}s", flush=True)

    model_turns = [x for x in turns if x["by"] == "model"]
    first_seen = next((x["turn"] for x in turns if x.get("gold_seen")), None)
    first_full = next((x["turn"] for x in turns if x.get("gold_full")), None)
    return {
        "needle": nd["needle"], "erb_type": nd["erb_type"], "question": nd["question"],
        "round0_parity": parity, "session_id": session, "final": final,
        "commands": n_cmd, "model_turns": len(model_turns),
        "command_mix": dict(Counter(x["cmd"] or "INVALID" for x in model_turns
                                    if x["cmd"] not in ("ANSWER", "NOT_FOUND"))),
        "gold_seen_turn": first_seen, "gold_full_turn": first_full,
        "tokens": {k: sum(x.get(k) or 0 for x in model_turns)
                   for k in ("input_tokens", "cache_read", "cache_write", "output_tokens")},
        "cost_usd": round(sum(x.get("cost_usd") or 0 for x in model_turns), 4),
        "wall_s": {"model": round(sum(x.get("wall_s") or 0 for x in model_turns), 1),
                   "retrieval": round(sum(x.get("retrieval_s") or 0 for x in turns), 1),
                   "jev": round(sum(x.get("jev_s") or 0 for x in turns), 1)},
        "turns": turns,
    }


def load_needles(limit: int) -> list:
    sample = json.loads((ONESHOT_DIR / "sample.json").read_text(encoding="utf-8"))
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    jr = json.loads(JEV_RECEIPT.read_text(encoding="utf-8"))
    delivered = {r["needle"]: r["delivered_ids"] for r in jr["needles"]}
    qtype = {r["needle"]: r["qtype"] for r in jr["needles"]}
    out = []
    for it in sample["items"]:
        if it["cohort"] != "crater":
            continue
        out.append({"needle": it["needle"], "erb_type": it["erb_type"], "question": it["question"],
                    "gold_answer": it["gold_answer"], "answer_facts": it["answer_facts"],
                    "gold": set(gold[it["needle"]]), "receipt_delivered": delivered[it["needle"]],
                    "qtype": qtype[it["needle"]]})
    return out[:limit] if limit else out


def stage_search(run: Path, limit: int) -> None:
    needles = load_needles(limit)
    out = run / "needles.jsonl"
    done = {r["needle"] for r in read_jsonl(out)}
    key_var = next((v for v in ("JEV_API_KEY", "TYPESAFE_API_KEY") if os.environ.get(v)), None)
    if key_var is None:
        raise SystemExit("ERROR: TYPESAFE_API_KEY not set")
    tools = Tools(run, os.environ[key_var])
    meta = run / "run_meta.json"
    if not meta.exists():
        meta.write_text(json.dumps({"cymatix_file": tools.cymatix_file, "loaded_config": tools.loaded_config,
                                    "api_key_var": key_var}, indent=1), encoding="utf-8")
    print(f"cymatix_context: {tools.cymatix_file}\nloaded config: {json.dumps(tools.loaded_config)}", flush=True)
    print(f"search: {len(done)} done, {len(needles) - len(done)} to go", flush=True)
    last_call = [0.0]
    try:
        for i, nd in enumerate(needles, 1):
            if nd["needle"] in done:
                continue
            t0 = time.perf_counter()
            print(f"[{i}/{len(needles)}] {nd['needle']} {nd['erb_type']}", flush=True)
            rec = run_needle(run, tools, nd, last_call)
            rec["needle_wall_s"] = round(time.perf_counter() - t0, 1)
            append_jsonl(out, rec)
            print(f"   -> {rec['final']['cmd']} parity={rec['round0_parity']} cmds={rec['commands']} "
                  f"gold_seen_turn={rec['gold_seen_turn']} cost=${rec['cost_usd']} "
                  f"jev_calls={tools.jev_calls} {(rec['final']['text'] or '')[:100]!r}", flush=True)
    finally:
        tools.close()


# ---------------------------------------------------------------- grade + report

def stage_grade(run: Path) -> None:
    sample = {it["needle"]: it for it in
              json.loads((ONESHOT_DIR / "sample.json").read_text(encoding="utf-8"))["items"]}
    out = run / "graded.jsonl"
    done = {r["needle"] for r in read_jsonl(out) if r["label"] != "grader_error"}
    last_call = [0.0]
    for rec in read_jsonl(run / "needles.jsonl"):
        if rec["needle"] in done:
            continue
        f, it = rec["final"], sample[rec["needle"]]
        row = {"needle": rec["needle"], "final_cmd": f["cmd"], "candidate": f.get("text")}
        if f["cmd"] == "NOT_FOUND":
            row.update(label="not_found", why="NOT_FOUND (local)", grader=None)
        elif f["cmd"] != "ANSWER":
            row.update(label="no_answer", why=f"loop ended with {f['cmd']}", grader=None)
        else:
            prompt = json.dumps({"question": it["question"], "gold_answer": it["gold_answer"],
                                 "answer_facts": it["answer_facts"], "candidate": f["text"] or "(no answer)"},
                                ensure_ascii=False, indent=1)
            g = claude_turn(run, GRADER_SYSTEM, prompt, GRADER_MODEL, last_call, persist=False)
            m = re.match(r"\s*LABEL:\s*(correct|partial|incorrect|not_found)\s*\|?\s*(.*)",
                         g.get("text") or "", re.I | re.S)
            row.update(label=m.group(1).lower() if m else "grader_error",
                       why=(m.group(2).strip() if m else (g.get("text") or g.get("error") or ""))[:300],
                       grader=g)
        append_jsonl(out, row)
        print(f"graded {rec['needle']}: {row['label']}", flush=True)


def median(xs: list):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def stage_report(run: Path) -> None:
    recs = {r["needle"]: r for r in read_jsonl(run / "needles.jsonl")}
    graded = {g["needle"]: g for g in read_jsonl(run / "graded.jsonl")}   # last label wins
    oneshot = {(r["needle"], r["arm"]): r["label"] for r in
               json.loads((ONESHOT_DIR / "receipt.json").read_text(encoding="utf-8"))["rows"]}
    receipt_delivered = {nd["needle"]: nd["receipt_delivered"] for nd in load_needles(0)}
    rows = []
    for n, r in sorted(recs.items()):
        lab = graded.get(n, {}).get("label", "ungraded")
        seen = r["gold_seen_turn"] is not None
        if lab in ("not_found", "no_answer"):
            outcome = "abstained_saw_gold" if seen else "abstained"
        elif seen:
            outcome = "recovered_correct" if lab == "correct" else "recovered_wrong"
        else:
            outcome = "lucky" if lab == "correct" else "hallucinated"
        gold = set()
        for t in r["turns"]:
            gold |= set(t.get("gold_seen") or [])
        gold_p = [round(p, 2) for t in r["turns"] if isinstance(t.get("judge_p"), dict)
                  for gid, p in t["judge_p"].items() if gid in gold]
        gold_p += [round(t["judge_p"], 2) for t in r["turns"]
                   if t.get("cmd") == "READ" and t.get("gold_seen") and "judge_p" in t]
        any_p = [p for t in r["turns"] for p in (t["judge_p"].values() if isinstance(t.get("judge_p"), dict)
                                                 else [t.get("judge_p")]) if isinstance(p, (int, float))]
        rows.append({**{k: r[k] for k in ("needle", "erb_type", "final", "commands", "model_turns",
                                          "command_mix", "gold_seen_turn", "gold_full_turn", "tokens",
                                          "cost_usd", "wall_s", "round0_parity")},
                     "label": lab, "why": graded.get(n, {}).get("why"), "outcome": outcome,
                     "oneshot_cymatix": oneshot.get((n, "cymatix")), "oneshot_jev": oneshot.get((n, "jev")),
                     "packet_p_trace": [round(t["packet_p"], 2) for t in r["turns"] if "packet_p" in t],
                     "gold_judge_p": gold_p,
                     "max_judge_p": round(max(any_p, default=0.0), 3),
                     "max_packet_p": round(max((t["packet_p"] for t in r["turns"] if "packet_p" in t), default=0.0), 3),
                     "round0_set_parity": set(r["turns"][0]["delivered"]) == set(receipt_delivered[n])})

    def in_tokens(r):
        return r["tokens"]["input_tokens"] + r["tokens"]["cache_read"] + r["tokens"]["cache_write"]

    def agg(rs):
        return {"n": len(rs), "labels": dict(Counter(r["label"] for r in rs)),
                "outcomes": dict(Counter(r["outcome"] for r in rs)),
                "gold_seen": sum(r["gold_seen_turn"] is not None for r in rs),
                "gold_shown_full": sum(r["gold_full_turn"] is not None for r in rs),
                "commands_median": median([r["commands"] for r in rs]),
                "commands_max": max((r["commands"] for r in rs), default=None),
                "budget_exhausted": sum(r["commands"] >= MAX_COMMANDS for r in rs),
                "command_mix": dict(sum((Counter(r["command_mix"]) for r in rs), Counter())),
                "tokens_total": {k: sum(r["tokens"][k] for r in rs)
                                 for k in ("input_tokens", "cache_read", "cache_write", "output_tokens")},
                "input_tokens_per_needle_median": median([in_tokens(r) for r in rs]),
                "output_tokens_per_needle_median": median([r["tokens"]["output_tokens"] for r in rs]),
                "cli_cost_usd": round(sum(r["cost_usd"] for r in rs), 2),
                "round0_parity": sum(bool(r["round0_parity"]) for r in rs),
                "round0_set_parity": sum(r["round0_set_parity"] for r in rs)}

    def auroc(pos, neg):
        if not pos or not neg:
            return None
        return round(sum((p > q) + 0.5 * (p == q) for p in pos for q in neg) / (len(pos) * len(neg)), 3)

    def answer_gate(key):
        # Jev as a post-hoc answer gate: the max score seen anywhere in the loop, correct vs wrong/partial.
        cor = [r[key] for r in rows if r["label"] == "correct"]
        bad = [r[key] for r in rows if r["label"] in ("incorrect", "partial")]
        return {"auroc_correct_vs_wrong": auroc(cor, bad), "n_correct": len(cor), "n_wrong": len(bad),
                "thresholds": {str(t): {"keep_correct": sum(v >= t for v in cor), "keep_wrong": sum(v >= t for v in bad)}
                               for t in (0.3, 0.5, 0.7, 0.9)}}

    summary = {"all": agg(rows), **{t: agg([r for r in rows if r["erb_type"] == t])
                                    for t in sorted({r["erb_type"] for r in rows})}}
    summary["jev_answer_gate"] = {"max_judge_p": answer_gate("max_judge_p"), "max_packet_p": answer_gate("max_packet_p")}
    summary["vs_oneshot_cymatix"] =dict(Counter(f"{r['oneshot_cymatix']}->{r['label']}" for r in rows))
    summary["by_label"] = {lab: {"n": len(rs), "commands_median": median([r["commands"] for r in rs]),
                                 "input_tokens_median": median([in_tokens(r) for r in rs])}
                           for lab in ("correct", "partial", "incorrect", "not_found", "no_answer")
                           for rs in [[r for r in rows if r["label"] == lab]]}
    meta = json.loads((run / "run_meta.json").read_text(encoding="utf-8"))
    jev_cache = read_jsonl(run / "jev_cache.jsonl")
    grader = [g["grader"] for g in graded.values() if g.get("grader")]
    receipt = {
        "experimental": True, "harness": "benchmarks/dogfood/jev/sonnet_crater_search.py",
        "commit": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "behind_master": git("rev-list", "--count", "HEAD..origin/master"),
        "bed": str(BED), "epoch": EPOCH, **meta, "pythonhashseed": os.environ.get("PYTHONHASHSEED"),
        "needles_from": str(ONESHOT_DIR / "sample.json"), "gold_file": str(GOLD.relative_to(REPO_ROOT)),
        "answer_model": ANSWER_MODEL, "grader_model": GRADER_MODEL, "effort": EFFORT,
        "claude_bin": CLAUDE_BIN[0], "claude_version": subprocess.run(
            [CLAUDE_BIN[0], "--version"], capture_output=True, text=True, creationflags=NO_WINDOW).stdout.strip(),
        "max_commands": MAX_COMMANDS, "k": K, "full_docs": FULL_DOCS,
        "jev_models": sorted({r["resp"].get("model") for r in jev_cache if r["resp"].get("model")}),
        "jev_calls": len(jev_cache),
        "jev_input_tokens": sum((r["resp"].get("usage") or {}).get("input_tokens", 0) for r in jev_cache),
        "grader_spend": {"calls": len(grader), "cli_cost_usd": round(sum(g.get("cost_usd") or 0 for g in grader), 2)},
        "prompts": {"system": SYSTEM, "grader_system": GRADER_SYSTEM},
        "summary": summary, "rows": rows,
        "transcripts": [recs[n] for n in sorted(recs)],
    }
    (run / "receipt.json").write_text(json.dumps(receipt, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=1))


def main() -> int:
    global ANSWER_MODEL
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--stage", choices=("search", "grade", "report", "all"), default="all")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--claude-bin", default="claude")
    ap.add_argument("--answer-model", default=ANSWER_MODEL, help="the searcher; the grader stays Opus 5.5")
    a = ap.parse_args()
    if os.environ.get("PYTHONHASHSEED") != "0":
        print("ERROR: run with PYTHONHASHSEED=0", file=sys.stderr)
        return 2
    CLAUDE_BIN[0] = a.claude_bin
    ANSWER_MODEL = a.answer_model
    run = Path(a.run_dir)
    run.mkdir(parents=True, exist_ok=True)
    lock = run / "run.lock"
    if lock.exists():
        other = int(lock.read_text().strip() or 0)
        if other and other != os.getpid() and pid_alive(other):
            print(f"ERROR: {lock} held by live PID {other}", file=sys.stderr)
            return 2
    lock.write_text(str(os.getpid()))
    try:
        stages = ("search", "grade", "report") if a.stage == "all" else (a.stage,)
        for st in stages:
            if st == "search":
                stage_search(run, a.limit)
            elif st == "grade":
                stage_grade(run)
            else:
                stage_report(run)
    finally:
        lock.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
