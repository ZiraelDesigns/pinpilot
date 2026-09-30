from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest

from app.database import SessionLocal
from app.models import (
    AnalyticsSnapshot,
    Pin,
    Product,
    PinterestAccount,
    PinterestAccountAnalyticsSnapshot,
    PublishedPinterestPin,
)
from app.services.pinterest import PinterestApiService
from app.services.pinterest_analytics import (
    AnalyticsConfigurationError,
    AnalyticsCollector,
    AnalyticsNormalizationError,
)
from app.services.pinterest_analytics_provider import PinterestApiAnalyticsProvider
from app.services.pinterest_api import (
    PINTEREST_ORGANIC_ACCOUNT_METRICS,
    PINTEREST_ORGANIC_PIN_METRICS,
    PinterestApiClient,
    PinterestAuthenticationError,
    PinterestInvalidPayload,
    PinterestPinNotFound,
    PinterestRateLimited,
    PinterestTemporaryError,
)


START = date(2026, 9, 1)
END = date(2026, 9, 7)
PERIOD_START = datetime(2026, 9, 1)
PERIOD_END = datetime(2026, 9, 8)


def _client(handler):
    return PinterestApiClient(
        "mock-access-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _service(handler):
    return PinterestApiService(None, None, api_client=_client(handler))


def test_pin_analytics_uses_documented_endpoint_dates_and_metric_types():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "external-pin-7": {
                    "summary_metrics": {"IMPRESSION": 21, "SAVE": 3}
                }
            },
        )

    response = _client(handler).get_pin_analytics("external-pin-7", START, END)
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/v5/pins/external-pin-7/analytics"
    assert request.url.params["start_date"] == "2026-09-01"
    assert request.url.params["end_date"] == "2026-09-07"
    assert request.url.params["metric_types"] == ",".join(PINTEREST_ORGANIC_PIN_METRICS)
    assert response["external-pin-7"]["summary_metrics"]["IMPRESSION"] == 21


def test_account_analytics_uses_documented_user_account_endpoint():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"all": {"summary_metrics": {"FOLLOW": 2}}})

    response = _client(handler).get_user_account_analytics(START, END)
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/v5/user_account/analytics"
    assert request.url.params["metric_types"] == ",".join(PINTEREST_ORGANIC_ACCOUNT_METRICS)
    assert response["all"]["summary_metrics"]["FOLLOW"] == 2


@pytest.mark.parametrize("bad_range", [(END, START), (START, date(2026, 12, 1))])
def test_analytics_client_rejects_invalid_or_overlong_date_ranges(bad_range):
    client = _client(lambda request: pytest.fail("invalid range must not make a request"))
    with pytest.raises(PinterestInvalidPayload):
        client.get_pin_analytics("p1", *bad_range)


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, PinterestAuthenticationError),
        (404, PinterestPinNotFound),
        (429, PinterestRateLimited),
        (503, PinterestTemporaryError),
    ],
)
def test_pin_analytics_errors_use_existing_api_error_classification(status, error_type):
    client = _client(lambda request: httpx.Response(status, json={"message": "safe mock error"}))
    with pytest.raises(error_type):
        client.get_pin_analytics("p1", START, END)


def test_provider_normalizes_counts_rates_nulls_and_explicit_zero():
    body = {
        "ext-pin": {
            "summary_metrics": {
                "IMPRESSION": 0.0,
                "SAVE": 4.0,
                "PIN_CLICK": None,
                "OUTBOUND_CLICK": 2,
                "ENGAGEMENT": 6,
                "ENGAGEMENT_RATE": 0.125,
                "PIN_CLICK_RATE": None,
                "OUTBOUND_CLICK_RATE": 0.04,
            }
        }
    }
    provider = PinterestApiAnalyticsProvider(lambda _: _service(lambda request: httpx.Response(200, json=body)))
    result = provider.fetch_pin_analytics("account", "ext-pin", PERIOD_START, PERIOD_END)
    assert result.impressions == 0
    assert result.saves == 4
    assert result.pin_clicks is None
    assert result.outbound_clicks == 2
    assert result.engagements == 6
    assert result.engagement_rate == Decimal("0.125")
    assert result.pin_click_rate is None
    assert result.outbound_click_rate == Decimal("0.04")
    assert result.metric_date is None


def test_provider_preserves_omitted_pin_metrics_as_null():
    provider = PinterestApiAnalyticsProvider(
        lambda _: _service(lambda request: httpx.Response(200, json={}))
    )
    result = provider.fetch_pin_analytics("account", "missing-pin", PERIOD_START, PERIOD_END)
    assert result.impressions is None
    assert result.saves is None
    assert result.pin_clicks is None
    assert result.outbound_clicks is None


def test_provider_normalizes_daily_pin_metrics_and_keeps_zero_distinct_from_null():
    body = {"ext-pin": {
        "summary_metrics": {"IMPRESSION": 999},
        "daily_metrics": [
            {"data_status": "READY", "date": "2026-09-06", "metrics": {
                "IMPRESSION": 0, "SAVE": None, "PIN_CLICK": 2,
            }},
            {"data_status": "READY", "date": "2026-09-07", "metrics": {
                "IMPRESSION": 5, "SAVE": 1,
            }},
            {"data_status": "PROCESSING", "date": "2026-09-08", "metrics": {
                "IMPRESSION": 100,
            }},
        ],
    }}
    provider = PinterestApiAnalyticsProvider(lambda _: _service(lambda request: httpx.Response(200, json=body)))

    result = provider.fetch_pin_analytics("account", "ext-pin", PERIOD_START, PERIOD_END)

    assert [(row.metric_date, row.impressions, row.saves, row.pin_clicks) for row in result] == [
        (date(2026, 9, 6), 0, None, 2),
        (date(2026, 9, 7), 5, 1, None),
    ]


def test_provider_rejects_duplicate_or_out_of_range_daily_pin_rows():
    duplicated = {"p1": {"daily_metrics": [
        {"data_status": "READY", "date": "2026-09-02", "metrics": {"IMPRESSION": 1}},
        {"data_status": "READY", "date": "2026-09-02", "metrics": {"IMPRESSION": 2}},
    ]}}
    provider = PinterestApiAnalyticsProvider(lambda _: _service(lambda request: httpx.Response(200, json=duplicated)))
    with pytest.raises(AnalyticsNormalizationError, match="duplicate daily"):
        provider.fetch_pin_analytics("account", "p1", PERIOD_START, PERIOD_END)

    outside = {"p1": {"daily_metrics": [
        {"data_status": "READY", "date": "2026-08-31", "metrics": {"IMPRESSION": 1}},
    ]}}
    provider = PinterestApiAnalyticsProvider(lambda _: _service(lambda request: httpx.Response(200, json=outside)))
    with pytest.raises(AnalyticsNormalizationError, match="outside"):
        provider.fetch_pin_analytics("account", "p1", PERIOD_START, PERIOD_END)


def test_provider_rejects_malformed_metric_values():
    provider = PinterestApiAnalyticsProvider(
        lambda _: _service(lambda request: httpx.Response(
            200, json={"p1": {"summary_metrics": {"IMPRESSION": "many"}}}
        ))
    )
    with pytest.raises(AnalyticsNormalizationError):
        provider.fetch_pin_analytics("account", "p1", PERIOD_START, PERIOD_END)


def test_account_provider_keeps_account_metrics_separate_and_nullable():
    provider = PinterestApiAnalyticsProvider(
        lambda _: _service(lambda request: httpx.Response(200, json={
            "all": {"summary_metrics": {
                "PROFILE_VISIT": 0,
                "FOLLOW": 3,
                "TOTAL_AUDIENCE": None,
                "ENGAGED_AUDIENCE": 1,
            }}
        }))
    )
    result = provider.fetch_account_analytics("account", PERIOD_START, PERIOD_END)
    assert result.profile_visits == 0
    assert result.follows == 3
    assert result.total_audience is None
    assert result.engaged_audience == 1


def test_account_provider_normalizes_daily_metric_rows():
    provider = PinterestApiAnalyticsProvider(lambda _: _service(lambda request: httpx.Response(200, json={
        "all": {"daily_metrics": [
            {"data_status": "READY", "date": "2026-09-06", "metrics": {"FOLLOW": 0}},
            {"data_status": "READY", "date": "2026-09-07", "metrics": {"FOLLOW": 4}},
        ]}
    })))

    result = provider.fetch_account_analytics("account", PERIOD_START, PERIOD_END)

    assert [(row.metric_date, row.follows) for row in result] == [
        (date(2026, 9, 6), 0), (date(2026, 9, 7), 4),
    ]


def test_database_provider_classifies_missing_credentials_without_network_or_secret_output():
    db = SessionLocal()
    try:
        account = PinterestAccount(
            account_name="Unconnected analytics account",
            account_identifier="unconnected-account",
            is_active=True,
        )
        db.add(account)
        db.commit()
        provider = PinterestApiAnalyticsProvider.from_database(db)
        with pytest.raises(AnalyticsConfigurationError, match="credentials are unavailable"):
            provider.fetch_pin_analytics(
                account.account_identifier, "external-pin", PERIOD_START, PERIOD_END
            )
    finally:
        db.close()


def test_api_provider_collects_into_existing_published_pin_snapshots_without_real_network():
    http_calls = []

    def handler(request):
        http_calls.append(request.url.path)
        if request.url.path.endswith("/analytics") and "/pins/" in request.url.path:
            return httpx.Response(200, json={"external-1": {"summary_metrics": {
                "IMPRESSION": 25,
                "SAVE": 4,
                "PIN_CLICK": 2,
                "OUTBOUND_CLICK": 1,
                "ENGAGEMENT": 7,
                "ENGAGEMENT_RATE": 0.28,
            }}})
        return httpx.Response(200, json={"all": {"summary_metrics": {
            "PROFILE_VISIT": 5,
            "FOLLOW": 1,
        }}})

    db = SessionLocal()
    try:
        account = PinterestAccount(
            account_name="Mock analytics account",
            account_identifier="mock-account",
            is_active=True,
        )
        pin = Pin(
            product=Product(title="Analytics test product"),
            title="Published Pin",
            description="Mocked Pin",
            status="published",
            published_at=datetime(2026, 8, 1),
        )
        publication = PublishedPinterestPin(
            pin=pin,
            account=account,
            external_pin_id="external-1",
            published_at=pin.published_at,
            account_identifier_snapshot="mock-account",
        )
        db.add(publication)
        db.commit()

        provider = PinterestApiAnalyticsProvider(lambda _: _service(handler))
        result = AnalyticsCollector(db, provider).collect(
            account.id, PERIOD_START, PERIOD_END
        )

        snapshot = db.query(AnalyticsSnapshot).one()
        account_snapshot = db.query(PinterestAccountAnalyticsSnapshot).one()
        assert result.status == "completed"
        assert snapshot.published_pin_id == publication.id
        assert snapshot.pin_id == pin.id
        assert snapshot.impressions == 25
        assert snapshot.outbound_clicks == 1
        assert snapshot.engagement_rate == Decimal("0.28")
        assert account_snapshot.account_id == account.id
        assert account_snapshot.profile_visits == 5
        assert http_calls == [
            "/v5/pins/external-1/analytics",
            "/v5/user_account/analytics",
        ]
    finally:
        db.close()


def test_api_daily_rows_are_persisted_as_distinct_utc_day_snapshots():
    def handler(request):
        if "/pins/" in request.url.path:
            return httpx.Response(200, json={"external-1": {"daily_metrics": [
                {"data_status": "READY", "date": "2026-09-06", "metrics": {
                    "IMPRESSION": 0, "SAVE": None,
                }},
                {"data_status": "READY", "date": "2026-09-07", "metrics": {
                    "IMPRESSION": 8, "SAVE": 2,
                }},
            ]}})
        return httpx.Response(200, json={"all": {"daily_metrics": [
            {"data_status": "READY", "date": "2026-09-06", "metrics": {"FOLLOW": 0}},
            {"data_status": "READY", "date": "2026-09-07", "metrics": {"FOLLOW": 3}},
        ]}})

    db = SessionLocal()
    try:
        account = PinterestAccount(
            account_name="Daily API account", account_identifier="daily-account", is_active=True
        )
        pin = Pin(
            product=Product(title="Daily Pin product"), title="Daily Pin", description="Test",
            status="published", published_at=datetime(2026, 8, 1),
        )
        publication = PublishedPinterestPin(
            pin=pin, account=account, external_pin_id="external-1", published_at=pin.published_at,
            account_identifier_snapshot=account.account_identifier,
        )
        db.add(publication)
        db.commit()
        provider = PinterestApiAnalyticsProvider(lambda _: _service(handler))
        start = datetime(2026, 9, 6)
        end = datetime(2026, 9, 7, 23, 59, 59)

        first = AnalyticsCollector(db, provider).collect(account.id, start, end)
        second = AnalyticsCollector(db, provider).collect(account.id, start, end)

        pin_rows = db.query(AnalyticsSnapshot).order_by(AnalyticsSnapshot.metric_date).all()
        account_rows = db.query(PinterestAccountAnalyticsSnapshot).order_by(
            PinterestAccountAnalyticsSnapshot.metric_date
        ).all()
        assert first.status == second.status == "completed"
        assert first.run_id != second.run_id
        assert first.pin_snapshots_written == 2 and second.pin_snapshots_written == 2
        assert [(row.metric_date, row.period_start, row.period_end, row.impressions, row.saves) for row in pin_rows] == [
            (date(2026, 9, 6), datetime(2026, 9, 6), datetime(2026, 9, 7), 0, None),
            (date(2026, 9, 7), datetime(2026, 9, 7), datetime(2026, 9, 8), 8, 2),
        ]
        assert len(account_rows) == 2
        assert {row.collection_run_id for row in pin_rows} == {second.run_id}
        assert {row.collection_run_id for row in account_rows} == {second.run_id}
        assert [(row.metric_date, row.follows) for row in account_rows] == [
            (date(2026, 9, 6), 0), (date(2026, 9, 7), 3),
        ]
    finally:
        db.close()
