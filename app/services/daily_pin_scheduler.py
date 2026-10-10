"""Prepare a bounded daily Pin queue from the local creative pool.

This module only turns existing creatives into local ``Pin`` records and queues
AI demand independently from local Pin scheduling; it never calls a provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import Pin, PinCreative, PinGenerationJob, PinterestAccount
from app.models.core import PinCreativeSourceType, PinCreativeStatus, PinStatus
from app.services.ai_pipeline import AI_DAILY_CREATIVE_QUOTA, enqueue_daily_generation_job
from app.services.content_portfolio_optimizer import optimize_content_portfolio
from app.services.opportunity_engine import score_existing_creative_opportunities
from app.services.keyword_intelligence import normalize_keyword


DAILY_PIN_TARGET = 15
DAILY_LOCAL_PIN_TARGET = DAILY_PIN_TARGET
DAILY_AI_CREATIVE_QUOTA = AI_DAILY_CREATIVE_QUOTA
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
    portfolio_summary: dict[str, Any] | None = None

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
        ai_jobs_created = int(enqueue_daily_generation_job(self.db))
        portfolio_summary: dict[str, Any] = optimize_content_portfolio([], target=0)["summary"]

        if not slots_needed:
            self.db.commit()
            pending_jobs = self.db.query(PinGenerationJob).filter_by(status="pending").count()
            return DailyScheduleResult(
                target=self.daily_target,
                already_prepared=already_prepared,
                prepared=(),
                mockups_prepared=0,
                ai_prepared=0,
                ai_jobs_created=ai_jobs_created,
                pending_ai_jobs=pending_jobs,
                portfolio_summary=portfolio_summary,
            )

        used_creative_ids = set(self.db.scalars(
            select(Pin.creative_id).where(Pin.creative_id.is_not(None))
        ).all())
        used_images: set[tuple[int, str]] = set()
        prior_image_rows = self.db.execute(
            select(Pin.product_id, Pin.image_path, PinCreative.source_image_url)
            .outerjoin(PinCreative, Pin.creative_id == PinCreative.id)
            .where(Pin.product_id.is_not(None))
        )
        for product_id, image_path, source_image_url in prior_image_rows:
            for image_identifier in (image_path, source_image_url):
                if image_identifier:
                    used_images.add((product_id, image_identifier))

        candidates = self.db.scalars(
            select(PinCreative).where(
                PinCreative.status.in_(_ELIGIBLE_CREATIVE_STATUSES),
                PinCreative.source_type.in_(
                    (PinCreativeSourceType.MOCKUP.value, PinCreativeSourceType.AI.value)
                ),
            )
        ).all()
        candidates.sort(key=self._candidate_sort_key)
        available: list[PinCreative] = []
        for creative in candidates:
            if creative.id in used_creative_ids:
                continue
            image_identifier = creative.source_image_url or creative.image_path
            if image_identifier and (
                creative.product_id,
                image_identifier,
            ) in used_images:
                continue
            available.append(creative)
            if image_identifier:
                # Reserve the source image as soon as its highest-priority
                # creative enters the candidate pool, before portfolio ranking.
                used_images.add((creative.product_id, image_identifier))

        active_accounts = list(self.db.scalars(
            select(PinterestAccount).where(PinterestAccount.is_active.is_(True)).order_by(PinterestAccount.id)
        ))
        account_id = active_accounts[0].id if len(active_accounts) == 1 else None
        opportunity_by_creative = score_existing_creative_opportunities(
            self.db, available, account_id=account_id, apply_learning=False
        )
        recent_history = self._recent_portfolio_history(target_date)
        saturation = self._saturation_counts(recent_history)
        portfolio_candidates = [
            self._portfolio_candidate(creative, opportunity_by_creative.get(creative.id, {}), saturation)
            for creative in available
        ]
        portfolio = optimize_content_portfolio(
            portfolio_candidates, target=slots_needed, recent_history=recent_history
        )
        portfolio_summary = portfolio["summary"]
        selected_candidates = portfolio["selected"]
        selected = [candidate["creative"] for candidate in selected_candidates]
        selection_by_id = {candidate["candidate_id"]: candidate for candidate in selected_candidates}
        for creative in selected:
            used_creative_ids.add(creative.id)
            image_identifier = creative.source_image_url or creative.image_path
            if image_identifier:
                used_images.add((creative.product_id, image_identifier))

        prepared_rows = []
        for index, creative in enumerate(selected):
            pin = self._pin_from_creative(creative, target_date, already_prepared + index)
            selected_candidate = selection_by_id[creative.id]
            pin.portfolio_snapshot = {
                **selected_candidate["portfolio"],
                "opportunity_score": selected_candidate.get("opportunity_score"),
                "creative_id": creative.id,
                "creative_type": creative.creative_type,
                "creative_angle": selected_candidate.get("creative_angle"),
                "primary_keyword": selected_candidate.get("keyword"),
                "keyword_cluster": selected_candidate.get("cluster"),
                "board": {
                    "id": selected_candidate.get("board"),
                    "name": selected_candidate.get("board_name"),
                } if selected_candidate.get("board") is not None else None,
                "season": selected_candidate.get("season"),
                "performance_status": selected_candidate.get("performance_status"),
                "performance_sample_count": selected_candidate.get("performance_sample_count", 0),
                "performance_source_snapshot_ids": selected_candidate.get("performance_source_snapshot_ids", []),
                "portfolio_summary": portfolio_summary,
            }
            prepared_rows.append(pin)
        prepared = tuple(prepared_rows)
        self.db.add_all(prepared)
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
            portfolio_summary=portfolio_summary,
        )

    def _recent_portfolio_history(self, target_date: date) -> list[dict[str, Any]]:
        start = datetime.combine(target_date - timedelta(days=30), time.min)
        end = datetime.combine(target_date, time.max)
        rows = self.db.scalars(
            select(Pin).where(
                Pin.scheduled_for >= start,
                Pin.scheduled_for <= end,
                Pin.status.in_((PinStatus.SCHEDULED.value, PinStatus.PUBLISHED.value)),
            ).options(joinedload(Pin.creative))
        ).all()
        history = []
        for pin in rows:
            creative = pin.creative
            if creative is None:
                continue
            metadata = creative.seo_metadata if isinstance(creative.seo_metadata, dict) else {}
            keyword = normalize_keyword(metadata.get("primary_keyword", ""))
            if not keyword:
                keyword = next((normalize_keyword(value) for value in (creative.keywords or [])
                                if isinstance(value, str) and normalize_keyword(value)), "")
            portfolio = pin.portfolio_snapshot or {}
            history.append({
                "keyword": keyword,
                "cluster": portfolio.get("keyword_cluster") or keyword,
                "creative_type": creative.creative_type,
                "creative_angle": metadata.get("creative_angle") or "",
                "board": (portfolio.get("board") or {}).get("id") if isinstance(portfolio.get("board"), dict) else portfolio.get("board"),
                "season": portfolio.get("season"),
            })
        return history

    @staticmethod
    def _saturation_counts(history: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        fields = ("keyword", "cluster", "creative_type", "creative_angle", "board")
        for field in fields:
            values: dict[str, int] = {}
            for row in history:
                value = row.get(field)
                if value not in (None, ""):
                    key = str(value)
                    values[key] = values.get(key, 0) + 1
            counts[field] = values
        return counts

    @staticmethod
    def _portfolio_candidate(
        creative: PinCreative, opportunity: dict[str, Any], saturation: dict[str, dict[str, int]]
    ) -> dict[str, Any]:
        metadata = creative.seo_metadata if isinstance(creative.seo_metadata, dict) else {}
        keyword = normalize_keyword(metadata.get("primary_keyword", ""))
        if not keyword:
            keyword = next((normalize_keyword(value) for value in (creative.keywords or [])
                            if isinstance(value, str) and normalize_keyword(value)), "")
        keyword = keyword or None
        cluster = opportunity.get("keyword_cluster") or keyword
        board_id = opportunity.get("board_id")
        board_label = str(board_id) if board_id is not None else opportunity.get("board_name")
        components = opportunity.get("components") or {}
        feature_values = {
            "keyword": keyword,
            "cluster": cluster,
            "creative_type": creative.creative_type,
            "creative_angle": opportunity.get("creative_angle") or metadata.get("creative_angle"),
            "board": board_label,
            "board_name": opportunity.get("board_name"),
        }
        learned = opportunity.get("performance_learning") or {}
        return {
            **feature_values,
            "candidate_id": creative.id,
            "creative": creative,
            "priority_tier": DailyPinScheduler._candidate_sort_key(creative)[:2],
            "opportunity_score": opportunity.get("opportunity_score"),
            "opportunity_components": components,
            "opportunity_learning_applied": opportunity.get("learning_applied", False),
            "quality_score": components.get("seo_quality"),
            "board_fit": components.get("board_fit"),
            "seasonal_score": components.get("seasonal_relevance"),
            "season": opportunity.get("season"),
            "performance_learning": learned,
            "performance_status": learned.get("status", "unknown"),
            "performance_sample_count": learned.get("sample_count", 0),
            "performance_source_snapshot_ids": learned.get("source_snapshot_ids", []),
            "saturation": {
                field: saturation.get(field, {}).get(str(value), 0)
                for field, value in feature_values.items()
            } | {"total": sum(saturation.get(field, {}).get(str(value), 0)
                              for field, value in feature_values.items())},
        }

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
