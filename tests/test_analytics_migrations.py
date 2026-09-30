from pathlib import Path
import tempfile
from datetime import datetime

from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app.analytics_migrations import _SNAPSHOT_INDEXES, upgrade_analytics_schema
from app.database import Base
from app.models import AnalyticsSnapshot, SEOGeneration, SEOKeywordIntelligence, SEOQualityAssessment
import app.models  # noqa: F401 - register all mapped tables before create_all.


def test_analytics_migration_runs_on_clean_database_and_is_idempotent():
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'clean.db'}")
        try:
            Base.metadata.create_all(bind=engine)
            upgrade_analytics_schema(engine)
            upgrade_analytics_schema(engine)

            tables = set(inspect(engine).get_table_names())
            assert "published_pinterest_pins" in tables
            assert "pinterest_publish_intents" in tables
            assert "analytics_collection_runs" in tables
            assert "pinterest_account_analytics_snapshots" in tables
            assert "seo_generations" in tables
            assert "seo_keyword_intelligence" in tables
            assert "seo_quality_assessments" in tables
            published_columns = {
                column["name"] for column in inspect(engine).get_columns("published_pinterest_pins")
            }
            assert "seo_generation_id" in published_columns
            columns = {column["name"] for column in inspect(engine).get_columns("analytics_snapshots")}
            assert {
                "pin_id", "impressions", "saves", "outbound_clicks", "recorded_at",
                "published_pin_id", "collection_run_id", "metric_date", "period_start",
                "period_end", "fetched_at", "pin_clicks", "engagements", "engagement_rate",
                "pin_click_rate", "outbound_click_rate", "metric_schema_version",
            } <= columns
        finally:
            engine.dispose()


def test_keyword_intelligence_schema_upgrade_preserves_legacy_seo_generations():
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'legacy-seo.db'}")
        try:
            legacy_tables = [
                table for table in Base.metadata.sorted_tables
                if table.name != "seo_keyword_intelligence"
            ]
            Base.metadata.create_all(bind=engine, tables=legacy_tables)
            with Session(engine) as session:
                session.add(SEOGeneration(
                    id=27,
                    started_at=datetime(2026, 9, 1),
                    completed_at=datetime(2026, 9, 1),
                    provider="legacy-provider",
                    model_name="legacy-model",
                    prompt_version="legacy-prompt",
                    schema_version="legacy-schema",
                    status="completed",
                    output_snapshot={"seo_metadata": {"primary_keyword": "legacy candle"}},
                ))
                session.commit()

            upgrade_analytics_schema(engine)
            upgrade_analytics_schema(engine)

            assert "seo_keyword_intelligence" in inspect(engine).get_table_names()
            with Session(engine) as session:
                preserved = session.get(SEOGeneration, 27)
                assert preserved.output_snapshot == {"seo_metadata": {"primary_keyword": "legacy candle"}}
                assert preserved.prompt_version == "legacy-prompt"
                assert session.query(SEOGeneration).count() == 1
                # Migration must not fabricate analysis/provenance for old rows.
                assert session.query(SEOKeywordIntelligence).count() == 0
                assert session.query(SEOQualityAssessment).count() == 0
        finally:
            engine.dispose()


def test_analytics_migration_preserves_legacy_rows_and_does_not_invent_publications():
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'legacy.db'}")
        try:
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE pins (id INTEGER PRIMARY KEY)"))
                connection.execute(text(
                    "CREATE TABLE analytics_snapshots ("
                    "id INTEGER PRIMARY KEY, pin_id INTEGER, impressions INTEGER NOT NULL, "
                    "saves INTEGER NOT NULL, outbound_clicks INTEGER NOT NULL, recorded_at DATETIME)"
                ))
                connection.execute(text("INSERT INTO pins (id) VALUES (41)"))
                connection.execute(text(
                    "INSERT INTO analytics_snapshots "
                    "(id, pin_id, impressions, saves, outbound_clicks, recorded_at) "
                    "VALUES (7, 41, 120, 9, 3, '2026-09-01 12:00:00')"
                ))

            Base.metadata.create_all(bind=engine)
            upgrade_analytics_schema(engine)
            upgrade_analytics_schema(engine)

            assert "pinterest_publish_intents" in inspect(engine).get_table_names()
            with engine.connect() as connection:
                row = connection.execute(text(
                    "SELECT id, pin_id, impressions, saves, outbound_clicks, published_pin_id, "
                    "collection_run_id FROM analytics_snapshots WHERE id = 7"
                )).one()
                assert tuple(row) == (7, 41, 120, 9, 3, None, None)
                assert connection.execute(text("SELECT COUNT(*) FROM published_pinterest_pins")).scalar_one() == 0
                assert connection.execute(text("SELECT COUNT(*) FROM analytics_snapshots")).scalar_one() == 1
        finally:
            engine.dispose()


def test_stage_one_rebuild_preserves_all_values_indexes_and_unique_constraints():
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'stage-one.db'}")
        try:
            other_tables = [table for table in Base.metadata.sorted_tables if table.name != "analytics_snapshots"]
            Base.metadata.create_all(bind=engine, tables=other_tables)

            source = AnalyticsSnapshot.__table__
            old_metadata = MetaData()
            for foreign_key in source.foreign_keys:
                foreign_key.column.table.to_metadata(old_metadata)
            old_table = source.to_metadata(old_metadata)
            for name in ("impressions", "saves", "outbound_clicks"):
                old_table.c[name].nullable = False
            with engine.begin() as connection:
                connection.execute(CreateTable(old_table))
                for statement in _SNAPSHOT_INDEXES:
                    connection.execute(text(statement))
                connection.execute(text(
                    "INSERT INTO analytics_snapshots ("
                    "id, pin_id, impressions, saves, outbound_clicks, recorded_at, "
                    "published_pin_id, collection_run_id, metric_date, period_start, period_end, "
                    "fetched_at, pin_clicks, engagements, engagement_rate, pin_click_rate, "
                    "outbound_click_rate, metric_schema_version) VALUES ("
                    "39, 41, 0, 7, 0, '2026-09-01 12:00:00', 55, 62, '2026-09-01', "
                    "'2026-08-31 00:00:00', '2026-09-01 00:00:00', '2026-09-01 12:01:00', "
                    "4, 5, 0.5, 0.25, 0.125, 'v1')"
                ))

            upgrade_analytics_schema(engine)
            upgrade_analytics_schema(engine)

            columns = {column["name"]: column for column in inspect(engine).get_columns("analytics_snapshots")}
            assert all(columns[name]["nullable"] for name in ("impressions", "saves", "outbound_clicks"))
            with engine.connect() as connection:
                row = connection.execute(text(
                    "SELECT id, pin_id, impressions, saves, outbound_clicks, recorded_at, "
                    "published_pin_id, collection_run_id, metric_date, period_start, period_end, "
                    "fetched_at, pin_clicks, engagements, engagement_rate, pin_click_rate, "
                    "outbound_click_rate, metric_schema_version FROM analytics_snapshots WHERE id=39"
                )).one()
                assert tuple(row) == (
                    39, 41, 0, 7, 0, "2026-09-01 12:00:00", 55, 62, "2026-09-01",
                    "2026-08-31 00:00:00", "2026-09-01 00:00:00", "2026-09-01 12:01:00",
                    4, 5, 0.5, 0.25, 0.125, "v1",
                )
                indexes = {index["name"]: index for index in inspect(connection).get_indexes("analytics_snapshots")}
                assert indexes["ix_analytics_snapshots_published_pin_id_metric_date"]
                assert indexes["ix_analytics_snapshots_published_period_fetched"]
                assert indexes["uq_analytics_snapshots_run_pin_period"]["unique"] == 1
                assert indexes["uq_analytics_snapshots_published_daily_metric"]["unique"] == 1
                account_indexes = {
                    index["name"]: index
                    for index in inspect(connection).get_indexes("pinterest_account_analytics_snapshots")
                }
                assert account_indexes["uq_account_analytics_pinterest_daily_metric"]["unique"] == 1
                foreign_keys = inspect(connection).get_foreign_keys("analytics_snapshots")
                fk_targets = {foreign_key["referred_table"] for foreign_key in foreign_keys}
                assert {"pins", "published_pinterest_pins", "analytics_collection_runs"} <= fk_targets
                set_null_targets = {
                    foreign_key["referred_table"]
                    for foreign_key in foreign_keys
                    if foreign_key["options"].get("ondelete", "").upper() == "SET NULL"
                }
                assert {"published_pinterest_pins", "analytics_collection_runs"} <= set_null_targets

            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
                try:
                    connection.execute(text(
                        "INSERT INTO analytics_snapshots (id, pin_id, impressions) VALUES (41, 999, 0)"
                    ))
                    connection.commit()
                except IntegrityError:
                    connection.rollback()
                else:
                    raise AssertionError("Snapshot foreign keys must still reject unknown local Pin IDs")

            try:
                with engine.begin() as connection:
                    connection.execute(text(
                        "INSERT INTO analytics_snapshots (id, published_pin_id, collection_run_id, "
                        "period_start, period_end) VALUES "
                        "(40, 55, 62, '2026-08-31 00:00:00', '2026-09-01 00:00:00')"
                    ))
            except IntegrityError:
                pass
            else:
                raise AssertionError("The unique run/publication/period index must remain active")
        finally:
            engine.dispose()


def test_nullable_metrics_persist_null_and_zero_as_distinct_values():
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'nullable.db'}")
        try:
            Base.metadata.create_all(bind=engine)
            upgrade_analytics_schema(engine)
            with Session(engine) as session:
                session.add_all([
                    AnalyticsSnapshot(id=1, impressions=None, saves=0, outbound_clicks=None),
                    AnalyticsSnapshot(id=2, impressions=0, saves=None, outbound_clicks=0),
                ])
                session.commit()
                null_row = session.get(AnalyticsSnapshot, 1)
                zero_row = session.get(AnalyticsSnapshot, 2)
                assert null_row.impressions is None and null_row.saves == 0 and null_row.outbound_clicks is None
                assert zero_row.impressions == 0 and zero_row.saves is None and zero_row.outbound_clicks == 0
        finally:
            engine.dispose()


def test_pipeline_control_and_quota_tables_install_idempotently_with_default_on():
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'pipeline.db'}")
        try:
            Base.metadata.create_all(bind=engine)
            upgrade_analytics_schema(engine)
            upgrade_analytics_schema(engine)
            with engine.connect() as connection:
                tables = set(inspect(connection).get_table_names())
                assert {"ai_pipeline_controls", "ai_daily_quota_slots"} <= tables
                assert connection.execute(text(
                    "SELECT enabled FROM ai_pipeline_controls WHERE id=1"
                )).scalar_one() == 1
                assert connection.execute(text(
                    "SELECT COUNT(*) FROM ai_pipeline_controls"
                )).scalar_one() == 1
        finally:
            engine.dispose()
