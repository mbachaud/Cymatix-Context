"""#239 delivery-BALANCED bench — the production-refit bed the §3 bed couldn't be.

Three cells manufacture genuine label balance + discriminative features:
  - answerable  : ingest gold + neutral distractors           -> causal=1 (delivered & used)
  - heldout     : ingest ONLY distractors (gold NOT ingested) -> causal=0 (answer un-retrievable)
  - competition : ingest gold + a WRONG-answer sibling + dist -> causal=? (delivered-but-maybe-ignored;
                  only the faithfulness graph can label it — the case §3 lacked)

Answers are single-token in Qwen3-4B and unique across in-corpus needles so the
causal check ("is the gold answer token the answer-logit's top driver?") is
unambiguous. Held-out answers may reuse the pool (their gold is never ingested).
Neutral distractors share the entity anchor but never the relation word or any
answer word, so 'answer present in context' means the RIGHT fact was delivered.
Egress: synthetic only.
"""
import random

# bucket -> (relation phrase, question tail, doc label, component noun)
_BUCKETS = [
    ("animal",   "is nicknamed the", "is nicknamed the", "field guide",   "sentry"),
    ("material", "is built on",       "is built on",      "spec",          "store"),
    ("color",    "accent is",         "accent is",        "UI kit",        "console"),
    ("fruit",    "is codenamed",      "is codenamed",     "release notes", "engine"),
    ("element",  "is powered by",     "is powered by",    "power log",     "array"),
]

# 74 distinct entity Name words (none collide with answer words below)
_NAMES = [
    "Beacon","Cascade","Atlas","Sentinel","Warden","Lookout","Ranger","Picket","Herald","Aegis",
    "Vault","Relay","Forge","Nova","Delta","Echo","Bastion","Quarry","Anvil","Foundry",
    "Pillar","Mesh","Pulse","Grove","Ember","Fern","Lattice","Prairie","Meadow","Bramble",
    "Prism","Orbit","Halo","Comet","Ridge","Spire","Zenith","Vertex","Apex","Summit",
    "Zephyr","Onyx","Cinder","Flux","Gale","Torrent","Drift","Surge","Harbor","Vesper",
    "Quill","Tundra","Willow","Cedar","Basalt","Cliff","Dune","Fjord","Glade","Heath",
    "Juniper","Kestrel","Larch","Marsh","Quiver","Reef","Sable","Thorn","Umbra","Vale",
    "Wisp","Cove","Ledger","Vantage",
]

_ANSWERS = [
    "red","blue","green","teal","cyan","pink","violet","amber","azure","crimson","olive","coral","ivory","jade","ruby","beige",
    "quartz","granite","copper","marble","bronze","slate","iron","steel","brass","zinc","chrome","nickel","silver","tin","glass","stone","clay","titanium",
    "tiger","eagle","shark","wolf","lion","bear","hawk","fox","owl","crane","robin","dove","seal","whale","deer",
    "mango","peach","cherry","lemon","grape","plum","lime","apple","berry","fig","date","pear",
    "neon","helium","carbon","oxygen","sodium","lithium",
]

_ATTRS = [
    lambda r: f"listens on port {r.randint(8000, 8999)}",
    lambda r: f"was deployed in {r.randint(2016, 2024)}",
    lambda r: f"is written in {r.choice(['Rust','Go','Python','Java','Scala','Kotlin','Elixir','Zig'])}",
    lambda r: f"reports to the {r.choice(['ops','platform','data','infra','security','sre'])} team",
    lambda r: f"runs in region {r.choice(['us-east','us-west','eu-central','ap-south','sa-east'])}",
    lambda r: f"has a {r.randint(7, 90)}-day retention window",
    lambda r: f"exposes {r.randint(3, 40)} metric endpoints",
    lambda r: f"was last audited in {r.choice(['January','March','May','July','September','November'])}",
    lambda r: f"uses {r.randint(2, 64)} worker threads",
    lambda r: f"is tagged {r.choice(['alpha','beta','ga','lts','edge'])} in the registry",
]

# cell composition (counts). Graph budget: competition graphed in full; answerable
# + heldout validated on stratified samples then imputed.
N_ANSWERABLE = 30
N_HELDOUT = 30
N_COMPETITION = 12


def _norm(s):
    while "  " in s:
        s = s.replace("  ", " ")
    return s.strip()


def build_balanced(seed=239):
    rng = random.Random(seed)
    total = N_ANSWERABLE + N_HELDOUT + N_COMPETITION
    assert total <= len(_NAMES), f"need {total} names, have {len(_NAMES)}"
    # unique answers for in-corpus needles (answerable golds + competition gold+wrong)
    need_unique = N_ANSWERABLE + 2 * N_COMPETITION
    assert need_unique <= len(_ANSWERS), f"need {need_unique} unique answers, have {len(_ANSWERS)}"
    ans_pool = list(_ANSWERS)
    rng.shuffle(ans_pool)
    heldout_pool = list(_ANSWERS)  # reuse allowed (never ingested)
    rng.shuffle(heldout_pool)

    needles = []
    ai = 0        # answerable/competition unique-answer cursor
    for i in range(total):
        name = _NAMES[i]
        bucket, rel, qtail, doc, comp = _BUCKETS[i % len(_BUCKETS)]
        entity = f"{name} {comp}"
        prefix = f"Redwood Inference {doc}:"
        q = _norm(f"At Redwood Inference, the {entity} {qtail}")
        k = i % 5  # graded 0..4 neutral distractors (keeps golds deliverable)
        drng = random.Random(1000 + i)
        attrs = drng.sample(_ATTRS, min(k, len(_ATTRS)))
        while len(attrs) < k:
            attrs.append(drng.choice(_ATTRS))
        distractors = [_norm(f"{prefix} the {entity} {fn(drng)}.") for fn in attrs]

        if i < N_ANSWERABLE:
            cell = "answerable"
            ans = ans_pool[ai]; ai += 1
            gold = _norm(f"{prefix} the {entity} {rel} {ans}.")
            ingest = [gold] + distractors
            comp_ans = None
        elif i < N_ANSWERABLE + N_HELDOUT:
            cell = "heldout"
            ans = heldout_pool[i % len(heldout_pool)]
            gold = _norm(f"{prefix} the {entity} {rel} {ans}.")  # defined but NOT ingested
            ingest = list(distractors)                            # distractors only
            comp_ans = None
        else:
            cell = "competition"
            ans = ans_pool[ai]; ai += 1
            comp_ans = ans_pool[ai]; ai += 1
            gold = _norm(f"{prefix} the {entity} {rel} {ans}.")
            competitor = _norm(f"{prefix} the {entity} {rel} {comp_ans}.")
            ingest = [gold, competitor] + distractors

        needles.append({
            "id": f"{cell[:4]}_{name.lower()}", "cell": cell, "bucket": bucket,
            "entity": entity, "q": q, "ans": ans, "competitor_ans": comp_ans,
            "gold": gold, "ingest": ingest, "k_distractors": k,
        })
    return needles


NEEDLES_239B = build_balanced()

if __name__ == "__main__":
    ns = build_balanced()
    from collections import Counter
    print(f"{len(ns)} needles;", dict(Counter(n["cell"] for n in ns)))
    print("total docs ingested:", sum(len(n["ingest"]) for n in ns))
    seen = {}
    for n in ns:
        if n["cell"] in ("answerable", "competition"):
            assert n["ans"] not in seen, f"dup answer {n['ans']}"
            seen[n["ans"]] = 1
            if n["competitor_ans"]:
                assert n["competitor_ans"] not in seen, f"dup comp {n['competitor_ans']}"
                seen[n["competitor_ans"]] = 1
    print("in-corpus answer uniqueness OK; distinct in-corpus answers:", len(seen))
    for cell in ("answerable", "heldout", "competition"):
        ex = next(n for n in ns if n["cell"] == cell)
        print(f"\n[{cell}] {ex['id']}  K={ex['k_distractors']}  ans={ex['ans']}"
              + (f" vs comp={ex['competitor_ans']}" if ex['competitor_ans'] else ""))
        print("  Q:", ex["q"])
        for d in ex["ingest"][:3]:
            print("  ingest:", d)
