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
    PinterestAccount,
    PinterestBoard,
    PinterestBoardRecommendation,
    PinterestBoardSEOProfile,
    SEOGeneration,
    SEOKeywordIntelligence,
    SEOQualityAssessment,
    SEOPerformanceLearning,
    SEOTrendSeasonalAssessment,
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


def _sqlite_board_unique_indexes(engine: Engine) -> list[tuple[str, list[str], str]]:
    with engine.connect() as connection:
        indexes = connection.exec_driver_sql("PRAGMA index_list('pinterest_boards')").all()
        result = []
        for row in indexes:
            if not row[2]:
                continue
            name = str(row[1])
            escaped = name.replace("'", "''")
            columns = [str(item[2]) for item in connection.exec_driver_sql(
                f"PRAGMA index_info('{escaped}')"
            ).all()]
            result.append((name, columns, str(row[3])))
        return result


def _board_has_global_external_id_unique(engine: Engine) -> bool:
    if engine.dialect.name == "sqlite":
        return any(columns == ["board_id"] for _, columns, _ in _sqlite_board_unique_indexes(engine))
    inspector = inspect(engine)
    unique_constraints = inspector.get_unique_constraints("pinterest_boards")
    if any(item.get("column_names") == ["board_id"] for item in unique_constraints):
        return True
    indexes = inspector.get_indexes("pinterest_boards")
    return any(item.get("unique") and item.get("column_names") == ["board_id"] for item in indexes)


def _board_has_account_scoped_unique(engine: Engine) -> bool:
    if engine.dialect.name == "sqlite":
        return any(
            columns == ["account_id", "board_id"]
            for _, columns, _ in _sqlite_board_unique_indexes(engine)
        )
    inspector = inspect(engine)
    if any(
        item.get("column_names") == ["account_id", "board_id"]
        for item in inspector.get_unique_constraints("pinterest_boards")
    ):
        return True
    return any(
        item.get("unique") and item.get("column_names") == ["account_id", "board_id"]
        for item in inspector.get_indexes("pinterest_boards")
    )


def _ensure_account_scoped_board_unique(engine: Engine) -> None:
    if _board_has_account_scoped_unique(engine):
        return
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pinterest_boards_account_external_id "
            "ON pinterest_boards (account_id, board_id)"
        ))


def _upgrade_pinterest_board_schema(engine: Engine) -> None:
    """Add board provenance and scope external-ID uniqueness by Pinterest account.

    SQLite cannot drop a UNIQUE constraint in place. Its board table is rebuilt
    with IDs and dependent references copied unchanged; foreign keys are disabled
    only for the transaction and checked immediately after it commits.
    """
    if "pinterest_boards" not in inspect(engine).get_table_names():
        return
    additions = {
        "source": "VARCHAR(64)",
        "fetched_at": "DATETIME",
        "metadata_version": "VARCHAR(64)",
    }
    columns = {item["name"] for item in inspect(engine).get_columns("pinterest_boards")}
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE pinterest_boards ADD COLUMN {name} {definition}"))
                columns.add(name)

    if not _board_has_global_external_id_unique(engine):
        _ensure_account_scoped_board_unique(engine)
        return
    if engine.dialect.name == "sqlite":
        global_uniques = [
            (name, origin)
            for name, columns, origin in _sqlite_board_unique_indexes(engine)
            if columns == ["board_id"]
        ]
        if global_uniques and all(origin == "c" for _, origin in global_uniques):
            # The historical ORM mapping often created a standalone unique
            # index. Removing that index in place avoids rebuilding the table.
            with engine.begin() as connection:
                preparer = engine.dialect.identifier_preparer
                for name, _ in global_uniques:
                    connection.exec_driver_sql(f"DROP INDEX {preparer.quote(name)}")
            for index in PinterestBoard.__table__.indexes:
                index.create(bind=engine, checkfirst=True)
            _ensure_account_scoped_board_unique(engine)
            return
    if engine.dialect.name == "postgresql":
        inspector = inspect(engine)
        unique_constraints = inspector.get_unique_constraints("pinterest_boards")
        constraint_names = {
            item["name"] for item in unique_constraints
            if item.get("name") and item.get("column_names") == ["board_id"]
        }
        with engine.begin() as connection:
            preparer = engine.dialect.identifier_preparer
            for name in sorted(constraint_names):
                connection.execute(text(
                    f"ALTER TABLE pinterest_boards DROP CONSTRAINT {preparer.quote(name)}"
                ))
            for item in inspector.get_indexes("pinterest_boards"):
                if (
                    item.get("unique")
                    and item.get("column_names") == ["board_id"]
                    and not item.get("duplicates_constraint")
                ):
                    connection.execute(text(f"DROP INDEX IF EXISTS {preparer.quote(item['name'])}"))
        _ensure_account_scoped_board_unique(engine)
        return
    if engine.dialect.name != "sqlite":
        raise RuntimeError(
            "The legacy Pinterest board external-ID uniqueness needs a dialect-specific, data-preserving migration."
        )

    inspector = inspect(engine)
    unique_index_names = {
        name for name, columns, _ in _sqlite_board_unique_indexes(engine) if columns == ["board_id"]
    }
    with engine.connect() as connection:
        saved_objects = connection.execute(text(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE tbl_name = 'pinterest_boards' AND type IN ('index', 'trigger') "
            "AND sql IS NOT NULL ORDER BY type, name"
        )).all()
        expected_count = connection.execute(
            text("SELECT COUNT(*) FROM pinterest_boards")
        ).scalar_one()
    temporary_name = "pinterest_boards__account_scope_migration"
    raw = engine.raw_connection()
    cursor = raw.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (temporary_name,))
        if cursor.fetchone():
            raise RuntimeError(f"Unexpected leftover migration table: {temporary_name}")

        replacement_metadata = MetaData()
        PinterestAccount.__table__.to_metadata(replacement_metadata)
        replacement = PinterestBoard.__table__.to_metadata(
            replacement_metadata, name=temporary_name
        )
        cursor.execute(str(CreateTable(replacement).compile(dialect=engine.dialect)))
        preparer = engine.dialect.identifier_preparer
        names = [column.name for column in PinterestBoard.__table__.columns]
        quoted_names = ", ".join(preparer.quote(name) for name in names)
        cursor.execute(
            f"INSERT INTO {preparer.quote(temporary_name)} ({quoted_names}) "
            f"SELECT {quoted_names} FROM {preparer.quote('pinterest_boards')}"
        )
        cursor.execute("DROP TABLE pinterest_boards")
        cursor.execute(
            f"ALTER TABLE {preparer.quote(temporary_name)} RENAME TO pinterest_boards"
        )
        for object_type, object_name, statement in saved_objects:
            if object_type == "trigger":
                cursor.execute(statement)
            elif object_name not in unique_index_names:
                cursor.execute(statement)
        cursor.execute("COMMIT")
    except Exception:
        try:
            cursor.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        raw.close()

    for index in PinterestBoard.__table__.indexes:
        index.create(bind=engine, checkfirst=True)
    _ensure_account_scoped_board_unique(engine)
    with engine.connect() as connection:
        actual_count = connection.execute(text("SELECT COUNT(*) FROM pinterest_boards")).scalar_one()
        fk_violations = connection.execute(text("PRAGMA foreign_key_check")).all()
    if actual_count != expected_count:
        raise RuntimeError(
            f"Pinterest board row count changed during migration ({expected_count} -> {actual_count})."
        )
    if fk_violations:
        raise RuntimeError("Pinterest board migration produced foreign-key violations.")


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
            PinterestAccount.__table__,
            PinterestBoard.__table__,
            SEOGeneration.__table__,
            SEOKeywordIntelligence.__table__,
            SEOQualityAssessment.__table__,
            SEOTrendSeasonalAssessment.__table__,
            SEOPerformanceLearning.__table__,
            PinterestBoardSEOProfile.__table__,
            PinterestBoardRecommendation.__table__,
            AIPipelineControl.__table__,
            AIDailyQuotaSlot.__table__,
            Experiment.__table__,
            ExperimentVariant.__table__,
            ExperimentAssignment.__table__,
            ExperimentEvaluation.__table__,
            ExperimentEvaluationResult.__table__,
        ],
    )
    _upgrade_pinterest_board_schema(engine)
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
