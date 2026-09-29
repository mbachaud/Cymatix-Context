"""packet_token_cost: tokenizer counts over saved packet captures."""
import json
import sys
from pathlib import Path

import pytest

tiktoken = pytest.importorskip("tiktoken")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "dogfood" / "erb"))
import packet_token_cost  # noqa: E402


def _capture(directory, name, context, delivered, added=None):
    row = {"context": context, "decoder": "Answer using ONLY the context.", "delivered_ids": delivered}
    if added is not None:
        row["added_ids"] = added
    (directory / name).write_text(json.dumps(row), encoding="utf-8")


def test_counts_tokens_and_companions_per_arm(tmp_path):
    base, plus = tmp_path / "base", tmp_path / "plus"
    base.mkdir()
    plus.mkdir()
    _capture(base, "qst_0001.json", "alpha beta gamma", ["a", "b"])
    _capture(base, "qst_0002.json", "delta", ["c"])
    _capture(plus, "qst_0001.json", "alpha beta gamma delta epsilon", ["a", "b", "x"], ["x"])
    out = tmp_path / "receipt.json"

    assert packet_token_cost.main(["--arm", f"base={base}", "--arm", f"plus={plus}", "--out", str(out)]) == 0

    receipt = json.loads(out.read_text(encoding="utf-8"))
    encoder = tiktoken.get_encoding("o200k_base")
    arm = receipt["arms"]["base"]
    assert arm["n"] == 2
    assert arm["tokens_o200k_base"]["total"] == len(encoder.encode("alpha beta gamma")) + len(encoder.encode("delta"))
    assert arm["chars"]["max"] == len("alpha beta gamma")
    assert "added_ids" not in arm
    assert receipt["arms"]["plus"]["added_ids"]["total"] == 1


def test_refuses_empty_directory_and_existing_receipt(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError):
        packet_token_cost.main(["--arm", f"x={empty}", "--out", str(tmp_path / "r.json")])
    existing = tmp_path / "exists.json"
    existing.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        packet_token_cost.main(["--arm", f"x={empty}", "--out", str(existing)])
