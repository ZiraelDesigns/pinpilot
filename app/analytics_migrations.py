"""Small analytics upgrades on the application's existing schema migration path."""

from sqlalchemy import Engine, MetaData, Table, inspect, text
from sqlalchemy.schema import CreateTable

from app.database import Base
from app.models import (
    AIDailyQuotaSlot,
    AIPipelineControl,
    Experiment,
    ExperimentAssignment,
    ExperimentEvaluation,
    ExperimentEvaluationResult,
    ExperimentVariant,
    SEOGeneration,
    SEOKeywordIntelligence,
    SEOQualityAssessment,
)


_SNAPSHOT_COLUMNS = {
    "published_pin_id": (
        "INTEGER REFERENCES published_pinterest_pins(id) ON DELETE SET NULL"
    ),
    "collection_run_id": (
        "INTEGER REFERENCES analytics_collection_runs(id) ON DELETE SET NULL"
    ),
    "metric_date": "DATE",
    "period_start": "DATETIME",
    "period_end": "DATETIME",
    "fetched_at": "DATETIME",
    "pin_clicks": "BIGINT",
    "engagements": "BIGINT",
    "engagement_rate": "NUMERIC(12, 6)",
    "pin_click_rate": "NUMERIC(12, 6)",
    "outbound_click_rate": "NUMERIC(12, 6)",
    "metric_schema_version": "VARCHAR(32)",
}

_SNAPSHOT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_analytics_snapshots_published_pin_id_metric_date "
    "ON analytics_snapshots (published_pin_id, metric_date)",
    "CREATE INDEX IF NOT EXISTS ix_analytics_snapshots_published_period_fetched "
    "ON analytics_snapshots (published_pin_id, period_start, period_end, fetched_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_snapshots_run_pin_period "
    "ON analytics_snapshots (collection_run_id, published_pin_id, period_start, period_end)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_snapshots_published_daily_metric "
    "ON analytics_snapshots (published_pin_id, metric_date) "
    "WHERE metric_schema_version = 'pinterest_v5_organic_daily' AND metric_date IS NOT NULL",
)

_ACCOUNT_DAILY_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_account_analytics_pinterest_daily_metric "
    "ON pinterest_account_analytics_snapshots (account_id, metric_date) "
    "WHERE metric_schema_version = 'pinterest_v5_organic_daily' AND metric_date IS NOT NULL"
)

_LEGACY_METRIC_COLUMNS = ("impressions", "saves", "outbound_clicks")


def _make_legacy_metrics_nullable(engine: Engine) -> None:
    """Rebuild SQLite's snapshot table while copying every column and explicit index."""
    if engine.dialect.name != "sqlite":
        columns = {column["name"]: column for column in inspect(engine).get_columns("analytics_snapshots")}
        non_nullable = [name for name in _LEGACY_METRIC_COLUMNS if not columns[name]["nullable"]]
        if not non_nullable:
            return
        if engine.dialect.name == "postgresql":
            with engine.begin() as connection:
                for name in non_nullable:
                    connection.execute(text(
                        f"ALTER TABLE analytics_snapshots ALTER COLUMN {name} DROP NOT NULL"
                    ))
            return
        raise RuntimeError(
            "Making legacy analytics metric columns nullable is implemented for SQLite and PostgreSQL only."
        )

    with engine.begin() as connection:
        columns = {column["name"]: column for column in inspect(connection).get_columns("analytics_snapshots")}
        non_nullable = [name for name in _LEGACY_METRIC_COLUMNS if not columns[name]["nullable"]]
        if not non_nullable:
            return

        # Preserve hand-created indexes and triggers as well as ORM-known ones.
        saved_objects = connection.execute(text(
            "SELECT type, sql FROM sqlite_master "
            "WHERE tbl_name = 'analytics_snapshots' AND type IN ('index', 'trigger') "
            "AND sql IS NOT NULL ORDER BY type, name"
        )).all()
        old_count = connection.execute(text(
            "SELECT COUNT(*) FROM analytics_snapshots"
        )).scalar_one()

        metadata = MetaData()
        source = Table("analytics_snapshots", metadata, autoload_with=connection)
        temporary_name = "analytics_snapshots__nullable_migration"
        if temporary_name in inspect(connection).get_table_names():
            raise RuntimeError(f"Unexpected leftover migration table: {temporary_name}")
        replacement_metadata = MetaData()
        for foreign_key in source.foreign_keys:
            foreign_key.column.table.to_metadata(replacement_metadata)
        replacement = source.to_metadata(replacement_metadata, name=temporary_name)
        for name in _LEGACY_METRIC_COLUMNS:
            replacement.c[name].nullable = True

        connection.execute(CreateTable(replacement))
        preparer = engine.dialect.identifier_preparer
        column_list = ", ".join(preparer.quote(column.name) for column in source.columns)
        connection.execute(text(
            f"INSERT INTO {preparer.quote(temporary_name)} ({column_list}) "
            f"SELECT {column_list} FROM {preparer.quote('analytics_snapshots')}"
        ))
        connection.execute(text("DROP TABLE analytics_snapshots"))
        connection.execute(text(
            f"ALTER TABLE {preparer.quote(temporary_name)} "
            "RENAME TO analytics_snapshots"
        ))
        for _, statement in saved_objects:
            connection.execute(text(statement))

        new_count = connection.execute(text(
            "SELECT COUNT(*) FROM analytics_snapshots"
        )).scalar_one()
        if new_count != old_count:
            raise RuntimeError(
                f"Analytics snapshot row count changed during migration ({old_count} -> {new_count})."
            )


def upgrade_analytics_schema(engine: Engine) -> None:
    """Add analytics links/metrics to legacy tables without rewriting their rows.

    The caller creates all mapped tables first, matching the application's existing
    ``Base.metadata.create_all`` startup strategy. New columns are nullable so old
    snapshots remain valid and no external Pinterest identity is fabricated.
    The legacy metric columns are made nullable with a data-preserving SQLite table
    rebuild (or PostgreSQL nullability alteration). Repeated calls are safe.
    """
    # Additive, create-if-missing extension of the application's existing
    # create_all + guarded-upgrade migration path. Existing analytics tables and
    # their rows are not altered by experiment setup.
    Base.metadata.create_all(
        bind=engine,
        tables=[
            SEOGeneration.__table__,
            SEOKeywordIntelligence.__table__,
            SEOQualityAssessment.__table__,
            AIPipelineControl.__table__,
            AIDailyQuotaSlot.__table__,
            Experiment.__table__,
            ExperimentVariant.__table__,
            ExperimentAssignment.__table__,
            ExperimentEvaluation.__table__,
            ExperimentEvaluationResult.__table__,
        ],
    )
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO ai_pipeline_controls (id, enabled, updated_at) "
            "SELECT 1, 1, CURRENT_TIMESTAMP WHERE NOT EXISTS "
            "(SELECT 1 FROM ai_pipeline_controls WHERE id = 1)"
        ))
    table_names = set(inspect(engine).get_table_names())
    if "pinterest_account_analytics_snapshots" in table_names:
        with engine.begin() as connection:
            connection.execute(text(_ACCOUNT_DAILY_INDEX))
    experiment_columns = {
        "experiments": {
            "pinterest_account_id": (
                "INTEGER REFERENCES pinterest_accounts(id) ON DELETE SET NULL"
            ),
            "account_identifier_snapshot": "VARCHAR(255)",
        },
        "experiment_evaluations": {
            "snapshot_ids": "JSON NOT NULL DEFAULT '[]'",
        },
    }
    provenance_columns = {
        "published_pinterest_pins": {
            "seo_generation_id": "INTEGER REFERENCES seo_generations(id) ON DELETE SET NULL",
        },
    }
    with engine.begin() as connection:
        for table_name, additions in {**experiment_columns, **provenance_columns}.items():
            if table_name not in table_names:
                continue
            columns = {column["name"] for column in inspect(connection).get_columns(table_name)}
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(text(
                        f"ALTER TABLE {table_name} ADD COLUMN {name} {definition}"
                    ))
        if "published_pinterest_pins" in table_names:
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_published_pinterest_pins_seo_generation_id "
                "ON published_pinterest_pins (seo_generation_id)"
            ))
    if "analytics_snapshots" not in table_names:
        return

    columns = {column["name"] for column in inspect(engine).get_columns("analytics_snapshots")}
    with engine.begin() as connection:
        for name, definition in _SNAPSHOT_COLUMNS.items():
            if name not in columns:
                connection.execute(text(
                    f"ALTER TABLE analytics_snapshots ADD COLUMN {name} {definition}"
                ))
                columns.add(name)
        for statement in _SNAPSHOT_INDEXES:
            connection.execute(text(statement))
    _make_legacy_metrics_nullable(engine)
