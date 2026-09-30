import json
from datetime import datetime

from app.database import SessionLocal
from app.models import (
    AnalyticsSnapshot,
    Pin,
    Product,
    PinterestAccount,
    PublishedPinterestPin,
    SEOGeneration,
    SEOKeywordIntelligence,
)
from app.models.core import PinCreativeType
from app.services.ai_content import AIContentService
from app.services.keyword_intelligence import (
    analyze_keyword_set,
    ensure_keyword_intelligence,
    normalize_keyword,
)


def test_keyword_normalization_handles_case_whitespace_punctuation_and_unicode():
    assert normalize_keyword("  HANDMADE---Soy   Candle! ") == "handmade soy candle"
    assert normalize_keyword("CAFÉ & Tea") == "café tea"
    assert normalize_keyword("!!!   ") is None
    assert normalize_keyword("") is None


def test_keyword_duplicates_keep_raw_to_normalized_mapping_and_type():
    result = analyze_keyword_set({
        "primary_keyword": "Handmade Candle",
        "secondary_keywords": [" handmade   candle! ", "soy candle"],
        "long_tail_keywords": ["handmade candle for calm evenings"],
        "search_intents": ["product_search"],
    }, title="Handmade soy candle", description="Soy candle for calm evenings")

    primary, duplicate, secondary, long_tail = result["keyword_items"]
    assert primary["raw"] == "Handmade Candle"
    assert primary["normalized"] == "handmade candle"
    assert primary["keyword_type"] == "PRIMARY"
    assert duplicate["normalized"] == primary["normalized"]
    assert duplicate["duplicate"] is True and duplicate["duplicate_of"] == 0
    assert secondary["keyword_type"] == "SECONDARY"
    assert long_tail["keyword_type"] == "LONG_TAIL"
    assert primary["search_intent"] == "product_search"


def test_semantic_grouping_is_deterministic_and_does_not_rewrite_keywords():
    seo = {
        "primary_keyword": "gym shirt",
        "secondary_keywords": ["workout shirt", "fitness tee"],
        "long_tail_keywords": [],
    }
    first = analyze_keyword_set(seo)
    second = analyze_keyword_set(seo)
    items = first["keyword_items"]
    assert [item["normalized"] for item in items] == ["gym shirt", "workout shirt", "fitness tee"]
    assert len({item["semantic_group"] for item in items}) == 1
    assert first == second
    assert "semantic_keyword_stuffing_risk" in first["quality_summary"]["validation_flags"]


def test_relevance_quality_and_intent_are_computed_only_from_available_context():
    result = analyze_keyword_set({
        "primary_keyword": "botanical candle gift",
        "secondary_keywords": ["unrelated yacht accessories"],
        "long_tail_keywords": [],
        "search_intents": ["gift_intent", "product_search"],
        "creative_angle": "botanical decor",
    }, title="Botanical soy candle", description="A floral candle for a quiet room", product_tags=["soy candle"])

    relevant, unrelated = result["keyword_items"]
    assert relevant["relevance"]["score"] > unrelated["relevance"]["score"]
    assert relevant["relevance"]["method"] == "deterministic_context_overlap"
    assert relevant["topic_terms"]
    assert relevant["search_intent"] == "gift_intent"
    assert unrelated["search_intent"] == "unknown"
    assert relevant["quality"]["signal_origin"] == "computed"
    assert relevant["quality"]["heuristic_quality_score"] > unrelated["quality"]["heuristic_quality_score"]


def test_empty_long_special_character_and_case_whitespace_inputs_are_safe():
    result = analyze_keyword_set({
        "primary_keyword": "...",
        "secondary_keywords": ["X" * 161, "café—MUG", " Café  Mug "],
        "long_tail_keywords": [],
    })
    items = result["keyword_items"]
    assert items[0]["valid"] is False
    assert items[1]["valid"] is False and items[1]["invalid_reason"] == "too_long"
    assert items[1]["normalized"] == "x" * 161
    assert items[2]["normalized"] == "café mug"
    assert items[3]["normalized"] == items[2]["normalized"] and items[3]["duplicate"] is True


def test_external_volume_popularity_competition_and_trend_are_explicitly_unavailable():
    result = analyze_keyword_set({"primary_keyword": "soy candle"})
    for field in ("search_volume", "popularity", "competition", "trend"):
        assert result["external_signals"][field] == {"status": "not_collected", "value": None, "source": None}
    assert result["signal_origins"]["pinterest_external_data"] == "unavailable_not_collected"


class KeywordProvider:
    provider_name = "mock-keyword-test"
    model_name = "mock-v1"

    def __init__(self):
        self.responses = iter([
            self.response("botanical candle", "quiet reading"),
            self.response("botanical soy candle", "cozy desk"),
        ])

    @staticmethod
    def response(primary, angle):
        return json.dumps({
            "title": f"{primary.title()} for a Calm Home",
            "description": f"Explore {primary} for calm evenings and cozy home decor.",
            "call_to_action": "See details",
            "seo": {
                "primary_keyword": primary,
                "secondary_keywords": ["soy candle", "botanical decor"],
                "long_tail_keywords": [f"{primary} for calm evenings"],
                "audience_keywords": ["thoughtful shoppers"],
                "use_case_keywords": ["calm evenings"],
                "search_intents": ["product_search"],
                "creative_angle": angle,
            },
        })

    def generate_json(self, _prompt):
        return next(self.responses)


def test_generation_persists_keyword_intelligence_and_separate_history_per_generation():
    with SessionLocal() as db:
        product = Product(title="Botanical Candle", description="Soy candle for calm evenings")
        db.add(product)
        db.flush()
        service = AIContentService(db, KeywordProvider())
        creatives = [
            service.generate(product, PinCreativeType.PRODUCT_FOCUS, 1, force_new=True)[0],
            service.generate(product, PinCreativeType.PRODUCT_FOCUS, 1, force_new=True)[0],
        ]
        db.commit()
        generations = db.query(SEOGeneration).order_by(SEOGeneration.id).all()
        records = db.query(SEOKeywordIntelligence).order_by(SEOKeywordIntelligence.id).all()

        assert len(generations) == len(records) == 2
        assert [generation.creative_id for generation in generations] == [creative.id for creative in creatives]
        assert [record.seo_generation_id for record in records] == [generation.id for generation in generations]
        assert all(record.algorithm_version == "keyword_intelligence_v1" for record in records)
        assert all(record.status == "completed" for record in records)
        assert records[0].keyword_items[0]["raw"] == "botanical candle"
        assert records[0].candidate_sets[0]["selection"] == "current_generation_output"
        assert "api_key" not in repr(records[0].keyword_items).casefold()
        assert "token" not in repr(records[0].keyword_items).casefold()


def test_keyword_intelligence_is_cached_per_generation_and_does_not_drift():
    with SessionLocal() as db:
        generation = SEOGeneration(
            started_at=datetime(2026, 9, 30),
            completed_at=datetime(2026, 9, 30),
            provider="legacy",
            model_name="unknown",
            prompt_version="unknown",
            schema_version="unknown",
            status="completed",
            output_snapshot={"title": "Soy candle", "seo_metadata": {"primary_keyword": "soy candle"}},
        )
        db.add(generation)
        db.flush()
        first = ensure_keyword_intelligence(db, generation, title="soy candle")
        original_items = first.keyword_items
        original_time = first.computed_at
        generation.output_snapshot = {"seo_metadata": {"primary_keyword": "changed"}}
        again = ensure_keyword_intelligence(db, generation, title="unrelated")
        db.commit()

        assert again.id == first.id
        assert again.computed_at == original_time
        assert again.keyword_items == original_items
        assert db.query(SEOKeywordIntelligence).count() == 1


def test_legacy_generation_without_seo_snapshot_is_explicitly_unavailable_and_unchanged():
    with SessionLocal() as db:
        generation = SEOGeneration(
            started_at=datetime(2026, 9, 30),
            completed_at=datetime(2026, 9, 30),
            provider="unknown",
            model_name="unknown",
            prompt_version="unknown",
            schema_version="unknown",
            status="completed",
            output_snapshot=None,
        )
        db.add(generation)
        db.flush()
        record = ensure_keyword_intelligence(db, generation)
        db.commit()

        assert record.status == "unavailable"
        assert record.keyword_items == []
        assert generation.output_snapshot is None


def test_keyword_intelligence_generation_remains_reachable_from_published_pin_analytics():
    with SessionLocal() as db:
        product = Product(title="Soy candle", description="Soy candle for a calm evening")
        db.add(product)
        db.flush()
        creative = AIContentService(db, KeywordProvider()).generate(
            product, PinCreativeType.PRODUCT_FOCUS, 1
        )[0]
        db.flush()
        generation = db.query(SEOGeneration).filter_by(creative_id=creative.id).one()
        intelligence = db.query(SEOKeywordIntelligence).filter_by(
            seo_generation_id=generation.id
        ).one()
        pin = Pin(
            product=product,
            creative=creative,
            title=creative.title,
            description=creative.description,
            image_path="https://media.example.test/keyword.png",
        )
        account = PinterestAccount(account_name="keyword test account", is_active=True)
        db.add_all([pin, account])
        db.flush()
        publication = PublishedPinterestPin(
            pin=pin,
            account=account,
            external_pin_id="keyword-intelligence-chain-pin",
            published_at=datetime(2026, 9, 30),
            seo_generation_id=generation.id,
            metadata_snapshot=PublishedPinterestPin.capture_metadata(pin),
        )
        snapshot = AnalyticsSnapshot(
            pin=pin,
            published_pin=publication,
            impressions=0,
            metric_date=datetime(2026, 9, 30).date(),
        )
        db.add(snapshot)
        db.commit()

        persisted = db.get(AnalyticsSnapshot, snapshot.id)
        assert persisted.published_pin.seo_generation.keyword_intelligence.id == intelligence.id
