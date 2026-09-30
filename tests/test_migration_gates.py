"""Prepared benchmark arms and paired gates run without benchmark stores."""

import importlib
import json

import pytest


def _gates():
    return importlib.import_module("benchmarks.dogfood.migrations.gates")


def test_arm_configs_pin_all_flags_and_only_change_the_named_treatment():
    source = '''[budget]
wire_format = "legacy"
tier_seat_floor_enabled = false
min_delivered_docs = 12
[retrieval]
harmonic_batching_enabled = false
rrf_k = 20
'''
    gates = _gates()
    for name, flags in gates.ARMS.items():
        text = gates.arm_config(source, flags)
        import tomllib
        config = tomllib.loads(text)
        assert config["budget"]["min_delivered_docs"] == 12
        assert config["retrieval"]["rrf_k"] == 20
        for path, value in flags.items():
            section, field = path.split(".")
            assert config[section][field] == value, name


def test_arm_config_rejects_missing_pin_instead_of_silently_using_defaults():
    with pytest.raises(ValueError, match="wire_format"):
        _gates().arm_config("[budget]\n", _gates().ARMS["baseline"])


def test_wire_normalization_preserves_body_and_normalizes_owned_structure():
    gates = _gates()
    legacy = '<expressed_context>\n[gene=abc ◆ fired=tag:1.0 100→42c]\n<GENE src="a&b">\ngenes and codons are user prose\n</GENE>\n</expressed_context>'
    canonical = '<expressed_context>\n[document=abc ◆ fired=tag:1.0 100→54c]\n<DOCUMENT src="a&amp;b">\ngenes and codons are user prose\n</DOCUMENT>\n</expressed_context>'
    assert gates.normalized_context(legacy, ["abc"], "legacy") == gates.normalized_context(canonical, ["abc"], "canonical")
    changed = canonical.replace("genes and codons", "documents and fragments")
    assert gates.normalized_context(legacy, ["abc"], "legacy") != gates.normalized_context(changed, ["abc"], "canonical")


def test_wire_normalization_rejects_truncated_and_ambiguous_structure():
    with pytest.raises(ValueError, match="wrapper"):
        _gates().normalized_context('<expressed_context>\n[gene=abc ◆ fired=none 10c]\n<GENE>\ncut ...[budget-trimmed]\n</expressed_context>', ["abc"], "legacy")


def _receipt(arm="baseline"):
    return {
        "pins": {"code_sha": "a" * 40, "bed_identity": "b" * 64,
                 "resolved_sha256": "c" * 64, "gold_sha256": "d" * 64,
                 "ingest_c": 6, "tagger_version": 1, "k": 12,
                 "base_config_sha256": "e" * 64, "needle_count": 1,
                 "needle_order_sha256": _gates().digest(["n"]),
                 "source_state": "source", "ccr_state": "ccr", "toin_sha256": "toin",
                 "headroom_config_sha256": "headroom", "common_config_sha256": "config",
                 "runtime_environment": {"PYTHONHASHSEED": "0"}, "python": "3.14", "sqlite": "3.50"},
        "arm": arm, "flags": _gates().ARMS[arm],
        "harmonic_bind_cap": 1 if arm == "harmonic_staged" else None,
        "errors": [], "harmonic_rows": 2,
        "per_query": [{"needle": "n", "gold_ranks": [1],
                       "rank_of_first_gold": 1, "final_rank_of_first_gold": 1,
                       "score_map_sha256": "score", "ranked_ids_sha256": "rank",
                       "delivered_ids": ["gold"], "delivered_count": 1, "delivered_gold": 1,
                       "budget_tier": "tight", "context_sha256": "raw",
                       "decoder_sha256": "decoder", "normalized_context_sha256": "normal",
                       "normalized_decoder_sha256": "decoder", "harmonic_contributions": 2,
                       "harmonic_batched_calls": 0, "harmonic_overflow_calls": 0}],
    }


def test_pair_gate_rejects_missing_needles_and_mismatched_pins():
    base = _receipt()
    on = _receipt("tier")
    on["per_query"] = []
    assert not _gates().compare(base, on, "tier")["passed"]
    on = _receipt("tier")
    on["pins"]["code_sha"] = "f" * 40
    assert not _gates().compare(base, on, "tier")["passed"]


def test_tier_gate_allows_gains_but_rejects_any_paired_delivery_loss():
    base = _receipt()
    on = _receipt("tier")
    on["per_query"][0]["delivered_ids"].append("more")
    assert _gates().compare(base, on, "tier")["passed"]
    on["per_query"][0]["delivered_gold"] = 0
    assert not _gates().compare(base, on, "tier")["passed"]


def test_wire_gate_checks_raw_changes_separately_from_normalized_equivalence():
    base = _receipt()
    on = _receipt("wire")
    on["per_query"][0]["context_sha256"] = "different canonical wrapper"
    result = _gates().compare(base, on, "wire")
    assert result["passed"]
    assert result["raw_context_changes"] == ["n"]
    on["per_query"][0]["normalized_context_sha256"] = "changed body"
    assert not _gates().compare(base, on, "wire")["passed"]


def test_harmonic_gate_rejects_unexercised_or_empty_feature():
    base = _receipt("harmonic_on")
    on = _receipt("harmonic_staged")
    assert not _gates().compare(base, on, "harmonic")["passed"]
    on["per_query"][0]["harmonic_batched_calls"] = 1
    assert _gates().compare(base, on, "harmonic")["passed"]
    base["harmonic_rows"] = 0
    assert not _gates().compare(base, on, "harmonic")["passed"]


def test_compare_cli_writes_failure_receipt_and_exits_nonzero(tmp_path):
    paths = [tmp_path / name for name in ("off.json", "on.json", "verdict.json")]
    base = _receipt()
    paths[0].write_text(json.dumps(base), encoding="utf-8")
    on = _receipt("tier")
    on["per_query"][0]["delivered_gold"] = 0
    paths[1].write_text(json.dumps(on), encoding="utf-8")
    result = _gates().main(["--off", str(paths[0]), "--on", str(paths[1]),
                            "--gate", "tier", "--out", str(paths[2])])
    assert result == 1
    assert json.loads(paths[2].read_text(encoding="utf-8"))["passed"] is False


@pytest.mark.parametrize("gate", ["wire", "tier", "harmonic"])
def test_duplicate_baseline_is_never_a_migration_gate(gate):
    assert not _gates().compare(_receipt(), _receipt(), gate)["passed"]


def test_missing_delivery_field_and_shortened_bank_fail_closed():
    base, on = _receipt(), _receipt("tier")
    del on["per_query"][0]["delivered_gold"]
    assert not _gates().compare(base, on, "tier")["passed"]
    base, on = _receipt(), _receipt("tier")
    base["pins"]["needle_count"] = on["pins"]["needle_count"] = 2
    assert not _gates().compare(base, on, "tier")["passed"]


@pytest.mark.parametrize("slate", [None, ["key=value with genes and codons"]])
def test_real_assembly_normalizes_owned_wrappers_and_decoder(slate):
    from cymatix_context.config import BudgetConfig
    from cymatix_context.context_manager import CymatixContextManager
    from tests.conftest import make_cymatix_config, make_gene
    from benchmarks.dogfood.migrations.capture import observe_window

    observations = []
    gene = make_gene("Genes and codons are user prose.", gene_id="0123456789abcdef")
    gene.source_id = "notes.md"
    for mode in ("legacy", "canonical"):
        manager = CymatixContextManager(make_cymatix_config(budget=BudgetConfig(wire_format=mode)))
        try:
            text = gene.content
            if mode == "legacy":
                text = f'<GENE src="notes.md">\n{text}\n</GENE>'
            window = manager._assemble("q", [gene], {gene.gene_id: text}, answer_slate=slate)
            window.retrieval_scores = {gene.gene_id: 1.0}
            observations.append(observe_window(manager, window, mode))
        finally:
            manager.close()
    assert "normalization_error" not in observations[0]
    assert "normalization_error" not in observations[1]
    assert observations[0]["context_sha256"] != observations[1]["context_sha256"]
    assert observations[0]["normalized_context_sha256"] == observations[1]["normalized_context_sha256"]
    assert observations[0]["normalized_decoder_sha256"] == observations[1]["normalized_decoder_sha256"]


def test_observed_real_ladder_aligns_warmup_and_order(tmp_path):
    from benchmarks.dogfood.migrations.capture import run_observed_arm
    from cymatix_context.knowledge_store import KnowledgeStore
    from tests.conftest import make_gene

    bed = tmp_path / "bed.db"
    store = KnowledgeStore(str(bed))
    try:
        store.upsert_gene(make_gene("quartz deployment port is 4567", gene_id="0123456789abcdef",
                                    domains=["deployment"], entities=["quartz", "port"]))
    finally:
        store.close()
    config = tmp_path / "config.toml"
    config.write_text('[ingestion]\nsema_embed_on_ingest = false\n', encoding="utf-8")
    needles = [{"name": "first", "query": "quartz deployment port"},
               {"name": "second", "query": "quartz port 4567"}]
    result = run_observed_arm(config, bed, needles,
                             {row["name"]: {"0123456789abcdef"} for row in needles}, "legacy")
    assert result["errors"] == []
    assert [row["needle"] for row in result["per_query"]] == ["first", "second"]
    for row in result["per_query"]:
        assert row["delivered_ids"] == ["0123456789abcdef"]
        assert row["build_only_ms"] > 0
        assert len(row["context_sha256"]) == 64


def test_sqlite_snapshot_preserves_source_and_refuses_overwrite(tmp_path):
    import sqlite3
    from benchmarks.dogfood.migrations.capture import snapshot

    source, target = tmp_path / "source.db", tmp_path / "target.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE evidence (value TEXT)")
        conn.execute("INSERT INTO evidence VALUES ('preserved')")
    snapshot(source, target)
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT value FROM evidence").fetchone() == ("preserved",)
    with pytest.raises(ValueError, match="distinct new destination"):
        snapshot(source, target)


@pytest.mark.parametrize("gold", [{"n": "document-id"}, {"n": [""]}, {"n": [4]}])
def test_preparation_rejects_malformed_gold(gold):
    from benchmarks.dogfood.migrations.prepare import validate_bank
    with pytest.raises(ValueError, match="document ID strings"):
        validate_bank([{"name": "n", "query": "question"}], gold)


def test_preparation_creates_full_arms_without_opening_missing_bed(tmp_path, monkeypatch):
    from benchmarks.dogfood.migrations import prepare

    monkeypatch.setattr(prepare, "code_sha", lambda ref: "a" * 40)
    resolved, gold = tmp_path / "needles.json", tmp_path / "gold.json"
    resolved.write_text(json.dumps({"needles": [{"name": "n", "query": "question"}]}), encoding="utf-8")
    gold.write_text(json.dumps({"n": ["document-id"]}), encoding="utf-8")
    output = tmp_path / "prepared"
    args = ["--code-ref", "frozen-test", "--source-bed", str(tmp_path / "missing.db"),
            "--bed-identity", "b" * 64, "--ingest-c", "6", "--tagger-version", "1",
            "--resolved", str(resolved), "--gold", str(gold),
            "--headroom-toin", str(tmp_path / "toin.json"),
            "--headroom-ccr", str(tmp_path / "ccr.db"),
            "--headroom-config", str(tmp_path / "input-config"), "--out-dir", str(output)]
    assert prepare.main(args) == 0
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    assert plan["status"] == "prepared_not_run"
    assert set(plan["arms"]) == {"baseline", "wire", "tier", "harmonic_on", "harmonic_staged"}
    assert len(plan["commands"]) == 8
    assert not list(output.rglob("*.db"))
    unsafe_args = list(args)
    unsafe_args[unsafe_args.index("--out-dir") + 1] = str(tmp_path / "input-config" / "nested")
    with pytest.raises(SystemExit):
        prepare.main(unsafe_args)


def _legacy_bed_evidence(tmp_path, ingest_c="unknown"):
    import hashlib
    import sqlite3
    from benchmarks.dogfood.migrations.capture import data_state

    source = tmp_path / "legacy.db"
    conn = sqlite3.connect(source)
    try:
        conn.execute("CREATE TABLE genes (gene_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO genes VALUES ('document-a')")
        conn.commit()
    finally:
        conn.close()
    evidence = {
        "source_bed": str(source.resolve()),
        "bed_identity": hashlib.sha256(b"document-a\x00").hexdigest(),
        "ingest_c": ingest_c, "tagger_version": 1,
        "source_state": data_state(source),
        "provenance_sources": ["docs/benchmarks/BASELINES.md — legacy within-bed comparison; original ingest concurrency unknown."],
    }
    path = tmp_path / "bed-provenance.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    plan = {
        "source_bed": str(source.resolve()),
        "pins": {key: evidence[key] for key in ("bed_identity", "ingest_c", "tagger_version")},
        "bed_provenance_evidence": {"path": str(path), "sha256": _gates().file_digest(path)},
    }
    plan["pins"]["bed_provenance_sha256"] = plan["bed_provenance_evidence"]["sha256"]
    return source, evidence, plan


def _legacy_prepare_args(tmp_path, plan):
    resolved, gold = tmp_path / "needles.json", tmp_path / "gold.json"
    resolved.write_text(json.dumps({"needles": [{"name": "n", "query": "question"}]}), encoding="utf-8")
    gold.write_text(json.dumps({"n": ["document-a"]}), encoding="utf-8")
    return [
        "--code-ref", "frozen-test", "--source-bed", plan["source_bed"],
        "--bed-identity", plan["pins"]["bed_identity"],
        "--ingest-c", str(plan["pins"]["ingest_c"]), "--tagger-version", "1",
        "--resolved", str(resolved), "--gold", str(gold),
        "--headroom-toin", str(tmp_path / "toin.json"),
        "--headroom-ccr", str(tmp_path / "ccr.db"),
        "--headroom-config", str(tmp_path / "input-config"),
        "--out-dir", str(tmp_path / "prepared"),
    ]


def test_prepare_pins_external_unknown_provenance_without_opening_bed(tmp_path, monkeypatch):
    from benchmarks.dogfood.migrations import prepare

    source, evidence, plan = _legacy_bed_evidence(tmp_path)
    # Preparation validates supplied evidence, not the bed itself.
    source.unlink()
    monkeypatch.setattr(prepare, "code_sha", lambda ref: "a" * 40)
    args = _legacy_prepare_args(tmp_path, plan) + ["--bed-provenance", plan["bed_provenance_evidence"]["path"]]
    assert prepare.main(args) == 0
    prepared = json.loads((tmp_path / "prepared" / "plan.json").read_text(encoding="utf-8"))
    assert prepared["pins"]["ingest_c"] == "unknown"
    assert prepared["bed_provenance_evidence"] == plan["bed_provenance_evidence"]
    assert not list((tmp_path / "prepared").rglob("*.db"))


def test_prepare_rejects_unknown_concurrency_without_external_evidence(tmp_path, capsys):
    from benchmarks.dogfood.migrations import prepare

    _, _, plan = _legacy_bed_evidence(tmp_path)
    with pytest.raises(SystemExit):
        prepare.main(_legacy_prepare_args(tmp_path, plan))
    assert "--bed-provenance" in capsys.readouterr().err
    assert not (tmp_path / "prepared").exists()


@pytest.mark.parametrize("field,value", [
    ("source_bed", "other.db"), ("bed_identity", "f" * 64),
    ("ingest_c", 6), ("tagger_version", 2),
    ("provenance_sources", []), ("source_state", {"db": None, "-wal": None}),
])
def test_prepare_rejects_unbound_or_incomplete_external_evidence(tmp_path, monkeypatch, field, value):
    from benchmarks.dogfood.migrations import prepare

    _, evidence, plan = _legacy_bed_evidence(tmp_path)
    evidence[field] = value
    path = plan["bed_provenance_evidence"]["path"]
    from pathlib import Path
    Path(path).write_text(json.dumps(evidence), encoding="utf-8")
    monkeypatch.setattr(prepare, "code_sha", lambda ref: "a" * 40)
    with pytest.raises(SystemExit):
        prepare.main(_legacy_prepare_args(tmp_path, plan) + ["--bed-provenance", path])
    assert not (tmp_path / "prepared").exists()


def test_capture_verifies_evidence_digest_and_source_state_before_snapshot(tmp_path):
    from pathlib import Path
    from benchmarks.dogfood.migrations.capture import load_bed_evidence

    source, evidence, plan = _legacy_bed_evidence(tmp_path)
    assert load_bed_evidence(plan) == evidence
    path = Path(plan["bed_provenance_evidence"]["path"])
    path.write_text(json.dumps({**evidence, "provenance_sources": ["changed citation"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        load_bed_evidence(plan)
    path.write_text(json.dumps(evidence), encoding="utf-8")
    with source.open("ab") as handle:
        handle.write(b"changed source state")
    with pytest.raises(ValueError, match="source state"):
        load_bed_evidence(plan)
    assert not (tmp_path / "snapshot.db").exists()


def test_snapshot_rechecks_external_state_before_opening_destination(tmp_path):
    from benchmarks.dogfood.migrations.capture import load_bed_evidence, snapshot

    source, _, plan = _legacy_bed_evidence(tmp_path)
    evidence = load_bed_evidence(plan)
    with source.open("ab") as handle:
        handle.write(b"source changed after evidence check")
    target = tmp_path / "snapshot.db"
    with pytest.raises(ValueError, match="source state"):
        snapshot(source, target, expected_state=evidence["source_state"])
    assert not target.exists()


def test_external_evidence_allows_missing_build_stamp_without_writing_one(tmp_path):
    import sqlite3
    from benchmarks.dogfood.migrations.capture import load_bed_evidence, snapshot, verify_snapshot_provenance

    source, _, plan = _legacy_bed_evidence(tmp_path)
    evidence = load_bed_evidence(plan)
    target = tmp_path / "snapshot.db"
    snapshot(source, target)
    conn = sqlite3.connect(target)
    try:
        verify_snapshot_provenance(conn, plan, evidence)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='bed_provenance'").fetchone() is None
        conn.execute("INSERT INTO genes VALUES ('unexpected-document')")
        with pytest.raises(ValueError, match="identity"):
            verify_snapshot_provenance(conn, plan, evidence)
    finally:
        conn.close()


def test_missing_build_stamp_still_requires_external_evidence(tmp_path):
    import sqlite3
    from benchmarks.dogfood.migrations.capture import load_bed_evidence, verify_snapshot_provenance

    source, _, plan = _legacy_bed_evidence(tmp_path)
    del plan["bed_provenance_evidence"]
    with pytest.raises(ValueError, match="unknown.*external"):
        load_bed_evidence(plan)
    plan["pins"]["ingest_c"] = 6
    conn = sqlite3.connect(source)
    try:
        with pytest.raises(ValueError, match="build.*external"):
            verify_snapshot_provenance(conn, plan, None)
    finally:
        conn.close()


@pytest.mark.parametrize("stamp_identity,stamp_concurrency,stamp_tagger", [
    ("f" * 64, 6, 1), (None, 2, 1), (None, 6, 2),
])
def test_external_evidence_cannot_override_conflicting_build_stamp(tmp_path, stamp_identity, stamp_concurrency, stamp_tagger):
    import sqlite3
    from cymatix_context.storage.provenance import create_table
    from benchmarks.dogfood.migrations.capture import verify_snapshot_provenance

    source, evidence, plan = _legacy_bed_evidence(tmp_path, ingest_c=6)
    conn = sqlite3.connect(source)
    try:
        create_table(conn.cursor())
        conn.execute(
            "INSERT INTO bed_provenance (at_utc,event,provenance_version,cymatix_version,"
            "identity_sha256,ingest_c,config_json) VALUES ('2026-09-09','build',1,'test',?,?,?)",
            (stamp_identity or evidence["bed_identity"], stamp_concurrency, json.dumps({"tagger_version": stamp_tagger})),
        )
        with pytest.raises(ValueError, match="does not match"):
            verify_snapshot_provenance(conn, plan, evidence)
    finally:
        conn.close()


def test_external_evidence_fills_only_missing_tagger_on_valid_build(tmp_path):
    import sqlite3
    from cymatix_context.storage.provenance import create_table
    from benchmarks.dogfood.migrations.capture import verify_snapshot_provenance

    source, evidence, plan = _legacy_bed_evidence(tmp_path, ingest_c=6)
    conn = sqlite3.connect(source)
    try:
        create_table(conn.cursor())
        conn.execute(
            "INSERT INTO bed_provenance (at_utc,event,provenance_version,cymatix_version,"
            "identity_sha256,ingest_c,config_json) VALUES ('2026-09-09','build',1,'test',?,?,NULL)",
            (evidence["bed_identity"], 6),
        )
        verify_snapshot_provenance(conn, plan, evidence)
        assert conn.execute("SELECT COUNT(*) FROM bed_provenance").fetchone()[0] == 1
    finally:
        conn.close()


def test_pair_gate_requires_matching_external_evidence_and_receipt_pins():
    off, on = _receipt(), _receipt("tier")
    for receipt in (off, on):
        receipt["pins"]["bed_provenance_sha256"] = "a" * 64
        receipt["bed_provenance_evidence"] = {"path": "external-evidence.json", "sha256": "a" * 64}
    assert _gates().compare(off, on, "tier")["passed"]
    on["pins"]["bed_provenance_sha256"] = "b" * 64
    on["bed_provenance_evidence"]["sha256"] = "b" * 64
    assert not _gates().compare(off, on, "tier")["passed"]
    on["pins"]["bed_provenance_sha256"] = "a" * 64
    assert not _gates().compare(off, on, "tier")["passed"]


def test_pair_gate_rejects_unknown_concurrency_without_external_evidence():
    off, on = _receipt(), _receipt("tier")
    for receipt in (off, on):
        receipt["pins"]["ingest_c"] = "unknown"
    assert not _gates().compare(off, on, "tier")["passed"]
