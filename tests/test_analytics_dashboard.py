from datetime import date, datetime

from sqlalchemy import event

from app.database import SessionLocal, engine
from app.main import app
from app.models import (
    AnalyticsSnapshot,
    Pin,
    PinCreative,
    PinterestAccount,
    PinterestAccountAnalyticsSnapshot,
    PinterestBoard,
    Product,
    PublishedPinterestPin,
)
from app.services.analytics_dashboard import DashboardFilters, get_dashboard_data
from fastapi.testclient import TestClient


def _published(
    db,
    *,
    title="Product",
    account_name="account-a",
    external_id="external-1",
    board=True,
    creative_type="product_focus",
    source_type="mockup",
    angle="gift idea",
    keyword="wooden gift",
    product=None,
):
    account = db.query(PinterestAccount).filter_by(account_name=account_name).one_or_none()
    if account is None:
        account = PinterestAccount(account_name=account_name, account_identifier=account_name, is_active=True)
        db.add(account)
        db.flush()
    product = product or Product(title=title)
    creative = PinCreative(
        product=product,
        creative_type=creative_type,
        title=f"{title} creative",
        description="A test creative",
        keywords=[keyword],
        seo_metadata={"primary_keyword": keyword, "creative_angle": angle},
        call_to_action="Explore",
        source_type=source_type,
        generation_key=f"key-{account_name}-{title}-{external_id}",
    )
    pin = Pin(product=product, creative=creative, title=creative.title, description=creative.description)
    db.add(pin)
    db.flush()
    board_row = None
    if board:
        board_row = db.query(PinterestBoard).filter_by(account_id=account.id).first()
        if board_row is None:
            board_row = PinterestBoard(account=account, board_id=f"board-{account_name}", name="Seasonal Ideas")
            db.add(board_row)
            db.flush()
    published = PublishedPinterestPin(
        pin=pin,
        account=account,
        board=board_row,
        external_pin_id=external_id,
        published_at=datetime(2026, 9, 20, 12),
        metadata_snapshot=PublishedPinterestPin.capture_metadata(pin),
    )
    db.add(published)
    db.flush()
    return account, product, creative, pin, published


def _snapshot(db, published, *, day=date(2026, 9, 20), impressions=10, saves=2, clicks=3, outbound=1,
              engagements=5, engagement_rate=None, period_start=None, fetched_at=None):
    start = period_start or datetime.combine(day, datetime.min.time())
    snapshot = AnalyticsSnapshot(
        pin_id=published.pin_id,
        published_pin=published,
        metric_date=day,
        period_start=start,
        period_end=start.replace(hour=23, minute=59),
        fetched_at=fetched_at or start.replace(hour=23, minute=59),
        recorded_at=fetched_at or start.replace(hour=23, minute=59),
        impressions=impressions,
        saves=saves,
        pin_clicks=clicks,
        outbound_clicks=outbound,
        engagements=engagements,
        engagement_rate=engagement_rate,
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _filters(**kwargs):
    defaults = {"start": date(2026, 9, 1), "end": date(2026, 9, 30)}
    defaults.update(kwargs)
    return DashboardFilters(**defaults)


def test_kpi_summary_and_sql_aggregates_are_computed_from_snapshots():
    with SessionLocal() as db:
        _, _, _, _, first = _published(db, title="First")
        _, _, _, _, second = _published(db, title="Second", external_id="external-2", board=False)
        _snapshot(db, first, impressions=10, saves=2, clicks=3, outbound=1, engagements=5,
                  engagement_rate=0.5)
        _snapshot(db, second, impressions=20, saves=4, clicks=6, outbound=2, engagements=10,
                  engagement_rate=0.25)
        db.commit()

        data = get_dashboard_data(db, _filters())

    assert data["kpis"] == {
        "impressions": 30,
        "saves": 6,
        "pin_clicks": 9,
        "outbound_clicks": 3,
        "engagements": 15,
        "engagement_rate": 0.375,
        "pin_click_rate": "Not available",
        "outbound_click_rate": "Not available",
    }
    assert data["publication_count"] == 2


def test_date_filter_limits_kpis_and_chart_rows():
    with SessionLocal() as db:
        _, _, _, _, publication = _published(db)
        _snapshot(db, publication, day=date(2026, 9, 5), impressions=12)
        _snapshot(db, publication, day=date(2026, 9, 25), impressions=50,
                  period_start=datetime(2026, 9, 25))
        db.commit()
        data = get_dashboard_data(db, _filters(start=date(2026, 9, 1), end=date(2026, 9, 10)))

    assert data["kpis"]["impressions"] == 12
    assert [point["date"] for point in data["charts"]["impressions"]] == ["2026-09-05"]


def test_product_aggregation_counts_unique_pins_and_handles_nulls():
    with SessionLocal() as db:
        _, product, _, _, first = _published(db, title="Grouped")
        _, _, _, _, second = _published(db, title="Grouped", external_id="external-2", product=product)
        _snapshot(db, first, impressions=None, saves=0, clicks=None, outbound=0)
        _snapshot(db, second, impressions=8, saves=None, clicks=0, outbound=None)
        db.commit()
        data = get_dashboard_data(db, _filters())

    assert data["products"][0]["product_title"] == "Grouped"
    assert data["products"][0]["pins"] == 2
    assert data["products"][0]["impressions"] == 8
    assert data["products"][0]["saves"] == 0
    assert data["products"][0]["pin_clicks"] == 0
    assert data["products"][0]["outbound_clicks"] == 0


def test_creative_type_and_angle_groups_use_as_published_metadata():
    with SessionLocal() as db:
        _, _, creative, _, published = _published(db, creative_type="product_focus", angle="old angle")
        _snapshot(db, published)
        creative.creative_type = "lifestyle"
        creative.seo_metadata = {"primary_keyword": "new keyword", "creative_angle": "new angle"}
        db.commit()
        data = get_dashboard_data(db, _filters())

    assert data["creative_types"][0]["creative_type"] == "product_focus"
    assert data["angles"][0]["creative_angle"] == "old angle"
    assert data["keywords"][0]["primary_keyword"] == "wooden gift"


def test_source_type_and_board_aggregation_including_unknown_board():
    with SessionLocal() as db:
        _, _, _, _, boarded = _published(db, external_id="boarded")
        _, _, _, _, unboarded = _published(db, title="No board", external_id="unboarded", board=False)
        _snapshot(db, boarded, impressions=5)
        _snapshot(db, unboarded, impressions=7)
        db.commit()
        data = get_dashboard_data(db, _filters())

    assert data["sources"][0]["source_type"] == "mockup"
    assert len(data["boards"]) == 2
    assert {row["board_name"] for row in data["boards"]} == {"Seasonal Ideas", None}
    assert sum(row["pins"] for row in data["boards"]) == 2


def test_account_analytics_are_separate_from_pin_kpis_and_filter_by_account():
    with SessionLocal() as db:
        first_account, _, _, _, first_pin = _published(db, account_name="account-first")
        second_account, _, _, _, second_pin = _published(db, account_name="account-second", external_id="second")
        _snapshot(db, first_pin, impressions=10)
        _snapshot(db, second_pin, impressions=90)
        db.add_all([
            PinterestAccountAnalyticsSnapshot(account=first_account, metric_date=date(2026, 9, 20),
                                              period_start=datetime(2026, 9, 20), period_end=datetime(2026, 9, 21),
                                              profile_visits=4, follows=2),
            PinterestAccountAnalyticsSnapshot(account=second_account, metric_date=date(2026, 9, 20),
                                              period_start=datetime(2026, 9, 20), period_end=datetime(2026, 9, 21),
                                              profile_visits=40, follows=20),
        ])
        db.commit()
        data = get_dashboard_data(db, _filters(account_id=first_account.id))

    assert data["kpis"]["impressions"] == 10
    assert data["account_kpis"]["profile_visits"] == 4
    assert data["account_kpis"]["follows"] == 2
    assert data["account_snapshot_count"] == 1


def test_inactive_account_id_cannot_read_dashboard_snapshots():
    with SessionLocal() as db:
        account, _, _, _, publication = _published(db)
        _snapshot(db, publication, impressions=123)
        account.is_active = False
        db.commit()
        data = get_dashboard_data(db, _filters(account_id=account.id))

    assert data["snapshot_count"] == 0
    assert data["kpis"]["impressions"] == "Not available"


def test_null_metrics_are_not_rendered_as_zero_but_explicit_zero_is():
    with SessionLocal() as db:
        _, _, _, _, publication = _published(db)
        _snapshot(db, publication, impressions=None, saves=0, clicks=None, outbound=0, engagements=None)
        db.commit()
        data = get_dashboard_data(db, _filters())

    assert data["kpis"]["impressions"] == "Not available"
    assert data["kpis"]["saves"] == 0
    assert data["kpis"]["engagements"] == "Not available"
    assert data["charts"]["impressions"][0]["value"] is None
    assert data["charts"]["saves"][0]["value"] == 0


def test_empty_date_range_and_missing_account_have_explicit_empty_state():
    with SessionLocal() as db:
        _published(db)
        db.commit()
        data = get_dashboard_data(db, _filters(start=date(2026, 8, 1), end=date(2026, 8, 31)))

    assert data["snapshot_count"] == 0
    assert data["publication_count"] == 0
    assert all(value == "Not available" for value in data["kpis"].values())
    assert data["has_any_account"] is True
    assert data["account_snapshot_count"] == 0


def test_product_pagination_applies_limit_and_offset():
    with SessionLocal() as db:
        for index in range(22):
            _, _, _, _, publication = _published(db, title=f"Product {index:02}", external_id=f"pin-{index}")
            _snapshot(db, publication, impressions=index)
        db.commit()
        first_page = get_dashboard_data(db, _filters(page=1, page_size=20))
        second_page = get_dashboard_data(db, _filters(page=2, page_size=20))

    assert first_page["product_count"] == 22
    assert len(first_page["products"]) == 20
    assert len(second_page["products"]) == 2
    assert {row["product_id"] for row in first_page["products"]}.isdisjoint(
        {row["product_id"] for row in second_page["products"]}
    )


def test_historical_creative_type_filter_does_not_use_current_creative():
    with SessionLocal() as db:
        _, _, creative, _, publication = _published(db, creative_type="product_focus")
        _snapshot(db, publication)
        creative.creative_type = "lifestyle"
        db.commit()
        historical = get_dashboard_data(db, _filters(creative_type="product_focus"))
        current = get_dashboard_data(db, _filters(creative_type="lifestyle"))

    assert historical["snapshot_count"] == 1
    assert current["snapshot_count"] == 0


def test_dashboard_queries_are_aggregated_without_per_publication_n_plus_one():
    with SessionLocal() as db:
        for index in range(20):
            _, _, _, _, publication = _published(db, title=f"Perf {index}", external_id=f"perf-{index}")
            _snapshot(db, publication)
        db.commit()
        statements = []

        def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            data = get_dashboard_data(db, _filters())
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)

    assert data["publication_count"] == 20
    assert len(statements) < 40


def test_dashboard_route_renders_analytics_empty_state_and_date_controls():
    with SessionLocal() as db:
        db.add(PinterestAccount(account_name="dashboard-account", account_identifier="dashboard-account", is_active=True))
        db.commit()
    with TestClient(app) as client:
        response = client.get("/", params={"period": "custom", "start_date": "2026-08-01", "end_date": "2026-08-31"})

    assert response.status_code == 200
    assert "Pinterest Analytics" in response.text
    assert "Date range" in response.text
    assert "No analytics or published Pins are available for this date range." in response.text
