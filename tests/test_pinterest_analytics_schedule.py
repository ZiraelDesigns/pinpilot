from datetime import date

from app.config import settings
from app.database import SessionLocal
from app.models import AnalyticsCollectionRun, PinterestAccount, PinterestAccountAnalyticsSnapshot
from app.services.pinterest_analytics import AccountAnalyticsDTO
from scripts.collect_pinterest_analytics import collect_recent_days


class DailyAccountProvider:
    def fetch_pin_analytics(self, account_identifier, external_pin_id, period_start, period_end):
        return ()

    def fetch_account_analytics(self, account_identifier, period_start, period_end):
        return (AccountAnalyticsDTO(metric_date=period_start.date(), follows=3),)


def test_collection_entrypoint_is_closed_without_instantiating_provider(monkeypatch):
    monkeypatch.setattr(settings, "pinterest_analytics_collection_enabled", False)

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled analytics collection must not touch a provider or database")

    result = collect_recent_days(db_factory=forbidden, provider_factory=forbidden)

    assert result == {"enabled": False, "runs": 0, "failed": 0, "snapshots": 0}


def test_scheduled_retry_reuses_account_utc_day_run_and_snapshot(monkeypatch):
    monkeypatch.setattr(settings, "pinterest_analytics_collection_enabled", True)
    with SessionLocal() as db:
        db.add(PinterestAccount(
            account_name="Scheduled account", account_identifier="scheduled-account", is_active=True
        ))
        db.commit()

    provider = DailyAccountProvider()
    first = collect_recent_days(
        provider_factory=lambda _: provider,
        today_utc=date(2026, 9, 30),
        days=1,
    )
    second = collect_recent_days(
        provider_factory=lambda _: provider,
        today_utc=date(2026, 9, 30),
        days=1,
    )

    with SessionLocal() as db:
        runs = db.query(AnalyticsCollectionRun).all()
        snapshots = db.query(PinterestAccountAnalyticsSnapshot).all()
        assert len(runs) == 1
        assert runs[0].period_start.date() == date(2026, 9, 29)
        assert len(snapshots) == 1
        assert snapshots[0].metric_date == date(2026, 9, 29)
        assert snapshots[0].follows == 3
        assert first["runs"] == second["runs"] == 1
        assert first["failed"] == second["failed"] == 0
