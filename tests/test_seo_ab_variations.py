from datetime import date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    AnalyticsSnapshot, Pin, PinCreative, Product, PinterestAccount, PinterestPublishIntent, PublishedPinterestPin,
    PinterestBoardRecommendation, SEOQualityAssessment, SEOTrendSeasonalAssessment,
    SEOABComparison, SEOABExperiment, SEOABVariant, SEOABVariantPublication,
    SEOGeneration, SEOKeywordIntelligence, SEOPerformanceLearning,
)
from app.services.keyword_intelligence import ensure_keyword_intelligence
from app.services.seo_ab_variations import (
    COMPARISON_VERSION, EXPERIMENT_VERSION, VARIATION_VERSION,
    compare_seo_ab_experiment, create_seo_ab_experiment, create_seo_ab_variant,
    link_verified_variant_publication, transition_seo_ab_experiment,
)


def _generation(db):
    product = Product(title="A/B source product", url="https://etsy.example.test/ab-product")
    creative = PinCreative(
        product=product, creative_type="product_focus", title="Botanical candle creative",
        description="A botanical candle for quiet reading.", keywords=["botanical soy candle"],
        seo_metadata={"primary_keyword": "botanical soy candle"}, call_to_action="Explore",
        image_path="https://media.example.test/ab-source.png", source_type="ai",
        destination_url=product.url, generation_key=f"seo-ab-{uuid4().hex}",
    )
    db.add(creative)
    db.flush()
    generation = SEOGeneration(
        product_id=product.id,
        creative_id=creative.id,
        started_at=datetime(2026, 9, 1), completed_at=datetime(2026, 9, 1),
        provider="mock", model_name="mock-model", prompt_version="seo-prompt-v2",
        schema_version="seo-schema-v2", status="completed",
        output_snapshot={
            "title": "Botanical Soy Candle for a Calm Reading Room and Botanical Home Decor",
            "description": "Bring a botanical soy candle into your calm reading room for quiet evenings. Explore this thoughtful botanical home decor accent and view the product details today.",
            "call_to_action": "View product details",
            "seo_metadata": {
                "primary_keyword": "botanical soy candle",
                "secondary_keywords": ["natural wax candle", "calm reading room", "botanical home decor"],
                "long_tail_keywords": ["botanical soy candle for a calm reading room"],
                "audience_keywords": ["thoughtful home shoppers"],
                "use_case_keywords": ["calm reading room"],
                "search_intents": ["product_search"],
                "creative_angle": "botanical reading nook",
            },
        },
    )
    db.add(generation)
    db.flush()
    ensure_keyword_intelligence(db, generation)
    return generation


def test_experiment_variants_are_deterministic_and_preserve_original_provenance():
    with SessionLocal() as db:
        generation = _generation(db)
        original = generation.output_snapshot.copy()
        first = create_seo_ab_experiment(db, generation.id, hypothesis="Measure keyword emphasis", hypothesis_source="human_defined")
        variants = list(first.variants)
        again = create_seo_ab_experiment(db, generation.id, hypothesis="Measure keyword emphasis", hypothesis_source="human_defined")

        assert first.experiment_version == EXPERIMENT_VERSION
        assert first.variation_version == VARIATION_VERSION
        assert again.id == first.id
        assert len(variants) == 4
        assert all(v.variant_type == "KEYWORD_FOCUS" for v in variants)
        assert all(v.provider == "deterministic" for v in variants)
        assert all(v.change_set["new_keywords_created"] is False for v in variants)
        assert all(v.provenance_snapshot["source_generation_id"] == generation.id for v in variants)
        assert all(v.provenance_snapshot["source_keyword_intelligence_id"] is not None for v in variants)
        assert all(v.quality_snapshot["score"]["score_origin"] == "computed_heuristic_not_pinterest_ranking" for v in variants)
        assert generation.output_snapshot == original
        assert db.query(SEOABVariant).filter_by(experiment_id=first.id).count() == 4
        explicit = create_seo_ab_variant(db, first.id, candidate_keyword=" NATURAL   WAX CANDLE ")
        assert explicit.id in {item.id for item in variants}
        assert create_seo_ab_variant(db, first.id, candidate_keyword="natural wax candle").id == explicit.id
        with pytest.raises(ValueError, match="existing valid"):
            create_seo_ab_variant(db, first.id, candidate_keyword="invented candle keyword")
        second_hypothesis = create_seo_ab_experiment(db, generation.id, hypothesis="A distinct human hypothesis")
        assert second_hypothesis.id != first.id


def test_controlled_variant_creation_is_idempotent_and_uses_only_existing_keyword_candidates():
    with SessionLocal() as db:
        generation = _generation(db)
        experiment = create_seo_ab_experiment(
            db, generation.id, hypothesis="Create one controlled candidate", generate_variants=False,
        )
        assert experiment.variants == []
        first = create_seo_ab_variant(
            db, experiment.id, variant_type="KEYWORD_FOCUS", candidate_keyword="natural wax candle",
        )
        repeated = create_seo_ab_variant(
            db, experiment.id, variant_type="KEYWORD_FOCUS", candidate_keyword=" NATURAL   WAX CANDLE ",
        )
        assert first.id == repeated.id
        assert first.output_snapshot["seo_metadata"]["primary_keyword"] == "natural wax candle"
        assert first.change_set["new_keywords_created"] is False
        assert first.quality_snapshot["validation"]["status"] == first.quality_status
        assert db.query(SEOABVariant).filter_by(experiment_id=experiment.id).count() == 1


def test_unsupported_variant_types_are_not_silently_claimed_and_lifecycle_is_guarded():
    with SessionLocal() as db:
        generation = _generation(db)
        experiment = create_seo_ab_experiment(db, generation.id, hypothesis="Test keyword focus")
        with pytest.raises(ValueError, match="Invalid"):
            transition_seo_ab_experiment(db, experiment.id, "RUNNING")
        ready = transition_seo_ab_experiment(db, experiment.id, "READY")
        assert ready.status == "READY"
        assert transition_seo_ab_experiment(db, experiment.id, "RUNNING").status == "RUNNING"
        assert transition_seo_ab_experiment(db, experiment.id, "PAUSED").status == "PAUSED"
        assert transition_seo_ab_experiment(db, experiment.id, "RUNNING").status == "RUNNING"
        with pytest.raises(ValueError, match="analytics are observed"):
            transition_seo_ab_experiment(db, experiment.id, "COMPLETED")
        assert transition_seo_ab_experiment(db, experiment.id, "CANCELLED").status == "CANCELLED"
        with pytest.raises(ValueError, match="Invalid"):
            transition_seo_ab_experiment(db, experiment.id, "RUNNING")


def test_fail_quality_variant_is_not_ready_for_lifecycle():
    with SessionLocal() as db:
        generation = _generation(db)
        generation.output_snapshot = {"title": "", "description": "", "seo_metadata": {
            "primary_keyword": "botanical soy candle", "secondary_keywords": ["natural wax candle"],
            "long_tail_keywords": ["botanical candle for a calm reading room"],
            "search_intents": ["product_search"], "creative_angle": "reading nook",
        }}
        # Stored Keyword Intelligence reflects this snapshot's existing generation and is immutable.
        experiment = create_seo_ab_experiment(db, generation.id, hypothesis="Test only existing keyword")
        assert experiment.variants
        assert all(variant.quality_status == "FAIL" for variant in experiment.variants)
        with pytest.raises(ValueError, match="non-failing"):
            transition_seo_ab_experiment(db, experiment.id, "READY")


def test_experiment_and_variant_snapshots_whitelist_seo_fields_not_credentials():
    with SessionLocal() as db:
        generation = _generation(db)
        generation.output_snapshot["access_token"] = "must-not-be-copied"
        generation.output_snapshot["client_secret"] = "must-not-be-copied"
        generation.output_snapshot["seo_metadata"]["api_key"] = "must-not-be-copied"
        experiment = create_seo_ab_experiment(db, generation.id, hypothesis="Check snapshot safety")
        assert "access_token" not in experiment.source_snapshot
        assert "client_secret" not in experiment.source_snapshot
        assert "api_key" not in experiment.source_snapshot["seo_metadata"]
        assert all("access_token" not in variant.output_snapshot for variant in experiment.variants)
        assert all("client_secret" not in variant.output_snapshot for variant in experiment.variants)
        assert all("api_key" not in variant.output_snapshot["seo_metadata"] for variant in experiment.variants)


def test_source_board_seasonal_and_learning_context_is_traced_without_recalculation():
    with SessionLocal() as db:
        generation = _generation(db)
        source_intelligence_id = generation.keyword_intelligence.id
        quality = SEOQualityAssessment(
            seo_generation_id=generation.id, assessed_at=datetime(2026, 9, 1),
            score_version="seo_full_score_v1", validation_version="seo_validation_v1",
            overall_score=88, score_breakdown={}, validation_status="PASS", validation_result={},
        )
        board = PinterestBoardRecommendation(
            seo_generation_id=generation.id, external_board_id_snapshot="board-x",
            board_name_snapshot="Botanical Decor", source="local", algorithm_version="board_match_v1",
            scope_key="all_accounts", cohort_fingerprint="b" * 64, calculated_at=datetime(2026, 9, 1),
            match_score=80, rank=1, status="recommended", match_breakdown={},
            positive_signals=["shared_keyword"], negative_signals=[],
        )
        seasonal = SEOTrendSeasonalAssessment(
            seo_generation_id=generation.id, reference_date=date(2026, 9, 1), region_code="US",
            assessed_at=datetime(2026, 9, 1), algorithm_version="trend_seasonal_v1",
            calendar_version="calendar_v1", source="calendar", source_type="calendar_derived",
            calculation_status="computed", seasonal_score=20, score_breakdown={}, calendar_snapshot={},
            keyword_matches=[], recommendations=[], warnings=[], external_trend_snapshot={"status": "not_collected"},
        )
        learning = SEOPerformanceLearning(
            window_start=date(2026, 8, 1), window_end=date(2026, 8, 31), calculated_at=datetime(2026, 9, 1),
            algorithm_version="seo_performance_learning_v1", status="insufficient_data", sample_count=2,
            source_fingerprint="c" * 64, source_snapshot_ids=[], calculation_metadata={},
            result_snapshot={"dimensions": {"keyword": []}},
        )
        db.add_all([quality, board, seasonal, learning])
        db.flush()
        experiment = create_seo_ab_experiment(
            db, generation.id, hypothesis="Use prior observed keywords as context",
            performance_learning_ids=[learning.id],
        )
        assert generation.keyword_intelligence.id == source_intelligence_id
        assert experiment.provenance_snapshot["source_quality_assessment_id"] == quality.id
        assert experiment.provenance_snapshot["board_recommendation_ids"] == [board.id]
        assert experiment.provenance_snapshot["seasonal_assessment_ids"] == [seasonal.id]
        assert experiment.provenance_snapshot["performance_learning_context"][0]["signal_type"] == "inferred"
        assert experiment.provenance_snapshot["external_calls"] is False


def test_publication_link_requires_explicit_variant_provenance_and_comparison_is_observed_only():
    with SessionLocal() as db:
        generation = _generation(db)
        experiment = create_seo_ab_experiment(db, generation.id, hypothesis="Compare existing keyword emphasis")
        variant = next(item for item in experiment.variants if item.status == "READY")
        transition_seo_ab_experiment(db, experiment.id, "READY")
        transition_seo_ab_experiment(db, experiment.id, "RUNNING")
        account = PinterestAccount(account_name="A/B test", account_identifier="ab-test", is_active=True)
        db.add(account)
        db.flush()
        baseline_pin = Pin(title="Baseline", description="Baseline Pin")
        variant_pin = Pin(title="Variant", description="Variant Pin")
        second_variant_pin = Pin(title="Variant repeat", description="Variant Pin repeat")
        db.add_all([baseline_pin, variant_pin, second_variant_pin])
        db.flush()
        baseline = PublishedPinterestPin(
            pin=baseline_pin, account=account, external_pin_id="baseline-ab", seo_generation_id=generation.id,
            published_at=datetime(2026, 9, 5), metadata_snapshot={"seo_generation_id": generation.id},
        )
        publication = PublishedPinterestPin(
            pin=variant_pin, account=account, external_pin_id="variant-ab", seo_generation_id=generation.id,
            published_at=datetime(2026, 9, 6), metadata_snapshot={"seo_generation_id": generation.id, "seo_ab_variant_id": variant.id},
        )
        repeated_variant_publication = PublishedPinterestPin(
            pin=second_variant_pin, account=account, external_pin_id="variant-ab-repeat",
            seo_generation_id=generation.id, published_at=datetime(2026, 9, 7),
            metadata_snapshot={"seo_generation_id": generation.id, "seo_ab_variant_id": variant.id},
        )
        db.add_all([baseline, publication, repeated_variant_publication])
        db.flush()
        db.add_all([
            PinterestPublishIntent(
                pin=variant_pin, account=account, published_pin=publication,
                account_identifier_snapshot=account.account_identifier,
                seo_ab_variant_id=variant.id, status="published",
            ),
            PinterestPublishIntent(
                pin=second_variant_pin, account=account, published_pin=repeated_variant_publication,
                account_identifier_snapshot=account.account_identifier,
                seo_ab_variant_id=variant.id, status="published",
            ),
        ])
        db.flush()
        link = link_verified_variant_publication(db, variant.id, publication.id)
        assert link.variant_id == variant.id
        assert link_verified_variant_publication(db, variant.id, publication.id).id == link.id
        link_verified_variant_publication(db, variant.id, repeated_variant_publication.id)
        with pytest.raises(ValueError, match="explicit matching"):
            link_verified_variant_publication(db, variant.id, baseline.id)

        for pub, impressions, outbound in ((baseline, 100, 10), (publication, 100, 20), (repeated_variant_publication, 100, 5)):
            db.add(AnalyticsSnapshot(
                published_pin=pub, metric_date=date(2026, 9, 10), fetched_at=datetime(2026, 9, 11),
                impressions=impressions, saves=5, pin_clicks=None, outbound_clicks=outbound,
                engagements=None, metric_schema_version="pinterest_v5_organic_daily",
            ))
        db.flush()
        first = compare_seo_ab_experiment(db, experiment.id, period_start=date(2026, 9, 1), period_end=date(2026, 9, 30))
        again = compare_seo_ab_experiment(db, experiment.id, period_start=date(2026, 9, 1), period_end=date(2026, 9, 30))
        row = next(item for item in first.result_snapshot["variants"] if item["variant_id"] == variant.id)
        assert again.id == first.id
        assert first.comparison_version == COMPARISON_VERSION
        assert row["metrics"]["outbound_clicks"] == 25
        assert row["metrics"]["pin_clicks"] is None
        assert row["relative_lift"] == 0.25
        assert row["sample_count"] == 2
        assert row["aggregate_metric_value"] == 25
        assert row["metric_value"] == 12.5
        assert row["confidence"] == "insufficient_data"
        assert first.result_snapshot["winner_selected"] is False
        assert first.result_snapshot["decision"].endswith("no_automatic_winner_or_optimization")
        assert first.result_snapshot["cohort_publication_ids"]["baseline_publications"] == [baseline.id]
        assert transition_seo_ab_experiment(db, experiment.id, "COMPLETED").status == "COMPLETED"


def test_null_and_zero_metrics_remain_distinct_and_no_analytics_is_insufficient_data():
    with SessionLocal() as db:
        generation = _generation(db)
        experiment = create_seo_ab_experiment(db, generation.id, hypothesis="Null safety")
        zero = compare_seo_ab_experiment(db, experiment.id, period_start=date(2026, 9, 1), period_end=date(2026, 9, 30))
        assert zero.result_snapshot["status"] == "insufficient_data"
        assert zero.result_snapshot["baseline"]["metrics"]["impressions"] is None
        assert zero.result_snapshot["winner_selected"] is False


def test_comparison_requires_account_scope_when_publications_span_accounts():
    with SessionLocal() as db:
        generation = _generation(db)
        experiment = create_seo_ab_experiment(db, generation.id, hypothesis="Keep account results isolated")
        first_variant = next(item for item in experiment.variants if item.status == "READY")
        second_variant = next(item for item in experiment.variants if item.id != first_variant.id and item.status == "READY")
        transition_seo_ab_experiment(db, experiment.id, "READY")
        transition_seo_ab_experiment(db, experiment.id, "RUNNING")
        accounts = [
            PinterestAccount(account_name="Account A", account_identifier="account-a", is_active=True),
            PinterestAccount(account_name="Account B", account_identifier="account-b", is_active=True),
        ]
        db.add_all(accounts)
        db.flush()
        pins = [Pin(title=f"Pin {index}", description="Pin") for index in range(2)]
        db.add_all(pins)
        db.flush()
        publications = [
            PublishedPinterestPin(pin=pins[index], account=accounts[index], external_pin_id=f"scope-{index}",
                seo_generation_id=generation.id, published_at=datetime(2026, 9, 6),
                metadata_snapshot={"seo_generation_id": generation.id, "seo_ab_variant_id": variant.id})
            for index, variant in enumerate((first_variant, second_variant))
        ]
        db.add_all(publications)
        db.flush()
        db.add_all([
            PinterestPublishIntent(
                pin=pins[index], account=accounts[index], published_pin=publications[index],
                account_identifier_snapshot=accounts[index].account_identifier,
                seo_ab_variant_id=variant.id, status="published",
            )
            for index, variant in enumerate((first_variant, second_variant))
        ])
        db.flush()
        link_verified_variant_publication(db, first_variant.id, publications[0].id)
        link_verified_variant_publication(db, second_variant.id, publications[1].id)
        with pytest.raises(ValueError, match="account scope is required"):
            compare_seo_ab_experiment(
                db, experiment.id, period_start=date(2026, 9, 1), period_end=date(2026, 9, 30)
            )
        scoped = compare_seo_ab_experiment(
            db, experiment.id, period_start=date(2026, 9, 1), period_end=date(2026, 9, 30),
            account_identifier="account-a",
        )
        assert scoped.result_snapshot["account_scope"] == "account-a"
        assert scoped.result_snapshot["cohort_publication_ids"]["variant_publications"][str(first_variant.id)] == [publications[0].id]
        assert scoped.result_snapshot["cohort_publication_ids"]["variant_publications"][str(second_variant.id)] == []


def test_variant_publisher_attribution_analytics_comparison_and_learning_chain_is_observed_only():
    from app.models.core import PinStatus
    from app.services.pinterest_publisher import (
        PinterestPinPublishResult,
        PinterestPublisher,
    )
    from app.services.seo_performance_learning import run_performance_learning

    class FakeProvider:
        publishing_enabled = True

        def __init__(self):
            self.requests = []

        def publish_pin(self, account, request):
            self.requests.append(request)
            return PinterestPinPublishResult("ab-remote-pin", datetime(2026, 9, 12, 10))

    with SessionLocal() as db:
        generation = _generation(db)
        experiment = create_seo_ab_experiment(db, generation.id, hypothesis="Observe a keyword focus variant")
        variant = next(item for item in experiment.variants if item.status == "READY")
        transition_seo_ab_experiment(db, experiment.id, "READY")
        transition_seo_ab_experiment(db, experiment.id, "RUNNING")
        account = PinterestAccount(account_name="A/B publish test", account_identifier="ab-publish-account", is_active=True)
        pin = Pin(
            product_id=generation.product_id, creative_id=generation.creative_id,
            title="Original local title", description="Original local description.",
            image_path="https://media.example.test/ab.png", destination_url="https://etsy.example.test/item",
            status=PinStatus.SCHEDULED.value,
        )
        db.add_all([account, pin])
        db.flush()
        provider = FakeProvider()

        published = PinterestPublisher(db, provider).publish_pin(
            pin.id, account.id, seo_ab_variant_id=variant.id,
        )
        repeated_publish = PinterestPublisher(db, provider).publish_pin(
            pin.id, account.id, seo_ab_variant_id=variant.id,
        )

        assert provider.requests[0].title == variant.output_snapshot["title"]
        assert repeated_publish.id == published.id
        assert len(provider.requests) == 1
        assert published.metadata_snapshot["seo_ab_variant_id"] == variant.id
        assert published.metadata_snapshot["seo_ab_experiment_id"] == experiment.id
        link = db.scalar(select(SEOABVariantPublication).where(
            SEOABVariantPublication.published_pin_id == published.id
        ))
        assert link is not None and link.variant_id == variant.id

        snapshot = AnalyticsSnapshot(
            published_pin_id=published.id, pin_id=pin.id, metric_date=date(2026, 9, 12),
            fetched_at=datetime(2026, 9, 13), impressions=120, saves=12, pin_clicks=None,
            outbound_clicks=9, engagements=None, metric_schema_version="pinterest_v5_organic_daily",
        )
        db.add(snapshot)
        db.flush()
        comparison = compare_seo_ab_experiment(
            db, experiment.id, period_start=date(2026, 9, 1), period_end=date(2026, 9, 30),
        )
        compared_variant = next(item for item in comparison.result_snapshot["variants"] if item["variant_id"] == variant.id)
        assert compared_variant["metrics"]["impressions"] == 120
        assert compared_variant["metrics"]["saves"] == 12
        assert compared_variant["metrics"]["outbound_clicks"] == 9
        assert compared_variant["metrics"]["pin_clicks"] is None
        assert comparison.result_snapshot["winner_selected"] is False

        learning = run_performance_learning(
            db, account_id=account.id,
            window_start=date(2026, 9, 1), window_end=date(2026, 9, 30),
        )
        observation = learning.result_snapshot["observations"][0]
        assert observation["seo_ab_experiment_id"] == experiment.id
        assert observation["seo_ab_variant_id"] == variant.id
        assert observation["seo_ab_variant_key"] == variant.variant_key
        assert variant.variant_key in {row["value"] for row in learning.result_snapshot["dimensions"]["variant"]}
        promoted = variant.provenance_snapshot["source_keyword_candidate"]
        assert promoted in {row["value"] for row in learning.result_snapshot["dimensions"]["keyword"]}
        assert learning.result_snapshot["external_calls"] is False


def test_failed_quality_variant_cannot_be_sent_to_publisher():
    from app.models.core import PinStatus
    from app.services.pinterest_publisher import PinterestPublishRejected, PinterestPublisher

    class NeverProvider:
        publishing_enabled = True

        def publish_pin(self, _account, _request):
            pytest.fail("quality-failing variant must be rejected before provider invocation")

    with SessionLocal() as db:
        generation = _generation(db)
        generation.output_snapshot = {"title": "", "description": "", "seo_metadata": {
            "primary_keyword": "botanical soy candle", "secondary_keywords": ["natural wax candle"],
        }}
        experiment = create_seo_ab_experiment(db, generation.id, hypothesis="Reject failed SEO quality")
        variant = next(item for item in experiment.variants if item.quality_status == "FAIL")
        account = PinterestAccount(account_name="A/B fail gate", account_identifier="ab-fail-gate", is_active=True)
        pin = Pin(title="Local", description="Local", status=PinStatus.SCHEDULED.value)
        db.add_all([account, pin])
        db.flush()
        with pytest.raises(PinterestPublishRejected, match="quality-approved"):
            PinterestPublisher(db, NeverProvider()).publish_pin(
                pin.id, account.id, seo_ab_variant_id=variant.id,
            )
