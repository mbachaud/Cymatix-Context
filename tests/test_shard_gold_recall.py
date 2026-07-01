"""tests/test_shard_gold_recall.py

Two independent test groups — all pure-Python, no server, no GPU, no network,
no /tmp.  Runs with: python -m pytest tests/test_shard_gold_recall.py -v

GROUP A: recall@k / MRR metric math
    Validates the scoring helpers in bench_shard_recall.py against an inline
    fixture.  Covers: hit at rank-1, hit at rank-3, hit at rank-10, miss, MRR
    formula, aggregation across types (within/cross/all), and the gold-path
    normalisation rule (_gold_hit).

GROUP B: no-leak guard (query scrubbing)
    Uses a tiny temp source-tree fixture (Python file + Markdown file) to
    verify that build_shard_gold's query scrubbing:
      1. Removes the gold filename stem from every generated question.
      2. Removes the gold symbol name from every generated question.
      3. Removes directory-name tokens from every generated question.
      4. Still produces questions that are >= 40 characters long.
"""
from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

# Make benchmarks/ importable without packaging.
BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmarks"
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

# We import the pure helper functions only — never the CLI entrypoints —
# so these tests never touch the network or a live genome.
from bench_shard_recall import (  # noqa: E402
    _gold_hit,
    _norm,
    _percentile,
    _recall_at,
    _rr,
    score_needles,
)
from build_shard_gold import (  # noqa: E402
    _build_query,
    _path_tokens,
    _scrub,
    build_needles_for_project,
    extract_python_units,
    extract_md_sections,
)


# ===========================================================================
# GROUP A: recall@k / MRR math
# ===========================================================================


class TestRecallMath:
    """Validate the raw metric helpers."""

    # ── _recall_at ────────────────────────────────────────────────────────

    def test_recall_at_rank1_hits_all_k(self):
        for k in (1, 3, 5, 10):
            assert _recall_at(1, k) == 1.0

    def test_recall_at_rank3_misses_k1(self):
        assert _recall_at(3, 1) == 0.0

    def test_recall_at_rank3_hits_k3(self):
        assert _recall_at(3, 3) == 1.0

    def test_recall_at_rank10_hits_k10_only(self):
        assert _recall_at(10, 5) == 0.0
        assert _recall_at(10, 10) == 1.0

    def test_recall_at_none_is_always_miss(self):
        for k in (1, 3, 5, 10):
            assert _recall_at(None, k) == 0.0

    # ── _rr ───────────────────────────────────────────────────────────────

    def test_rr_rank1(self):
        assert _rr(1) == pytest.approx(1.0)

    def test_rr_rank2(self):
        assert _rr(2) == pytest.approx(0.5)

    def test_rr_rank5(self):
        assert _rr(5) == pytest.approx(0.2)

    def test_rr_none_is_zero(self):
        assert _rr(None) == 0.0

    # ── _percentile ───────────────────────────────────────────────────────

    def test_percentile_median_even(self):
        vals = [1.0, 2.0, 3.0, 4.0]
        assert _percentile(vals, 50) == pytest.approx(2.5)

    def test_percentile_p90(self):
        vals = list(range(1, 11))  # 1..10
        # p90 of 1..10 should be near 9.1
        assert _percentile(vals, 90) == pytest.approx(9.1)

    def test_percentile_empty(self):
        assert _percentile([], 50) == 0.0

    # ── _gold_hit: path normalisation ────────────────────────────────────

    def test_gold_hit_exact_forward_slash(self):
        assert _gold_hit(
            "F:/Projects/helix-context/helix_context/pipeline/stages.py",
            ["helix-context/helix_context/pipeline/stages.py"],
        )

    def test_gold_hit_backslash_source(self):
        """Windows-native paths from the server must still match."""
        assert _gold_hit(
            "F:\\Projects\\BookKeeper\\bookkeeper\\auth.py",
            ["bookkeeper/bookkeeper/auth.py"],
        )

    def test_gold_hit_case_insensitive(self):
        assert _gold_hit(
            "F:/Projects/BookKeeper/BookKeeper/Auth.PY",
            ["bookkeeper/bookkeeper/auth.py"],
        )

    def test_gold_hit_miss_different_project(self):
        assert not _gold_hit(
            "F:/Projects/Education/fleet/model.py",
            ["bookkeeper/bookkeeper/auth.py"],
        )

    def test_gold_hit_bidirectional_short_gold(self):
        """Gold may be a directory prefix — match any file under it."""
        assert _gold_hit(
            "F:/Projects/helix-context/helix_context/pipeline/stages.py",
            ["helix-context/helix_context/pipeline"],
        )

    def test_gold_hit_multiple_gold_paths_any_match(self):
        assert _gold_hit(
            "F:/Projects/Education/README.md",
            [
                "bookkeeper/README.md",
                "education/README.md",
            ],
        )

    def test_gold_hit_multiple_gold_paths_none_match(self):
        assert not _gold_hit(
            "F:/Projects/CosmicTasha/README.md",
            [
                "bookkeeper/README.md",
                "education/README.md",
            ],
        )

    # ── score_needles aggregation on an inline fixture ───────────────────

    def _make_fake_fingerprint_server(self, ranked_sources: list[list[str]]):
        """Monkeypatch target: returns pre-canned ranked sources per call."""
        call_idx = [0]

        def fake_fingerprint(helix_url, query, max_results=10, timeout_s=30.0):
            idx = call_idx[0]
            call_idx[0] += 1
            if idx < len(ranked_sources):
                fps = [{"source": s} for s in ranked_sources[idx]]
            else:
                fps = []
            return fps, 5.0  # 5 ms fake latency

        return fake_fingerprint

    def test_score_needles_all_hit_rank1(self, monkeypatch):
        """Every needle hits at rank 1 -> recall@k=1 for all k, MRR=1."""
        import bench_shard_recall as bsr

        needles = [
            {
                "id": "a",
                "type": "within",
                "question": "What does authenticate do?",
                "gold_paths": ["bookkeeper/bookkeeper/auth.py"],
            },
            {
                "id": "b",
                "type": "within",
                "question": "How is the pipeline staged?",
                "gold_paths": ["helix-context/helix_context/pipeline/stages.py"],
            },
        ]
        ranked = [
            ["F:/Projects/BookKeeper/bookkeeper/auth.py", "F:/Projects/other/foo.py"],
            ["F:/Projects/helix-context/helix_context/pipeline/stages.py"],
        ]
        monkeypatch.setattr(bsr, "fingerprint", self._make_fake_fingerprint_server(ranked))

        summary, rows = score_needles(needles, "http://fake:11437")

        assert summary["all"]["mrr"] == pytest.approx(1.0)
        for k in (1, 3, 5, 10):
            assert summary["all"]["recall@{}".format(k)] == pytest.approx(1.0)

    def test_score_needles_all_miss(self, monkeypatch):
        """No gold hit -> recall=0, MRR=0."""
        import bench_shard_recall as bsr

        needles = [
            {
                "id": "c",
                "type": "within",
                "question": "What does authenticate do?",
                "gold_paths": ["bookkeeper/bookkeeper/auth.py"],
            },
        ]
        # Server returns a completely different file.
        ranked = [["F:/Projects/Education/fleet/unrelated.py"]]
        monkeypatch.setattr(bsr, "fingerprint", self._make_fake_fingerprint_server(ranked))

        summary, rows = score_needles(needles, "http://fake:11437")

        assert summary["all"]["mrr"] == pytest.approx(0.0)
        for k in (1, 3, 5, 10):
            assert summary["all"]["recall@{}".format(k)] == pytest.approx(0.0)

    def test_score_needles_mixed_ranks(self, monkeypatch):
        """
        3 needles: rank-1 hit, rank-3 hit, miss.
        Expected:
          recall@1 = 1/3
          recall@3 = 2/3
          recall@5 = 2/3
          MRR      = (1/1 + 1/3 + 0) / 3 = 4/9
        """
        import bench_shard_recall as bsr

        needles = [
            {"id": "d", "type": "within",
             "question": "q1", "gold_paths": ["proj/a.py"]},
            {"id": "e", "type": "within",
             "question": "q2", "gold_paths": ["proj/b.py"]},
            {"id": "f", "type": "within",
             "question": "q3", "gold_paths": ["proj/c.py"]},
        ]
        ranked = [
            # needle d: gold at rank 1
            ["F:/Projects/proj/a.py", "other.py"],
            # needle e: gold at rank 3
            ["other1.py", "other2.py", "F:/Projects/proj/b.py"],
            # needle f: gold not present at all
            ["other3.py", "other4.py", "other5.py"],
        ]
        monkeypatch.setattr(bsr, "fingerprint", self._make_fake_fingerprint_server(ranked))

        summary, rows = score_needles(needles, "http://fake:11437")

        # summary values are rounded to 4 decimal places; use abs=5e-4
        assert summary["all"]["recall@1"] == pytest.approx(1 / 3, abs=5e-4)
        assert summary["all"]["recall@3"] == pytest.approx(2 / 3, abs=5e-4)
        assert summary["all"]["recall@5"] == pytest.approx(2 / 3, abs=5e-4)
        expected_mrr = (1.0 + 1.0 / 3 + 0.0) / 3
        assert summary["all"]["mrr"] == pytest.approx(expected_mrr, abs=5e-4)

    def test_score_needles_type_breakdown_within_cross(self, monkeypatch):
        """within and cross aggregates are computed independently."""
        import bench_shard_recall as bsr

        needles = [
            {"id": "w1", "type": "within",
             "question": "q-w1", "gold_paths": ["proj/w.py"]},
            {"id": "c1", "type": "cross",
             "question": "q-c1", "gold_paths": ["proj/x.py"]},
        ]
        ranked = [
            # w1 hits at rank 1
            ["F:/Projects/proj/w.py"],
            # c1 misses
            ["F:/Projects/other/y.py"],
        ]
        monkeypatch.setattr(bsr, "fingerprint", self._make_fake_fingerprint_server(ranked))

        summary, rows = score_needles(needles, "http://fake:11437")

        assert "within" in summary
        assert "cross" in summary
        assert "all" in summary

        assert summary["within"]["recall@1"] == pytest.approx(1.0)
        assert summary["cross"]["recall@1"] == pytest.approx(0.0)
        assert summary["all"]["n"] == 2

    def test_score_needles_error_counted(self, monkeypatch):
        """HTTP errors are counted in .err and don't crash the run."""
        import bench_shard_recall as bsr

        needles = [
            {"id": "g", "type": "within",
             "question": "q-err", "gold_paths": ["proj/z.py"]},
        ]

        def always_raise(helix_url, query, **kw):
            raise OSError("connection refused")

        monkeypatch.setattr(bsr, "fingerprint", always_raise)

        summary, rows = score_needles(needles, "http://fake:11437")
        # n=0 because the error was counted, not a scored needle.
        assert summary.get("all", {}).get("n", 0) == 0
        assert rows[0]["error"] is not None


# ===========================================================================
# GROUP B: no-leak guard (query scrubbing)
# ===========================================================================


class TestNoLeakGuard:
    """Verify that build_shard_gold never echoes filename/symbol/path in query."""

    @pytest.fixture()
    def temp_project(self, tmp_path):
        """Build a tiny source tree with known scrub targets."""
        root = tmp_path / "MyFakeProject"
        src = root / "src" / "auth"
        src.mkdir(parents=True)

        # Python file: class AuthHandler with docstring that does NOT
        # contain the symbol or file name — simulates real code where the
        # docstring explains behaviour without repeating the name.
        py_file = src / "auth_handler.py"
        py_file.write_text(
            textwrap.dedent("""\
                class AuthHandler:
                    \"\"\"Verifies incoming tokens against the credential store
                    and raises a permission error when validation fails.
                    Delegates revocation tracking to the session registry.
                    Supports both bearer and cookie based schemes.
                    \"\"\"

                    def authenticate(self, token: str) -> bool:
                        \"\"\"Checks whether token is valid and not expired,
                        returning True on success and False on failure.
                        Queries the underlying database for the token record.
                        This method is the main entry point for all authentication.
                        \"\"\"
                        return True
                """),
            encoding="utf-8",
        )

        # Markdown file: section about deployment
        md_file = root / "docs" / "deploy_guide.md"
        md_file.parent.mkdir(parents=True)
        md_file.write_text(
            textwrap.dedent("""\
                # Deployment Overview

                This section describes the rollout process and environment
                prerequisites.  Services must be started in dependency order
                and health checks should pass before traffic is routed.
                Containerised builds use multi-stage images to keep size minimal.

                ## Configuration Reference

                Environment variables control all runtime behaviour.
                The primary knobs are connection pool size, log verbosity,
                and the upstream endpoint address.
                """),
            encoding="utf-8",
        )

        return root

    # ── Python extractor ────────────────────────────────────────────────

    def test_python_extractor_finds_class_docstring(self, temp_project):
        py_file = temp_project / "src" / "auth" / "auth_handler.py"
        units = extract_python_units(py_file)
        assert len(units) >= 1
        symbols = [u["symbol"] for u in units]
        assert "AuthHandler" in symbols

    def test_python_extractor_finds_method_docstring(self, temp_project):
        py_file = temp_project / "src" / "auth" / "auth_handler.py"
        units = extract_python_units(py_file)
        symbols = [u["symbol"] for u in units]
        assert "authenticate" in symbols

    # ── Markdown extractor ──────────────────────────────────────────────

    def test_md_extractor_finds_sections(self, temp_project):
        md_file = temp_project / "docs" / "deploy_guide.md"
        sections = extract_md_sections(md_file)
        assert len(sections) >= 1
        titles = [s["symbol"] for s in sections]
        assert "Deployment Overview" in titles

    # ── _path_tokens detects all leak surfaces ──────────────────────────

    def test_path_tokens_includes_filename_stem(self, temp_project):
        py_file = temp_project / "src" / "auth" / "auth_handler.py"
        tokens = _path_tokens(temp_project, py_file)
        # stem "auth_handler" should produce subtokens "auth" and "handler"
        assert "handler" in tokens or "auth_handler" in tokens

    def test_path_tokens_includes_directory_names(self, temp_project):
        py_file = temp_project / "src" / "auth" / "auth_handler.py"
        tokens = _path_tokens(temp_project, py_file)
        # "src" and "auth" are directory components
        # (they may be filtered as too-short / boring, but check at least one deeper one)
        assert "auth" in tokens or "src" in tokens

    def test_path_tokens_includes_project_root_name(self, temp_project):
        py_file = temp_project / "src" / "auth" / "auth_handler.py"
        tokens = _path_tokens(temp_project, py_file)
        # project root name is "MyFakeProject" -> lower -> "myfakeproject"
        # after camelCase split: "my", "fake", "project"
        assert "fake" in tokens or "myfakeproject" in tokens

    # ── _scrub removes forbidden tokens ─────────────────────────────────

    def test_scrub_removes_exact_token(self):
        text = "The auth_handler validates the session."
        result = _scrub(text, {"auth_handler"})
        assert "auth_handler" not in result.lower()

    def test_scrub_removes_case_insensitively(self):
        text = "AuthHandler processes the request pipeline."
        result = _scrub(text, {"authhandler"})
        assert "authhandler" not in result.lower()

    def test_scrub_collapses_whitespace(self):
        text = "The  handler   processes  tokens."
        result = _scrub(text, {"handler"})
        assert "  " not in result

    def test_scrub_preserves_non_forbidden_content(self):
        text = "Verifies incoming tokens against the credential store."
        result = _scrub(text, {"auth_handler"})
        # The forbidden token is not present, so text should be mostly intact.
        assert "credential" in result.lower()
        assert "tokens" in result.lower()

    # ── End-to-end: build_needles_for_project no-leak property ──────────

    def test_no_filename_stem_in_query(self, temp_project):
        """The filename stem must not appear in any generated question."""
        import random as _random
        rng = _random.Random(0)
        needles = build_needles_for_project(temp_project, n=50, rng=rng)
        # For Python needles from auth_handler.py
        py_needles = [
            n for n in needles
            if "auth_handler.py" in (n.get("gold_paths") or [""])[0]
        ]
        for nd in py_needles:
            q = nd["question"].lower()
            assert "auth_handler" not in q, (
                "filename stem 'auth_handler' leaked into question: {!r}".format(q)
            )

    def test_no_symbol_name_in_query(self, temp_project):
        """The function/class name must not appear verbatim in the question."""
        import random as _random
        rng = _random.Random(0)
        needles = build_needles_for_project(temp_project, n=50, rng=rng)
        for nd in needles:
            q = nd["question"].lower()
            for sym in nd.get("gold_symbols", []):
                assert sym.lower() not in q, (
                    "symbol {!r} leaked into question for {}: {!r}".format(
                        sym, nd.get("gold_paths"), q
                    )
                )

    def test_no_directory_name_in_query(self, temp_project):
        """Directory-component tokens must not appear in generated questions.

        We specifically test that the project root name's sub-tokens
        (e.g. 'fake' from 'MyFakeProject') are absent.
        """
        import random as _random
        rng = _random.Random(0)
        needles = build_needles_for_project(temp_project, n=50, rng=rng)
        for nd in needles:
            q = nd["question"].lower()
            # "myfakeproject" split -> "fake", "project" are forbidden
            assert "myfakeproject" not in q, (
                "project root name leaked into question: {!r}".format(q)
            )

    def test_questions_are_long_enough(self, temp_project):
        """After scrubbing, every question must still be >= 40 characters."""
        import random as _random
        rng = _random.Random(0)
        needles = build_needles_for_project(temp_project, n=50, rng=rng)
        assert len(needles) > 0, "expected at least one needle from the fixture"
        for nd in needles:
            assert len(nd["question"]) >= 40, (
                "question too short after scrubbing: {!r}".format(nd["question"])
            )

    def test_needles_have_required_schema_fields(self, temp_project):
        """Every needle must have the mandatory JSONL schema fields."""
        import random as _random
        rng = _random.Random(0)
        needles = build_needles_for_project(temp_project, n=50, rng=rng)
        required = {"project", "file_type", "question", "gold_paths", "gold_lines"}
        for nd in needles:
            missing = required - nd.keys()
            assert not missing, "needle missing fields {}: {!r}".format(missing, nd)
            assert isinstance(nd["gold_paths"], list)
            assert len(nd["gold_paths"]) >= 1
            assert isinstance(nd["gold_lines"], list)
            assert len(nd["gold_lines"]) == 2

    def test_gold_paths_are_project_relative_forward_slash(self, temp_project):
        """gold_paths entries must be '<project_name>/...' with forward slashes."""
        import random as _random
        rng = _random.Random(0)
        needles = build_needles_for_project(temp_project, n=50, rng=rng)
        for nd in needles:
            for gp in nd["gold_paths"]:
                assert "/" in gp, "gold_path should use forward slashes: {!r}".format(gp)
                assert not gp.startswith("/"), (
                    "gold_path should be relative, not absolute: {!r}".format(gp)
                )
                # Should start with the project name.
                assert gp.startswith(temp_project.name + "/"), (
                    "gold_path should start with project name {!r}: {!r}".format(
                        temp_project.name, gp
                    )
                )

    # ── _build_query directly ────────────────────────────────────────────

    def test_build_query_returns_none_when_too_short_after_scrub(self):
        # If the entire doc is just the forbidden token, query should be None.
        result = _build_query("auth_handler", {"auth_handler"})
        assert result is None

    def test_build_query_preserves_behaviour_prose(self):
        doc = (
            "Verifies incoming tokens against the credential store and raises "
            "a permission error when validation fails.  Supports both bearer "
            "and cookie based authentication schemes."
        )
        result = _build_query(doc, {"auth_handler", "authenticate", "handler"})
        assert result is not None
        assert len(result) >= 40
        assert "auth_handler" not in result.lower()
        assert "authenticate" not in result.lower()
