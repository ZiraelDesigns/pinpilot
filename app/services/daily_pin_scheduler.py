"""Prepare a bounded daily Pin queue from the local creative pool.

This module deliberately has no Etsy, Pinterest, or AI-provider dependency. It
only turns existing creatives into local ``Pin`` records; publishing and remote
creative generation remain separate, explicit workflows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Pin, PinCreative, PinGenerationJob, Product
from app.models.core import PinCreativeSourceType, PinCreativeStatus, PinStatus


DAILY_PIN_TARGET = 15
SCHEDULE_HOURS = (9, 12, 15, 18, 21)
_ELIGIBLE_CREATIVE_STATUSES = (
    PinCreativeStatus.DRAFT.value,
    PinCreativeStatus.APPROVED.value,
)


@dataclass(frozen=True)
class DailyScheduleResult:
    """A local scheduling result, with no side effects beyond database rows."""

    target: int
    already_prepared: int
    prepared: tuple[Pin, ...]
    mockups_prepared: int
    ai_prepared: int
    ai_jobs_created: int
    pending_ai_jobs: int

    @property
    def remaining(self) -> int:
        return max(0, self.target - self.already_prepared - len(self.prepared))


class DailyPinScheduler:
    """Choose unused creatives, always preferring Etsy mockups over AI copy."""

    def __init__(self, db: Session, daily_target: int = DAILY_PIN_TARGET):
        if daily_target < 1:
            raise ValueError("daily_target en az 1 olmalıdır.")
        self.db = db
        self.daily_target = daily_target

    def schedule_daily(self, target_date: date | None = None) -> DailyScheduleResult:
        """Create at most the remaining local Pin slots for ``target_date``.

        A creative is eligible only once, via ``Pin.creative_id``. Existing Pin
        rows without that link are also respected when they use the same product
        and source image. Pending AI jobs are observed but never executed.
        """
        target_date = target_date or datetime.now().date()
        day_start = datetime.combine(target_date, time.min)
        day_end = day_start + timedelta(days=1)

        already_prepared = len(self.db.scalars(
            select(Pin).where(
                Pin.scheduled_for >= day_start,
                Pin.scheduled_for < day_end,
                Pin.status.in_((PinStatus.SCHEDULED.value, PinStatus.PUBLISHED.value)),
            )
        ).all())
        slots_needed = max(0, self.daily_target - already_prepared)
        pending_jobs = self.db.query(PinGenerationJob).filter_by(status="pending").count()

        if not slots_needed:
            return DailyScheduleResult(
                target=self.daily_target,
                already_prepared=already_prepared,
                prepared=(),
                mockups_prepared=0,
                ai_prepared=0,
                ai_jobs_created=0,
                pending_ai_jobs=pending_jobs,
            )

        used_creative_ids = set(self.db.scalars(
            select(Pin.creative_id).where(Pin.creative_id.is_not(None))
        ).all())
        used_images = {
            (product_id, image_path)
            for product_id, image_path in self.db.execute(
                select(Pin.product_id, Pin.image_path).where(Pin.image_path.is_not(None))
            )
            if product_id is not None and image_path
        }

        candidates = self.db.scalars(
            select(PinCreative).where(
                PinCreative.status.in_(_ELIGIBLE_CREATIVE_STATUSES),
                PinCreative.source_type.in_(
                    (PinCreativeSourceType.MOCKUP.value, PinCreativeSourceType.AI.value)
                ),
            )
        ).all()
        candidates.sort(key=self._candidate_sort_key)

        selected: list[PinCreative] = []
        for creative in candidates:
            if creative.id in used_creative_ids:
                continue
            image_identifier = creative.source_image_url or creative.image_path
            if image_identifier and (
                creative.product_id,
                image_identifier,
            ) in used_images:
                continue
            selected.append(creative)
            used_creative_ids.add(creative.id)
            if image_identifier:
                used_images.add((creative.product_id, image_identifier))
            if len(selected) == slots_needed:
                break

        prepared = tuple(
            self._pin_from_creative(creative, target_date, already_prepared + index)
            for index, creative in enumerate(selected)
        )
        self.db.add_all(prepared)
        shortage = slots_needed - len(prepared)
        ai_jobs_created = self._queue_missing_ai_work(shortage, selected)
        self.db.commit()

        return DailyScheduleResult(
            target=self.daily_target,
            already_prepared=already_prepared,
            prepared=prepared,
            mockups_prepared=sum(
                creative.source_type == PinCreativeSourceType.MOCKUP.value
                for creative in selected
            ),
            ai_prepared=sum(
                creative.source_type == PinCreativeSourceType.AI.value
                for creative in selected
            ),
            ai_jobs_created=ai_jobs_created,
            pending_ai_jobs=self.db.query(PinGenerationJob).filter_by(status="pending").count(),
        )

    def _queue_missing_ai_work(
        self, shortage: int, selected: list[PinCreative]
    ) -> int:
        """Queue only uncovered creative capacity; never execute it here."""
        if shortage <= 0:
            return 0

        pending_capacity = sum(
            job.requested_count
            for job in self.db.scalars(
                select(PinGenerationJob).where(PinGenerationJob.status == "pending")
            )
        )
        missing_capacity = max(0, shortage - pending_capacity)
        if not missing_capacity:
            return 0

        product = selected[0].product if selected else self.db.scalars(
            select(Product).order_by(Product.id)
        ).first()
        if not product:
            return 0

        self.db.add(PinGenerationJob(
            product_id=product.id,
            requested_count=missing_capacity,
            status="pending",
        ))
        return 1

    @staticmethod
    def _candidate_sort_key(creative: PinCreative) -> tuple[int, int, datetime, int]:
        source_priority = 0 if creative.source_type == PinCreativeSourceType.MOCKUP.value else 1
        approval_priority = 0 if creative.status == PinCreativeStatus.APPROVED.value else 1
        return (source_priority, approval_priority, creative.created_at, creative.id)

    def _pin_from_creative(
        self, creative: PinCreative, target_date: date, slot_index: int
    ) -> Pin:
        # The fixed production queue is 3 Pins at each of 09:00, 12:00, 15:00,
        # 18:00 and 21:00. This is planning data only, never a Pinterest call.
        hour = SCHEDULE_HOURS[min(slot_index // 3, len(SCHEDULE_HOURS) - 1)]
        scheduled_for = datetime.combine(target_date, time(hour=hour))
        product = creative.product
        title = (creative.title or "").strip() or (product.title or "").strip() or "Etsy product"
        description = (
            (creative.description or "").strip()
            or (product.description or "").strip()
            or f"Explore {title} and view the product details."
        )
        return Pin(
            product_id=creative.product_id,
            creative_id=creative.id,
            title=title[:255],
            description=description,
            image_path=creative.image_path or creative.source_image_url or product.image_url,
            destination_url=creative.destination_url or product.url,
            status=PinStatus.SCHEDULED.value,
            scheduled_for=scheduled_for,
        )
