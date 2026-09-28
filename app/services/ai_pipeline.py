"""Persistent AI-pipeline control and concurrency-safe daily generation quota."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import AIDailyQuotaSlot, AIPipelineControl, PinCreative, PinGenerationJob, Product
from app.models.core import PinCreativeSourceType

AI_DAILY_CREATIVE_QUOTA = 15


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AIPipelinePausedError(RuntimeError):
    """Raised when a new generation is requested while the pipeline is paused."""


class AIDailyQuotaExceededError(RuntimeError):
    """Raised when no AI creative capacity remains for the UTC day."""


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def ensure_pipeline_control(db: Session) -> AIPipelineControl:
    control = db.get(AIPipelineControl, 1)
    if control is None:
        control = AIPipelineControl(id=1, enabled=True)
        db.add(control)
        db.flush()
    return control


def pipeline_enabled(db: Session) -> bool:
    return bool(ensure_pipeline_control(db).enabled)


def lock_pipeline_if_enabled(db: Session) -> bool:
    """Serialize job creation/reservation with pause changes on the control row."""
    ensure_pipeline_control(db)
    result = db.execute(update(AIPipelineControl).where(
        AIPipelineControl.id == 1,
        AIPipelineControl.enabled.is_(True),
    ).values(enabled=True))
    return result.rowcount == 1


def set_pipeline_enabled(db: Session, enabled: bool) -> AIPipelineControl:
    control = ensure_pipeline_control(db)
    control.enabled = bool(enabled)
    control.updated_at = utc_now()
    db.commit()
    db.refresh(control)
    return control


def _ensure_daily_slots(db: Session, quota_day: date) -> None:
    rows = [
        {"quota_date": quota_day, "slot_number": number, "state": "available", "updated_at": utc_now()}
        for number in range(1, AI_DAILY_CREATIVE_QUOTA + 1)
    ]
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(AIDailyQuotaSlot).values(rows).on_conflict_do_nothing(
            index_elements=["quota_date", "slot_number"]
        )
        db.execute(statement)
    elif dialect == "postgresql":
        statement = pg_insert(AIDailyQuotaSlot).values(rows).on_conflict_do_nothing(
            constraint="uq_ai_quota_date_slot"
        )
        db.execute(statement)
    else:
        existing = set(db.scalars(select(AIDailyQuotaSlot.slot_number).where(
            AIDailyQuotaSlot.quota_date == quota_day
        )).all())
        for row in rows:
            if row["slot_number"] not in existing:
                db.add(AIDailyQuotaSlot(**row))
    db.flush()


def _legacy_success_count(db: Session, quota_day: date) -> int:
    start = datetime.combine(quota_day, time.min)
    end = start + timedelta(days=1)
    completed_slot_creatives = select(AIDailyQuotaSlot.creative_id).where(
        AIDailyQuotaSlot.quota_date == quota_day,
        AIDailyQuotaSlot.state == "completed",
        AIDailyQuotaSlot.creative_id.is_not(None),
    )
    return int(db.scalar(select(func.count(PinCreative.id)).where(
        PinCreative.source_type == PinCreativeSourceType.AI.value,
        PinCreative.created_at >= start,
        PinCreative.created_at < end,
        PinCreative.id.not_in(completed_slot_creatives),
    )) or 0)


def quota_counts(db: Session, quota_day: date | None = None) -> dict[str, int]:
    quota_day = quota_day or utc_today()
    legacy = _legacy_success_count(db, quota_day)
    completed = int(db.scalar(select(func.count(AIDailyQuotaSlot.id)).where(
        AIDailyQuotaSlot.quota_date == quota_day,
        AIDailyQuotaSlot.state == "completed",
    )) or 0)
    reserved = int(db.scalar(select(func.count(AIDailyQuotaSlot.id)).where(
        AIDailyQuotaSlot.quota_date == quota_day,
        AIDailyQuotaSlot.state == "reserved",
    )) or 0)
    used = min(AI_DAILY_CREATIVE_QUOTA, legacy + completed)
    return {
        "limit": AI_DAILY_CREATIVE_QUOTA,
        "generated": used,
        "reserved": reserved,
        "remaining": max(0, AI_DAILY_CREATIVE_QUOTA - used - reserved),
    }


def reserve_ai_capacity(
    db: Session,
    requested_count: int,
    *,
    job_id: int | None = None,
    quota_day: date | None = None,
) -> list[AIDailyQuotaSlot]:
    """Reserve up to the remaining quota via conditional DB updates.

    Each slot can transition from available to reserved only once at a time. This
    compare-and-set update is the cross-worker guard; callers commit reservations
    before making any provider request.
    """
    if requested_count < 1:
        return []
    if not lock_pipeline_if_enabled(db):
        raise AIPipelinePausedError("AI Pipeline kapalı. Yeni AI üretimi başlatılamaz.")
    quota_day = quota_day or utc_today()
    _ensure_daily_slots(db, quota_day)
    legacy = _legacy_success_count(db, quota_day)
    completed = int(db.scalar(select(func.count(AIDailyQuotaSlot.id)).where(
        AIDailyQuotaSlot.quota_date == quota_day,
        AIDailyQuotaSlot.state == "completed",
    )) or 0)
    reserved_count = int(db.scalar(select(func.count(AIDailyQuotaSlot.id)).where(
        AIDailyQuotaSlot.quota_date == quota_day,
        AIDailyQuotaSlot.state == "reserved",
    )) or 0)
    capacity = max(0, AI_DAILY_CREATIVE_QUOTA - min(AI_DAILY_CREATIVE_QUOTA, legacy + completed) - reserved_count)
    wanted = min(requested_count, capacity)
    if wanted == 0:
        raise AIDailyQuotaExceededError("Bugünkü 15 AI creative kotası doldu.")

    # Legacy AI creatives occupy the earliest logical quota slots. Existing slot
    # rows are reserved with an UPDATE predicate to handle simultaneous workers.
    candidates = db.scalars(select(AIDailyQuotaSlot).where(
        AIDailyQuotaSlot.quota_date == quota_day,
        AIDailyQuotaSlot.slot_number > legacy,
        AIDailyQuotaSlot.state == "available",
    ).order_by(AIDailyQuotaSlot.slot_number)).all()
    reserved: list[AIDailyQuotaSlot] = []
    for candidate in candidates:
        if len(reserved) >= wanted:
            break
        changed = db.execute(update(AIDailyQuotaSlot).where(
            AIDailyQuotaSlot.id == candidate.id,
            AIDailyQuotaSlot.state == "available",
        ).values(state="reserved", job_id=job_id, creative_id=None, updated_at=utc_now()))
        if changed.rowcount == 1:
            reserved.append(candidate)
    if not reserved:
        raise AIDailyQuotaExceededError("Bugünkü 15 AI creative kotası doldu.")
    db.commit()
    for slot in reserved:
        db.refresh(slot)
    return reserved


def finish_reservations(
    db: Session,
    slots: list[AIDailyQuotaSlot],
    creatives: list[PinCreative],
) -> None:
    for index, slot in enumerate(slots):
        if index < len(creatives):
            slot.state = "completed"
            slot.creative_id = creatives[index].id
        else:
            slot.state = "available"
            slot.job_id = None
            slot.creative_id = None
        slot.updated_at = utc_now()
    db.commit()


def release_reservations(db: Session, slot_ids: list[int]) -> None:
    """Free reserved capacity after provider failure without touching history."""
    if not slot_ids:
        return
    db.execute(update(AIDailyQuotaSlot).where(
        AIDailyQuotaSlot.id.in_(slot_ids), AIDailyQuotaSlot.state == "reserved",
    ).values(state="available", job_id=None, creative_id=None, updated_at=utc_now()))
    db.commit()


def create_generation_job(
    db: Session, product: Product, requested_count: int = AI_DAILY_CREATIVE_QUOTA
) -> PinGenerationJob | None:
    """Queue demand when enabled; generation itself reserves capacity later."""
    if not lock_pipeline_if_enabled(db):
        return None
    remaining = quota_counts(db)["remaining"]
    if remaining <= 0:
        return None
    db.flush()
    existing = db.query(PinGenerationJob.id).filter(
        PinGenerationJob.product_id == product.id,
        PinGenerationJob.status.in_(("pending", "processing", "running")),
    ).first()
    if existing:
        return None
    job = PinGenerationJob(
        product_id=product.id,
        requested_count=max(1, min(requested_count, remaining, AI_DAILY_CREATIVE_QUOTA)),
        status="pending",
    )
    db.add(job)
    return job


def enqueue_daily_generation_job(db: Session) -> bool:
    """Create at most one independent daily AI batch, regardless of mockup stock."""
    if not lock_pipeline_if_enabled(db):
        return False
    counts = quota_counts(db)
    # Pending/processing demand is not a quota reservation: it remains queued for
    # a later day if today's successful generation capacity is exhausted.
    if counts["remaining"] <= 0:
        return False
    product = db.scalars(select(Product).order_by(Product.id)).first()
    if not product:
        return False
    pending = db.scalars(select(PinGenerationJob).where(
        PinGenerationJob.status == "pending"
    ).order_by(PinGenerationJob.created_at, PinGenerationJob.id)).first()
    if pending:
        if pending.requested_count < counts["remaining"]:
            pending.requested_count = counts["remaining"]
            return True
        return False
    db.add(PinGenerationJob(
        product_id=product.id,
        requested_count=counts["remaining"],
        status="pending",
    ))
    return True


def dashboard_pipeline_status(db: Session) -> dict[str, int | bool]:
    counts = quota_counts(db)
    return {
        "enabled": pipeline_enabled(db),
        **counts,
        "pending_jobs": int(db.scalar(select(func.count(PinGenerationJob.id)).where(
            PinGenerationJob.status == "pending"
        )) or 0),
        "mockup_creatives": int(db.scalar(select(func.count(PinCreative.id)).where(
            PinCreative.source_type == PinCreativeSourceType.MOCKUP.value
        )) or 0),
        "ai_creatives": int(db.scalar(select(func.count(PinCreative.id)).where(
            PinCreative.source_type == PinCreativeSourceType.AI.value
        )) or 0),
    }
