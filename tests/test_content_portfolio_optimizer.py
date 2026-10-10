from collections import Counter
from datetime import date
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from app.analytics_migrations import upgrade_analytics_schema
from app.database import Base
from app.database import SessionLocal
from app.models import Pin, PinCreative, Product
from app.models.core import PinCreativeSourceType, PinCreativeStatus, PinCreativeType
from app.services.content_portfolio_optimizer import optimize_content_portfolio
from app.services.daily_pin_scheduler import DailyPinScheduler, SCHEDULE_HOURS
from app.services.opportunity_engine import score_opportunity


def _candidate(index, **overrides):
    item = {
        "candidate_id": index,
        "priority_tier": (0, 0),
        "opportunity_score": 70,
        "keyword": f"keyword {index}",
        "cluster": f"cluster {index}",
        "creative_type": "product_focus",
        "creative_angle": f"angle {index}",
        "board": f"board {index % 4}",
        "quality_score": 80,
        "board_fit": 80,
        "seasonal_score": 20,
        "performance_learning": {"status": "unknown", "adjustment": 0},
    }
    item.update(overrides)
    return item


def test_selection_is_deterministic_limited_to_fifteen_and_accepts_short_pool():
    candidates = [_candidate(index) for index in range(24)]
    first = optimize_content_portfolio(candidates)
    second = optimize_content_portfolio(list(reversed(candidates)))
    assert [row["candidate_id"] for row in first["selected"]] == [
        row["candidate_id"] for row in second["selected"]
    ]
    assert len(first["selected"]) == 15
    assert first["summary"]["selected_count"] == 15
    assert first["summary"]["total_candidates"] == 24
    short = optimize_content_portfolio(candidates[:3], target=15)
    assert len(short["selected"]) == 3


def test_keyword_type_angle_and_board_diversity_reduce_repetition():
    candidates = [
        _candidate(index, keyword="same keyword", cluster="same cluster",
                   creative_type="product_focus", creative_angle="same angle", board="same board")
        for index in range(6)
    ] + [
        _candidate(10 + index, keyword=f"other {index}", cluster=f"other {index}",
                   creative_type="lifestyle", creative_angle=f"angle {index}", board=f"board {index}")
        for index in range(6)
    ]
    result = optimize_content_portfolio(candidates, target=6)
    selected = result["selected"]
    assert len({row["keyword"] for row in selected}) > 1
    assert len({row["creative_type"] for row in selected}) > 1
    assert len({row["creative_angle"] for row in selected}) > 1
    assert len({row["board"] for row in selected}) > 1
    assert result["summary"]["keyword_diversity"] > 1


def test_strong_candidate_can_overcome_diversity_and_saturation_are_penalties():
    result = optimize_content_portfolio([
        _candidate(1, opportunity_score=100, keyword="shared", cluster="shared", creative_angle="same"),
        _candidate(2, opportunity_score=99, keyword="shared", cluster="shared", creative_angle="same"),
        _candidate(3, opportunity_score=30, keyword="unique", cluster="unique", creative_angle="different"),
    ], target=2)
    assert [row["candidate_id"] for row in result["selected"]] == [1, 2]
    repeated = result["selected"][1]["portfolio"]
    assert repeated["penalties"]["keyword"] > 0
    assert repeated["penalties"]["cannibalization"] > 0

    saturated = optimize_content_portfolio([
        _candidate(4, opportunity_score=80, keyword="old", saturation={"keyword": 8}),
        _candidate(5, opportunity_score=74, keyword="fresh"),
    ], target=1)
    assert saturated["selected"][0]["candidate_id"] == 5


def test_learning_is_bounded_and_uncertain_is_neutral():
    result = optimize_content_portfolio([
        _candidate(1, performance_learning={"status": "positive", "adjustment": 8}),
        _candidate(2, performance_learning={"status": "negative", "adjustment": -8}),
        _candidate(3, performance_learning={"status": "uncertain", "adjustment": 45}),
    ], target=3)
    by_id = {row["candidate_id"]: row["portfolio"] for row in result["selected"]}
    assert by_id[1]["learning_contribution"] == 8
    assert by_id[2]["learning_contribution"] == -8
    assert by_id[3]["learning_contribution"] == 0
    assert result["summary"]["learned_positive_count"] == 1


def test_learning_contribution_is_applied_once_across_opportunity_and_portfolio_scores():
    score_inputs = {
        "keyword_item": {"quality": {}, "relevance": {}},
        "quality_score": 50,
        "board_score": 50,
        "seasonal_score": 50,
        "creative_type": "product_focus",
        "type_history_count": 0,
        "keyword_overlap": 0,
        "angle_history_count": 0,
        "observed_performance_score": 100,
        "performance_adjustment": 8,
        "performance_status": "positive",
    }
    opportunity_includes_learning = score_opportunity(**score_inputs)
    opportunity_excludes_learning = score_opportunity(**score_inputs, apply_learning=False)
    assert opportunity_includes_learning["score"] - opportunity_excludes_learning["score"] == 8
    assert opportunity_excludes_learning["components"]["performance_learning_adjustment"] == 8
    assert opportunity_excludes_learning["learning_applied"] is False

    selected = optimize_content_portfolio([_candidate(
        90,
        opportunity_score=opportunity_excludes_learning["score"],
        opportunity_components=opportunity_excludes_learning["components"],
        opportunity_learning_applied=opportunity_excludes_learning["learning_applied"],
        performance_learning={"status": "positive", "adjustment": 8},
    )], target=1)["selected"][0]
    assert selected["portfolio"]["final_score"] == opportunity_excludes_learning["score"] + 8
    assert selected["portfolio"]["learning_contribution"] == 8
    assert selected["portfolio"]["score_lineage"]["final_score"] == opportunity_excludes_learning["score"] + 8

    already_applied = optimize_content_portfolio([_candidate(
        91,
        opportunity_score=opportunity_includes_learning["score"],
        opportunity_components=opportunity_includes_learning["components"],
        opportunity_learning_applied=True,
        performance_learning={"status": "positive", "adjustment": 8},
    )], target=1)["selected"][0]
    assert already_applied["portfolio"]["final_score"] == opportunity_includes_learning["score"]
    assert already_applied["portfolio"]["learning_contribution"] == 0


def test_exploration_is_deterministic_and_seasonal_summary_is_explainable():
    candidates = [
        _candidate(index, performance_learning={"status": "insufficient_data", "adjustment": 0},
                   seasonal_score=90, quality_score=90, board_fit=90)
        for index in range(15)
    ]
    first = optimize_content_portfolio(candidates)
    second = optimize_content_portfolio(candidates)
    assert first["summary"]["exploration_count"] == 3
    assert first["summary"]["seasonal_count"] == 15
    assert [row["candidate_id"] for row in first["selected"]] == [
        row["candidate_id"] for row in second["selected"]
    ]
    assert all(row["portfolio"]["selection_reason"] for row in first["selected"])
    assert all("mevsimsel fırsat" in row["portfolio"]["selection_reason"] for row in first["selected"])


def test_recent_history_contributes_saturation_penalty():
    result = optimize_content_portfolio(
        [_candidate(1, keyword="repeated"), _candidate(2, keyword="new")],
        target=1,
        recent_history=[{"keyword": "repeated"} for _ in range(10)],
    )
    assert result["selected"][0]["candidate_id"] == 2


def test_scheduler_persists_portfolio_and_keeps_fixed_schedule(monkeypatch):
    monkeypatch.setattr("app.services.daily_pin_scheduler.enqueue_daily_generation_job", lambda *_: False)
    db = SessionLocal()
    product = Product(title="Portfolio test product", description="Local scheduling fixture")
    db.add(product)
    db.flush()
    creatives = []
    types = list(PinCreativeType)
    for index in range(18):
        creatives.append(PinCreative(
            product_id=product.id,
            creative_type=types[index % len(types)].value,
            title=f"Portfolio creative {index}",
            description="Local scheduler fixture",
            keywords=[f"keyword-{index}"],
            call_to_action="Explore",
            image_path=f"https://images.example.test/portfolio-{index}.jpg",
            source_type=PinCreativeSourceType.MOCKUP.value,
            destination_url="https://example.test/product",
            status=PinCreativeStatus.DRAFT.value,
            generation_key=f"portfolio-test:{product.id}:{index}",
            seo_metadata={"primary_keyword": f"keyword {index}", "creative_angle": f"angle {index}"},
        ))
    db.add_all(creatives)
    db.commit()
    try:
        result = DailyPinScheduler(db).schedule_daily(date(2035, 4, 5))
        assert len(result.prepared) == 15
        assert Counter(pin.scheduled_for.hour for pin in result.prepared) == {
            hour: 3 for hour in SCHEDULE_HOURS
        }
        assert all(pin.portfolio_snapshot["version"] == "smart_content_portfolio_v1" for pin in result.prepared)
        assert all(pin.portfolio_snapshot["final_score"] is not None for pin in result.prepared)
        assert all(pin.portfolio_snapshot["selection_reason"] for pin in result.prepared)
        assert result.portfolio_summary["total_candidates"] == 18
    finally:
        db.query(Pin).filter(Pin.product_id == product.id).delete(synchronize_session=False)
        db.query(PinCreative).filter(PinCreative.product_id == product.id).delete(synchronize_session=False)
        db.delete(product)
        db.commit()
        db.close()


def test_portfolio_snapshot_migration_is_additive_and_idempotent():
    database_path = Path(__file__).parent / f".portfolio-migration-{uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        legacy_tables = [table for table in Base.metadata.sorted_tables if table.name != "pins"]
        Base.metadata.create_all(bind=engine, tables=legacy_tables)
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE pins (id INTEGER PRIMARY KEY, product_id INTEGER, creative_id INTEGER, "
                "title VARCHAR(255) NOT NULL, description TEXT, image_path VARCHAR(2048), "
                "destination_url VARCHAR(2048), status VARCHAR(32) NOT NULL DEFAULT 'draft', "
                "scheduled_for DATETIME, published_at DATETIME, created_at DATETIME)"
            ))
            connection.execute(text(
                "INSERT INTO pins (id, title, status) VALUES (41, 'legacy pin', 'scheduled')"
            ))
        upgrade_analytics_schema(engine)
        upgrade_analytics_schema(engine)
        columns = {column["name"] for column in inspect(engine).get_columns("pins")}
        assert "portfolio_snapshot" in columns
        with engine.connect() as connection:
            row = connection.execute(text(
                "SELECT id, title, portfolio_snapshot FROM pins WHERE id = 41"
            )).one()
        assert tuple(row) == (41, "legacy pin", None)
    finally:
        engine.dispose()
        database_path.unlink(missing_ok=True)
