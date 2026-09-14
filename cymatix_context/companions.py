"""Question-driven, same-source supplements; independent of retrieval scores."""
from collections import Counter
import math
import re

from .schemas import Gene

_STOP = set("a an and are as at be been by can could did do does for from has have how if in into is it its of on or our should that the their these they this to was we were what when where which who why will with would you your".split())


def _terms(text: str) -> list[str]:
    text = re.sub(r"\\u([a-fA-F0-9]{4})", lambda m: chr(int(m[1], 16)), text)
    text = text.replace(r"\n", " ").replace(r'\"', '"')
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 1]


def rank_companions(query: str, seeds: list[Gene], documents: list[Gene], *, accept=None, max_added=None) -> list[Gene]:
    """Greedy BM25/rank/diversity ordering, never selecting a new source.

    Stored sequence positions replace the offline probe's source-file offsets.
    Missing positions tie at zero, then document ID, without filesystem reads.
    """
    seed_ids = {d.gene_id for d in seeds}
    source_rank, positions = {}, {}
    for rank, doc in enumerate(seeds, 1):
        if doc.source_id:
            source_rank.setdefault(doc.source_id, rank)
            positions.setdefault(doc.source_id, []).append(doc.promoter.sequence_index or 0)
    candidates = list({d.gene_id: d for d in documents
                       if d.gene_id not in seed_ids and d.source_id in source_rank}.values())
    counts = {d.gene_id: Counter(_terms(d.content)) for d in candidates}
    query_terms = set(_terms(query))
    df = Counter(t for c in counts.values() for t in query_terms if t in c)
    n = len(candidates)
    avg = sum(sum(c.values()) for c in counts.values()) / max(1, n)
    scores = {}
    for doc in candidates:
        tf = counts[doc.gene_id]
        score = sum(math.log(1 + (n - df[t] + .5) / (df[t] + .5)) * tf[t] * 2.2
                    / (tf[t] + 1.2 * (.25 + .75 * sum(tf.values()) / max(1, avg)))
                    for t in query_terms if tf[t])
        scores[doc.gene_id] = score / (1 + .25 * (source_rank[doc.source_id] - 1))
    added = Counter()
    ordered = []
    while candidates and (max_added is None or len(ordered) < max_added):
        chosen = min(candidates, key=lambda d: (
            -scores[d.gene_id] / (1 + added[d.source_id]), source_rank[d.source_id],
            min(abs((d.promoter.sequence_index or 0) - p) for p in positions[d.source_id]),
            d.gene_id,
        ))
        candidates.remove(chosen)
        if accept is not None and not accept(chosen):
            continue
        ordered.append(chosen)
        added[chosen.source_id] += 1
    return ordered
