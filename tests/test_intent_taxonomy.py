"""Intent taxonomy tests (Step 3B, 2026-05-08)."""
import pytest
from helix_context.schemas import PromoterTags, IntentClass
from helix_context.retrieval.intent_router import sub_queries_for


def test_promoter_tags_has_intent_class():
    tags = PromoterTags()
    assert hasattr(tags, "intent_class")
    assert tags.intent_class == IntentClass.UNKNOWN


def test_promoter_tags_existing_fields_unchanged():
    """All pre-existing PromoterTags fields must still work."""
    tags = PromoterTags(
        domains=["helix"],
        entities=["port"],
        intent="The helix proxy listens on port 11437.",
        summary="port config",
    )
    assert tags.domains == ["helix"]
    assert tags.entities == ["port"]
    assert tags.intent == "The helix proxy listens on port 11437."


def test_classify_intent_config_knob():
    from helix_context.tagger import CpuTagger
    t = CpuTagger.__new__(CpuTagger)
    cls = t._classify_intent("bm25_shortlist_size = 50")
    assert cls == IntentClass.CONFIG_KNOB


def test_classify_intent_mechanism():
    from helix_context.tagger import CpuTagger
    t = CpuTagger.__new__(CpuTagger)
    cls = t._classify_intent("How the density gate computes and assigns chromatin state.")
    assert cls == IntentClass.MECHANISM


def test_classify_intent_data_structure():
    from helix_context.tagger import CpuTagger
    t = CpuTagger.__new__(CpuTagger)
    cls = t._classify_intent("CREATE TABLE genes (gene_id TEXT, content TEXT)")
    assert cls == IntentClass.DATA_STRUCTURE


def test_sub_queries_for_mechanism():
    sqs = sub_queries_for("how does the density gate work", IntentClass.MECHANISM)
    assert len(sqs) == 3
    assert all(isinstance(q, str) and len(q) > 5 for q in sqs)


def test_sub_queries_for_unknown_falls_back():
    sqs = sub_queries_for("how does the density gate work", IntentClass.UNKNOWN)
    assert sqs == ["how does the density gate work"]


def test_promoter_tags_deserializes_without_intent_class():
    """Existing genes without intent_class in JSON should deserialize to UNKNOWN."""
    import json
    from pydantic import TypeAdapter
    old_json = json.dumps({
        "domains": ["helix"],
        "entities": ["port"],
        "intent": "old gene",
        "summary": "",
    })
    tags = PromoterTags.model_validate_json(old_json)
    assert tags.intent_class == IntentClass.UNKNOWN
