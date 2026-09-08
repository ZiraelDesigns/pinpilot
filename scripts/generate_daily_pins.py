"""Prepare the daily local Pin queue without calling any remote provider."""

from __future__ import annotations

from app.database import SessionLocal
from app.services.daily_pin_scheduler import DAILY_PIN_TARGET, DailyPinScheduler


def main() -> None:
    db = SessionLocal()
    try:
        result = DailyPinScheduler(db).schedule_daily()
        print("PinPilot - Daily Pin Queue")
        print(f"Daily target: {DAILY_PIN_TARGET}")
        print(f"Already prepared: {result.already_prepared}")
        print(f"Prepared now: {len(result.prepared)}")
        print(f"Mockups prepared: {result.mockups_prepared}")
        print(f"AI creatives prepared: {result.ai_prepared}")
        print(f"Pending AI jobs retained: {result.pending_ai_jobs}")
        print(f"Unfilled slots: {result.remaining}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
