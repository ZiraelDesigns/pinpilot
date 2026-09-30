from datetime import date, datetime, timedelta

from sqlalchemy import inspect

from app.analytics_migrations import upgrade_analytics_schema
from app.database import SessionLocal, engine
from app.models import (
    AnalyticsSnapshot,
    Pin,
    PinterestAccount,
    PinterestBoard,
    PinterestBoardRecommendation,
    PublishedPinterestPin,
    SEOGeneration,
    SEOKeywordIntelligence,
    SEOQualityAssessment,
    SEOPerformanceLearning,
    SEOTrendSeasonalAssessment,
)
from app.services.seo_performance_learning import (
    PERFORMANCE_LEARNING_VERSION,
    PerformanceLearningService,
)


START = date(2026, 9, 1)
END = date(2026, 9, 30)


def _sample(db, account, *, index=0, with_provenance=True, impressions=100, saves=10, outbound=5,
            day=date(2026, 9, 10), board=None):
    generation = None
    if with_provenance:
        generation = SEOGeneration(
            started_at=datetime(2026, 9, 1), completed_at=datetime(2026, 9, 1),
            provider="mock", model_name="mock-model", prompt_version="v2", schema_version="v2",
            status="completed", output_snapshot={"title": "Fitness Shirt", "description": "Workout shirt",
                "seo_metadata": {"primary_keyword": "gym shirt", "secondary_keywords": ["workout shirt"],
                                 "long_tail_keywords": ["fitness shirt for training"],
                                 "audience_keywords": ["gym fans"], "use_case_keywords": ["training"],
                                 "search_intents": ["product_search"], "creative_angle": "training gear"}},
        )
        db.add(generation)
        db.flush()
        db.add(SEOKeywordIntelligence(
            seo_generation_id=generation.id, computed_at=datetime(2026, 9, 1),
            algorithm_version="keyword_intelligence_v1", status="completed",
            keyword_items=[
                {"raw": "Gym Shirt", "normalized": "gym shirt", "keyword_type": "PRIMARY", "valid": True,
                 "semantic_group": "fitness-clothing", "search_intent": "product_search"},
                {"raw": "Workout Shirt", "normalized": "workout shirt", "keyword_type": "SECONDARY", "valid": True,
                 "semantic_group": "fitness-clothing", "search_intent": "product_search"},
                {"raw": "Gym Fans", "normalized": "gym fans", "keyword_type": "AUDIENCE", "valid": True,
                 "semantic_group": "fitness-audience", "search_intent": "audience_intent"},
                {"raw": "Training", "normalized": "training", "keyword_type": "USE_CASE", "valid": True,
                 "semantic_group": "training", "search_intent": "use_case_intent"},
            ], candidate_sets=[], quality_summary={}, external_signals={}, signal_origins={},
        ))
        db.add(SEOQualityAssessment(
            seo_generation_id=generation.id, assessed_at=datetime(2026, 9, 1), score_version="seo_v1",
            validation_version="validation_v1", overall_score=85, score_breakdown={},
            validation_status="PASS", validation_result={},
        ))
    local_pin = Pin(title="Local Pin", description="A local pin", status="published")
    db.add(local_pin)
    db.flush()
    publication = PublishedPinterestPin(
        pin_id=local_pin.id, account_id=account.id, account_identifier_snapshot=account.account_identifier,
        board_id=board.id if board else None, external_pin_id=f"ext-{index}",
        seo_generation_id=generation.id if generation else None, published_at=datetime(2026, 9, 2),
        metadata_snapshot={"seo_generation_id": generation.id if generation else None},
    )
    db.add(publication)
    db.flush()
    db.add(AnalyticsSnapshot(
        pin_id=local_pin.id, published_pin_id=publication.id, metric_date=day, period_start=datetime.combine(day, datetime.min.time()),
        period_end=datetime.combine(day + timedelta(days=1), datetime.min.time()), fetched_at=datetime(2026, 9, 20),
        impressions=impressions, saves=saves, outbound_clicks=outbound, pin_clicks=3,
        engagements=None, engagement_rate=None, pin_click_rate=None, outbound_click_rate=None,
        metric_schema_version="pinterest_v5_organic_daily",
    ))
    if generation and board:
        db.add(SEOTrendSeasonalAssessment(
            seo_generation_id=generation.id, reference_date=date(2026, 9, 1), region_code="US",
            assessed_at=datetime(2026, 9, 1), algorithm_version="trend_seasonal_v1",
            calendar_version="calendar_v1", source="calendar", source_type="calendar_derived",
            calculation_status="computed", seasonal_score=20, score_breakdown={},
            calendar_snapshot={"season": {"name": "fall"}, "events": [
                {"event_key": "halloween", "keyword_matches": [{"normalized_keyword": "halloween decor"}]}
            ]}, keyword_matches={},
            recommendations=[], warnings=[], external_trend_snapshot={"status": "not_collected"},
        ))
        db.add(PinterestBoardRecommendation(
            seo_generation_id=generation.id, board_id=board.id, board_profile_id=None,
            external_board_id_snapshot="board-external", board_name_snapshot=board.name,
            source="local", algorithm_version="board_match_v1", scope_key="account",
            cohort_fingerprint="a" * 64, calculated_at=datetime(2026, 9, 1), match_score=82,
            rank=1, status="recommended", match_breakdown={}, positive_signals=[], negative_signals=[],
        ))
    return publication


def _run(db, account, *, now=datetime(2026, 9, 30, 12)):
    return PerformanceLearningService().recalculate(
        db, account_id=account.id, window_start=START, window_end=END, now=now
    )


def test_additive_migration_creates_learning_table_idempotently():
    upgrade_analytics_schema(engine)
    upgrade_analytics_schema(engine)
    assert "seo_performance_learnings" in inspect(engine).get_table_names()


def test_learning_aggregates_real_metrics_and_provenance_by_existing_dimensions():
    with SessionLocal() as db:
        account = PinterestAccount(account_name="Test", account_identifier="test-account")
        board = PinterestBoard(account=account, board_id="board-1", name="Fitness Apparel")
        db.add_all([account, board])
        db.flush()
        for index, (impressions, saves) in enumerate(((100, 10), (200, 40), (100, 5))):
            _sample(db, account, index=index, impressions=impressions, saves=saves, board=board)
        db.flush()
        result = _run(db, account)
        assert result.status == "completed"
        assert result.algorithm_version == PERFORMANCE_LEARNING_VERSION
        assert result.sample_count == 3
        assert result.result_snapshot["baseline"]["metrics"]["impressions"] == 400
        assert result.result_snapshot["baseline"]["metrics"]["saves"] == 55
        assert result.result_snapshot["baseline"]["metrics"]["save_rate"] == 0.1375
        assert result.result_snapshot["baseline"]["confidence"] == "low_confidence"
        assert any(item["value"] == "fitness-clothing" for item in result.result_snapshot["dimensions"]["semantic_group"])
        board_result = result.result_snapshot["dimensions"]["board"][0]
        assert board_result["computed_match_signal"] == 82
        assert "observed_performance_signal" in board_result
        observation = result.result_snapshot["observations"][0]
        assert observation["seo_generation_id"] is not None
        assert observation["source_snapshot_ids"]
        assert observation["seasonal_assessment_id"] is not None
        assert observation["external_trend_status"] == "not_collected"
        assert result.result_snapshot["external_calls"] is False
        assert any(item["value"] == "US:fall" for item in result.result_snapshot["dimensions"]["season"])
        assert any(item["value"] == "US:halloween" for item in result.result_snapshot["dimensions"]["holiday"])


def test_null_zero_rates_and_zero_denominator_are_not_conflated():
    with SessionLocal() as db:
        account = PinterestAccount(account_name="Test", account_identifier="test-account")
        db.add(account)
        db.flush()
        _sample(db, account, index=1, impressions=0, saves=0, outbound=0)
        _sample(db, account, index=2, impressions=None, saves=None, outbound=None)
        _sample(db, account, index=3, impressions=100, saves=0, outbound=0)
        db.flush()
        result = _run(db, account)
        by_id = {item["published_pin_id"]: item for item in result.result_snapshot["observations"]}
        zero_snapshot = db.query(AnalyticsSnapshot).filter_by(impressions=0).one()
        zero_values = by_id[zero_snapshot.published_pin_id]["metrics"]
        assert zero_values["impressions"] == 0
        assert zero_values["saves"] == 0
        assert zero_values["save_rate"] is None
        null_snapshot = db.query(AnalyticsSnapshot).filter(AnalyticsSnapshot.impressions.is_(None)).one()
        null_values = by_id[null_snapshot.published_pin_id]["metrics"]
        assert null_values["impressions"] is None
        assert null_values["saves"] is None
        positive_zero = next(item["metrics"] for item in by_id.values() if item["metrics"]["impressions"] == 100)
        assert positive_zero["save_rate"] == 0


def test_latest_per_day_selected_and_repeat_recalculation_is_idempotent():
    with SessionLocal() as db:
        account = PinterestAccount(account_name="Test", account_identifier="test-account")
        db.add(account)
        db.flush()
        publication = _sample(db, account, index=1, impressions=100, saves=5)
        db.flush()
        first_snapshot = db.query(AnalyticsSnapshot).filter_by(published_pin_id=publication.id).one()
        first_snapshot.metric_schema_version = "legacy_periodic"
        db.add(AnalyticsSnapshot(
            pin_id=publication.pin_id, published_pin_id=publication.id, metric_date=date(2026, 9, 10),
            period_start=datetime(2026, 9, 10), period_end=datetime(2026, 9, 11),
            fetched_at=datetime(2026, 9, 21), impressions=100, saves=15, outbound_clicks=5,
            pin_clicks=3, metric_schema_version="legacy_periodic",
        ))
        db.flush()
        first = _run(db, account)
        second = _run(db, account, now=datetime(2026, 10, 1))
        assert first.id == second.id
        assert len(first.source_snapshot_ids) == 1
        assert first.result_snapshot["observations"][0]["metrics"]["saves"] == 15
        assert db.query(SEOPerformanceLearning).count() == 1
        assert db.query(AnalyticsSnapshot).count() == 2


def test_insufficient_data_and_missing_provenance_are_explicit_without_inventing_links():
    with SessionLocal() as db:
        account = PinterestAccount(account_name="Test", account_identifier="test-account")
        db.add(account)
        db.flush()
        _sample(db, account, index=1, with_provenance=False)
        db.flush()
        result = _run(db, account)
        assert result.status == "missing_provenance"
        assert result.sample_count == 1
        assert result.result_snapshot["observations"][0]["seo_generation_id"] is None
        assert result.result_snapshot["observations"][0]["provenance_status"] == "missing_provenance"


def test_unlinked_legacy_snapshot_is_not_attached_to_published_pin():
    with SessionLocal() as db:
        account = PinterestAccount(account_name="Test", account_identifier="test-account")
        db.add(account)
        db.flush()
        publication = _sample(db, account, index=1)
        db.flush()
        snapshot = db.query(AnalyticsSnapshot).filter_by(published_pin_id=publication.id).one()
        snapshot.published_pin_id = None
        db.flush()
        result = _run(db, account)
        assert result.sample_count == 0
        assert result.status == "missing_provenance"
        assert result.source_snapshot_ids == []


def test_same_published_pin_can_be_analyzed_in_later_windows_with_new_source_fingerprint():
    with SessionLocal() as db:
        account = PinterestAccount(account_name="Test", account_identifier="test-account")
        db.add(account)
        db.flush()
        _sample(db, account, index=1)
        db.flush()
        first = _run(db, account)
        later = PerformanceLearningService().recalculate(
            db, account_id=account.id, window_start=date(2026, 9, 5), window_end=END
        )
        assert first.id != later.id
        assert first.source_fingerprint != later.source_fingerprint


def test_account_scope_isolation_and_disconnected_account_identifier_are_supported():
    with SessionLocal() as db:
        first = PinterestAccount(account_name="First", account_identifier="first-account")
        second = PinterestAccount(account_name="Second", account_identifier="second-account")
        db.add_all([first, second])
        db.flush()
        publication = _sample(db, first, index=1)
        _sample(db, second, index=2, impressions=900, saves=900)
        db.flush()
        publication.account_id = None
        db.flush()
        result = PerformanceLearningService().recalculate(
            db, account_identifier="first-account", window_start=START, window_end=END
        )
        assert result.account_id is None
        assert result.sample_count == 1
        assert result.result_snapshot["baseline"]["metrics"]["impressions"] == 100
        assert result.account_identifier_snapshot == "first-account"


def test_snapshot_value_update_changes_fingerprint_without_mutating_old_learning():
    with SessionLocal() as db:
        account = PinterestAccount(account_name="Test", account_identifier="test-account")
        db.add(account)
        db.flush()
        _sample(db, account, index=1)
        db.flush()
        first = _run(db, account)
        snapshot = db.query(AnalyticsSnapshot).one()
        snapshot.saves = 25
        db.flush()
        second = _run(db, account)
        assert first.id != second.id
        assert first.source_fingerprint != second.source_fingerprint
        assert first.result_snapshot["baseline"]["metrics"]["saves"] == 10
        assert second.result_snapshot["baseline"]["metrics"]["saves"] == 25


def test_ten_distinct_pins_are_marked_adequate_and_score_is_baseline_relative():
    with SessionLocal() as db:
        account = PinterestAccount(account_name="Test", account_identifier="test-account")
        db.add(account)
        db.flush()
        for index in range(10):
            _sample(db, account, index=index, impressions=100, saves=10 + index)
        db.flush()
        result = _run(db, account)
        assert result.result_snapshot["baseline"]["confidence"] == "adequate_sample"
        keyword = next(row for row in result.result_snapshot["dimensions"]["keyword"] if row["value"] == "gym shirt")
        assert keyword["confidence"] == "adequate_sample"
        assert keyword["observed_performance_score"] == 50
