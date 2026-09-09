"""Prepare the daily local Pin queue without calling any remote provider."""

from __future__ import annotations

import logging

from app.database import SessionLocal
from app.services.daily_pin_scheduler import DAILY_PIN_TARGET, DailyPinScheduler

logger = logging.getLogger(__name__)


def run_daily_queue() -> int:
    """Run the local queue once and return a process-friendly status code.

    This is intentionally provider-free: it only reads/writes local scheduler
    records. Under systemd, stdout/stderr are captured by the journal.
    """
    db = SessionLocal()
    try:
        result = DailyPinScheduler(db).schedule_daily()
        logger.info(
            "Daily Pin queue completed: target=%s already_prepared=%s prepared=%s "
            "mockups=%s ai=%s pending_ai_jobs=%s unfilled=%s",
            DAILY_PIN_TARGET,
            result.already_prepared,
            len(result.prepared),
            result.mockups_prepared,
            result.ai_prepared,
            result.pending_ai_jobs,
            result.remaining,
        )
        return 0
    except Exception:
        # A failed oneshot unit is recorded in journalctl while pinpilot.service
        # remains independent and continues serving the dashboard.
        logger.exception("Daily Pin queue failed")
        return 1
    finally:
        db.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return run_daily_queue()


if __name__ == "__main__":
    raise SystemExit(main())
