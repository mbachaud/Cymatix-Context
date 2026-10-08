"""scripts/fit_know_lanes.py: folded betas reproduce the fitted model (#482)."""
from __future__ import annotations

import importlib.util
import json
import random
from pathlib import Path

import pytest

pytest.importorskip("sklearn")

from cymatix_context.config import load_config  # noqa: E402
from cymatix_context.scoring.know_lanes import FEATURE_NAMES, LanesModel, feature_values  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location("fit_know_lanes", ROOT / "scripts" / "fit_know_lanes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows(n, seed):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        agree = rng.randint(0, 3)
        hit = rng.random() < (0.15 + 0.2 * agree)
        top = rng.uniform(0.1, 0.5)
        second = top / rng.uniform(1.0, 1.6)
        rows.append({
            "needle": f"n{i}", "top_score": top, "score_gap": top - second, "ratio_top2": top / second,
            "pool_size": rng.randint(5, 50), "coordinate_confidence": rng.choice([None, rng.random()]),
            "rank_of_first_gold": 1 if hit else rng.choice([2, 5, None]),
            "lane_signals": {"lanes_fired": 3, "top1_lanes": agree, "lanes_top3_agree": agree,
                             "frac_lanes_agree": agree / 3, "fts5_top1_is_fused_top1": bool(agree),
                             "query_term_coverage": rng.choice([None, rng.random()])},
        })
    return rows


def _receipt(tmp_path, name, rows):
    p = tmp_path / f"know_replay_{name}_2026-10-05.json"
    p.write_text(json.dumps({"arms": [{"arm": "postflip_default", "per_query": rows}]}), encoding="utf-8")
    return p


def test_folded_betas_reproduce_the_pipeline_and_load_from_toml(tmp_path):
    mod = _script()
    paths = [f"{c}={_receipt(tmp_path, c, _rows(300, s))}" for c, s in (("a", 1), ("b", 2), ("c", 3))]
    out_json, out_toml = tmp_path / "report.json", tmp_path / "lanes.toml"
    assert mod.main([*sum((["--receipt", p] for p in paths), []),
                     "--out", str(out_json), "--toml-out", str(out_toml)]) == 0

    report = json.loads(out_json.read_text(encoding="utf-8"))
    assert report["n_rows"] == 900 and len(report["leave_one_corpus_out"]["folds"]) == 3

    know = load_config(str(out_toml)).know
    assert know.model == "lanes" and set(know.lanes_betas) <= set(FEATURE_NAMES)
    model = LanesModel(know.lanes_intercept, dict(know.lanes_betas))
    rows = _rows(50, 9)
    pipe = mod.fit_pipeline([feature_values(r) for r in _rows(300, 1) + _rows(300, 2) + _rows(300, 3)],
                            [mod.label(r) for r in _rows(300, 1) + _rows(300, 2) + _rows(300, 3)])
    expected = pipe.predict_proba([[feature_values(r)[n] for n in FEATURE_NAMES] for r in rows])[:, 1]
    got = [model.confidence(feature_values(r)) for r in rows]
    assert got == pytest.approx(list(expected), abs=1e-6)


def test_rows_without_lane_signals_are_rejected(tmp_path):
    mod = _script()
    rows = _rows(20, 4)
    for r in rows:
        r.pop("lane_signals")
    p = _receipt(tmp_path, "old", rows)
    with pytest.raises(SystemExit, match="lane_signals"):
        mod.main(["--receipt", f"old={p}", "--out", str(tmp_path / "r.json"), "--toml-out", str(tmp_path / "t.toml")])
