from collections import Counter
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Pin, PinCreative, PinGenerationJob, Product
from app.models.core import PinCreativeSourceType, PinCreativeStatus, PinCreativeType, PinStatus
from app.services.ai_content import AIContentService
from app.services.daily_pin_scheduler import DAILY_PIN_TARGET, DailyPinScheduler


def _product_with_creatives(db, mockup_count=0, ai_count=0, same_mockup_image=False):
    product = Product(title="Scheduler product", description="A product for scheduler tests")
    db.add(product)
    db.flush()

    creatives = []
    for source_type, count in (
        (PinCreativeSourceType.MOCKUP.value, mockup_count),
        (PinCreativeSourceType.AI.value, ai_count),
    ):
        for number in range(count):
            image_url = (
                "https://images.example.test/same.jpg"
                if source_type == PinCreativeSourceType.MOCKUP.value and same_mockup_image
                else f"https://images.example.test/{source_type}-{number}.jpg"
            )
            creatives.append(PinCreative(
                product_id=product.id,
                creative_type=PinCreativeType.PRODUCT_FOCUS.value,
                title=f"{source_type} creative {number}",
                description="Useful Pinterest description",
                keywords=["product"],
                call_to_action="View product",
                image_path=image_url,
                source_image_url=image_url if source_type == PinCreativeSourceType.MOCKUP.value else None,
                source_type=source_type,
                destination_url="https://example.test/product",
                status=PinCreativeStatus.DRAFT.value,
                generation_key=f"scheduler:{product.id}:{source_type}:{number}",
            ))
    db.add_all(creatives)
    db.commit()
    return product, creatives


def _cleanup(db, product):
    db.query(Pin).filter_by(product_id=product.id).delete(synchronize_session=False)
    db.query(PinGenerationJob).filter_by(product_id=product.id).delete(synchronize_session=False)
    db.query(PinCreative).filter_by(product_id=product.id).delete(synchronize_session=False)
    db.delete(product)
    db.commit()


def test_mockup_pool_fills_daily_target_without_ai_generation(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Scheduler must not invoke AI generation when mockups are available")

    monkeypatch.setattr(AIContentService, "generate", fail_if_called)
    with TestClient(app):
        db = SessionLocal()
        product, _ = _product_with_creatives(db, mockup_count=307)
        job = PinGenerationJob(product_id=product.id, status="pending")
        db.add(job)
        db.commit()
        try:
            pending_before = db.query(PinGenerationJob).filter_by(status="pending").count()
            result = DailyPinScheduler(db).schedule_daily(date(2035, 1, 1))

            assert result.target == DAILY_PIN_TARGET == 15
            assert len(result.prepared) == 15
            assert result.mockups_prepared == 15
            assert result.ai_prepared == 0
            assert result.pending_ai_jobs == pending_before
            assert {pin.creative_id for pin in result.prepared}.__len__() == 15
            assert all(pin.creative.source_type == PinCreativeSourceType.MOCKUP.value for pin in result.prepared)
            assert Counter(pin.scheduled_for.hour for pin in result.prepared) == {
                9: 3, 12: 3, 15: 3, 18: 3, 21: 3,
            }
            db.refresh(job)
            assert job.status == "pending"

            rerun = DailyPinScheduler(db).schedule_daily(date(2035, 1, 1))
            assert rerun.already_prepared == 15
            assert rerun.prepared == ()
        finally:
            _cleanup(db, product)
            db.close()


def test_ai_pool_fills_daily_target_when_no_mockups_exist(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Scheduler must not invoke an AI provider")

    monkeypatch.setattr(AIContentService, "generate", fail_if_called)
    with TestClient(app):
        db = SessionLocal()
        product, _ = _product_with_creatives(db, ai_count=20)
        try:
            result = DailyPinScheduler(db).schedule_daily(date(2035, 1, 6))

            assert len(result.prepared) == 15
            assert result.mockups_prepared == 0
            assert result.ai_prepared == 15
            assert all(pin.creative.source_type == "ai" for pin in result.prepared)
            assert result.ai_jobs_created == 0
        finally:
            _cleanup(db, product)
            db.close()


def test_shortfall_queues_pending_ai_capacity_without_provider_call(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Scheduler must never execute AI generation")

    monkeypatch.setattr(AIContentService, "generate", fail_if_called)
    with TestClient(app):
        db = SessionLocal()
        product, _ = _product_with_creatives(db, mockup_count=2)
        try:
            first = DailyPinScheduler(db).schedule_daily(date(2035, 1, 7))
            pending = db.query(PinGenerationJob).filter_by(status="pending").all()

            assert len(first.prepared) == 2
            assert first.mockups_prepared == 2
            assert first.ai_prepared == 0
            assert first.ai_jobs_created == 1
            assert len(pending) == 1
            assert pending[0].product_id == product.id
            assert pending[0].requested_count == 13

            rerun = DailyPinScheduler(db).schedule_daily(date(2035, 1, 7))
            assert rerun.prepared == ()
            assert rerun.ai_jobs_created == 0
            assert db.query(PinGenerationJob).filter_by(status="pending").count() == 1
        finally:
            _cleanup(db, product)
            db.close()


def test_ai_creatives_fill_only_the_shortfall_without_provider_calls(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Preparing existing AI creatives must not call an AI provider")

    monkeypatch.setattr(AIContentService, "generate", fail_if_called)
    with TestClient(app):
        db = SessionLocal()
        product, _ = _product_with_creatives(db, mockup_count=3, ai_count=20)
        try:
            result = DailyPinScheduler(db).schedule_daily(date(2035, 1, 2))

            assert len(result.prepared) == 15
            assert result.mockups_prepared == 3
            assert result.ai_prepared == 12
            assert [pin.creative.source_type for pin in result.prepared[:3]] == ["mockup"] * 3
            assert all(pin.creative.source_type == "ai" for pin in result.prepared[3:])
        finally:
            _cleanup(db, product)
            db.close()


def test_used_creative_and_same_product_image_are_never_scheduled_twice():
    with TestClient(app):
        db = SessionLocal()
        product, creatives = _product_with_creatives(
            db, mockup_count=2, same_mockup_image=True
        )
        try:
            first = DailyPinScheduler(db, daily_target=2).schedule_daily(date(2035, 1, 3))
            assert len(first.prepared) == 1
            assert first.prepared[0].creative_id == creatives[0].id
            assert first.prepared[0].creative.source_type == "mockup"
            assert first.prepared[0].status == PinStatus.SCHEDULED.value

            next_day = DailyPinScheduler(db, daily_target=2).schedule_daily(date(2035, 1, 4))
            assert next_day.prepared == ()

            # A legacy Pin without creative_id also prevents its source image from
            # entering the pool again.
            db.add(Pin(
                product_id=product.id,
                title="Legacy Pin",
                image_path="https://images.example.test/legacy.jpg",
                status=PinStatus.PUBLISHED.value,
                scheduled_for=datetime(2035, 1, 2),
                published_at=datetime(2035, 1, 2) + timedelta(hours=1),
            ))
            db.commit()
            legacy_creative = PinCreative(
                product_id=product.id,
                creative_type=PinCreativeType.PRODUCT_FOCUS.value,
                title="Legacy source creative",
                description="Description",
                keywords=["legacy"],
                call_to_action="View",
                image_path="https://images.example.test/legacy.jpg",
                source_image_url="https://images.example.test/legacy.jpg",
                source_type="mockup",
                destination_url="https://example.test/product",
                status="draft",
                generation_key=f"scheduler:{product.id}:legacy",
            )
            db.add(legacy_creative)
            db.commit()

            legacy_day = DailyPinScheduler(db, daily_target=1).schedule_daily(date(2035, 1, 5))
            assert legacy_day.prepared == ()
        finally:
            _cleanup(db, product)
            db.close()
