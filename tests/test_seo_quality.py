from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models import (
    Product,
    SEOGeneration,
    SEOKeywordIntelligence,
    SEOQualityAssessment,
)
from app.models.core import PinCreativeType
from app.services.ai_content import AIContentService
from app.services.keyword_intelligence import analyze_keyword_set
from app.services.seo_quality import (
    SEO_SCORE_VERSION,
    SEO_VALIDATION_VERSION,
    calculate_seo_quality,
    ensure_seo_quality_assessment,
)


def _quality_inputs(*, duplicate=False, primary="botanical soy candle", title=None, description=None):
    seo = {
        "primary_keyword": primary,
        "secondary_keywords": ["botanical home decor", "natural wax candle"],
        "long_tail_keywords": ["botanical soy candle for a calm reading room"],
        "audience_keywords": ["thoughtful home shoppers"],
        "use_case_keywords": ["calm reading room"],
        "search_intents": ["product_search"],
        "creative_angle": "botanical reading nook",
    }
    if duplicate:
        seo["secondary_keywords"].append(" BOTANICAL---HOME DECOR ")
    snapshot = {
        "title": title if title is not None else "Botanical Soy Candle for a Calm Reading Room",
        "description": description if description is not None else (
            "Bring a botanical soy candle into your reading room for calm evenings. "
            "Explore a thoughtful home decor accent and discover the candle details today."
        ),
        "call_to_action": "Explore product details",
        "seo_metadata": seo,
    }
    intelligence = analyze_keyword_set(
        seo,
        title="Botanical Soy Candle",
        description="Soy candle for a calm reading room and botanical home decor",
    )
    return snapshot, intelligence


def test_high_quality_seo_has_explainable_deterministic_score_and_passes_validation():
    snapshot, intelligence = _quality_inputs()
    result = calculate_seo_quality(snapshot, intelligence)
    repeated = calculate_seo_quality(snapshot, intelligence)

    assert result == repeated
    assert result["score"]["overall"] >= 70
    assert 0 <= result["score"]["overall"] <= 100
    assert result["score"]["calculation_version"] == SEO_SCORE_VERSION
    assert result["score"]["score_origin"] == "computed_heuristic_not_pinterest_ranking"
    assert set(result["score"]["components"]) == {
        "keyword_score", "title_score", "description_score", "metadata_score",
        "relevance_score", "spam_penalty",
    }
    assert "title_contains_primary_keyword" in result["score"]["positive_signals"]
    assert result["validation"]["status"] == "PASS"
    assert result["validation"]["failed_checks"] == []
    assert result["validation"]["validation_version"] == SEO_VALIDATION_VERSION
    assert "title_within_pinterest_limit" in result["validation"]["passed_checks"]
    assert "description_within_pinterest_limit" in result["validation"]["passed_checks"]


@pytest.mark.parametrize("length", [99, 100])
def test_title_pinterest_limit_accepts_boundary_and_unicode(length):
    prefix = "botanical soy candle "
    title = prefix + "ş" * (length - len(prefix))
    snapshot, intelligence = _quality_inputs(title=title)
    result = calculate_seo_quality(snapshot, intelligence)
    assert "title_too_long" not in result["validation"]["failed_checks"]
    assert "title_within_pinterest_limit" in result["validation"]["passed_checks"]


def test_title_above_pinterest_limit_fails_validation_and_score_gate():
    prefix = "botanical soy candle "
    title = prefix + "ş" * (101 - len(prefix))
    snapshot, intelligence = _quality_inputs(title=title)
    result = calculate_seo_quality(snapshot, intelligence)
    assert result["validation"]["status"] == "FAIL"
    assert "title_too_long" in result["validation"]["failed_checks"]
    assert "title_within_pinterest_limit" not in result["validation"]["passed_checks"]


@pytest.mark.parametrize("length", [799, 800])
def test_description_pinterest_limit_accepts_boundary_and_unicode(length):
    snapshot, intelligence = _quality_inputs()
    description = snapshot["description"]
    snapshot["description"] = description + "ğ" * (length - len(description))
    result = calculate_seo_quality(snapshot, intelligence)
    assert "description_too_long" not in result["validation"]["failed_checks"]
    assert "description_within_pinterest_limit" in result["validation"]["passed_checks"]


def test_description_above_pinterest_limit_fails_validation_and_score_gate():
    snapshot, intelligence = _quality_inputs()
    description = snapshot["description"]
    snapshot["description"] = description + "ğ" * (801 - len(description))
    result = calculate_seo_quality(snapshot, intelligence)
    assert result["validation"]["status"] == "FAIL"
    assert "description_too_long" in result["validation"]["failed_checks"]
    assert "description_within_pinterest_limit" not in result["validation"]["passed_checks"]


def test_missing_title_description_and_primary_produce_fail_errors():
    snapshot, intelligence = _quality_inputs(primary="", title="", description="")
    result = calculate_seo_quality(snapshot, intelligence)

    assert result["validation"]["status"] == "FAIL"
    codes = set(result["validation"]["failed_checks"])
    assert {"missing_title", "missing_description", "missing_primary_keyword"} <= codes
    assert result["score"]["overall"] < 70


def test_missing_required_metadata_and_title_keyword_relevance_fail_validation():
    snapshot, intelligence = _quality_inputs(title="A beautiful item")
    snapshot["seo_metadata"] = {"primary_keyword": "botanical soy candle"}
    result = calculate_seo_quality(snapshot, intelligence)

    assert result["validation"]["status"] == "FAIL"
    codes = set(result["validation"]["failed_checks"])
    assert "title_primary_keyword_mismatch" in codes
    assert "missing_secondary_keywords" in codes
    assert "missing_long_tail_keywords" in codes
    assert "missing_search_intents" in codes


def test_duplicate_semantic_stuffing_and_short_content_produce_warnings_not_rejection():
    snapshot, intelligence = _quality_inputs(
        duplicate=True,
        primary="candle",
        title="Candle",
        description="Candle candle candle candle.",
    )
    result = calculate_seo_quality(snapshot, intelligence)
    warning_codes = {item["code"] for item in result["validation"]["warnings"]}

    assert result["validation"]["status"] == "WARN"
    assert {"duplicate_keywords", "near_duplicate_keywords", "keyword_stuffing", "short_title", "short_description"} & warning_codes
    assert result["score"]["components"]["spam_penalty"] > 0
    assert "low_score_is_not_generation_failure" in result["validation"]


def test_low_relevance_is_reported_and_does_not_become_generation_failure_by_itself():
    snapshot, intelligence = _quality_inputs()
    for item in intelligence["keyword_items"]:
        if item["keyword_type"] in {"PRIMARY", "SECONDARY", "LONG_TAIL"}:
            item["relevance"]["score"] = 0
    result = calculate_seo_quality(snapshot, intelligence)

    assert "low_keyword_relevance" in {warning["code"] for warning in result["validation"]["warnings"]}
    assert "core_keyword_relevance_failure" in result["validation"]["failed_checks"]
    assert result["validation"]["status"] == "FAIL"
    # The service reports a failure result, but generation flow does not raise/retry from it.
    assert isinstance(result["score"]["overall"], int)


class SEOProvider:
    provider_name = "score-test-provider"
    model_name = "mock-only-v1"

    def generate_json(self, _prompt):
        import json

        return json.dumps({
            "title": "Botanical Soy Candle for Calm Evenings",
            "description": "A botanical soy candle for a calm reading room and quiet evenings. Explore product details.",
            "call_to_action": "Explore product details",
            "seo": {
                "primary_keyword": "botanical soy candle",
                "secondary_keywords": ["botanical home decor", "natural wax candle"],
                "long_tail_keywords": ["botanical soy candle for a calm reading room"],
                "audience_keywords": ["thoughtful home shoppers"],
                "use_case_keywords": ["calm reading room"],
                "search_intents": ["product_search"],
                "creative_angle": "botanical reading nook",
            },
        })


def test_generation_flow_persists_keyword_intelligence_then_score_and_validation():
    with SessionLocal() as db:
        product = Product(title="Botanical Soy Candle", description="A candle for a calm reading room")
        db.add(product)
        db.flush()
        creative = AIContentService(db, SEOProvider()).generate(
            product, PinCreativeType.PRODUCT_FOCUS, 1
        )[0]
        db.commit()
        generation = db.query(SEOGeneration).filter_by(creative_id=creative.id).one()
        intelligence = db.query(SEOKeywordIntelligence).filter_by(seo_generation_id=generation.id).one()
        assessment = db.query(SEOQualityAssessment).filter_by(seo_generation_id=generation.id).one()

        assert generation.keyword_intelligence.id == intelligence.id
        assert generation.quality_assessment.id == assessment.id
        assert assessment.assessed_at >= generation.completed_at
        assert assessment.calculation_type == "deterministic_heuristic"
        assert assessment.score_version == SEO_SCORE_VERSION
        assert assessment.validation_version == SEO_VALIDATION_VERSION
        assert assessment.overall_score == assessment.score_breakdown["overall"]
        assert assessment.validation_status == assessment.validation_result["status"]


def test_assessment_is_cached_once_and_legacy_generations_are_not_backfilled():
    with SessionLocal() as db:
        generation = SEOGeneration(
            started_at=datetime(2026, 9, 30),
            completed_at=datetime(2026, 9, 30),
            provider="legacy",
            model_name="unknown",
            prompt_version="unknown",
            schema_version="unknown",
            status="completed",
            output_snapshot=None,
        )
        db.add(generation)
        db.flush()
        intelligence = SEOKeywordIntelligence(
            seo_generation=generation,
            computed_at=datetime(2026, 9, 30),
            algorithm_version="keyword_intelligence_v1",
            status="unavailable",
            keyword_items=[],
            candidate_sets=[],
            quality_summary={},
            external_signals={},
            signal_origins={},
        )
        db.add(intelligence)
        db.flush()
        first = ensure_seo_quality_assessment(db, generation, intelligence)
        second = ensure_seo_quality_assessment(db, generation, intelligence)
        db.commit()

        assert first.id == second.id
        assert first.validation_status == "FAIL"
        assert db.query(SEOQualityAssessment).count() == 1


def test_assessment_payload_excludes_credentials_and_secrets():
    snapshot, intelligence = _quality_inputs()
    snapshot["access_token"] = "private-token-value"
    snapshot["client_secret"] = "private-secret-value"
    result = calculate_seo_quality(snapshot, intelligence)
    serialized = str(result).casefold()

    assert "private-token-value" not in serialized
    assert "private-secret-value" not in serialized
    assert "access_token" not in serialized
    assert "client_secret" not in serialized


def test_generation_has_one_assessment_unique_constraint():
    with SessionLocal() as db:
        generation = SEOGeneration(
            started_at=datetime(2026, 9, 30),
            completed_at=datetime(2026, 9, 30),
            provider="test",
            model_name="test",
            prompt_version="test",
            schema_version="test",
            status="completed",
            output_snapshot={},
        )
        db.add(generation)
        db.flush()
        snapshot, intelligence = _quality_inputs()
        generation.output_snapshot = snapshot
        record = SEOKeywordIntelligence(
            seo_generation=generation,
            computed_at=datetime(2026, 9, 30),
            algorithm_version="keyword_intelligence_v1",
            status="completed",
            keyword_items=intelligence["keyword_items"],
            candidate_sets=[],
            quality_summary=intelligence["quality_summary"],
            external_signals=intelligence["external_signals"],
            signal_origins=intelligence["signal_origins"],
        )
        db.add(record)
        db.flush()
        first = ensure_seo_quality_assessment(db, generation, record)
        db.add(SEOQualityAssessment(
            seo_generation_id=generation.id,
            assessed_at=datetime(2026, 9, 30),
            score_version=SEO_SCORE_VERSION,
            validation_version=SEO_VALIDATION_VERSION,
            calculation_type="deterministic_heuristic",
            overall_score=first.overall_score,
            score_breakdown=first.score_breakdown,
            validation_status=first.validation_status,
            validation_result=first.validation_result,
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        else:
            raise AssertionError("A generation must not accept duplicate quality assessments")
