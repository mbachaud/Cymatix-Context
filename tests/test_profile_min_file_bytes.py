"""Per-profile ``min_file_bytes`` override in ``scripts/build_fixture_matrix.py``.

Issue #482: the builder's 50-byte floor exists to keep stub files out of code
corpora, but BEIR Quora is short questions — 44% of its documents and 55% of
its gold sit under 50 bytes, so the floor would strand 52% of the test
queries. A profile may now set ``min_file_bytes``; profiles without it keep
the 50-byte floor byte-for-byte. Sharded mode checks the floor inside its
worker tasks, so it refuses an override rather than silently ignoring it.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_fixture_matrix as bfm  # noqa: E402


def _load_resolver():
    spec = importlib.util.spec_from_file_location(
        "resolve_bench_needles", ROOT / "scripts" / "resolve_bench_needles.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_default_floor_is_the_module_constant():
    assert bfm._profile_min_file_bytes({"label": "x"}) == bfm.MIN_FILE_SIZE == 50


def test_override_is_returned():
    assert bfm._profile_min_file_bytes({"min_file_bytes": 1}) == 1


@pytest.mark.parametrize("bad", [-1, "1", 1.5, None])
def test_override_must_be_a_non_negative_int(bad):
    with pytest.raises(ValueError):
        bfm._profile_min_file_bytes({"min_file_bytes": bad})


def test_discovery_admits_short_files_only_under_the_override(tmp_path):
    (tmp_path / "short.txt").write_text("What is 2+2?", encoding="utf-8")       # 12 bytes
    (tmp_path / "long.txt").write_text("x" * 80, encoding="utf-8")
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")

    def names(**kw):
        stats = {"missing_roots": [], "skipped": 0}
        files = bfm._iter_ingestable_files([str(tmp_path)], set(), [], stats, **kw)
        return sorted(Path(f).name for f, _ in files), stats["skipped"]

    assert names() == (["long.txt"], 2)
    assert names(min_bytes=1) == (["long.txt", "short.txt"], 1)


def test_ingest_tree_takes_the_floor_with_the_old_default():
    param = inspect.signature(bfm.ingest_tree).parameters["min_bytes"]
    assert param.default == bfm.MIN_FILE_SIZE


def test_sharded_mode_refuses_a_profile_with_an_override(monkeypatch, tmp_path):
    monkeypatch.setitem(bfm.PROFILES, "tiny_override", {
        "label": "t", "active_roots": 1, "roots": [str(tmp_path)],
        "extra_skip_dirs": set(), "skip_dirs_override": set(),
        "extra_filename_filters": [], "min_file_bytes": 1,
    })
    with pytest.raises(ValueError, match="min_file_bytes"):
        bfm.build_profile_sharded("tiny_override", str(tmp_path / "out"))


def test_beir_profiles_set_a_one_byte_floor_and_others_do_not():
    beir = {k: v for k, v in bfm.PROFILES.items() if k.startswith("beir_")}
    assert beir and all(v.get("min_file_bytes") == 1 for v in beir.values())
    others = [k for k, v in bfm.PROFILES.items() if not k.startswith("beir_") and "min_file_bytes" in v]
    assert others == []


def test_resolver_exposes_a_matching_floor_flag():
    src = (ROOT / "scripts" / "resolve_bench_needles.py").read_text(encoding="utf-8")
    assert "--min-file-bytes" in src
    mod = _load_resolver()
    assert mod.MIN_FILE_SIZE == 50
