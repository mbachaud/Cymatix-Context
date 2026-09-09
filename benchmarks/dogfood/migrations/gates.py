"""Pure arm construction, wire normalization and paired migration verdicts."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from pathlib import Path


_BASE = {
    "budget.wire_format": "legacy",
    "budget.tier_seat_floor_enabled": False,
    "retrieval.harmonic_batching_enabled": False,
}
ARMS = {
    "baseline": dict(_BASE),
    "wire": {**_BASE, "budget.wire_format": "canonical"},
    "tier": {**_BASE, "budget.tier_seat_floor_enabled": True},
    "harmonic_on": {**_BASE, "retrieval.harmonic_batching_enabled": True},
    "harmonic_staged": {**_BASE, "retrieval.harmonic_batching_enabled": True},
}
PAIRS = {"wire": ("baseline", "wire"), "tier": ("baseline", "tier"),
         "harmonic": ("harmonic_on", "harmonic_staged")}
PIN_FIELDS = ("code_sha", "bed_identity", "resolved_sha256", "gold_sha256",
              "ingest_c", "tagger_version", "k", "base_config_sha256", "needle_count",
              "needle_order_sha256", "source_state", "ccr_state", "toin_sha256",
              "headroom_config_sha256", "common_config_sha256", "runtime_environment",
              "python", "sqlite")


def digest(value):
    data = value if isinstance(value, str) else json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def arm_config(source, flags):
    """Preserve the entire shipped TOML, changing exactly the explicit pins."""
    lines = source.splitlines(keepends=True)
    section = ""
    found = set()
    for index, line in enumerate(lines):
        heading = re.match(r"^\[([^\]]+)\]\s*(?:#.*)?$", line.strip())
        if heading:
            section = heading[1]
        field = re.match(r"^(\s*)(\w+)\s*=", line)
        if not field:
            continue
        path = section + "." + field[2]
        if path in flags:
            if path in found:
                raise ValueError(f"Duplicate config pin: {path}")
            found.add(path)
            lines[index] = f"{field[1]}{field[2]} = {json.dumps(flags[path])}\n"
    if found != set(flags):
        raise ValueError(f"Missing explicit config pins: {sorted(set(flags) - found)}")
    return "".join(lines)


def normalized_context(text, delivered_ids, mode):
    """Normalize owned wrappers only; ambiguous/truncated parts fail visibly.

    Body bytes are never rewritten, including escaped security delimiters.
    This is deliberately narrower than a regex replacement across the prompt.
    """
    if not delivered_ids:
        return {"empty_context": text}
    prefix, suffix = "<expressed_context>\n", "\n</expressed_context>"
    if not (text.startswith(prefix) and text.endswith(suffix)):
        raise ValueError("Missing outer context wrapper")
    inner = text[len(prefix):-len(suffix)]
    matches = list(re.finditer(
        r"(?m)^\[(gene|document)=([^\s]+) (.+?) (\d+)(?:→(\d+))?c\]\n", inner,
    ))
    if [m[2] for m in matches] != [gid[:12] for gid in delivered_ids]:
        raise ValueError("Missing, ambiguous, or reordered document headers")
    expected_label = "document" if mode == "canonical" else "gene"
    expected_tag = "DOCUMENT" if mode == "canonical" else "GENE"
    normalized = []
    for index, match in enumerate(matches):
        if match[1] != expected_label or (index == 0 and match.start() != 0):
            raise ValueError("Unexpected document header")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(inner)
        part = inner[match.end():end]
        if index + 1 < len(matches):
            if not part.endswith("\n---\n"):
                raise ValueError("Unexpected document separator")
            part = part[:-5]
        opening, newline, rest = part.partition("\n")
        closing = f"\n</{expected_tag}>"
        if (not newline or not opening.startswith(f"<{expected_tag}")
                or not opening.endswith(">") or not rest.endswith(closing)):
            raise ValueError("Missing or truncated document wrapper")
        attrs = opening[len(expected_tag) + 1:-1]
        normalized_attrs = html.unescape(attrs) if mode == "canonical" else attrs
        # Unescaped embedded quotes cannot be disambiguated in legacy attrs.
        if normalized_attrs and not re.fullmatch(r'(?: (?:src|facts)="[^"\n]*")+', normalized_attrs):
            raise ValueError("Ambiguous wrapper attributes")
        wrapper_delta = 0
        if mode == "canonical":
            wrapper_delta = 8 + len(attrs) - len(normalized_attrs)
        raw = int(match[4])
        compressed = int(match[5] or match[4]) - wrapper_delta
        normalized.append({"id": delivered_ids[index], "header": match[3],
                           "raw_chars": raw, "compressed_chars": compressed,
                           "attributes": normalized_attrs, "body": rest[:-len(closing)]})
    return normalized


def compare(off, on, gate):
    """Fail closed on incomplete pairing, drift, delivery loss or inert probes."""
    failures = []
    for receipt, expected_arm in zip((off, on), PAIRS[gate], strict=True):
        if (receipt.get("arm") != expected_arm or receipt.get("flags") != ARMS[expected_arm]
                or receipt.get("harmonic_bind_cap") != (1 if expected_arm == "harmonic_staged" else None)):
            failures.append(f"wrong treatment: expected {expected_arm}")
    for field in PIN_FIELDS:
        if (field not in off.get("pins", {}) or field not in on.get("pins", {})
                or off["pins"][field] != on["pins"][field]):
            failures.append(f"pin mismatch/missing: {field}")
    if off.get("errors") or on.get("errors"):
        failures.append("arm reported errors")
    left_rows, right_rows = off.get("per_query", []), on.get("per_query", [])
    for receipt, rows in ((off, left_rows), (on, right_rows)):
        if (len(rows) != receipt.get("pins", {}).get("needle_count")
                or digest([row.get("needle") for row in rows]) != receipt.get("pins", {}).get("needle_order_sha256")):
            failures.append("incomplete ordered needle bank")
        if any(not all(field in row for field in ("delivered_ids", "delivered_count", "delivered_gold")) for row in rows):
            failures.append("missing required delivery fields")
    left = {row["needle"]: row for row in left_rows}
    right = {row["needle"]: row for row in right_rows}
    if (not left or len(left) != len(left_rows) or len(right) != len(right_rows)
            or list(left) != list(right)):
        failures.append("missing, duplicate, or reordered needles")
    fields = ["gold_ranks", "rank_of_first_gold", "final_rank_of_first_gold",
              "score_map_sha256", "ranked_ids_sha256", "budget_tier"]
    if gate in ("wire", "harmonic"):
        fields += ["delivered_ids", "delivered_gold"]
    if gate == "wire":
        fields += ["normalized_context_sha256", "normalized_decoder_sha256"]
    if gate == "harmonic":
        fields += ["context_sha256", "decoder_sha256"]
    gains, losses, raw_changes, decoder_changes = [], [], [], []
    for name in left.keys() & right.keys():
        a, b = left[name], right[name]
        if a.get("error") or b.get("error"):
            failures.append(f"{name}: query failed")
        if gate == "wire" and (a.get("normalization_error") or b.get("normalization_error")):
            failures.append(f"{name}: normalization unsupported")
        for field in fields:
            if field not in a or field not in b or a[field] != b[field]:
                failures.append(f"{name}: {field} differs/missing")
        if a.get("delivered_gold") and not b.get("delivered_gold"):
            losses.append(name)
        if b.get("delivered_gold") and not a.get("delivered_gold"):
            gains.append(name)
        if a.get("context_sha256") != b.get("context_sha256"):
            raw_changes.append(name)
        if a.get("decoder_sha256") != b.get("decoder_sha256"):
            decoder_changes.append(name)
    if losses:
        failures.append("paired delivered-gold losses")
    if gate == "harmonic":
        if not off.get("harmonic_rows") or not on.get("harmonic_rows"):
            failures.append("harmonic_links empty or missing")
        if any(row.get("harmonic_overflow_calls", 0) for row in left_rows):
            failures.append("reference overflowed natural harmonic bind limit")
        if not any(row.get("harmonic_batched_calls", 0) for row in right_rows):
            failures.append("staged harmonic path never exercised")
        if not any(row.get("harmonic_contributions", 0) for row in right_rows):
            failures.append("harmonic tier never contributed")
    return {"gate": gate, "passed": not failures, "failures": sorted(failures),
            "needle_count": len(left), "gained": sorted(gains), "lost": sorted(losses),
            "raw_context_changes": sorted(raw_changes),
            "raw_decoder_changes": sorted(decoder_changes),
            "latency_claim": "none; capture instrumentation and snapshot caches affect timing"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--off", required=True)
    parser.add_argument("--on", required=True)
    parser.add_argument("--gate", choices=PAIRS, required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = compare(json.loads(Path(args.off).read_text(encoding="utf-8")),
                     json.loads(Path(args.on).read_text(encoding="utf-8")), args.gate)
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
