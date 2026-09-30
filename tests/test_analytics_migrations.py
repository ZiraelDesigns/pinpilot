from pathlib import Path
import tempfile
from datetime import datetime

from sqlalchemy import MetaData, create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app.analytics_migrations import _SNAPSHOT_INDEXES, upgrade_analytics_schema
from app.database import Base
from app.models import (
    AnalyticsSnapshot,
    PinterestBoardRecommendation,
    PinterestBoardSEOProfile,
    SEOGeneration,
    SEOKeywordIntelligence,
    SEOQualityAssessment,
    SEOTrendSeasonalAssessment,
)
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
            assert "seo_trend_seasonal_assessments" in tables
            assert "pinterest_board_seo_profiles" in tables
            assert "pinterest_board_recommendations" in tables
            board_columns = {column["name"] for column in inspect(engine).get_columns("pinterest_boards")}
            assert {"source", "fetched_at", "metadata_version"} <= board_columns
            board_unique_indexes = {
                tuple(index["column_names"])
                for index in inspect(engine).get_indexes("pinterest_boards")
                if index["unique"]
            }
            board_unique_constraints = {
                tuple(item["column_names"])
                for item in inspect(engine).get_unique_constraints("pinterest_boards")
            }
            assert ("account_id", "board_id") in board_unique_indexes | board_unique_constraints
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
                assert session.query(SEOTrendSeasonalAssessment).count() == 0
                assert session.query(PinterestBoardSEOProfile).count() == 0
                assert session.query(PinterestBoardRecommendation).count() == 0
        finally:
            engine.dispose()


def test_pinterest_board_migration_preserves_ids_references_and_scopes_external_id_by_account():
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'legacy-boards.db'}")
        event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    "CREATE TABLE pinterest_accounts (id INTEGER PRIMARY KEY, account_name VARCHAR(255) NOT NULL, "
                    "account_identifier VARCHAR(255), is_active BOOLEAN NOT NULL, created_at DATETIME)"
                ))
                connection.execute(text(
                    "CREATE TABLE pinterest_boards (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL "
                    "REFERENCES pinterest_accounts(id), board_id VARCHAR(64) NOT NULL UNIQUE, "
                    "name VARCHAR(255) NOT NULL, description TEXT, privacy VARCHAR(32), updated_at DATETIME)"
                ))
                connection.execute(text(
                    "CREATE TABLE published_pinterest_pins (id INTEGER PRIMARY KEY, board_id INTEGER "
                    "REFERENCES pinterest_boards(id) ON DELETE SET NULL)"
                ))
                connection.execute(text(
                    "CREATE TABLE pinterest_publish_intents (id INTEGER PRIMARY KEY, board_id INTEGER "
                    "REFERENCES pinterest_boards(id) ON DELETE SET NULL)"
                ))
                connection.execute(text(
                    "INSERT INTO pinterest_accounts (id, account_name, account_identifier, is_active) "
                    "VALUES (1, 'Account One', 'account-one', 1), (2, 'Account Two', 'account-two', 1)"
                ))
                connection.execute(text(
                    "INSERT INTO pinterest_boards (id, account_id, board_id, name, description, updated_at) "
                    "VALUES (17, 1, 'external-board', 'Old board', 'Original description', '2026-01-01')"
                ))
                connection.execute(text("INSERT INTO published_pinterest_pins (id, board_id) VALUES (21, 17)"))
                connection.execute(text("INSERT INTO pinterest_publish_intents (id, board_id) VALUES (31, 17)"))

            upgrade_analytics_schema(engine)
            upgrade_analytics_schema(engine)

            with engine.connect() as connection:
                board = connection.execute(text(
                    "SELECT id, account_id, board_id, name, description, source, fetched_at, metadata_version "
                    "FROM pinterest_boards WHERE id=17"
                )).one()
                assert tuple(board) == (
                    17, 1, "external-board", "Old board", "Original description", None, None, None
                )
                assert connection.execute(text(
                    "SELECT board_id FROM published_pinterest_pins WHERE id=21"
                )).scalar_one() == 17
                assert connection.execute(text(
                    "SELECT board_id FROM pinterest_publish_intents WHERE id=31"
                )).scalar_one() == 17
                assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
                board_sql = connection.execute(text(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='pinterest_boards'"
                )).scalar_one()
                assert "UNIQUE (account_id, board_id)" in board_sql or "UNIQUE(account_id, board_id)" in board_sql
                assert "board_id VARCHAR(64) NOT NULL UNIQUE" not in board_sql

            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO pinterest_boards (id, account_id, board_id, name, updated_at) "
                    "VALUES (18, 2, 'external-board', 'Same external ID, other account', '2026-01-02')"
                ))
            with engine.begin() as connection:
                try:
                    connection.execute(text(
                        "INSERT INTO pinterest_boards (id, account_id, board_id, name, updated_at) "
                        "VALUES (19, 1, 'external-board', 'Duplicate in same account', '2026-01-02')"
                    ))
                except IntegrityError:
                    pass
                else:
                    raise AssertionError("Board IDs must remain unique within an account")
        finally:
            engine.dispose()


def test_pinterest_board_explicit_unique_index_is_replaced_without_table_rebuild():
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'legacy-board-index.db'}")
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    "CREATE TABLE pinterest_accounts (id INTEGER PRIMARY KEY, account_name VARCHAR(255) NOT NULL, "
                    "account_identifier VARCHAR(255), is_active BOOLEAN NOT NULL, created_at DATETIME)"
                ))
                connection.execute(text(
                    "CREATE TABLE pinterest_boards (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL "
                    "REFERENCES pinterest_accounts(id), board_id VARCHAR(64) NOT NULL, name VARCHAR(255) NOT NULL, "
                    "description TEXT, privacy VARCHAR(32), updated_at DATETIME NOT NULL)"
                ))
                connection.execute(text(
                    "CREATE UNIQUE INDEX ix_pinterest_boards_board_id ON pinterest_boards(board_id)"
                ))
                connection.execute(text(
                    "INSERT INTO pinterest_accounts (id, account_name, account_identifier, is_active) "
                    "VALUES (1, 'First', 'first-index-account', 1), (2, 'Second', 'second-index-account', 1)"
                ))
                connection.execute(text(
                    "INSERT INTO pinterest_boards (id, account_id, board_id, name, updated_at) "
                    "VALUES (8, 1, 'same-external', 'First board', '2026-01-01')"
                ))
            upgrade_analytics_schema(engine)
            upgrade_analytics_schema(engine)

            with engine.connect() as connection:
                board_sql = connection.execute(text(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='pinterest_boards'"
                )).scalar_one()
                assert "UNIQUE" not in board_sql
                assert connection.execute(text(
                    "SELECT id, board_id FROM pinterest_boards WHERE id=8"
                )).one() == (8, "same-external")
                indexes = inspect(connection).get_indexes("pinterest_boards")
                assert any(index["unique"] and index["column_names"] == ["account_id", "board_id"]
                           for index in indexes)
                assert not any(index["unique"] and index["column_names"] == ["board_id"]
                               for index in indexes)
            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO pinterest_boards (id, account_id, board_id, name, updated_at) "
                    "VALUES (9, 2, 'same-external', 'Other account board', '2026-01-02')"
                ))
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
