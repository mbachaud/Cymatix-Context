"""OKF bundle reader — conformance, degradation, and link capture.

Conformance is enforced against the vendored spec snapshot
(tests/fixtures/okf/SPEC-ee67a5ca.md), NOT the upstream reference
implementation: exactly ONE required frontmatter field (`type`).
The degradation policy tested here is helix's own (see
helix_context/okf/bundle.py module docstring) — spec §9 does not
cover missing frontmatter or empty `type`.
"""

from pathlib import Path

import pytest

from helix_context.okf import read_bundle
from helix_context.okf.bundle import (
    extract_links,
    normalize_link_target,
    split_frontmatter,
)

OKF_FIXTURES = Path(__file__).parent / "fixtures" / "okf"


class TestBundleWalk:
    def test_reserved_index_files_excluded_at_any_level(self):
        bundle = read_bundle(OKF_FIXTURES / "ga4")
        ids = {c.concept_id for c in bundle.concepts}
        # ga4 carries 6 index.md files (root + 5 nested); none is a concept.
        assert len(bundle.concepts) == 11
        assert not any(
            cid == "index" or cid.endswith("/index") for cid in ids
        )

    def test_non_markdown_files_ignored(self):
        # Both vendored bundles ship a viz.html; it is not a concept.
        bundle = read_bundle(OKF_FIXTURES / "crypto_bitcoin")
        assert len(bundle.concepts) == 5
        assert all(not c.concept_id.endswith(".html") for c in bundle.concepts)

    def test_concept_ids_are_posix_paths_without_md(self):
        bundle = read_bundle(OKF_FIXTURES / "crypto_bitcoin")
        ids = sorted(c.concept_id for c in bundle.concepts)
        assert ids == [
            "datasets/crypto_bitcoin",
            "tables/blocks",
            "tables/inputs",
            "tables/outputs",
            "tables/transactions",
        ]
        for c in bundle.concepts:
            assert "\\" not in c.concept_id
            assert c.source_path == c.concept_id + ".md"

    def test_concepts_sorted_deterministically(self):
        bundle = read_bundle(OKF_FIXTURES / "ga4")
        ids = [c.concept_id for c in bundle.concepts]
        assert ids == sorted(ids)

    def test_dot_directories_skipped(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "hidden.md").write_text(
            "---\ntype: X\n---\nbody", encoding="utf-8"
        )
        (tmp_path / "real.md").write_text(
            "---\ntype: X\n---\nbody", encoding="utf-8"
        )
        bundle = read_bundle(tmp_path)
        assert [c.concept_id for c in bundle.concepts] == ["real"]

    def test_log_md_reserved_at_any_level(self, tmp_path):
        (tmp_path / "log.md").write_text("# Update Log\n", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "log.md").write_text("# Log\n", encoding="utf-8")
        (tmp_path / "sub" / "c.md").write_text(
            "---\ntype: X\n---\nbody", encoding="utf-8"
        )
        bundle = read_bundle(tmp_path)
        assert [c.concept_id for c in bundle.concepts] == ["sub/c"]

    def test_bundle_root_must_be_directory(self, tmp_path):
        with pytest.raises(ValueError):
            read_bundle(tmp_path / "nope")

    def test_bundle_id_defaults_to_directory_name(self):
        bundle = read_bundle(OKF_FIXTURES / "type_only")
        assert bundle.bundle_id == "type_only"
        assert read_bundle(
            OKF_FIXTURES / "type_only", bundle_id="custom"
        ).bundle_id == "custom"


class TestConformance:
    def test_type_only_bundle_accepted_without_warnings(self):
        """Spec §9: `type` is the ONLY required field. A bundle whose
        frontmatter carries nothing else must be accepted cleanly (the
        reference implementation's 4-field validator is NOT copied)."""
        bundle = read_bundle(OKF_FIXTURES / "type_only")
        assert bundle.warnings == []
        assert bundle.skipped_files == []
        (concept,) = bundle.concepts
        assert concept.raw_type == "Note"
        assert concept.title is None
        assert concept.description is None
        assert concept.tags == ()

    def test_vendored_bundles_fully_conformant(self):
        for name in ("crypto_bitcoin", "ga4"):
            bundle = read_bundle(OKF_FIXTURES / name)
            assert bundle.warnings == [], name
            assert all(c.raw_type for c in bundle.concepts), name

    def test_frontmatter_stripped_from_body(self):
        bundle = read_bundle(OKF_FIXTURES / "crypto_bitcoin")
        for c in bundle.concepts:
            assert not c.body.startswith("---")
            assert "type: BigQuery" not in c.body

    def test_free_form_type_and_extensions_pass_through(self):
        bundle = read_bundle(OKF_FIXTURES / "crypto_bitcoin")
        concept = next(
            c for c in bundle.concepts if c.concept_id == "tables/blocks"
        )
        assert concept.raw_type == "BigQuery Table"  # free-form, verbatim
        # Scalar extension fields become key=value facts.
        assert any(kv.startswith("resource=") for kv in concept.key_values)
        assert any(kv.startswith("timestamp=") for kv in concept.key_values)

    def test_okf_version_read_from_root_index_only(self, tmp_path):
        (tmp_path / "index.md").write_text(
            '---\nokf_version: "0.1"\n---\n# Index\n', encoding="utf-8"
        )
        (tmp_path / "c.md").write_text(
            "---\ntype: X\n---\nbody", encoding="utf-8"
        )
        bundle = read_bundle(tmp_path)
        assert bundle.okf_version == "0.1"

    def test_missing_index_md_is_fine(self):
        bundle = read_bundle(OKF_FIXTURES / "type_only")
        assert bundle.okf_version is None
        assert bundle.warnings == []


class TestDegradationPolicy:
    """Helix's own policy: warn + generic document, bundle continues."""

    @pytest.fixture(scope="class")
    def degraded(self):
        return read_bundle(OKF_FIXTURES / "degraded")

    def test_all_degraded_files_still_become_concepts(self, degraded):
        assert sorted(c.concept_id for c in degraded.concepts) == [
            "bad_yaml",
            "dangling",
            "empty_type",
            "no_frontmatter",
        ]

    def test_warnings_carry_file_paths(self, degraded):
        assert len(degraded.warnings) == 3
        for expected in ("bad_yaml.md", "empty_type.md", "no_frontmatter.md"):
            assert any(expected in w for w in degraded.warnings)

    def test_missing_frontmatter_keeps_whole_body(self, degraded):
        concept = next(
            c for c in degraded.concepts if c.concept_id == "no_frontmatter"
        )
        assert concept.raw_type is None
        assert concept.body.startswith("# Orphan document")

    def test_empty_type_keeps_other_frontmatter(self, degraded):
        concept = next(
            c for c in degraded.concepts if c.concept_id == "empty_type"
        )
        assert concept.raw_type is None
        assert concept.title == "Empty type field"
        assert concept.tags == ("degraded", "still-tagged")

    def test_bad_yaml_strips_delimited_block(self, degraded):
        concept = next(
            c for c in degraded.concepts if c.concept_id == "bad_yaml"
        )
        assert concept.raw_type is None
        assert concept.frontmatter == {}
        assert "unclosed" not in concept.body

    def test_unterminated_frontmatter_keeps_whole_file(self, tmp_path):
        (tmp_path / "c.md").write_text(
            "---\ntype: X\nno closing delimiter\n", encoding="utf-8"
        )
        bundle = read_bundle(tmp_path)
        (concept,) = bundle.concepts
        assert concept.raw_type is None
        assert concept.body.startswith("---")
        assert any("unterminated" in w for w in bundle.warnings)

    def test_non_utf8_file_skipped_bundle_continues(self, tmp_path):
        (tmp_path / "bad.md").write_bytes(b"---\ntype: X\n---\n\xff\xfe broken")
        (tmp_path / "good.md").write_text(
            "---\ntype: X\n---\nbody", encoding="utf-8"
        )
        bundle = read_bundle(tmp_path)
        assert [c.concept_id for c in bundle.concepts] == ["good"]
        assert bundle.skipped_files == ["bad.md"]
        assert any("UTF-8" in w for w in bundle.warnings)


class TestLinkCapture:
    def test_links_captured_and_fenced_code_excluded(self):
        bundle = read_bundle(OKF_FIXTURES / "degraded")
        concept = next(
            c for c in bundle.concepts if c.concept_id == "dangling"
        )
        targets = [l.target_concept_id for l in concept.links]
        assert targets == ["missing/concept", "no_frontmatter"]
        # The fenced ```markdown block's link must not be captured.
        assert "empty_type" not in targets

    def test_link_text_preserved(self):
        bundle = read_bundle(OKF_FIXTURES / "degraded")
        concept = next(
            c for c in bundle.concepts if c.concept_id == "dangling"
        )
        assert concept.links[0].link_text == "a concept that does not exist"

    def test_absolute_target_is_bundle_relative(self):
        assert (
            normalize_link_target("/tables/customers.md", "datasets/sales.md")
            == "tables/customers"
        )

    def test_relative_target_resolves_against_source_dir(self):
        assert (
            normalize_link_target("./other.md", "tables/orders.md")
            == "tables/other"
        )
        assert (
            normalize_link_target("../datasets/sales.md", "tables/orders.md")
            == "datasets/sales"
        )

    def test_anchor_stripped(self):
        assert (
            normalize_link_target("/tables/orders.md#schema", "a.md")
            == "tables/orders"
        )

    def test_external_urls_not_links(self):
        for target in (
            "https://example.com/dash.md",
            "http://example.com",
            "mailto:x@example.com",
        ):
            assert normalize_link_target(target, "a.md") is None

    def test_non_md_targets_not_links(self):
        assert normalize_link_target("subdir/", "a.md") is None
        assert normalize_link_target("viz.html", "a.md") is None

    def test_reserved_targets_not_links(self):
        assert normalize_link_target("/index.md", "a.md") is None
        assert normalize_link_target("sub/log.md", "a.md") is None

    def test_images_not_links(self):
        links = extract_links("An image ![alt](/diagram.md) here", "a.md")
        assert links == []

    def test_escaping_target_kept_as_dangling(self):
        # A target that climbs out of the bundle can never resolve, but
        # the link is still recorded (dangling) rather than dropped.
        assert (
            normalize_link_target("../../outside.md", "tables/orders.md")
            == "../outside"
        )

    def test_spec_example_links_with_md_suffix_normalize(self):
        # Spec §4.3 links WITH .md; concept IDs strip it (§2).
        body = "FK into [customers](/tables/customers.md)."
        (link,) = extract_links(body, "tables/orders.md")
        assert link.target_concept_id == "tables/customers"


class TestNormalization:
    def test_crlf_and_bom_normalized_to_identical_body(self, tmp_path):
        text = "---\ntype: X\n---\nline one\nline two\n"
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "c.md").write_bytes(text.encode("utf-8"))
        (b / "c.md").write_bytes(
            b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8")
        )
        body_a = read_bundle(a).concepts[0].body
        body_b = read_bundle(b).concepts[0].body
        assert body_a == body_b
        assert "\r" not in body_b

    def test_split_frontmatter_roundtrip(self):
        block, body, err = split_frontmatter(
            "---\ntype: X\n---\n\nbody text\n"
        )
        assert block == "type: X"
        assert body == "body text\n"
        assert err is None
