"""Write the grain-ladder arm configs from the shipped cymatix.toml.

Each arm = shipped defaults (v0.11.3, neural-free retrieval) with only the
[budget] delivery knobs changed: no seat floor (variable delivery, caps
only), an ~18k-char packet budget, and a seat cap scaled to the grain size
so the cap is never what starves a small-grain arm.

Usage: python -P benchmarks/dogfood/grain_ladder/make_arm_configs.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / "configs"

PACKET_TOKENS = 4500          # ~18,000 chars at 4 chars/token
BASE_CAP, BASE_GRAIN = 12, 4000
GRAINS = (4000, 1824, 580)    # 4000 control, 18000/pi^2, 18000/pi^3
TIGHT_CHARS, FOCUSED_CHARS = 4500, 9000


def cap_for(grain: int) -> int:
    return round(BASE_CAP * BASE_GRAIN / grain)


def set_key(text: str, section: str, key: str, value: int, add: bool = False) -> str:
    """Replace ``key = ...`` inside ``[section]``; fail loudly if absent
    unless ``add`` (then insert it right under the section header)."""
    m = re.search(rf"^\[{re.escape(section)}\]\s*$", text, re.M)
    if not m:
        raise SystemExit(f"[{section}] not found")
    nxt = re.search(r"^\[", text[m.end():], re.M)
    end = m.end() + (nxt.start() if nxt else len(text) - m.end())
    body = text[m.end():end]
    body2, n = re.subn(rf"^{key}\s*=\s*[^#\n]*", f"{key} = {value} ", body, count=1, flags=re.M)
    if n != 1 and add:
        body2 = f"\n{key} = {value}" + body
    elif n != 1:
        raise SystemExit(f"{section}.{key} not found")
    return text[:m.end()] + body2 + text[end:]


def main() -> None:
    shipped = (ROOT / "cymatix.toml").read_text(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    for g in GRAINS:
        t = shipped
        t = set_key(t, "budget", "expression_tokens", PACKET_TOKENS)
        t = set_key(t, "budget", "min_delivered_docs", 0)
        t = set_key(t, "budget", "max_genes_per_turn", cap_for(g))
        p = OUT / f"grain_c{g}.toml"
        p.write_text(t, encoding="utf-8")
        print(p.name, "cap", cap_for(g))
        # charcap variant: TIGHT / FOCUSED cuts in chars, sized to what the
        # legacy 3 / 6 seats ship on the c4000 control (4.4-4.6k / 10-10.6k).
        tc = set_key(t, "budget", "tier_tight_chars", TIGHT_CHARS, add=True)
        tc = set_key(tc, "budget", "tier_focused_chars", FOCUSED_CHARS, add=True)
        p = OUT / f"grain_c{g}_charcap.toml"
        p.write_text(tc, encoding="utf-8")
        print(p.name, "tight/focused chars", TIGHT_CHARS, FOCUSED_CHARS)


if __name__ == "__main__":
    main()
