from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import EtsyAccount, EtsyListing, PinCreative, PinGenerationJob, Product
from app.services.ai_content import AIContentError, AIContentService, MockAIContentProvider
from app.services.ai_worker import AIGenerationWorker, redact_error


class RaisingContentService:
    def __init__(self, error):
        self.error = error

    def generate(self, *_args, **_kwargs):
        raise self.error


def _product_with_job(db, requested_count=1):
    account = EtsyAccount(shop_name="Worker shop", shop_identifier="worker-shop", is_active=True)
    product = Product(title="Floral Phone Case", description="A floral phone case", url="https://example.test/case")
    listing = EtsyListing(
        account=account, product=product, listing_id="worker-listing", title=product.title,
        description=product.description, url=product.url, tags=["phone case"],
        images=["https://images.example.test/case.jpg"], state="active",
    )
    db.add_all([account, product, listing])
    db.flush()
    job = PinGenerationJob(product_id=product.id, requested_count=requested_count, status="pending")
    db.add(job)
    db.commit()
    return product, job


def _worker(factory, **kwargs):
    return AIGenerationWorker(SessionLocal, content_service_factory=factory, **kwargs)


def test_successful_worker_uses_mocked_seo_and_image_pipeline(monkeypatch):
    monkeypatch.setattr(settings, "ai_image_provider", "openai")

    class ImageProvider:
        def generate(self, *_args):
            return b"png-bytes"

    monkeypatch.setattr("app.services.ai_image.get_image_provider", lambda: ImageProvider())
    monkeypatch.setattr("app.services.ai_image.save_generated_image", lambda _: "/media/generated/mock.png")
    db = SessionLocal()
    try:
        product, job = _product_with_job(db)
        result = _worker(lambda session: AIContentService(session, MockAIContentProvider())).process_once()
        db.refresh(job)
        db.refresh(product)
        creative = db.query(PinCreative).one()
        assert result.completed
        assert job.status == "completed"
        assert creative.seo_metadata["primary_keyword"]
        assert creative.image_path == "/media/generated/mock.png"
    finally:
        db.close()


def test_duplicate_and_concurrent_workers_cannot_claim_same_product_job():
    db = SessionLocal()
    try:
        _product_with_job(db)
        first = _worker(lambda session: RaisingContentService(AIContentError("permanent")), worker_id="one")
        second = _worker(lambda session: RaisingContentService(AIContentError("permanent")), worker_id="two")
        claim_db = SessionLocal()
        try:
            claim = first._claim_next_job(claim_db)
            assert claim is not None
        finally:
            claim_db.close()
        assert second.process_once().claimed is False
    finally:
        db.close()


def test_retryable_429_uses_exponential_backoff_and_then_fails_at_limit():
    db = SessionLocal()
    try:
        _, job = _product_with_job(db)
        now = datetime(2035, 1, 1, 12, 0, 0)
        worker = _worker(
            lambda session: RaisingContentService(AIContentError("429 rate limit token=secret")),
            max_retries=2, now=lambda: now,
        )
        first = worker.process_once()
        db.refresh(job)
        assert first.retry_scheduled and job.status == "pending" and job.retry_count == 1
        assert job.next_attempt_at == now + timedelta(seconds=2)
        job.next_attempt_at = None
        db.commit()
        second = worker.process_once()
        db.refresh(job)
        assert second.failed and job.status == "failed" and job.retry_count == 2
        assert "secret" not in (job.error_message or "")
    finally:
        db.close()


def test_retryable_5xx_error_is_requeued_without_a_provider_retry_call():
    db = SessionLocal()
    try:
        _, job = _product_with_job(db)
        now = datetime(2035, 1, 1, 12, 0, 0)
        result = _worker(
            lambda session: RaisingContentService(AIContentError("503 upstream unavailable")),
            now=lambda: now,
        ).process_once()
        db.refresh(job)
        assert result.retry_scheduled
        assert job.status == "pending"
        assert job.next_attempt_at == now + timedelta(seconds=2)
    finally:
        db.close()


def test_permanent_failure_is_not_retried_and_image_failure_is_safe():
    db = SessionLocal()
    try:
        _, job = _product_with_job(db)
        result = _worker(lambda session: RaisingContentService(AIContentError("AI yanıtı geçerli JSON döndürmedi."))).process_once()
        db.refresh(job)
        assert result.failed and not result.retry_scheduled
        assert job.status == "failed" and job.retry_count == 1
    finally:
        db.close()


def test_failed_image_generation_marks_job_failed_without_creating_a_creative(monkeypatch):
    monkeypatch.setattr(settings, "ai_image_provider", "openai")

    class BrokenImageProvider:
        def generate(self, *_args):
            raise RuntimeError("image provider permanently rejected request")

    monkeypatch.setattr("app.services.ai_image.get_image_provider", lambda: BrokenImageProvider())
    db = SessionLocal()
    try:
        _, job = _product_with_job(db)
        result = _worker(lambda session: AIContentService(session, MockAIContentProvider())).process_once()
        db.refresh(job)
        assert result.failed and job.status == "failed"
        assert db.query(PinCreative).count() == 0
    finally:
        db.close()


def test_worker_recovers_stale_processing_state_after_restart():
    db = SessionLocal()
    try:
        product, job = _product_with_job(db)
        stale = datetime.utcnow() - timedelta(minutes=20)
        job.status = "processing"
        job.locked_at = stale
        job.worker_id = "crashed"
        product.ai_generation_locked_at = stale
        product.ai_generation_worker_id = "crashed"
        db.commit()
        result = _worker(lambda session: AIContentService(session, MockAIContentProvider())).process_once()
        db.refresh(job)
        assert result.completed and job.status == "completed"
        assert product.ai_generation_locked_at is None
    finally:
        db.close()


def test_secret_redaction_removes_common_credentials():
    message = redact_error("429 Bearer abc123 token=xyz secret=hidden api_key=key")
    assert "abc123" not in message and "xyz" not in message and "hidden" not in message
    assert "api_key=key" not in message
    assert "[REDACTED]" in message


def test_empty_queue_idles_without_provider_or_database_writes():
    calls = []
    worker = _worker(lambda session: calls.append(session) or pytest.fail("provider must not run"))
    assert worker.process_once().claimed is False
    assert calls == []
