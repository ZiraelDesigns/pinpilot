"""Optional scheduled Pinterest analytics collection for completed UTC days.

This entry point is fail-closed by default. It does not instantiate an API
provider or access OAuth credentials unless the explicit collection setting is
enabled after Pinterest access is approved.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import AnalyticsCollectionRun, PinterestAccount
from app.services.pinterest_analytics import AnalyticsCollector
from app.services.pinterest_analytics_provider import PinterestApiAnalyticsProvider


def collect_recent_days(
    db_factory: Callable[[], Session] = SessionLocal,
    provider_factory: Callable[[Session], PinterestApiAnalyticsProvider] | None = None,
    *,
    today_utc: date | None = None,
    days: int = 7,
) -> dict[str, int | bool]:
    """Refresh the last complete UTC days, reusing each day's durable run on retry."""
    if not settings.pinterest_analytics_collection_enabled:
        return {"enabled": False, "runs": 0, "failed": 0, "snapshots": 0}
    if days < 1 or days > 90:
        raise ValueError("days must be between 1 and 90")

    db = db_factory()
    try:
        provider = (provider_factory or PinterestApiAnalyticsProvider.from_database)(db)
        collector = AnalyticsCollector(db, provider)
        current_day = today_utc or datetime.now(UTC).date()
        accounts = db.scalars(
            select(PinterestAccount)
            .where(PinterestAccount.is_active.is_(True))
            .order_by(PinterestAccount.id)
        ).all()
        report = {"enabled": True, "runs": 0, "failed": 0, "snapshots": 0}

        for account in accounts:
            for offset in range(1, days + 1):
                metric_day = current_day - timedelta(days=offset)
                period_start = datetime.combine(metric_day, time.min)
                # Pinterest's end_date is an inclusive UTC calendar date. Keep
                # this datetime on that same date; stored snapshot bounds use
                # the canonical half-open [midnight, next midnight) interval.
                period_end = datetime.combine(metric_day, time.max)
                run = db.scalar(
                    select(AnalyticsCollectionRun)
                    .where(
                        AnalyticsCollectionRun.account_id == account.id,
                        AnalyticsCollectionRun.period_start == period_start,
                        AnalyticsCollectionRun.period_end == period_end,
                        AnalyticsCollectionRun.scope == "all",
                    )
                    .order_by(AnalyticsCollectionRun.id.desc())
                )
                result = collector.collect(
                    account.id,
                    period_start,
                    period_end,
                    run_id=run.id if run else None,
                )
                report["runs"] += 1
                report["failed"] += int(result.status == "failed")
                report["snapshots"] += result.pin_snapshots_written
        return report
    finally:
        db.close()


def main() -> None:
    result = collect_recent_days()
    if not result["enabled"]:
        print("Pinterest analytics collection is disabled; no provider was initialized.")
        return
    print(
        "Pinterest analytics collection finished: "
        f"runs={result['runs']} failed={result['failed']} pin_snapshots={result['snapshots']}"
    )


if __name__ == "__main__":
    main()
