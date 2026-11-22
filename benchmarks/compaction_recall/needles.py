"""Needle extraction, query masking and the scoring predicates.

A needle is a ``(cls, key)`` pair: a concrete detail that occurs in the
pre-compaction history and that the agent itself wrote again after the
compaction (assistant text or a tool call input). Re-use is the evidence that
the detail mattered; it says nothing about where the agent got it from, which
is what the arms measure.

Classes and their canonical keys:

* ``sha``    — 7-40 hex chars with a digit and a letter; key = first 7, lowercase
* ``path``   — a slash path ending in ``name.ext``; key = last two segments, ``/``
* ``ref``    — ``#NNN`` issue/PR reference
* ``number`` — decimals with 3+ digits, ``947,000``, ``829k``
* ``ident``  — snake_case / UPPER_SNAKE with 3+ parts, CamelCase with 3+ humps

The query for a needle is the post-compaction text around its first re-use
with every variant of the needle masked: a proxy for "what the agent was doing
when it needed the detail", not an agent-authored search query.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache

from .transcript import Event

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_PATH = re.compile(r"(?<![\w.\-~/])(?:[A-Za-z]:)?/?(?:[\w.\-~]+/)+[\w.\-~]+\.[A-Za-z][A-Za-z0-9]{0,7}(?![\w/])")
_SHA = re.compile(r"(?<![\w#\-])[0-9a-f]{7,40}(?![\w\-])")
_REF = re.compile(r"(?<![\w&#/])#\d{2,5}(?!\w)")
_NUMBERS = [
    re.compile(r"(?<![\w.:/])\d+\.\d+(?![\w.])"),
    re.compile(r"(?<![\w.,])\d{1,3}(?:,\d{3})+(?!\d|,\d)"),
    re.compile(r"(?<![\w.])\d+(?:\.\d+)?[kKM](?!\w)"),
]
_IDENTS = [
    re.compile(r"(?<!\w)[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}(?!\w)"),
    re.compile(r"(?<!\w)[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){2,}(?!\w)"),
    re.compile(r"(?<!\w)[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+){2,}(?!\w)"),
]

QUALIFIER = re.compile(
    r"\b(unverified|not verified|untested|not tested|pending|todo|still owed|owed|"
    r"blocked|superseded|refuted|reverted|flaky|failing|fails|failed|not yet|wip|"
    r"caveat|unproven|incomplete|partial|deferred|no evidence|not measured|unmeasured|"
    r"in flight|in-flight|unconfirmed|open question|tbd|draft|uncommitted|unpushed|"
    r"not merged|killed|invalid|broken)\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.;!?])\s+|\n+")

MAX_NUMBER_PRE_UNITS = 10  # a number in more units than this is boilerplate
QUERY_SIDE_CHARS = 600
HEDGE_WINDOW = 160


def _norm(text: str) -> str:
    return text.replace("\\", "/")


def _path_key(token: str) -> str | None:
    segs = [s for s in token.split("/") if s]
    if len(segs) < 2:
        return None
    key = "/".join(segs[-2:])
    return key if len(key) >= 8 else None


def find_keys(text: str) -> list[tuple[str, str]]:
    """Every needle-shaped ``(cls, key)`` in ``text``, first-seen order, deduped."""
    text = _UUID.sub(lambda m: " " * len(m.group()), text)
    norm = _norm(text)
    out: dict[tuple[str, str], None] = {}
    taken: list[tuple[int, int]] = []

    def free(m) -> bool:
        return all(m.end() <= a or m.start() >= b for a, b in taken)

    for m in _PATH.finditer(norm):
        key = _path_key(m.group())
        if key:
            out[("path", key.lower())] = None
            taken.append(m.span())
    for m in _SHA.finditer(text):
        tok = m.group()
        if free(m) and re.search(r"\d", tok) and re.search(r"[a-f]", tok):
            out[("sha", tok[:7])] = None
            taken.append(m.span())
    for m in _REF.finditer(text):
        if free(m):
            out[("ref", m.group())] = None
    for rx in _NUMBERS:
        for m in rx.finditer(text):
            tok = m.group()
            if free(m) and _significant_digits(tok) >= (2 if tok[-1] in "kKM" else 3):
                out[("number", tok)] = None
                taken.append(m.span())
    for rx in _IDENTS:
        for m in rx.finditer(text):
            if free(m) and m.group() not in TOOL_VOCABULARY:
                out[("ident", m.group())] = None
    return list(out)


# Harness parameter names: re-used because the tool schema is in context every
# turn, not because anything was remembered.
TOOL_VOCABULARY = frozenset({"run_in_background", "dangerously_disable_sandbox"})


def _significant_digits(tok: str) -> int:
    """0.80 -> 1, 0.05 -> 1, 0.664 -> 3, 11.3 -> 3, 947,000 -> 6."""
    digits = tok.rstrip("kKM").replace(",", "")
    if "." in digits:
        digits = digits.rstrip("0")
    return len(digits.replace(".", "").lstrip("0"))


@lru_cache(maxsize=65536)
def _pattern(cls: str, key: str) -> re.Pattern:
    esc = re.escape(key)
    if cls == "sha":
        return re.compile(r"(?<![0-9a-zA-Z])" + esc + r"[0-9a-fA-F]{0,33}(?![0-9a-zA-Z])",
                          re.IGNORECASE)
    if cls == "path":
        body = esc.replace("/", r"[/\\]")
        return re.compile(r"(?<![\w.\-~])(?:[\w.\-~:]*[/\\])*" + body + r"(?![\w])",
                          re.IGNORECASE)
    if cls == "ref":
        return re.compile(r"(?<![\w&#/])" + esc + r"(?!\d)")
    if cls == "number":
        return re.compile(r"(?<![\w.])" + esc + r"(?!\d)")
    return re.compile(r"(?<!\w)" + esc + r"(?!\w)")


def contains(text: str, cls: str, key: str) -> bool:
    return _pattern(cls, key).search(text) is not None


def mask(text: str, cls: str, key: str) -> str:
    return _pattern(cls, key).sub("[MASK]", text)


def hedge_near(text: str, cls: str, key: str, window: int = HEDGE_WINDOW) -> bool:
    """True when a qualifier word sits within ``window`` chars of the needle."""
    for m in _pattern(cls, key).finditer(text):
        if QUALIFIER.search(text[max(0, m.start() - window): m.end() + window]):
            return True
    return False


@dataclass
class Needle:
    cls: str
    key: str
    pre_units: int          # pre-compaction units mentioning it
    use_index: int          # index into Event.post of the first agent re-use
    use_role: str           # assistant | tool_use
    seen_post_before_use: str | None  # role of a post unit that showed it first
    qualifier: str | None   # qualifier word on its last pre-compaction mention
    query: str


def _qualifier(ev: Event, cls: str, key: str) -> str | None:
    for u in reversed(ev.pre):
        if u.role != "assistant" or not contains(u.text, cls, key):
            continue
        sentences = [s for s in _SENTENCE.split(u.text) if contains(s, cls, key)]
        m = QUALIFIER.search(sentences[-1]) if sentences else None
        return " ".join(m.group(1).lower().split()) if m else None
    return None


def _query(ev: Event, j: int, cls: str, key: str) -> str:
    use = ev.post[j].text
    m = _pattern(cls, key).search(use)
    end = use.find("\n", m.end()) if m else -1
    use_ctx = use[: end if end != -1 else len(use)][-QUERY_SIDE_CHARS:]
    if j > 0:
        prev = ev.post[j - 1].text
    elif ev.preserved:
        prev = ev.preserved[-1].text
    else:
        prev = ev.summary
    return mask(prev[-QUERY_SIDE_CHARS:] + "\n" + use_ctx, cls, key)


def extract_needles(ev: Event) -> list[Needle]:
    pre_units: dict[tuple[str, str], int] = defaultdict(int)
    for u in ev.pre:
        for ck in find_keys(u.text):
            pre_units[ck] += 1

    first_seen: dict[tuple[str, str], str] = {}
    used: set[tuple[str, str]] = set()
    out: list[Needle] = []
    for j, u in enumerate(ev.post):
        keys = find_keys(u.text)
        if u.role not in ("assistant", "tool_use"):
            for ck in keys:
                first_seen.setdefault(ck, u.role)
            continue
        for ck in keys:
            if ck in used or ck not in pre_units:
                continue
            cls, key = ck
            if cls == "number" and pre_units[ck] > MAX_NUMBER_PRE_UNITS:
                continue
            used.add(ck)
            out.append(Needle(cls=cls, key=key, pre_units=pre_units[ck], use_index=j,
                              use_role=u.role, seen_post_before_use=first_seen.get(ck),
                              qualifier=_qualifier(ev, cls, key),
                              query=_query(ev, j, cls, key)))
    return out
