import json
from datetime import date, datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import (
    PinterestAccount,
    PinterestBoard,
    Product,
    SEOGeneration,
    SEOKeywordIntelligence,
    SEOQualityAssessment,
    SEOTrendSeasonalAssessment,
)
from app.models.core import PinCreativeType
from app.services.board_intelligence import recommend_boards_for_generation
from app.services.ai_content import AIContentService
from app.config import settings
from app.services.keyword_intelligence import analyze_keyword_set
from app.services.trend_seasonal import (
    CALENDAR_SOURCE,
    SEASONAL_CALENDAR_VERSION,
    TREND_SEASONAL_VERSION,
    TrendObservation,
    TrendProviderResult,
    _HOLIDAYS,
    _event_date,
    analyze_trend_seasonal_context,
    ensure_trend_seasonal_assessment,
)


def _generation(db, *, seo=None, at=datetime(2026, 11, 20), add_quality=True):
    seo = seo or {
        "primary_keyword": "thanksgiving decor",
        "secondary_keywords": ["thanksgiving table decor", "autumn table ideas"],
        "long_tail_keywords": ["thanksgiving table decor for family dinner"],
        "audience_keywords": ["families", "hosts"],
        "use_case_keywords": ["dinner", "decor"],
        "search_intents": ["aesthetic_style_intent"],
        "creative_angle": "warm autumn family table decor",
    }
    generation = SEOGeneration(
        started_at=at,
        completed_at=at,
        provider="test-provider",
        model_name="test-model",
        prompt_version="test-prompt",
        schema_version="test-schema",
        status="completed",
        output_snapshot={
            "title": "Thanksgiving Decor for a Warm Family Table",
            "description": "Thanksgiving table decor ideas for a warm autumn family dinner.",
            "seo_metadata": seo,
        },
    )
    db.add(generation)
    db.flush()
    intelligence_result = analyze_keyword_set(
        seo,
        title=generation.output_snapshot["title"],
        description=generation.output_snapshot["description"],
    )
    intelligence = SEOKeywordIntelligence(
        seo_generation_id=generation.id,
        computed_at=at,
        algorithm_version="keyword_intelligence_v1",
        status=intelligence_result["status"],
        keyword_items=intelligence_result["keyword_items"],
        candidate_sets=intelligence_result["candidate_sets"],
        quality_summary=intelligence_result["quality_summary"],
        external_signals=intelligence_result["external_signals"],
        signal_origins=intelligence_result["signal_origins"],
    )
    db.add(intelligence)
    if add_quality:
        db.add(SEOQualityAssessment(
            seo_generation_id=generation.id,
            assessed_at=at,
            score_version="seo_score_v1",
            validation_version="seo_validation_v1",
            calculation_type="deterministic_heuristic",
            overall_score=80,
            score_breakdown={"overall": 80},
            validation_status="PASS",
            validation_result={"status": "PASS"},
        ))
    db.flush()
    return generation, intelligence


def _event(key):
    return next(item for item in _HOLIDAYS if item.key == key)


def test_seasons_month_quarter_and_region_unknown_are_calendar_derived():
    with SessionLocal() as db:
        generation, intelligence = _generation(db, at=datetime(2026, 3, 20))
        us = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 3, 20), region_code="US"
        )
        tr = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 7, 2), region_code="TR"
        )
        global_context = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 3, 20)
        )

        assert us["season"]["name"] == "spring"
        assert us["season"]["source_type"] == "calendar_derived"
        assert us["month_quarter"] == {"month": 3, "quarter": 1}
        assert tr["season"]["name"] == "summer"
        assert tr["month_quarter"] == {"month": 7, "quarter": 3}
        assert global_context["season"]["status"] == "unavailable"
        assert global_context["region_code"] == "GLOBAL"
        assert global_context["warnings"] and "region_not_specified_country_specific_calendar_not_applied" in global_context["warnings"]


def test_winter_boundaries_handle_leap_years_without_local_timezone_assumptions():
    with SessionLocal() as db:
        generation, intelligence = _generation(db)
        december = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2023, 12, 31), region_code="US"
        )
        january = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2024, 1, 1), region_code="US"
        )
        assert december["season"]["name"] == "winter"
        assert december["season"]["end_date"] == "2024-02-29"
        assert january["season"]["start_date"] == "2023-12-01"
        assert january["season"]["end_date"] == "2024-02-29"


def test_seasonal_exact_and_semantic_keyword_matches_are_deterministic():
    with SessionLocal() as db:
        generation, intelligence = _generation(db, seo={
            "primary_keyword": "autumn decor",
            "secondary_keywords": ["fall home decor"],
            "search_intents": ["aesthetic_style_intent"],
        }, at=datetime(2026, 10, 4))
        result = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 10, 4), region_code="US"
        )
        again = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 10, 4), region_code="US"
        )
        assert result == again
        assert result["season"]["name"] == "fall"
        assert {item["match_method"] for item in result["season_keyword_matches"]} & {
            "exact_normalized_match", "calendar_phrase_token_match", "shared_semantic_tokens"
        }
        assert result["score"]["origin"] == "computed_calendar_context_not_pinterest_trend_or_ranking_score"


def test_region_isolation_for_us_and_tr_calendar_events():
    with SessionLocal() as db:
        generation, intelligence = _generation(db, at=datetime(2026, 10, 29))
        us = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 10, 31), region_code="US"
        )
        tr = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 10, 31), region_code="TR"
        )
        tr_holiday = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 10, 29), region_code="TR"
        )
        assert any(item["event_key"] == "halloween" for item in us["events"])
        assert not any(item["event_key"] == "halloween" for item in tr["events"])
        assert any(item["event_key"] == "republic_day_tr" for item in tr_holiday["events"])


def test_recurring_date_rules_cover_easter_leap_year_and_us_shopping_dates():
    assert _event_date(_event("easter"), 2024) == date(2024, 3, 31)
    assert _event_date(_event("easter"), 2025) == date(2025, 4, 20)
    assert _event_date(_event("thanksgiving"), 2026) == date(2026, 11, 26)
    assert _event_date(_event("black_friday"), 2026) == date(2026, 11, 27)
    assert _event_date(_event("cyber_monday"), 2026) == date(2026, 11, 30)
    assert _event_date(_event("mothers_day"), 2026) == date(2026, 5, 10)
    assert _event_date(_event("fathers_day"), 2026) == date(2026, 6, 21)


def test_relevance_window_includes_preparation_active_and_cooldown_boundaries():
    thanksgiving = _event("thanksgiving")
    event_day = _event_date(thanksgiving, 2026)
    with SessionLocal() as db:
        generation, intelligence = _generation(db, at=datetime(2026, 10, 1))
        before_start = analyze_trend_seasonal_context(
            generation, intelligence,
            reference_date=event_day - timedelta(days=thanksgiving.preparation_days + 1),
            region_code="US",
        )
        start = analyze_trend_seasonal_context(
            generation, intelligence,
            reference_date=event_day - timedelta(days=thanksgiving.preparation_days),
            region_code="US",
        )
        active = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=event_day, region_code="US"
        )
        cooldown_end = analyze_trend_seasonal_context(
            generation, intelligence,
            reference_date=event_day + timedelta(days=thanksgiving.active_days + thanksgiving.cooldown_days - 1),
            region_code="US",
        )
        outside = analyze_trend_seasonal_context(
            generation, intelligence,
            reference_date=event_day + timedelta(days=thanksgiving.active_days + thanksgiving.cooldown_days),
            region_code="US",
        )

        assert not any(row["event_key"] == "thanksgiving" for row in before_start["events"])
        assert next(row for row in start["events"] if row["event_key"] == "thanksgiving")["window_phase"] == "preparation"
        assert next(row for row in active["events"] if row["event_key"] == "thanksgiving")["window_phase"] == "active"
        assert next(row for row in cooldown_end["events"] if row["event_key"] == "thanksgiving")["window_phase"] == "cooldown"
        assert not any(row["event_key"] == "thanksgiving" for row in outside["events"])


def test_holiday_keyword_matching_intent_audience_use_case_and_opportunity_explanation():
    with SessionLocal() as db:
        generation, intelligence = _generation(db)
        result = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 11, 20), region_code="US"
        )
        thanksgiving = next(row for row in result["events"] if row["event_key"] == "thanksgiving")
        assert thanksgiving["relevance_status"] == "matched"
        assert thanksgiving["keyword_matches"][0]["normalized_keyword"] == "thanksgiving decor"
        assert thanksgiving["metadata_compatibility"]["intent_compatibility"] == 100
        assert thanksgiving["metadata_compatibility"]["audience_compatibility"] == 100
        assert thanksgiving["metadata_compatibility"]["use_case_compatibility"] == 100
        assert any(rec["type"] == "holiday_window_preparation" for rec in result["recommendations"])
        assert result["score"]["components"]["timing_relevance"]["score"] is not None


def test_unrelated_keywords_do_not_match_holiday_or_season_and_scores_are_not_platform_scores():
    with SessionLocal() as db:
        generation, intelligence = _generation(db, seo={
            "primary_keyword": "minimal ceramic vase",
            "secondary_keywords": ["modern pottery"],
            "search_intents": ["product_search"],
        })
        result = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 11, 20), region_code="US"
        )
        thanksgiving = next(row for row in result["events"] if row["event_key"] == "thanksgiving")
        assert thanksgiving["keyword_matches"] == []
        assert result["season_keyword_matches"] == []
        assert result["score"]["origin"].startswith("computed_")
        assert "pinterest" in result["score"]["origin"]


def test_default_provider_never_fabricates_external_trend_values():
    with SessionLocal() as db:
        generation, intelligence = _generation(db)
        result = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 11, 20), region_code="US"
        )
        external = result["external_trends"]
        assert external["status"] == "not_collected"
        assert external["source"] is None
        assert external["signals"] == []
        assert not any("trend_score" in str(item).lower() or "search_volume" in str(item).lower()
                       for item in external["signals"])


def test_mock_provider_statuses_collected_unavailable_stale_and_not_collected_are_distinct():
    class FakeTrendProvider:
        def __init__(self, result):
            self.result = result
            self.calls = []

        def collect_trends(self, keywords, region_code, reference_date):
            self.calls.append((keywords, region_code, reference_date))
            return self.result

    with SessionLocal() as db:
        generation, intelligence = _generation(db)
        collected = FakeTrendProvider(TrendProviderResult(
            status="collected",
            source="test-trend-provider",
            collected_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
            valid_from=date(2026, 11, 1),
            valid_until=date(2026, 11, 30),
            signals=(TrendObservation(
                term="thanksgiving decor", value=12.5, status="collected",
                source="test-trend-provider", collected_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
                valid_until=date(2026, 11, 30),
            ),),
        ))
        collected_result = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 11, 20), region_code="US", trend_provider=collected
        )
        stale = TrendProviderResult(
            status="collected", source="test-trend-provider",
            collected_at=datetime(2026, 10, 1, tzinfo=timezone.utc), valid_until=date(2026, 10, 31),
            signals=(TrendObservation("thanksgiving decor", value=10, source="test-trend-provider",
                                     collected_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                                     valid_until=date(2026, 10, 31)),),
        )
        stale_result = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 11, 20), region_code="US",
            trend_provider=FakeTrendProvider(stale),
        )
        unavailable = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 11, 20), region_code="US",
            trend_provider=FakeTrendProvider(TrendProviderResult(status="unavailable")),
        )
        not_collected = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 11, 20), region_code="US",
            trend_provider=FakeTrendProvider(TrendProviderResult(status="not_collected")),
        )

        assert collected.calls[0][1:] == ("US", date(2026, 11, 20))
        assert collected_result["external_trends"]["status"] == "collected"
        assert collected_result["external_trends"]["signals"][0]["value"] == 12.5
        assert stale_result["external_trends"]["status"] == "stale"
        assert stale_result["external_trends"]["signals"][0]["status"] == "stale"
        assert unavailable["external_trends"]["status"] == "unavailable"
        assert not_collected["external_trends"]["status"] == "not_collected"


def test_provider_errors_and_unsafe_source_text_do_not_leak_secrets_into_snapshots():
    class BrokenProvider:
        def collect_trends(self, *_args):
            raise RuntimeError("Bearer secret-token api_key=private")

    class UnsafeSourceProvider:
        def collect_trends(self, *_args):
            return TrendProviderResult(
                status="collected", source="Bearer-secret-token",
                collected_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
                signals=(TrendObservation("thanksgiving decor", value=17, source="api_key=secret",
                                          collected_at=datetime(2026, 11, 20, tzinfo=timezone.utc)),),
            )

    with SessionLocal() as db:
        generation, intelligence = _generation(db)
        broken = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 11, 20), region_code="US", trend_provider=BrokenProvider()
        )
        unsafe = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 11, 20), region_code="US", trend_provider=UnsafeSourceProvider()
        )
        serialized = repr(broken["external_trends"]) + repr(unsafe["external_trends"])
        assert broken["external_trends"]["status"] == "unavailable"
        assert "secret-token" not in serialized
        assert "api_key" not in serialized
        assert unsafe["external_trends"]["status"] == "unavailable"
        assert unsafe["external_trends"]["signals"] == []


def test_provider_cannot_add_unrequested_terms_or_non_finite_values():
    class UntrustedProvider:
        def collect_trends(self, *_args):
            return TrendProviderResult(
                status="collected", source="safe-provider",
                collected_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
                signals=(
                    TrendObservation("not requested bearer=secret", value=99, source="safe-provider",
                                     collected_at=datetime(2026, 11, 20, tzinfo=timezone.utc)),
                    TrendObservation("thanksgiving decor", value=float("nan"), source="safe-provider",
                                     collected_at=datetime(2026, 11, 20, tzinfo=timezone.utc)),
                ),
            )

    with SessionLocal() as db:
        generation, intelligence = _generation(db)
        result = analyze_trend_seasonal_context(
            generation, intelligence, reference_date=date(2026, 11, 20), region_code="US",
            trend_provider=UntrustedProvider(),
        )
        signals = result["external_trends"]["signals"]
        assert len(signals) == 1
        assert signals[0]["term"] == "thanksgiving decor"
        assert signals[0]["value"] is None
        assert signals[0]["status"] == "unavailable"
        assert "bearer" not in repr(signals).casefold()


def test_generation_assessment_is_versioned_idempotent_and_does_not_recall_provider():
    class CountingProvider:
        def __init__(self):
            self.calls = 0

        def collect_trends(self, *_args):
            self.calls += 1
            return TrendProviderResult(status="not_collected")

    with SessionLocal() as db:
        generation, _ = _generation(db)
        provider = CountingProvider()
        first = ensure_trend_seasonal_assessment(db, generation, region_code="US", trend_provider=provider)
        second = ensure_trend_seasonal_assessment(db, generation, region_code="US", trend_provider=provider)
        db.commit()
        different_region = ensure_trend_seasonal_assessment(db, generation, region_code="TR", trend_provider=provider)
        db.commit()
        assert first.id == second.id
        assert first.algorithm_version == TREND_SEASONAL_VERSION
        assert first.calendar_version == SEASONAL_CALENDAR_VERSION
        assert first.source == CALENDAR_SOURCE
        assert first.source_type == "calendar_derived"
        assert first.calculation_status == "computed"
        assert provider.calls == 2
        assert different_region.id != first.id
        assert db.query(SEOTrendSeasonalAssessment).count() == 2


def test_legacy_or_incomplete_upstream_generations_are_not_backfilled():
    with SessionLocal() as db:
        missing_intelligence, _ = _generation(db, add_quality=True)
        db.query(SEOKeywordIntelligence).filter_by(seo_generation_id=missing_intelligence.id).delete()
        missing_quality, _ = _generation(db, add_quality=False)
        db.flush()
        db.query(SEOQualityAssessment).filter_by(seo_generation_id=missing_quality.id).delete()
        failed = SEOGeneration(
            started_at=datetime(2025, 1, 1), completed_at=datetime(2025, 1, 1),
            provider="legacy", model_name="unknown", prompt_version="legacy", schema_version="legacy",
            status="failed", output_snapshot=None,
        )
        db.add(failed)
        db.flush()

        assert ensure_trend_seasonal_assessment(db, missing_intelligence) is None
        assert ensure_trend_seasonal_assessment(db, missing_quality) is None
        assert ensure_trend_seasonal_assessment(db, failed) is None
        assert db.query(SEOTrendSeasonalAssessment).count() == 0


def test_end_to_end_existing_keyword_quality_board_and_trend_chain():
    with SessionLocal() as db:
        generation, _ = _generation(db, at=datetime(2026, 11, 20))
        account = PinterestAccount(account_name="Calendar test", account_identifier="calendar-test", is_active=True)
        db.add(account)
        db.flush()
        db.add(PinterestBoard(
            account_id=account.id, board_id="thanksgiving-board", name="Thanksgiving Decor",
            description="Family dinner and table decor", source="test_local",
        ))
        db.flush()
        matches = recommend_boards_for_generation(db, generation.id, account_id=account.id)
        assessment = ensure_trend_seasonal_assessment(db, generation, region_code="US")
        db.commit()

        assert generation.keyword_intelligence is not None
        assert generation.quality_assessment is not None
        assert matches
        assert assessment is not None
        assert assessment.seo_generation_id == generation.id
        assert assessment.keyword_matches["events"]
        assert assessment.external_trend_snapshot["status"] == "not_collected"


def test_ai_content_generation_persists_trend_assessment_after_existing_seo_and_board_steps(monkeypatch):
    class MockSEOProvider:
        provider_name = "trend-integration-provider"
        model_name = "mock-seo-only"

        def generate_json(self, _prompt):
            return json.dumps({
                "title": "Thanksgiving Decor for a Warm Family Table",
                "description": "Thanksgiving table decor ideas for a warm autumn family dinner.",
                "call_to_action": "Explore details",
                "seo": {
                    "primary_keyword": "thanksgiving decor",
                    "secondary_keywords": ["family table decor"],
                    "long_tail_keywords": ["thanksgiving decor for family dinner"],
                    "audience_keywords": ["families"],
                    "use_case_keywords": ["dinner", "decor"],
                    "search_intents": ["product_search"],
                    "creative_angle": "warm family table",
                },
            })

    monkeypatch.setattr(settings, "seo_calendar_region", None)
    with SessionLocal() as db:
        product = Product(title="Thanksgiving table decor", description="Family dinner decor")
        db.add(product)
        db.flush()
        creative = AIContentService(db, MockSEOProvider()).generate(
            product, PinCreativeType.PRODUCT_FOCUS, 1
        )[0]
        db.flush()
        generation = db.query(SEOGeneration).filter_by(creative_id=creative.id).one()
        assessment = db.query(SEOTrendSeasonalAssessment).filter_by(
            seo_generation_id=generation.id
        ).one()

        assert generation.keyword_intelligence is not None
        assert generation.quality_assessment is not None
        assert assessment.source == CALENDAR_SOURCE
        assert assessment.region_code == "GLOBAL"
        assert assessment.external_trend_snapshot["status"] == "not_collected"
        assert "region_not_specified_country_specific_calendar_not_applied" in assessment.warnings


def test_assessment_month_quarter_and_date_are_persisted_against_utc_generation_day():
    aware_utc = datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc)
    with SessionLocal() as db:
        generation, _ = _generation(db, at=aware_utc.replace(tzinfo=None))
        assessment = ensure_trend_seasonal_assessment(db, generation, region_code="US")
        db.commit()
        assert assessment.reference_date == date(2026, 1, 1)
        assert assessment.calendar_snapshot["month_quarter"] == {"month": 1, "quarter": 1}
