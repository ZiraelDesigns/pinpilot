from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest

from app.database import SessionLocal
from app.models import (
    AnalyticsCollectionRun,
    AnalyticsSnapshot,
    Pin,
    PinterestAccount,
    PinterestAccountAnalyticsSnapshot,
    Product,
    PublishedPinterestPin,
)
from app.services.pinterest_analytics import (
    AccountAnalyticsDTO,
    AnalyticsAuthenticationError,
    AnalyticsCollector,
    AnalyticsConfigurationError,
    AnalyticsRateLimitError,
    AnalyticsNormalizationError,
    PinAnalyticsDTO,
    TemporaryAnalyticsProviderError,
)


PERIOD_START = datetime(2026, 9, 1)
PERIOD_END = datetime(2026, 9, 8)
METRIC_DATE = date(2026, 9, 7)


class FakePinterestAnalyticsProvider:
    """Scriptable test double. It has no HTTP client or network implementation."""

    def __init__(self, pin_results=None, account_result=None, account_error=None):
        self.pin_results = pin_results or {}
        self.account_result = account_result
        self.account_error = account_error
        self.pin_calls = []
        self.account_calls = []
        self.network_calls = 0

    def fetch_pin_analytics(self, account_identifier, external_pin_id, period_start, period_end):
        self.pin_calls.append((account_identifier, external_pin_id, period_start, period_end))
        result = self.pin_results.get(external_pin_id)
        if isinstance(result, Exception):
            raise result
        if result is None:
            return PinAnalyticsDTO(external_pin_id=external_pin_id)
        return result

    def fetch_account_analytics(self, account_identifier, period_start, period_end):
        self.account_calls.append((account_identifier, period_start, period_end))
        if self.account_error:
            raise self.account_error
        return self.account_result


def _seed_account(db, pin_ids=()):
    account = PinterestAccount(
        account_name="Analytics test",
        account_identifier="test-account-id",
        is_active=True,
    )
    db.add(account)
    publications = []
    for number, external_pin_id in enumerate(pin_ids, start=1):
        product = Product(title=f"Test product {number}")
        pin = Pin(
            product=product,
            title=f"Published Pin {number}",
            description="Test publication",
            status="published",
            published_at=datetime(2026, 8, number),
        )
        publication = PublishedPinterestPin(
            pin=pin,
            account=account,
            external_pin_id=external_pin_id,
            published_at=pin.published_at,
            account_identifier_snapshot=account.account_identifier,
        )
        db.add(publication)
        publications.append(publication)
    db.commit()
    return account, publications


def _collect(db, account, provider, *, run_id=None, include_pins=True, include_account=True):
    return AnalyticsCollector(db, provider).collect(
        account.id,
        PERIOD_START,
        PERIOD_END,
        run_id=run_id,
        include_pins=include_pins,
        include_account=include_account,
    )


def _pin_snapshot(db, published_pin_id, run_id=None):
    query = db.query(AnalyticsSnapshot).filter_by(published_pin_id=published_pin_id)
    if run_id is not None:
        query = query.filter_by(collection_run_id=run_id)
    return query.one()


def test_successful_pin_and_account_collection_persists_normalized_dtos():
    db = SessionLocal()
    try:
        account, publications = _seed_account(db, ["remote-1"])
        provider = FakePinterestAnalyticsProvider(
            {"remote-1": PinAnalyticsDTO(
                external_pin_id="remote-1",
                metric_date=METRIC_DATE,
                impressions=101,
                saves=7,
                pin_clicks=12,
                outbound_clicks=4,
                engagements=19,
                engagement_rate=Decimal("0.188118"),
                pin_click_rate=Decimal("0.118812"),
                outbound_click_rate=Decimal("0.039604"),
                metric_schema_version="internal-v1",
            )},
            AccountAnalyticsDTO(
                metric_date=METRIC_DATE,
                profile_visits=23,
                follows=5,
                total_audience=200,
                engaged_audience=31,
                metric_schema_version="internal-v1",
            ),
        )

        result = _collect(db, account, provider)

        snapshot = _pin_snapshot(db, publications[0].id, result.run_id)
        account_snapshot = db.query(PinterestAccountAnalyticsSnapshot).one()
        run = db.get(AnalyticsCollectionRun, result.run_id)
        assert result.status == "completed" and run.status == "completed"
        assert result.pin_snapshots_written == 1 and result.account_snapshot_written
        assert (snapshot.impressions, snapshot.saves, snapshot.pin_clicks) == (101, 7, 12)
        assert snapshot.outbound_clicks == 4 and snapshot.engagements == 19
        assert snapshot.engagement_rate == Decimal("0.188118")
        assert snapshot.metric_date == METRIC_DATE
        assert snapshot.period_start == PERIOD_START and snapshot.period_end == PERIOD_END
        assert account_snapshot.profile_visits == 23 and account_snapshot.follows == 5
        assert account_snapshot.total_audience == 200 and account_snapshot.engaged_audience == 31
    finally:
        db.close()


def test_multiple_pins_are_collected_independently():
    db = SessionLocal()
    try:
        account, publications = _seed_account(db, ["remote-a", "remote-b", "remote-c"])
        provider = FakePinterestAnalyticsProvider({
            "remote-a": PinAnalyticsDTO("remote-a", impressions=10),
            "remote-b": PinAnalyticsDTO("remote-b", impressions=20),
            "remote-c": PinAnalyticsDTO("remote-c", impressions=30),
        })
        result = _collect(db, account, provider, include_account=False)
        assert result.status == "completed"
        assert result.pin_snapshots_written == 3
        assert {call[1] for call in provider.pin_calls} == {"remote-a", "remote-b", "remote-c"}
        assert [_pin_snapshot(db, publication.id).impressions for publication in publications] == [10, 20, 30]
    finally:
        db.close()


def test_missing_and_null_pin_metrics_remain_null_but_explicit_zero_stays_zero():
    db = SessionLocal()
    try:
        account, publications = _seed_account(db, ["missing", "null", "zero"])
        provider = FakePinterestAnalyticsProvider({
            "missing": PinAnalyticsDTO("missing"),
            "null": PinAnalyticsDTO("null", impressions=None, saves=None, outbound_clicks=None),
            "zero": PinAnalyticsDTO("zero", impressions=0, saves=0, outbound_clicks=0, pin_clicks=0),
        })
        result = _collect(db, account, provider, include_account=False)
        missing, nulls, zero = [
            _pin_snapshot(db, publication.id, result.run_id) for publication in publications
        ]
        assert (missing.impressions, missing.saves, missing.outbound_clicks) == (None, None, None)
        assert (nulls.impressions, nulls.saves, nulls.outbound_clicks) == (None, None, None)
        assert (zero.impressions, zero.saves, zero.outbound_clicks, zero.pin_clicks) == (0, 0, 0, 0)
    finally:
        db.close()


def test_same_run_retry_upserts_instead_of_duplicating_pin_or_account_snapshots():
    db = SessionLocal()
    try:
        account, publications = _seed_account(db, ["repeat"])
        provider = FakePinterestAnalyticsProvider(
            {"repeat": PinAnalyticsDTO("repeat", impressions=1)},
            AccountAnalyticsDTO(follows=2),
        )
        first = _collect(db, account, provider)
        first_pin = _pin_snapshot(db, publications[0].id, first.run_id)
        first_pin_id = first_pin.id
        provider.pin_results["repeat"] = PinAnalyticsDTO("repeat", impressions=8, saves=3)
        provider.account_result = AccountAnalyticsDTO(follows=6)

        retry = _collect(db, account, provider, run_id=first.run_id)

        assert retry.status == "completed"
        assert db.query(AnalyticsSnapshot).filter_by(collection_run_id=first.run_id).count() == 1
        assert db.query(PinterestAccountAnalyticsSnapshot).filter_by(collection_run_id=first.run_id).count() == 1
        updated = _pin_snapshot(db, publications[0].id, first.run_id)
        assert updated.id == first_pin_id and updated.impressions == 8 and updated.saves == 3
        assert db.query(PinterestAccountAnalyticsSnapshot).one().follows == 6
    finally:
        db.close()


def test_different_runs_append_historical_snapshots():
    db = SessionLocal()
    try:
        account, publications = _seed_account(db, ["history"])
        provider = FakePinterestAnalyticsProvider({"history": PinAnalyticsDTO("history", impressions=5)})
        first = _collect(db, account, provider, include_account=False)
        provider.pin_results["history"] = PinAnalyticsDTO("history", impressions=9)
        second = _collect(db, account, provider, include_account=False)
        rows = db.query(AnalyticsSnapshot).filter_by(published_pin_id=publications[0].id).order_by(
            AnalyticsSnapshot.collection_run_id
        ).all()
        assert first.run_id != second.run_id
        assert [(row.collection_run_id, row.impressions) for row in rows] == [
            (first.run_id, 5), (second.run_id, 9)
        ]
    finally:
        db.close()


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (TemporaryAnalyticsProviderError("token=do-not-store"), "temporary_provider_error"),
        (AnalyticsRateLimitError("secret=do-not-store"), "rate_limit"),
        (AnalyticsAuthenticationError("bearer do-not-store"), "authentication_or_configuration"),
        (AnalyticsConfigurationError("api_key=do-not-store"), "authentication_or_configuration"),
    ],
)
def test_provider_error_classes_fail_run_without_persisting_exception_text(error, category):
    db = SessionLocal()
    try:
        account, _ = _seed_account(db, ["error-pin"])
        provider = FakePinterestAnalyticsProvider({"error-pin": error})
        result = _collect(db, account, provider, include_account=False)
        run = db.get(AnalyticsCollectionRun, result.run_id)
        assert result.status == "failed" and run.status == "failed"
        assert result.failures[0].category == category
        assert category in run.error_summary
        assert "do-not-store" not in run.error_summary
        assert "token" not in run.error_summary and "secret" not in run.error_summary
    finally:
        db.close()


def test_invalid_metric_is_a_normalization_failure():
    db = SessionLocal()
    try:
        account, _ = _seed_account(db, ["invalid"])
        provider = FakePinterestAnalyticsProvider({"invalid": PinAnalyticsDTO("invalid", saves=-1)})
        result = _collect(db, account, provider, include_account=False)
        assert result.status == "failed"
        assert result.failures[0].category == "normalization_or_validation"
        assert "normalization_or_validation" in db.get(AnalyticsCollectionRun, result.run_id).error_summary
    finally:
        db.close()


def test_normalizer_rejects_a_repeated_response_for_the_wrong_pin():
    db = SessionLocal()
    try:
        account, _ = _seed_account(db, ["requested"])
        provider = FakePinterestAnalyticsProvider({"requested": PinAnalyticsDTO("other-pin", impressions=1)})
        result = _collect(db, account, provider, include_account=False)
        assert result.status == "failed"
        assert result.failures[0].category == "normalization_or_validation"
    finally:
        db.close()


def test_partial_failure_keeps_successful_pin_and_account_snapshots():
    db = SessionLocal()
    try:
        account, publications = _seed_account(db, ["good", "temporary-failure"])
        provider = FakePinterestAnalyticsProvider(
            {
                "good": PinAnalyticsDTO("good", impressions=17),
                "temporary-failure": TemporaryAnalyticsProviderError("private token omitted"),
            },
            AccountAnalyticsDTO(follows=2),
        )
        result = _collect(db, account, provider)
        assert result.status == "failed"
        assert result.pin_snapshots_written == 1 and result.account_snapshot_written
        assert _pin_snapshot(db, publications[0].id, result.run_id).impressions == 17
        assert db.query(AnalyticsSnapshot).filter_by(published_pin_id=publications[1].id).count() == 0
        assert db.query(PinterestAccountAnalyticsSnapshot).filter_by(collection_run_id=result.run_id).count() == 1
        assert "private token" not in db.get(AnalyticsCollectionRun, result.run_id).error_summary
    finally:
        db.close()


def test_existing_legacy_analytics_snapshot_is_preserved():
    db = SessionLocal()
    try:
        account, publications = _seed_account(db, ["new-pin"])
        legacy = AnalyticsSnapshot(
            pin_id=publications[0].pin_id,
            impressions=0,
            saves=6,
            outbound_clicks=0,
            recorded_at=datetime(2026, 7, 1),
        )
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
        provider = FakePinterestAnalyticsProvider({"new-pin": PinAnalyticsDTO("new-pin", impressions=4)})
        result = _collect(db, account, provider, include_account=False)
        preserved = db.get(AnalyticsSnapshot, legacy_id)
        assert (preserved.impressions, preserved.saves, preserved.outbound_clicks) == (0, 6, 0)
        assert preserved.collection_run_id is None and preserved.published_pin_id is None
        assert db.query(AnalyticsSnapshot).filter_by(collection_run_id=result.run_id).count() == 1
    finally:
        db.close()


def test_account_collection_works_without_any_published_pins():
    db = SessionLocal()
    try:
        account, publications = _seed_account(db)
        provider = FakePinterestAnalyticsProvider(
            account_result=AccountAnalyticsDTO(profile_visits=0, follows=None)
        )
        result = _collect(db, account, provider)
        snapshot = db.query(PinterestAccountAnalyticsSnapshot).one()
        assert not publications
        assert result.status == "completed" and result.pin_snapshots_written == 0
        assert result.account_snapshot_written
        assert snapshot.profile_visits == 0 and snapshot.follows is None
        assert not provider.pin_calls and len(provider.account_calls) == 1
    finally:
        db.close()


def test_no_published_pin_rows_still_allows_account_provider_to_return_no_data():
    db = SessionLocal()
    try:
        account, _ = _seed_account(db)
        provider = FakePinterestAnalyticsProvider(account_result=None)
        result = _collect(db, account, provider)
        assert result.status == "completed"
        assert not result.account_snapshot_written
        assert db.query(PinterestAccountAnalyticsSnapshot).count() == 0
        assert len(provider.account_calls) == 1
    finally:
        db.close()


def test_collector_and_fake_provider_make_no_http_or_network_calls(monkeypatch):
    def deny_network(*args, **kwargs):
        raise AssertionError("Network access is forbidden in analytics collector tests")

    monkeypatch.setattr(httpx, "get", deny_network)
    monkeypatch.setattr(httpx, "post", deny_network)
    db = SessionLocal()
    try:
        account, _ = _seed_account(db, ["offline"])
        provider = FakePinterestAnalyticsProvider(
            {"offline": PinAnalyticsDTO("offline", impressions=1)},
            AccountAnalyticsDTO(follows=1),
        )
        result = _collect(db, account, provider)
        assert result.status == "completed"
        assert provider.network_calls == 0
        assert provider.pin_calls[0][0] == "test-account-id"
        assert len(provider.account_calls) == 1
    finally:
        db.close()
