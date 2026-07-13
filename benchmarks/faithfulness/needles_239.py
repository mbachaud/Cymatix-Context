"""#239 needle set — 48 Redwood-Inference facts with GRADED same-entity
distractors, so retrieval quality (and thus gold survival into expressed_context)
varies across families. Shared by the helix env (stage-1 features) and the graph
env (stage-2 faithfulness). Answers are all single-token in Qwen3-4B (tok_probe)
and unique across the set, so 'answer word present in context' unambiguously
means the RIGHT gold fact was delivered. Egress: synthetic only.

Each family:
  id, doc, ans, q            — gold question + single-token answer
  gold                       — the one fact that answers q
  distractors                — K same-entity facts (different attribute); share
                               the entity lexical anchor but NOT the relation
                               word, so they compress the score gap / occupy
                               splice budget without answering the question.
K is graded 0..16 across families (decoupled from bucket via i*7 % 17).
"""
import random

# (bucket relation phrase, question tail, doc label)
_BUCKETS = {
    "animal":   ("is nicknamed the", "is nicknamed the", "field guide"),
    "material": ("is built on",       "is built on",      "spec"),
    "color":    ("accent is",         "accent is",        "UI kit"),
    "fruit":    ("is codenamed",      "is codenamed",     "release notes"),
    "element":  ("is powered by",     "is powered by",    "power log"),
}

# entity phrase -> (id, bucket, single-token answer). Answers unique across set.
_FAMILIES = [
    # animals (nicknamed)
    ("Beacon monitoring bot",   "beacon",   "animal",   "tiger"),
    ("Cascade ingestion daemon","cascade",  "animal",   "eagle"),
    ("Atlas watchdog",          "atlas",    "animal",   "shark"),
    ("Sentinel scanner",        "sentinel", "animal",   "wolf"),
    ("Warden audit probe",      "warden",   "animal",   "lion"),
    ("Lookout health checker",  "lookout",  "animal",   "bear"),
    ("Ranger trace collector",  "ranger",   "animal",   "hawk"),
    ("Picket alert relay",      "picket",   "animal",   "fox"),
    ("Herald event notifier",   "herald",   "animal",   "owl"),
    ("Aegis firewall guard",    "aegis",    "animal",   "crane"),
    # materials (built on)
    ("Vault storage tier",      "vault",    "material", "quartz"),
    ("Relay index shard",       "relay",    "material", "granite"),
    ("Forge cache layer",       "forge",    "material", "copper"),
    ("Nova routing plane",      "nova",     "material", "marble"),
    ("Delta ledger store",      "delta",    "material", "bronze"),
    ("Echo queue buffer",       "echo",     "material", "slate"),
    ("Bastion backup vault",    "bastion",  "material", "iron"),
    ("Quarry blob pool",        "quarry",   "material", "steel"),
    ("Anvil compaction job",    "anvil",    "material", "brass"),
    ("Foundry build cache",     "foundry",  "material", "zinc"),
    # colors (accent is)
    ("Pillar dashboard",        "pillar",   "color",    "red"),
    ("Mesh dashboard",          "mesh",     "color",    "blue"),
    ("Pulse dashboard",         "pulse",    "color",    "green"),
    ("Grove dashboard",         "grove",    "color",    "teal"),
    ("Ember dashboard",         "ember",    "color",    "cyan"),
    ("Fern dashboard",          "fern",     "color",    "pink"),
    ("Lattice dashboard",       "lattice",  "color",    "violet"),
    ("Prairie dashboard",       "prairie",  "color",    "amber"),
    ("Meadow dashboard",        "meadow",   "color",    "azure"),
    ("Bramble dashboard",       "bramble",  "color",    "crimson"),
    # fruits (codenamed)
    ("Prism analytics engine",  "prism",    "fruit",    "mango"),
    ("Orbit query engine",      "orbit",    "fruit",    "peach"),
    ("Halo search engine",      "halo",     "fruit",    "cherry"),
    ("Comet stream engine",     "comet",    "fruit",    "lemon"),
    ("Ridge batch engine",      "ridge",    "fruit",    "grape"),
    ("Spire graph engine",      "spire",    "fruit",    "plum"),
    ("Zenith ranking engine",   "zenith",   "fruit",    "lime"),
    ("Vertex join engine",      "vertex",   "fruit",    "apple"),
    ("Apex filter engine",      "apex",     "fruit",    "berry"),
    ("Summit merge engine",     "summit",   "fruit",    "fig"),
    # elements (powered by)
    ("Zephyr compute array",    "zephyr",   "element",  "neon"),
    ("Onyx render array",       "onyx",     "element",  "helium"),
    ("Cinder GPU array",        "cinder",   "element",  "carbon"),
    ("Flux vector array",       "flux",     "element",  "oxygen"),
    ("Gale shuffle array",      "gale",     "element",  "sodium"),
    ("Torrent scatter array",   "torrent",  "element",  "lithium"),
    ("Drift gather array",      "drift",    "element",  "silver"),
    ("Surge reduce array",      "surge",    "element",  "titanium"),
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


def _norm(s: str) -> str:
    while "  " in s:
        s = s.replace("  ", " ")
    return s.strip()


def build_needles():
    needles = []
    for i, (entity, nid, bucket, ans) in enumerate(_FAMILIES):
        rel, qtail, doc = _BUCKETS[bucket]
        prefix = f"Redwood Inference {doc}:"
        gold = _norm(f"{prefix} the {entity} {rel} {ans}.")
        q = _norm(f"At Redwood Inference, the {entity} {qtail}")
        k = (i * 7) % 17  # graded 0..16, decoupled from bucket
        rng = random.Random(1000 + i)
        attrs = rng.sample(_ATTRS, min(k, len(_ATTRS)))
        # if k > len(_ATTRS), wrap with fresh random values for repeats
        while len(attrs) < k:
            attrs.append(rng.choice(_ATTRS))
        distractors = [_norm(f"{prefix} the {entity} {fn(rng)}.") for fn in attrs]
        needles.append({
            "id": nid, "bucket": bucket, "doc": doc, "entity": entity,
            "ans": ans, "q": q, "gold": gold, "distractors": distractors,
            "k_distractors": k,
        })
    return needles


NEEDLES_239 = build_needles()

if __name__ == "__main__":
    ns = build_needles()
    print(f"{len(ns)} families; K range {min(n['k_distractors'] for n in ns)}"
          f"..{max(n['k_distractors'] for n in ns)}; "
          f"total distractors {sum(n['k_distractors'] for n in ns)}")
    for n in ns[:3]:
        print(f"\n[{n['id']}] K={n['k_distractors']}  ans={n['ans']}")
        print("  Q :", n["q"])
        print("  G :", n["gold"])
        for d in n["distractors"][:2]:
            print("  D :", d)
