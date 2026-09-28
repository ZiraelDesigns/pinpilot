"""Database-side read models for the local analytics dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import Date, Integer, and_, cast, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AnalyticsSnapshot,
    PinterestAccount,
    PinterestAccountAnalyticsSnapshot,
    PinterestBoard,
    Pin,
    Product,
    PublishedPinterestPin,
)


@dataclass(frozen=True)
class DashboardFilters:
    start: date
    end: date
    account_id: int | None = None
    product_id: int | None = None
    creative_type: str | None = None
    source_type: str | None = None
    board_id: int | None = None
    page: int = 1
    page_size: int = 20


def _metric_date_expression():
    return func.coalesce(
        AnalyticsSnapshot.metric_date,
        cast(AnalyticsSnapshot.period_start, Date),
        cast(func.coalesce(AnalyticsSnapshot.fetched_at, AnalyticsSnapshot.recorded_at), Date),
    )


def _latest_pin_rows(db: Session, filters: DashboardFilters):
    """Latest fetched row per published Pin/reporting period, ranked in SQL."""
    metadata = PublishedPinterestPin.metadata_snapshot
    seo = metadata["seo_metadata"]
    product_id = func.coalesce(
        cast(metadata["product_id"].as_string(), Integer), Pin.product_id
    ).label("product_id")
    creative_type = metadata["creative_type"].as_string().label("creative_type")
    source_type = metadata["source_type"].as_string().label("source_type")
    creative_angle = func.coalesce(
        metadata["creative_angle"].as_string(), seo["creative_angle"].as_string()
    ).label("creative_angle")
    primary_keyword = func.coalesce(
        metadata["primary_keyword"].as_string(), seo["primary_keyword"].as_string()
    ).label("primary_keyword")
    metric_day = _metric_date_expression().label("metric_day")
    row_number = func.row_number().over(
        partition_by=(
            AnalyticsSnapshot.published_pin_id,
            AnalyticsSnapshot.period_start,
            AnalyticsSnapshot.period_end,
        ),
        order_by=(
            AnalyticsSnapshot.fetched_at.desc().nullslast(),
            AnalyticsSnapshot.recorded_at.desc(),
            AnalyticsSnapshot.id.desc(),
        ),
    ).label("row_number")
    start_at = datetime.combine(filters.start, time.min)
    end_at = datetime.combine(filters.end + timedelta(days=1), time.min)
    conditions = [
        AnalyticsSnapshot.published_pin_id.is_not(None),
        PublishedPinterestPin.account_id.is_not(None),
        PinterestAccount.is_active.is_(True),
        or_(
            and_(
                AnalyticsSnapshot.metric_date.is_not(None),
                AnalyticsSnapshot.metric_date >= filters.start,
                AnalyticsSnapshot.metric_date <= filters.end,
            ),
            and_(
                AnalyticsSnapshot.metric_date.is_(None),
                AnalyticsSnapshot.period_start.is_not(None),
                AnalyticsSnapshot.period_start < end_at,
                AnalyticsSnapshot.period_end >= start_at,
            ),
            and_(
                AnalyticsSnapshot.metric_date.is_(None),
                AnalyticsSnapshot.period_start.is_(None),
                func.coalesce(AnalyticsSnapshot.fetched_at, AnalyticsSnapshot.recorded_at) >= start_at,
                func.coalesce(AnalyticsSnapshot.fetched_at, AnalyticsSnapshot.recorded_at) < end_at,
            ),
        ),
    ]
    if filters.account_id is not None:
        conditions.append(PublishedPinterestPin.account_id == filters.account_id)
    if filters.product_id is not None:
        conditions.append(product_id == filters.product_id)
    if filters.creative_type:
        conditions.append(creative_type == filters.creative_type)
    if filters.source_type:
        conditions.append(source_type == filters.source_type)
    if filters.board_id is not None:
        conditions.append(PublishedPinterestPin.board_id == filters.board_id)

    return (
        select(
            AnalyticsSnapshot.id.label("snapshot_id"),
            AnalyticsSnapshot.published_pin_id.label("published_pin_id"),
            AnalyticsSnapshot.impressions.label("impressions"),
            AnalyticsSnapshot.saves.label("saves"),
            AnalyticsSnapshot.pin_clicks.label("pin_clicks"),
            AnalyticsSnapshot.outbound_clicks.label("outbound_clicks"),
            AnalyticsSnapshot.engagements.label("engagements"),
            AnalyticsSnapshot.engagement_rate.label("engagement_rate"),
            AnalyticsSnapshot.pin_click_rate.label("pin_click_rate"),
            AnalyticsSnapshot.outbound_click_rate.label("outbound_click_rate"),
            PublishedPinterestPin.account_id.label("account_id"),
            PublishedPinterestPin.board_id.label("board_id"),
            product_id,
            creative_type,
            source_type,
            creative_angle,
            primary_keyword,
            metric_day,
            row_number,
        )
        .join(PublishedPinterestPin, AnalyticsSnapshot.published_pin_id == PublishedPinterestPin.id)
        .join(PinterestAccount, PublishedPinterestPin.account_id == PinterestAccount.id)
        .outerjoin(Pin, PublishedPinterestPin.pin_id == Pin.id)
        .where(*conditions)
        .subquery("ranked_pin_analytics")
    )


def _latest_account_rows(db: Session, filters: DashboardFilters):
    start_at = datetime.combine(filters.start, time.min)
    end_at = datetime.combine(filters.end + timedelta(days=1), time.min)
    metric_day = func.coalesce(
        PinterestAccountAnalyticsSnapshot.metric_date,
        cast(PinterestAccountAnalyticsSnapshot.period_start, Date),
        cast(PinterestAccountAnalyticsSnapshot.fetched_at, Date),
    )
    conditions = [
        PinterestAccountAnalyticsSnapshot.account_id.is_not(None),
        PinterestAccount.is_active.is_(True),
        or_(
            and_(
                PinterestAccountAnalyticsSnapshot.metric_date >= filters.start,
                PinterestAccountAnalyticsSnapshot.metric_date <= filters.end,
            ),
            and_(
                PinterestAccountAnalyticsSnapshot.metric_date.is_(None),
                PinterestAccountAnalyticsSnapshot.period_start.is_not(None),
                PinterestAccountAnalyticsSnapshot.period_start < end_at,
                PinterestAccountAnalyticsSnapshot.period_end >= start_at,
            ),
            and_(
                PinterestAccountAnalyticsSnapshot.metric_date.is_(None),
                PinterestAccountAnalyticsSnapshot.period_start.is_(None),
                PinterestAccountAnalyticsSnapshot.fetched_at >= start_at,
                PinterestAccountAnalyticsSnapshot.fetched_at < end_at,
            ),
        ),
    ]
    if filters.account_id is not None:
        conditions.append(PinterestAccountAnalyticsSnapshot.account_id == filters.account_id)
    rank = func.row_number().over(
        partition_by=(
            PinterestAccountAnalyticsSnapshot.account_id,
            PinterestAccountAnalyticsSnapshot.period_start,
            PinterestAccountAnalyticsSnapshot.period_end,
        ),
        order_by=(
            PinterestAccountAnalyticsSnapshot.fetched_at.desc().nullslast(),
            PinterestAccountAnalyticsSnapshot.id.desc(),
        ),
    ).label("row_number")
    return select(
        PinterestAccountAnalyticsSnapshot.id.label("snapshot_id"),
        PinterestAccountAnalyticsSnapshot.account_id.label("account_id"),
        PinterestAccountAnalyticsSnapshot.profile_visits.label("profile_visits"),
        PinterestAccountAnalyticsSnapshot.follows.label("follows"),
        PinterestAccountAnalyticsSnapshot.total_audience.label("total_audience"),
        PinterestAccountAnalyticsSnapshot.engaged_audience.label("engaged_audience"),
        metric_day.label("metric_day"),
        rank,
    ).join(
        PinterestAccount,
        PinterestAccountAnalyticsSnapshot.account_id == PinterestAccount.id,
    ).where(*conditions).subquery("ranked_account_analytics")


def _sum_value(db: Session, query, column: str):
    return db.scalar(select(func.sum(query.c[column])).where(query.c.row_number == 1))


def _rate_average(db: Session, query, column: str):
    return db.scalar(select(func.avg(query.c[column])).where(query.c.row_number == 1))


def _format_metric(value):
    return "Not available" if value is None else value


def get_dashboard_data(db: Session, filters: DashboardFilters) -> dict:
    rows = _latest_pin_rows(db, filters)
    account_rows = _latest_account_rows(db, filters)
    kpis = {
        "impressions": _format_metric(_sum_value(db, rows, "impressions")),
        "saves": _format_metric(_sum_value(db, rows, "saves")),
        "pin_clicks": _format_metric(_sum_value(db, rows, "pin_clicks")),
        "outbound_clicks": _format_metric(_sum_value(db, rows, "outbound_clicks")),
        "engagements": _format_metric(_sum_value(db, rows, "engagements")),
        "engagement_rate": _format_metric(_rate_average(db, rows, "engagement_rate")),
        "pin_click_rate": _format_metric(_rate_average(db, rows, "pin_click_rate")),
        "outbound_click_rate": _format_metric(_rate_average(db, rows, "outbound_click_rate")),
    }

    product_count = db.scalar(
        select(func.count(func.distinct(rows.c.product_id))).where(
            rows.c.row_number == 1, rows.c.product_id.is_not(None)
        )
    ) or 0
    product_id = rows.c.product_id
    product_aggregates = (
        select(
            product_id.label("product_id"),
            Product.title.label("product_title"),
            func.count(func.distinct(rows.c.published_pin_id)).label("pins"),
            func.sum(rows.c.impressions).label("impressions"),
            func.sum(rows.c.saves).label("saves"),
            func.sum(rows.c.pin_clicks).label("pin_clicks"),
            func.sum(rows.c.outbound_clicks).label("outbound_clicks"),
            func.sum(rows.c.engagements).label("engagements"),
        )
        .join(Product, Product.id == product_id)
        .where(rows.c.row_number == 1)
        .group_by(product_id, Product.title)
        .order_by(func.sum(rows.c.impressions).desc().nullslast(), Product.title)
    )
    products = db.execute(
        product_aggregates.limit(filters.page_size).offset((filters.page - 1) * filters.page_size)
    ).mappings().all()

    def dimension_rows(*dimensions):
        query = select(
            *dimensions,
            func.count(func.distinct(rows.c.published_pin_id)).label("pins"),
            func.sum(rows.c.impressions).label("impressions"),
            func.sum(rows.c.saves).label("saves"),
            func.sum(rows.c.pin_clicks).label("pin_clicks"),
            func.sum(rows.c.outbound_clicks).label("outbound_clicks"),
        ).where(rows.c.row_number == 1).group_by(*dimensions)
        return db.execute(query.order_by(func.sum(rows.c.impressions).desc().nullslast())).mappings().all()

    creative_types = dimension_rows(rows.c.creative_type)
    sources = dimension_rows(rows.c.source_type)
    angles = dimension_rows(rows.c.creative_angle)
    keywords = dimension_rows(rows.c.primary_keyword)

    board_rows = db.execute(
        select(
            rows.c.board_id,
            PinterestBoard.name.label("board_name"),
            func.count(func.distinct(rows.c.published_pin_id)).label("pins"),
            func.sum(rows.c.impressions).label("impressions"),
            func.sum(rows.c.saves).label("saves"),
            func.sum(rows.c.pin_clicks).label("pin_clicks"),
            func.sum(rows.c.outbound_clicks).label("outbound_clicks"),
        )
        .select_from(rows)
        .outerjoin(PinterestBoard, PinterestBoard.id == rows.c.board_id)
        .where(rows.c.row_number == 1)
        .group_by(rows.c.board_id, PinterestBoard.name)
        .order_by(func.sum(rows.c.impressions).desc().nullslast(), PinterestBoard.name)
    ).mappings().all()

    chart_rows = db.execute(
        select(
            rows.c.metric_day,
            func.sum(rows.c.impressions).label("impressions"),
            func.sum(rows.c.outbound_clicks).label("outbound_clicks"),
            func.sum(rows.c.saves).label("saves"),
        )
        .where(rows.c.row_number == 1, rows.c.metric_day.is_not(None))
        .group_by(rows.c.metric_day)
        .order_by(rows.c.metric_day)
    ).mappings().all()
    charts = {}
    for metric in ("impressions", "outbound_clicks", "saves"):
        values = [row[metric] for row in chart_rows if row[metric] is not None]
        maximum = max(values, default=0)
        charts[metric] = [
            {
                "date": row["metric_day"].isoformat(),
                "value": row[metric],
                "width": round(row[metric] / maximum * 100, 2) if row[metric] is not None and maximum else 0,
            }
            for row in chart_rows
        ]

    account_aggregates = {
        key: _format_metric(_sum_value(db, account_rows, key))
        for key in ("profile_visits", "follows", "total_audience", "engaged_audience")
    }
    account_snapshot_count = db.scalar(
        select(func.count()).select_from(account_rows).where(account_rows.c.row_number == 1)
    ) or 0
    publication_count = db.scalar(
        select(func.count(func.distinct(rows.c.published_pin_id))).where(rows.c.row_number == 1)
    ) or 0
    snapshot_count = db.scalar(
        select(func.count()).select_from(rows).where(rows.c.row_number == 1)
    ) or 0
    active_accounts = db.scalars(
        select(PinterestAccount).where(PinterestAccount.is_active.is_(True)).order_by(PinterestAccount.account_name)
    ).all()
    products_filter = db.scalars(select(Product).order_by(Product.title)).all()
    boards_filter_query = select(PinterestBoard).order_by(PinterestBoard.name)
    if filters.account_id is not None:
        boards_filter_query = boards_filter_query.where(PinterestBoard.account_id == filters.account_id)
    boards_filter = db.scalars(boards_filter_query).all()

    return {
        "kpis": kpis,
        "products": products,
        "product_count": product_count,
        "product_page": filters.page,
        "product_page_size": filters.page_size,
        "creative_types": creative_types,
        "sources": sources,
        "angles": angles,
        "keywords": keywords,
        "boards": board_rows,
        "charts": charts,
        "account_kpis": account_aggregates,
        "account_snapshot_count": account_snapshot_count,
        "publication_count": publication_count,
        "snapshot_count": snapshot_count,
        "accounts": active_accounts,
        "products_filter": products_filter,
        "boards_filter": boards_filter,
        "has_any_account": bool(active_accounts),
    }
