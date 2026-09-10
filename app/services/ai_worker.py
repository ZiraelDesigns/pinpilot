"""Reliable local worker for pending AI creative-generation jobs.

The worker has no Etsy or Pinterest dependency.  It claims jobs atomically,
uses the existing SEO-v2/content-image pipeline, and stores only redacted error
messages.  A product-level database lease prevents duplicate work across worker
processes, including SQLite deployments where row-level locks are unavailable.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.models import PinGenerationJob, Product
from app.models.core import PinCreativeType
from app.services.ai_content import AIContentError, AIContentService
from app.services.ai_image import AIImageError

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
POLL_SECONDS = 15.0
LEASE_SECONDS = 15 * 60


@dataclass(frozen=True)
class WorkerResult:
    claimed: bool
    completed: bool = False
    retry_scheduled: bool = False
    failed: bool = False


def redact_error(error: Exception | str) -> str:
    """Keep diagnostics useful without persisting credentials or bearer tokens."""
    message = str(error)
    message = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", message)
    message = re.sub(r"(?i)(api[_ -]?key|token|secret)\s*[=:]\s*[^\s,;]+", r"\1=[REDACTED]", message)
    return message[:1000]


def is_retryable_error(error: Exception) -> bool:
    """Classify provider/upstream failures without exposing provider internals."""
    message = str(error).casefold()
    if any(value in message for value in ("429", "rate limit", "resource_exhausted", "timeout", "temporar", "connection")):
        return True
    return bool(re.search(r"\b(500|502|503|504)\b", message))


class AIGenerationWorker:
    def __init__(
        self,
        db_factory: Callable[[], Session],
        worker_id: str | None = None,
        content_service_factory: Callable[[Session], AIContentService] = AIContentService,
        max_retries: int = MAX_RETRIES,
        poll_seconds: float = POLL_SECONDS,
        now: Callable[[], datetime] = datetime.utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.db_factory = db_factory
        self.worker_id = worker_id or uuid.uuid4().hex
        self.content_service_factory = content_service_factory
        self.max_retries = max_retries
        self.poll_seconds = poll_seconds
        self.now = now
        self.sleep = sleep

    def process_once(self) -> WorkerResult:
        """Claim and process at most one due job. Safe to call concurrently."""
        db = self.db_factory()
        try:
            job = self._claim_next_job(db)
            if not job:
                return WorkerResult(claimed=False)
            job_id, product_id = job.id, job.product_id
        finally:
            db.close()

        # Run provider code outside the short claim transaction. The lease remains
        # durable so another worker cannot process this product concurrently.
        db = self.db_factory()
        try:
            job = db.get(PinGenerationJob, job_id)
            product = db.get(Product, product_id) if product_id else None
            if not job or job.status != "processing" or job.worker_id != self.worker_id or not product:
                return WorkerResult(claimed=True, failed=True)
            try:
                created = self.content_service_factory(db).generate(
                    product, PinCreativeType.PRODUCT_FOCUS, max(1, job.requested_count)
                )
            except Exception as exc:
                return self._record_failure(db, job, product, exc)
            job.status = "completed"
            job.completed_at = self.now()
            job.error_message = None
            job.next_attempt_at = None
            job.locked_at = None
            job.worker_id = None
            self._release_product(db, product)
            db.commit()
            logger.info("AI generation job %s completed with %s creative(s)", job.id, len(created))
            return WorkerResult(claimed=True, completed=True)
        finally:
            db.close()

    def run_forever(self, stop: Callable[[], bool] | None = None) -> None:
        """Poll slowly when idle; systemd controls restart after process crashes."""
        stop = stop or (lambda: False)
        while not stop():
            result = self.process_once()
            if not result.claimed:
                self.sleep(self.poll_seconds)

    def _claim_next_job(self, db: Session) -> PinGenerationJob | None:
        now = self.now()
        stale_before = now - timedelta(seconds=LEASE_SECONDS)
        # Recover interrupted workers. A lease expiry is intentionally conservative
        # so a slow image request is not duplicated by a second process.
        db.execute(update(PinGenerationJob).where(
            PinGenerationJob.status == "processing", PinGenerationJob.locked_at < stale_before
        ).values(status="pending", worker_id=None, locked_at=None))
        db.execute(update(Product).where(Product.ai_generation_locked_at < stale_before).values(
            ai_generation_locked_at=None, ai_generation_worker_id=None
        ))
        db.commit()

        candidates = db.scalars(select(PinGenerationJob).where(
            PinGenerationJob.status == "pending",
            or_(PinGenerationJob.next_attempt_at.is_(None), PinGenerationJob.next_attempt_at <= now),
        ).order_by(PinGenerationJob.created_at, PinGenerationJob.id)).all()
        for candidate in candidates:
            if not candidate.product_id:
                continue
            product_lock = db.execute(update(Product).where(
                Product.id == candidate.product_id,
                or_(Product.ai_generation_locked_at.is_(None), Product.ai_generation_locked_at < stale_before),
            ).values(ai_generation_locked_at=now, ai_generation_worker_id=self.worker_id))
            if product_lock.rowcount != 1:
                continue
            claim = db.execute(update(PinGenerationJob).where(
                PinGenerationJob.id == candidate.id, PinGenerationJob.status == "pending",
            ).values(status="processing", locked_at=now, worker_id=self.worker_id, error_message=None))
            if claim.rowcount == 1:
                db.commit()
                return db.get(PinGenerationJob, candidate.id)
            db.execute(update(Product).where(
                Product.id == candidate.product_id, Product.ai_generation_worker_id == self.worker_id
            ).values(ai_generation_locked_at=None, ai_generation_worker_id=None))
            db.commit()
        return None

    def _record_failure(
        self, db: Session, job: PinGenerationJob, product: Product, error: Exception
    ) -> WorkerResult:
        job.retry_count += 1
        job.error_message = redact_error(error)
        job.locked_at = None
        job.worker_id = None
        self._release_product(db, product)
        if is_retryable_error(error) and job.retry_count < self.max_retries:
            job.status = "pending"
            job.next_attempt_at = self.now() + timedelta(seconds=2 ** job.retry_count)
            db.commit()
            logger.warning("AI generation job %s will retry", job.id)
            return WorkerResult(claimed=True, retry_scheduled=True)
        job.status = "failed"
        job.completed_at = self.now()
        job.next_attempt_at = None
        db.commit()
        logger.warning("AI generation job %s failed", job.id)
        return WorkerResult(claimed=True, failed=True)

    def _release_product(self, db: Session, product: Product) -> None:
        db.execute(update(Product).where(
            Product.id == product.id, Product.ai_generation_worker_id == self.worker_id
        ).values(ai_generation_locked_at=None, ai_generation_worker_id=None))
