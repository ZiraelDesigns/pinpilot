from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analytics_migrations import upgrade_analytics_schema
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    AnalyticsSnapshot,
    Experiment,
    ExperimentAssignment,
    ExperimentEvaluation,
    ExperimentEvaluationResult,
    ExperimentVariant,
    Pin,
    PinCreative,
    PinterestAccount,
    Product,
    PublishedPinterestPin,
)
from app.services.experiments import (
    ExperimentConflictError,
    ExperimentInputError,
    add_variant,
    assign_target,
    create_experiment,
    evaluate_experiment,
    transition_experiment,
)


EXPERIMENT_TABLES = {
    "experiments",
    "experiment_variants",
    "experiment_assignments",
    "experiment_evaluations",
    "experiment_evaluation_results",
}


def _creative(db, *, angle="original angle", keyword="original keyword"):
    product = Product(title="Experiment product")
    creative = PinCreative(
        product=product,
        creative_type="product_focus",
        title="Original creative",
        description="Original description",
        keywords=[keyword],
        seo_metadata={"creative_angle": angle, "primary_keyword": keyword, "audience_keywords": ["makers"]},
        call_to_action="Explore",
        source_type="mockup",
        destination_url="https://etsy.example.test/item/1",
        generation_key=f"experiment-{angle}-{keyword}",
    )
    db.add(creative)
    db.flush()
    return creative


def _experiment(db, *, metric="outbound_clicks", name="Listing angle test"):
    experiment = create_experiment(
        db,
        name=name,
        description="Compare presentation approaches",
        hypothesis="Different angles may reach different audiences",
        evaluation_metric=metric,
        status="draft",
        start_at=datetime(2026, 9, 1),
        end_at=datetime(2026, 10, 1),
    )
    first = add_variant(db, experiment.id, name="Variant A", creative_type="product_focus")
    second = add_variant(db, experiment.id, name="Variant B", creative_type="lifestyle")
    transition_experiment(db, experiment.id, "running")
    return experiment, first, second


def _published_pin(db, creative):
    account = db.query(PinterestAccount).filter_by(account_identifier="ab-test-account").one_or_none()
    if account is None:
        account = PinterestAccount(account_name="A/B test account", account_identifier="ab-test-account", is_active=True)
    pin = Pin(
        product=creative.product,
        creative=creative,
        title=creative.title,
        description=creative.description,
        status="published",
    )
    db.add_all([account, pin])
    db.flush()
    publication = PublishedPinterestPin(
        pin=pin,
        account=account,
        external_pin_id=f"experiment-published-{creative.id}",
        published_at=datetime(2026, 9, 5, 12),
        metadata_snapshot=PublishedPinterestPin.capture_metadata(pin),
    )
    db.add(publication)
    db.flush()
    return publication


def _snapshot(db, publication, *, impressions=100, outbound_clicks=10):
    db.add(AnalyticsSnapshot(
        pin_id=publication.pin_id,
        published_pin=publication,
        metric_date=date(2026, 9, 5),
        period_start=datetime(2026, 9, 5),
        period_end=datetime(2026, 9, 6),
        fetched_at=datetime(2026, 9, 6),
        impressions=impressions,
        outbound_clicks=outbound_clicks,
        saves=5,
        pin_clicks=20,
        engagements=30,
    ))
    db.flush()


def test_create_experiment_variant_and_relationships():
    with SessionLocal() as db:
        experiment, first, second = _experiment(db)
        assert experiment.status == "running"
        assert experiment.evaluation_metric == "outbound_clicks"
        assert [variant.name for variant in experiment.variants] == ["Variant A", "Variant B"]
        assert first.experiment_id == second.experiment_id == experiment.id


def test_lifecycle_allows_only_declared_transitions_and_requires_variants():
    with SessionLocal() as db:
        experiment = create_experiment(db, name="Lifecycle", evaluation_metric="impressions")
        with pytest.raises(ExperimentInputError, match="At least two variants"):
            transition_experiment(db, experiment.id, "running")
        first = add_variant(db, experiment.id, name="A")
        second = add_variant(db, experiment.id, name="B")
        assert transition_experiment(db, experiment.id, "running").status == "running"
        assert transition_experiment(db, experiment.id, "paused").status == "paused"
        assert transition_experiment(db, experiment.id, "running").status == "running"
        assert transition_experiment(db, experiment.id, "completed").status == "completed"
        with pytest.raises(ExperimentInputError, match="Invalid experiment status transition"):
            transition_experiment(db, experiment.id, "running")
        with pytest.raises(ExperimentInputError, match="only be added"):
            add_variant(db, experiment.id, name="Too late")
        with pytest.raises(ExperimentInputError):
            create_experiment(db, name="Bad initial state", evaluation_metric="impressions", status="completed")
        assert first.experiment_id == second.experiment_id == experiment.id


def test_sqlite_foreign_keys_are_enabled_and_composite_fk_and_restrict_are_enforced():
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    with SessionLocal() as db:
        experiment_a, variant_a, _ = _experiment(db, name="FK A")
        experiment_b, variant_b, _ = _experiment(db, name="FK B")
        creative = _creative(db)
        db.add(ExperimentAssignment(experiment_id=experiment_a.id, variant_id=variant_b.id,
                                    creative_id=creative.id))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        publication = _published_pin(db, creative)
        assignment = assign_target(db, experiment_id=experiment_a.id, variant_id=variant_a.id,
                                   published_pin_id=publication.id)
        with pytest.raises(IntegrityError):
            db.execute(delete(PublishedPinterestPin).where(PublishedPinterestPin.id == publication.id))
            db.commit()
        db.rollback()
        assert db.get(ExperimentAssignment, assignment.id) is not None


def test_published_pin_can_be_reused_in_another_experiment():
    with SessionLocal() as db:
        first_exp, first_variant, _ = _experiment(db, name="Reuse A")
        second_exp, second_variant, _ = _experiment(db, name="Reuse B")
        publication = _published_pin(db, _creative(db))
        a = assign_target(db, experiment_id=first_exp.id, variant_id=first_variant.id,
                          published_pin_id=publication.id)
        b = assign_target(db, experiment_id=second_exp.id, variant_id=second_variant.id,
                          published_pin_id=publication.id)
        assert a.id != b.id


def test_evaluation_metric_is_validated_without_assuming_provider_support():
    with SessionLocal() as db:
        with pytest.raises(ExperimentInputError):
            create_experiment(db, name="Invalid", evaluation_metric="imaginary_metric")


def test_variant_configuration_rejects_credential_key_names():
    with SessionLocal() as db:
        experiment = create_experiment(db, name="Draft configuration", evaluation_metric="impressions")
        with pytest.raises(ExperimentInputError, match="credential fields"):
            add_variant(db, experiment.id, name="Unsafe", configuration={"nested": {"access_token": "x"}})


def test_assignment_captures_creative_history_and_prevents_duplicate_in_experiment():
    with SessionLocal() as db:
        experiment, variant, _ = _experiment(db)
        creative = _creative(db)
        assignment = assign_target(
            db, experiment_id=experiment.id, variant_id=variant.id, creative_id=creative.id
        )
        creative.creative_type = "lifestyle"
        creative.seo_metadata = {"creative_angle": "changed", "primary_keyword": "changed"}
        creative.product.title = "Changed product title"
        db.commit()
        persisted = db.get(ExperimentAssignment, assignment.id)

        assert persisted.metadata_snapshot["creative_type"] == "product_focus"
        assert persisted.metadata_snapshot["creative_angle"] == "original angle"
        assert persisted.metadata_snapshot["primary_keyword"] == "original keyword"
        assert persisted.metadata_snapshot["product_title"] == "Experiment product"
        with pytest.raises(ExperimentConflictError):
            assign_target(db, experiment_id=experiment.id, variant_id=variant.id, creative_id=creative.id)


def test_same_creative_can_be_assigned_to_a_different_experiment():
    with SessionLocal() as db:
        first_experiment, first_variant, _ = _experiment(db, name="Experiment one")
        second_experiment, second_variant, _ = _experiment(db, name="Experiment two")
        creative = _creative(db)
        first = assign_target(db, experiment_id=first_experiment.id, variant_id=first_variant.id,
                              creative_id=creative.id)
        second = assign_target(db, experiment_id=second_experiment.id, variant_id=second_variant.id,
                               creative_id=creative.id)

        assert first.id != second.id


def test_assignment_rejects_inconsistent_variant_or_missing_and_multiple_targets():
    with SessionLocal() as db:
        experiment, variant, _ = _experiment(db)
        creative = _creative(db)
        other_experiment, _, _ = _experiment(db, name="Other experiment")
        with pytest.raises(ExperimentInputError, match="does not belong"):
            assign_target(db, experiment_id=experiment.id, variant_id=other_experiment.variants[0].id,
                          creative_id=creative.id)
        with pytest.raises(ExperimentInputError, match="Exactly one"):
            assign_target(db, experiment_id=experiment.id, variant_id=variant.id)
        with pytest.raises(ExperimentInputError, match="Exactly one"):
            assign_target(db, experiment_id=experiment.id, variant_id=variant.id,
                          creative_id=creative.id, published_pin_id=1)


def test_published_pin_assignment_captures_publication_metadata():
    with SessionLocal() as db:
        experiment, variant, _ = _experiment(db)
        creative = _creative(db, angle="published angle")
        publication = _published_pin(db, creative)
        assignment = assign_target(db, experiment_id=experiment.id, variant_id=variant.id,
                                   published_pin_id=publication.id)
        assert assignment.metadata_snapshot["creative_angle"] == "published angle"
        assert assignment.metadata_snapshot["destination_url"] == "https://etsy.example.test/item/1"
        assert assignment.metadata_snapshot["published_pin_id"] == publication.id
        assert assignment.assigned_at is not None
        with pytest.raises(ExperimentConflictError):
            assign_target(db, experiment_id=experiment.id, variant_id=variant.id,
                          published_pin_id=publication.id)


def test_evaluation_persists_period_sample_conditions_and_returns_variant_aggregates():
    with SessionLocal() as db:
        experiment, first, second = _experiment(db)
        first_creative = _creative(db, angle="A")
        second_creative = _creative(db, angle="B", keyword="second keyword")
        first_pin = _published_pin(db, first_creative)
        second_pin = _published_pin(db, second_creative)
        assign_target(db, experiment_id=experiment.id, variant_id=first.id, published_pin_id=first_pin.id)
        assign_target(db, experiment_id=experiment.id, variant_id=second.id, published_pin_id=second_pin.id)
        _snapshot(db, first_pin, impressions=100, outbound_clicks=10)
        _snapshot(db, second_pin, impressions=200, outbound_clicks=20)
        db.commit()

        evaluation, results = evaluate_experiment(
            db,
            experiment.id,
            period_start=datetime(2026, 9, 1),
            period_end=datetime(2026, 9, 30, 23, 59),
            notes="First manual review",
        )

        assert evaluation.period_start == datetime(2026, 9, 1)
        assert evaluation.period_end == datetime(2026, 9, 30, 23, 59)
        assert evaluation.sample_size == 2
        assert evaluation.metric_name == "outbound_clicks"
        assert evaluation.calculation_metadata["winner_selection"] is False
        assert [row["metric_value"] for row in results] == [10, 20]
        assert [row["impressions_denominator"] for row in results] == [100, 200]
        assert db.query(ExperimentEvaluation).count() == 1
        assert len(evaluation.snapshot_ids) == 2
        assert db.query(ExperimentEvaluationResult).count() == 2


def test_evaluation_preserves_null_metric_and_distinguishes_real_zero():
    with SessionLocal() as db:
        experiment, variant, second_variant = _experiment(db, metric="outbound_clicks")
        null_creative = _creative(db, angle="null metric")
        zero_creative = _creative(db, angle="zero metric", keyword="zero keyword")
        null_pin = _published_pin(db, null_creative)
        zero_pin = _published_pin(db, zero_creative)
        assign_target(db, experiment_id=experiment.id, variant_id=variant.id, published_pin_id=null_pin.id)
        assign_target(db, experiment_id=experiment.id, variant_id=second_variant.id,
                      published_pin_id=zero_pin.id)
        _snapshot(db, null_pin, outbound_clicks=None)
        _snapshot(db, zero_pin, outbound_clicks=0)
        db.commit()
        evaluation, results = evaluate_experiment(
            db, experiment.id, period_start=datetime(2026, 9, 1), period_end=datetime(2026, 9, 30)
        )

    assert evaluation.sample_size == 1
    assert results[0]["metric_value"] is None
    assert results[0]["sample_size"] == 0
    assert results[1]["metric_value"] == 0, results
    assert results[1]["sample_size"] == 1


def test_evaluation_rejects_invalid_period_and_keeps_snapshot_rows_unchanged():
    with SessionLocal() as db:
        experiment, variant, _ = _experiment(db)
        creative = _creative(db)
        publication = _published_pin(db, creative)
        assign_target(db, experiment_id=experiment.id, variant_id=variant.id, published_pin_id=publication.id)
        _snapshot(db, publication)
        snapshot_id = db.scalar(select(AnalyticsSnapshot.id))
        with pytest.raises(ExperimentInputError, match="positive duration"):
            evaluate_experiment(db, experiment.id, period_start=datetime(2026, 9, 30),
                                period_end=datetime(2026, 9, 1))
        assert db.get(AnalyticsSnapshot, snapshot_id).outbound_clicks == 10
        assert db.query(AnalyticsSnapshot).count() == 1


def test_experiment_list_aggregates_assignments_and_reports_data_availability():
    with SessionLocal() as db:
        experiment, variant, _ = _experiment(db)
        creative = _creative(db)
        publication = _published_pin(db, creative)
        assign_target(db, experiment_id=experiment.id, variant_id=variant.id,
                      published_pin_id=publication.id)
        _snapshot(db, publication)
        db.commit()

        from app.services.experiments import experiment_summaries

        summary = experiment_summaries(db)[0]

    assert summary["variant_count"] == 2
    assert summary["assignment_count"] == 1
    assert summary["published_sample_count"] == 1
    assert summary["observed_sample_count"] == 0
    assert summary["analytics_available"] is False


def test_creative_assignment_resolves_exact_publication_created_later():
    with SessionLocal() as db:
        experiment, variant, _ = _experiment(db)
        creative = _creative(db)
        assign_target(db, experiment_id=experiment.id, variant_id=variant.id, creative_id=creative.id)
        publication = _published_pin(db, creative)
        with pytest.raises(ExperimentConflictError):
            assign_target(db, experiment_id=experiment.id, variant_id=variant.id,
                          published_pin_id=publication.id)
        _snapshot(db, publication, outbound_clicks=7)
        db.commit()
        evaluation, results = evaluate_experiment(db, experiment.id,
            period_start=datetime(2026, 9, 1), period_end=datetime(2026, 9, 30))
        assert evaluation.sample_size == 1
        assert results[0]["published_pin_count"] == 1
        assert results[0]["metric_value"] == 7


def test_overlapping_snapshot_periods_use_one_pin_observation():
    with SessionLocal() as db:
        experiment, variant, _ = _experiment(db)
        publication = _published_pin(db, _creative(db))
        assign_target(db, experiment_id=experiment.id, variant_id=variant.id, published_pin_id=publication.id)
        weekly = AnalyticsSnapshot(pin_id=publication.pin_id, published_pin=publication,
            metric_date=date(2026, 9, 5), period_start=datetime(2026, 9, 1), period_end=datetime(2026, 9, 7),
            fetched_at=datetime(2026, 9, 8), outbound_clicks=100)
        daily = AnalyticsSnapshot(pin_id=publication.pin_id, published_pin=publication,
            metric_date=date(2026, 9, 5), period_start=datetime(2026, 9, 5), period_end=datetime(2026, 9, 6),
            fetched_at=datetime(2026, 9, 9), outbound_clicks=4)
        db.add_all([weekly, daily]); db.flush(); expected_id = daily.id
        evaluation, results = evaluate_experiment(db, experiment.id,
            period_start=datetime(2026, 9, 1), period_end=datetime(2026, 9, 30))
        assert evaluation.sample_size == results[0]["observation_count"] == 1
        assert results[0]["metric_value"] == 4
        assert results[0]["source_snapshot_ids"] == [expected_id]


def test_evaluation_is_frozen_when_new_collector_snapshot_arrives():
    with SessionLocal() as db:
        experiment, variant, _ = _experiment(db)
        publication = _published_pin(db, _creative(db))
        assign_target(db, experiment_id=experiment.id, variant_id=variant.id, published_pin_id=publication.id)
        _snapshot(db, publication, outbound_clicks=3)
        evaluation, results = evaluate_experiment(db, experiment.id,
            period_start=datetime(2026, 9, 1), period_end=datetime(2026, 9, 30))
        original_ids = list(evaluation.snapshot_ids)
        db.add(AnalyticsSnapshot(pin_id=publication.pin_id, published_pin=publication,
            metric_date=date(2026, 9, 6), period_start=datetime(2026, 9, 6), period_end=datetime(2026, 9, 7),
            fetched_at=datetime(2026, 9, 10), outbound_clicks=99))
        db.commit()
        frozen = db.scalar(select(ExperimentEvaluationResult).where(
            ExperimentEvaluationResult.evaluation_id == evaluation.id,
            ExperimentEvaluationResult.variant_id == variant.id))
        assert frozen.selected_metric_value == results[0]["metric_value"] == 3
        assert frozen.source_snapshot_ids == original_ids


def test_evaluation_rejects_creative_publications_from_multiple_accounts():
    with SessionLocal() as db:
        experiment, variant, _ = _experiment(db)
        creative = _creative(db)
        assign_target(db, experiment_id=experiment.id, variant_id=variant.id, creative_id=creative.id)
        publication = _published_pin(db, creative)
        other = PinterestAccount(account_name="Other", account_identifier="other-account", is_active=True)
        db.add(other); db.flush()
        duplicate_publication = PublishedPinterestPin(pin_id=publication.pin_id, account=other,
            external_pin_id="different-external-pin", published_at=datetime(2026, 9, 6),
            metadata_snapshot=dict(publication.metadata_snapshot))
        db.add(duplicate_publication); db.commit()
        with pytest.raises(ExperimentInputError, match="cannot combine Pinterest accounts"):
            evaluate_experiment(db, experiment.id, period_start=datetime(2026, 9, 1),
                                period_end=datetime(2026, 9, 30))


def test_dashboard_renders_frozen_variant_values_null_zero_and_period_scoped_empty_state():
    with SessionLocal() as db:
        experiment, variant, second = _experiment(db)
        null_pub = _published_pin(db, _creative(db, angle="null"))
        zero_pub = _published_pin(db, _creative(db, angle="zero", keyword="zero"))
        assign_target(db, experiment_id=experiment.id, variant_id=variant.id, published_pin_id=null_pub.id)
        assign_target(db, experiment_id=experiment.id, variant_id=second.id, published_pin_id=zero_pub.id)
        _snapshot(db, null_pub, outbound_clicks=None)
        _snapshot(db, zero_pub, outbound_clicks=0)
        evaluation, _ = evaluate_experiment(db, experiment.id, period_start=datetime(2026, 9, 1),
                                             period_end=datetime(2026, 9, 30))
        evaluation_id = evaluation.id
        older = create_experiment(db, name="Old data only", evaluation_metric="impressions")
        old_a = add_variant(db, older.id, name="A"); add_variant(db, older.id, name="B")
        old_pub = _published_pin(db, _creative(db, angle="old"))
        assign_target(db, experiment_id=older.id, variant_id=old_a.id, published_pin_id=old_pub.id)
        db.add(AnalyticsSnapshot(pin_id=old_pub.pin_id, published_pin=old_pub,
            metric_date=date(2026, 1, 1), period_start=datetime(2026, 1, 1), period_end=datetime(2026, 1, 2),
            fetched_at=datetime(2026, 1, 2), impressions=10))
        db.commit()
        experiment_id = experiment.id
    with TestClient(app) as client:
        html = client.get("/").text
        detail = client.get(f"/experiments/{experiment_id}").json()
    assert "Değerlendirme için analiz verisi yok" in html
    assert "Veri yok" in html and ">0<" in html
    assert detail["evaluations"][0]["id"] == evaluation_id
    assert len(detail["evaluations"][0]["variants"]) == 2


def test_experiment_schema_migration_is_additive_and_idempotent_on_existing_database():
    test_engine = create_engine("sqlite:///:memory:")
    legacy_tables = [table for table in Base.metadata.sorted_tables if table.name not in EXPERIMENT_TABLES]
    Base.metadata.create_all(bind=test_engine, tables=legacy_tables)
    with Session(test_engine) as db:
        db.add(AnalyticsSnapshot(id=71, impressions=0, saves=None, outbound_clicks=4,
                                 recorded_at=datetime(2026, 9, 1)))
        db.commit()

    upgrade_analytics_schema(test_engine)
    upgrade_analytics_schema(test_engine)
    inspector = inspect(test_engine)
    assert EXPERIMENT_TABLES.issubset(set(inspector.get_table_names()))
    with Session(test_engine) as db:
        snapshot = db.get(AnalyticsSnapshot, 71)
        assert snapshot is not None
        assert (snapshot.impressions, snapshot.saves, snapshot.outbound_clicks) == (0, None, 4)
        assert db.query(Experiment).count() == 0
    test_engine.dispose()


def test_experiment_schema_migration_runs_on_clean_database_twice():
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=test_engine)
    upgrade_analytics_schema(test_engine)
    upgrade_analytics_schema(test_engine)
    assert EXPERIMENT_TABLES.issubset(set(inspect(test_engine).get_table_names()))
    test_engine.dispose()


def test_experiment_migration_adds_new_columns_to_legacy_tables_without_losing_rows():
    test_engine = create_engine("sqlite:///:memory:")
    legacy_tables = [table for table in Base.metadata.sorted_tables if table.name not in EXPERIMENT_TABLES]
    Base.metadata.create_all(bind=test_engine, tables=legacy_tables)
    with test_engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TABLE experiments (
                id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, description TEXT, hypothesis TEXT,
                status VARCHAR(16) NOT NULL, evaluation_metric VARCHAR(32) NOT NULL,
                start_at DATETIME, end_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
            )
        """)
        connection.exec_driver_sql("""
            CREATE TABLE experiment_evaluations (
                id INTEGER PRIMARY KEY, experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE RESTRICT,
                evaluated_at DATETIME NOT NULL, period_start DATETIME NOT NULL, period_end DATETIME NOT NULL,
                sample_size INTEGER NOT NULL, metric_name VARCHAR(32) NOT NULL, notes TEXT,
                calculation_metadata JSON NOT NULL
            )
        """)
        connection.exec_driver_sql("INSERT INTO experiments VALUES (501,'Legacy','d','h','completed','impressions',NULL,NULL,'2026-09-01','2026-09-02')")
        connection.exec_driver_sql("INSERT INTO experiment_evaluations VALUES (601,501,'2026-09-03','2026-09-01','2026-09-02',0,'impressions',NULL,'{}')")
    upgrade_analytics_schema(test_engine)
    upgrade_analytics_schema(test_engine)
    with Session(test_engine) as db:
        experiment = db.get(Experiment, 501)
        evaluation = db.get(ExperimentEvaluation, 601)
        assert experiment.name == "Legacy"
        assert experiment.status == "completed"
        assert evaluation.experiment_id == 501
        assert evaluation.snapshot_ids == []
        assert experiment.account_identifier_snapshot is None
    test_engine.dispose()


def test_api_create_variant_assignment_list_detail_and_evaluation():
    with SessionLocal() as db:
        creative = _creative(db)
        db.commit()
        creative_id = creative.id

    with TestClient(app) as client:
        created = client.post("/experiments", json={
            "name": "API experiment",
            "evaluation_metric": "impressions",
            "status": "draft",
        })
        assert created.status_code == 201
        invalid_initial = client.post("/experiments", json={
            "name": "Invalid initial lifecycle",
            "evaluation_metric": "impressions",
            "status": "completed",
        })
        experiment_id = created.json()["id"]
        variant = client.post(f"/experiments/{experiment_id}/variants", json={
            "name": "Treatment A", "creative_angle": "simple", "configuration": {"layout": "plain"}
        })
        assert variant.status_code == 201
        variant_id = variant.json()["id"]
        second_variant = client.post(f"/experiments/{experiment_id}/variants", json={"name": "Treatment B"})
        transitioned = client.patch(f"/experiments/{experiment_id}/status", json={"target_status": "running"})
        assignment = client.post(f"/experiments/{experiment_id}/assignments", json={
            "variant_id": variant_id, "creative_id": creative_id
        })
        assert assignment.status_code == 201
        duplicate = client.post(f"/experiments/{experiment_id}/assignments", json={
            "variant_id": variant_id, "creative_id": creative_id
        })
        assert duplicate.status_code == 409
        listed = client.get("/experiments")
        detailed = client.get(f"/experiments/{experiment_id}")
        evaluation = client.post(f"/experiments/{experiment_id}/evaluations", json={
            "period_start": "2026-09-01T00:00:00",
            "period_end": "2026-09-30T23:59:00",
        })
        invalid_transition = client.patch(f"/experiments/{experiment_id}/status", json={"target_status": "running"})

    assert invalid_initial.status_code == 422
    assert second_variant.status_code == 201
    assert transitioned.status_code == 200 and transitioned.json()["status"] == "running"
    assert invalid_transition.status_code == 400
    assert listed.status_code == 200
    assert listed.json()["items"][0]["name"] == "API experiment"
    assert detailed.status_code == 200
    assert len(detailed.json()["variants"][0]["assignments"]) == 1
    assert evaluation.status_code == 200
    assert evaluation.json()["analytics_data_available"] is False
    assert evaluation.json()["winner_selected"] is False


def test_dashboard_keeps_analytics_sections_and_shows_empty_experiments():
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Pin performans özeti" in response.text
    assert "Hesap performansı" in response.text
    assert "Deneyler" in response.text
    assert "Henüz deney oluşturulmadı." in response.text
