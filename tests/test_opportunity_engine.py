from datetime import datetime

from starlette.requests import Request

from app.database import SessionLocal
from app.main import dashboard
from app.models import (
    PinCreative, PinterestAccount, PinterestBoardRecommendation, Product, SEOGeneration, SEOKeywordIntelligence,
    SEOQualityAssessment, SEOPerformanceLearning,
)
from app.security import AuthPrincipal
from app.services.opportunity_engine import (
    _account_learning_rows, _learning_signals, _matching_learning_evidence,
    get_next_best_pin_opportunities, score_existing_creative_opportunities, score_opportunity,
)
from app.services.seo_performance_learning import get_performance_learning_dashboard


def _keyword(raw="botanical candle", score=85):
    return {
        "raw": raw,
        "normalized": raw.casefold(),
        "keyword_type": "SECONDARY",
        "valid": True,
        "duplicate": False,
        "relevance": {"score": score, "evidence": []},
        "quality": {"heuristic_quality_score": score},
    }


def _request():
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/", "raw_path": b"/", "query_string": b"", "headers": [],
        "server": ("testserver", 80), "client": ("testclient", 50000),
    })


def test_score_is_deterministic_and_missing_signals_remain_unknown():
    args = dict(
        keyword_item=_keyword(), quality_score=80, board_score=None, seasonal_score=None,
        creative_type="lifestyle", type_history_count=0, keyword_overlap=0,
        angle_history_count=0,
    )
    first = score_opportunity(**args)
    second = score_opportunity(**args)
    assert first == second
    assert first["score_origin"] == "computed_heuristic_not_pinterest_ranking"
    assert first["components"]["board_fit"] is None
    assert first["components"]["seasonal_relevance"] is None
    assert first["components"]["performance_learning_score"] is None
    assert first["performance_status"] == "unknown"


def test_each_available_signal_and_cannibalization_affect_score():
    base = dict(
        keyword_item=_keyword(), quality_score=50, board_score=50, seasonal_score=50,
        creative_type="lifestyle", type_history_count=0, keyword_overlap=0.0,
        angle_history_count=0,
    )
    baseline = score_opportunity(**base)
    stronger = score_opportunity(**{**base, "board_score": 100, "seasonal_score": 100,
                                     "observed_performance_score": 100})
    collided = score_opportunity(**{**base, "keyword_overlap": 1.0, "type_history_count": 2,
                                    "angle_history_count": 1})
    assert stronger["score"] > baseline["score"] > collided["score"]
    assert stronger["performance_status"] == "insufficient_data"
    assert collided["components"]["cannibalization_penalty"] == 40


def test_keyword_board_season_and_creative_history_signals_have_independent_effects():
    base = dict(
        keyword_item=_keyword(score=40), quality_score=50, board_score=40, seasonal_score=20,
        creative_type="lifestyle", type_history_count=1, keyword_overlap=0,
        angle_history_count=0,
    )
    score = score_opportunity(**base)["score"]
    assert score_opportunity(**{**base, "keyword_item": _keyword(score=90)})["score"] > score
    assert score_opportunity(**{**base, "board_score": 90})["score"] > score
    assert score_opportunity(**{**base, "seasonal_score": 90})["score"] > score
    assert score_opportunity(**{**base, "type_history_count": 0})["score"] > score


def test_engine_builds_ranked_opportunities_from_existing_generation():
    with SessionLocal() as db:
        product = Product(title="Botanical candle", description="Handmade soy candle")
        db.add(product)
        db.flush()
        creative = PinCreative(
            product_id=product.id, creative_type="product_focus", title="Botanical candle",
            description="Handmade soy candle", keywords=["botanical candle"],
            seo_metadata={"primary_keyword": "botanical candle", "creative_angle": "product details"},
            call_to_action="Explore", generation_key="opportunity-test-creative", source_type="ai",
        )
        db.add(creative)
        db.flush()
        generation = SEOGeneration(
            product_id=product.id, creative_id=creative.id, started_at=datetime(2026, 1, 1),
            completed_at=datetime(2026, 1, 1), provider="mock", model_name="test",
            prompt_version="test", schema_version="test", status="completed",
            output_snapshot={"seo_metadata": {"primary_keyword": "botanical candle"},
                             "creative_type": "product_focus"},
        )
        db.add(generation)
        db.flush()
        db.add(SEOKeywordIntelligence(
            seo_generation_id=generation.id, computed_at=datetime(2026, 1, 1),
            algorithm_version="test", status="completed", keyword_items=[
                _keyword("botanical candle", 90), _keyword("soy candle gift", 80),
            ], candidate_sets=[], quality_summary={}, external_signals={}, signal_origins={},
        ))
        db.add(SEOQualityAssessment(
            seo_generation_id=generation.id, assessed_at=datetime(2026, 1, 1),
            score_version="test", validation_version="test", calculation_type="computed",
            overall_score=82, score_breakdown={}, validation_status="PASS", validation_result={},
        ))
        db.commit()

        opportunities = get_next_best_pin_opportunities(db)
        assert opportunities
        assert opportunities == sorted(
            opportunities,
            key=lambda item: (-item["score"], item["product_title"].casefold(),
                              item["primary_keyword"].casefold(), item["creative_type"],
                              item["generation_id"]),
        )
        assert all(item["performance_status"] == "unknown" for item in opportunities)
        assert not any(item["creative_type"] == "product_focus"
                       and item["primary_keyword"] == "botanical candle" for item in opportunities)
        response = dashboard(_request(), db=db, principal=AuthPrincipal(
            username="test-admin", role="admin", csrf_token="test-csrf-token"
        ))
        rendered = response.body.decode("utf-8")
        assert "Sıradaki En İyi Pin Fırsatları" in rendered
        assert "soy candle gift" in rendered
        assert "Performans verisi: henüz mevcut değil" in rendered
        assert "Performance Learning" in rendered
        assert "Öğrenme için yeterli veri yok." in rendered


def test_unscoped_opportunities_do_not_select_a_board_from_multiple_accounts():
    with SessionLocal() as db:
        product = Product(title="Account scoped candle", description="Handmade soy candle")
        db.add(product)
        db.flush()
        creative = PinCreative(
            product_id=product.id, creative_type="product_focus", title="Botanical candle",
            description="Handmade soy candle", keywords=["botanical candle"],
            seo_metadata={"primary_keyword": "botanical candle"}, call_to_action="Explore",
            generation_key="opportunity-account-scope", source_type="ai",
        )
        db.add(creative)
        db.flush()
        generation = SEOGeneration(
            product_id=product.id, creative_id=creative.id, started_at=datetime(2026, 1, 1),
            completed_at=datetime(2026, 1, 1), provider="mock", model_name="test",
            prompt_version="test", schema_version="test", status="completed",
            output_snapshot={"seo_metadata": {"primary_keyword": "botanical candle"}},
        )
        db.add(generation)
        first = PinterestAccount(account_name="First", account_identifier="first", is_active=True)
        second = PinterestAccount(account_name="Second", account_identifier="second", is_active=True)
        db.add_all([first, second])
        db.flush()
        db.add(SEOKeywordIntelligence(
            seo_generation_id=generation.id, computed_at=datetime(2026, 1, 1), algorithm_version="test",
            status="completed", keyword_items=[_keyword("soy candle gift", 80)], candidate_sets=[],
            quality_summary={}, external_signals={}, signal_origins={},
        ))
        db.add(SEOQualityAssessment(
            seo_generation_id=generation.id, assessed_at=datetime(2026, 1, 1), score_version="test",
            validation_version="test", calculation_type="computed", overall_score=82,
            score_breakdown={}, validation_status="PASS", validation_result={},
        ))
        db.flush()
        for account, board_name in ((first, "First account board"), (second, "Second account board")):
            db.add(PinterestBoardRecommendation(
                seo_generation_id=generation.id, account_identifier_snapshot=account.account_identifier,
                external_board_id_snapshot=f"board-{account.id}", board_name_snapshot=board_name,
                source="test", algorithm_version="test", scope_key=f"account:{account.id}",
                cohort_fingerprint=str(account.id) * 64, calculated_at=datetime(2026, 1, 1),
                match_score=95, rank=1, status="recommended", match_breakdown={},
                positive_signals=[], negative_signals=[],
            ))
        db.commit()

        unscoped = get_next_best_pin_opportunities(db)
        scoped = get_next_best_pin_opportunities(db, account_id=first.id)

    assert unscoped
    assert all(item["board_name"] is None for item in unscoped)
    assert scoped
    assert {item["board_name"] for item in scoped} == {"First account board"}


def test_dashboard_renders_empty_opportunity_state():
    with SessionLocal() as db:
        response = dashboard(_request(), db=db, principal=AuthPrincipal(
            username="test-admin", role="admin", csrf_token="test-csrf-token"
        ))
    rendered = response.body.decode("utf-8")
    assert "Sıradaki En İyi Pin Fırsatları" in rendered
    assert "Henüz fırsat önerisi oluşturmak için yeterli kayıtlı SEO verisi yok." in rendered


def test_learning_evidence_requires_adequate_account_local_samples_and_is_deterministic():
    insufficient_row = SEOPerformanceLearning(
        id=1, account_id=1, account_identifier_snapshot="account-one",
        window_start=datetime(2026, 1, 1).date(), window_end=datetime(2026, 1, 30).date(),
        calculated_at=datetime(2026, 1, 31), algorithm_version="learning-v1", status="completed",
        sample_count=4, source_fingerprint="a" * 64, source_snapshot_ids=[1, 2],
        result_snapshot={"dimensions": {"keyword": [{
            "value": "botanical candle", "sample_count": 4, "confidence": "low_confidence",
            "signal_status": "positive", "observed_performance_score": 80,
            "source_snapshot_ids": [1, 2],
        }]}}
    )
    sufficient_row = SEOPerformanceLearning(
        id=2, account_id=1, account_identifier_snapshot="account-one",
        window_start=datetime(2026, 1, 1).date(), window_end=datetime(2026, 1, 30).date(),
        calculated_at=datetime(2026, 1, 31), algorithm_version="learning-v1", status="completed",
        sample_count=12, source_fingerprint="b" * 64, source_snapshot_ids=list(range(12)),
        result_snapshot={"dimensions": {"keyword": [{
            "value": "botanical candle", "sample_count": 12, "confidence": "adequate_sample",
            "signal_status": "positive", "observed_performance_score": 75,
            "source_snapshot_ids": list(range(12)),
        }]}}
    )
    weak = _learning_signals([insufficient_row])
    strong = _learning_signals([sufficient_row])
    candidate = {"normalized": "botanical candle", "raw": "Botanical candle"}
    no_evidence = _matching_learning_evidence(
        {}, keyword=candidate, creative_type="lifestyle", angle="everyday style",
        board_id=None, season=None, region_code=None,
    )
    weak_evidence = _matching_learning_evidence(
        weak, keyword=candidate, creative_type="lifestyle", angle="everyday style",
        board_id=None, season=None, region_code=None,
    )
    evidence = _matching_learning_evidence(
        strong, keyword=candidate, creative_type="lifestyle", angle="everyday style",
        board_id=None, season=None, region_code=None,
    )
    assert no_evidence["status"] == "unknown"
    assert weak_evidence["status"] == "insufficient_data"
    assert weak_evidence["adjustment"] == 0
    assert evidence == _matching_learning_evidence(
        strong, keyword=candidate, creative_type="lifestyle", angle="everyday style",
        board_id=None, season=None, region_code=None,
    )
    assert evidence["status"] == "positive"
    assert evidence["adjustment"] == 4.0
    assert evidence["source_snapshot_ids"] == list(range(12))


def test_learning_rows_and_dashboard_are_account_scoped():
    with SessionLocal() as db:
        first = PinterestAccount(account_name="First", account_identifier="account-one", is_active=True)
        second = PinterestAccount(account_name="Second", account_identifier="account-two", is_active=True)
        db.add_all([first, second])
        db.flush()
        for account, suffix in ((first, "a"), (second, "b")):
            db.add(SEOPerformanceLearning(
                account_id=account.id, account_identifier_snapshot=account.account_identifier,
                window_start=datetime(2026, 1, 1).date(), window_end=datetime(2026, 1, 30).date(),
                calculated_at=datetime(2026, 1, 31), algorithm_version="learning-v1", status="completed",
                sample_count=10, source_fingerprint=suffix * 64, source_snapshot_ids=[account.id],
                result_snapshot={"dimensions": {"keyword": [{
                    "value": f"keyword-{suffix}", "sample_count": 10, "confidence": "adequate_sample",
                    "signal_status": "positive", "observed_performance_score": 70,
                    "source_snapshot_ids": [account.id], "metrics": {}, "decayed_rates": {},
                    "baseline_rates": {},
                }]}}
            ))
        db.flush()
        assert [row.account_id for row in _account_learning_rows(db, first.id)] == [first.id]
        summary = get_performance_learning_dashboard(db, account_id=first.id)
        assert summary["status"] == "available"
        assert [item["value"] for item in summary["strategies"]] == ["keyword-a"]
        assert get_performance_learning_dashboard(db)["status"] == "account_selection_required"


def test_opportunity_engine_applies_only_adequate_account_learning_signal():
    with SessionLocal() as db:
        account = PinterestAccount(account_name="First", account_identifier="account-one")
        product = Product(title="Botanical candle", description="Handmade soy candle")
        db.add_all([account, product])
        db.flush()
        creative = PinCreative(
            product_id=product.id, creative_type="product_focus", title="Botanical candle",
            description="Handmade soy candle", keywords=["botanical candle"],
            seo_metadata={"primary_keyword": "botanical candle"}, call_to_action="Explore",
            generation_key="learning-opportunity-creative", source_type="ai",
        )
        db.add(creative)
        db.flush()
        generation = SEOGeneration(
            product_id=product.id, creative_id=creative.id, started_at=datetime(2026, 1, 1),
            completed_at=datetime(2026, 1, 1), provider="mock", model_name="test",
            prompt_version="test", schema_version="test", status="completed",
            output_snapshot={"seo_metadata": {"primary_keyword": "botanical candle"},
                             "creative_type": "lifestyle"},
        )
        db.add(generation)
        db.flush()
        db.add_all([
            SEOKeywordIntelligence(
                seo_generation_id=generation.id, computed_at=datetime(2026, 1, 1),
                algorithm_version="test", status="completed", keyword_items=[_keyword()],
                candidate_sets=[], quality_summary={}, external_signals={}, signal_origins={},
            ),
            SEOQualityAssessment(
                seo_generation_id=generation.id, assessed_at=datetime(2026, 1, 1),
                score_version="test", validation_version="test", calculation_type="computed",
                overall_score=80, score_breakdown={}, validation_status="PASS", validation_result={},
            ),
            SEOPerformanceLearning(
                account_id=account.id, account_identifier_snapshot=account.account_identifier,
                window_start=datetime(2026, 1, 1).date(), window_end=datetime(2026, 1, 30).date(),
                calculated_at=datetime(2026, 1, 31), algorithm_version="learning-v1", status="completed",
                sample_count=10, source_fingerprint="c" * 64, source_snapshot_ids=list(range(10)),
                result_snapshot={"dimensions": {"keyword": [{
                    "value": "botanical candle", "sample_count": 10, "confidence": "adequate_sample",
                    "signal_status": "positive", "observed_performance_score": 80,
                    "source_snapshot_ids": list(range(10)),
                }]}}
            ),
        ])
        db.flush()
        baseline = get_next_best_pin_opportunities(db)
        learned = get_next_best_pin_opportunities(db, account_id=account.id)
        candidate = next(item for item in learned if item["primary_keyword"] == "botanical candle")
        baseline_candidate = next(item for item in baseline if item["primary_keyword"] == "botanical candle")
        assert candidate["performance_status"] == "positive"
        assert candidate["performance_learning"]["source_snapshot_ids"] == list(range(10))
        assert candidate["components"]["performance_learning_adjustment"] == 4.8
        assert candidate["score"] > baseline_candidate["score"]


def test_account_scoped_opportunity_and_scheduler_board_fit_do_not_cross_accounts():
    with SessionLocal() as db:
        first = PinterestAccount(account_name="First", account_identifier="board-scope-one", is_active=True)
        second = PinterestAccount(account_name="Second", account_identifier="board-scope-two", is_active=True)
        product = Product(title="Botanical candle", description="Handmade soy candle")
        db.add_all([first, second, product])
        db.flush()
        creative = PinCreative(
            product_id=product.id, creative_type="product_focus", title="Botanical candle",
            description="Handmade soy candle", keywords=["botanical candle"],
            seo_metadata={"primary_keyword": "botanical candle"}, call_to_action="Explore",
            generation_key="account-board-scope-creative", source_type="ai",
        )
        db.add(creative)
        db.flush()
        generation = SEOGeneration(
            product_id=product.id, creative_id=creative.id, started_at=datetime(2026, 1, 1),
            completed_at=datetime(2026, 1, 1), provider="mock", model_name="test",
            prompt_version="test", schema_version="test", status="completed",
            output_snapshot={"seo_metadata": {"primary_keyword": "botanical candle"},
                             "creative_type": "product_focus"},
        )
        db.add(generation)
        db.flush()
        db.add_all([
            SEOKeywordIntelligence(
                seo_generation_id=generation.id, computed_at=datetime(2026, 1, 1),
                algorithm_version="test", status="completed", keyword_items=[_keyword()],
                candidate_sets=[], quality_summary={}, external_signals={}, signal_origins={},
            ),
            SEOQualityAssessment(
                seo_generation_id=generation.id, assessed_at=datetime(2026, 1, 1),
                score_version="test", validation_version="test", calculation_type="computed",
                overall_score=80, score_breakdown={}, validation_status="PASS", validation_result={},
            ),
            PinterestBoardRecommendation(
                seo_generation_id=generation.id, account_identifier_snapshot=first.account_identifier,
                external_board_id_snapshot="first-board", board_name_snapshot="First account board",
                source="test", algorithm_version="test", scope_key=f"account:{first.id}",
                cohort_fingerprint="a" * 64, calculated_at=datetime(2026, 1, 1),
                match_score=60, rank=1, status="recommended", match_breakdown={},
                positive_signals=[], negative_signals=[],
            ),
            PinterestBoardRecommendation(
                seo_generation_id=generation.id, account_identifier_snapshot=second.account_identifier,
                external_board_id_snapshot="second-board", board_name_snapshot="Second account board",
                source="test", algorithm_version="test", scope_key=f"account:{second.id}",
                cohort_fingerprint="b" * 64, calculated_at=datetime(2026, 1, 1),
                match_score=99, rank=1, status="recommended", match_breakdown={},
                positive_signals=[], negative_signals=[],
            ),
        ])
        db.flush()

        ideas = get_next_best_pin_opportunities(db, account_id=first.id)
        assert ideas
        assert all(item["board_name"] == "First account board" for item in ideas)
        scheduler_scores = score_existing_creative_opportunities(db, [creative], account_id=first.id)
        assert scheduler_scores[creative.id]["board_name"] == "First account board"
