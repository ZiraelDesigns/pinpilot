"""Batch, deterministic learning signals over already-persisted Pinterest analytics.

No provider or network calls are made here. A sample is one distinct published Pin;
daily observations are first reduced to the newest fetched snapshot for each Pin/day.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AnalyticsSnapshot,
    PinterestAccount,
    PinterestBoard,
    PinterestBoardRecommendation,
    Pin,
    PublishedPinterestPin,
    SEOGeneration,
    SEOKeywordIntelligence,
    SEOQualityAssessment,
    SEOPerformanceLearning,
    SEOTrendSeasonalAssessment,
)


PERFORMANCE_LEARNING_VERSION = "performance_learning_v1"
MIN_BASELINE_SAMPLES = 3
LOW_CONFIDENCE_SAMPLES = 3
ADEQUATE_CONFIDENCE_SAMPLES = 10
_COUNT_METRICS = ("impressions", "saves", "pin_clicks", "outbound_clicks", "engagements")
_RATE_METRICS = ("engagement_rate", "pin_click_rate", "outbound_click_rate")


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _date_filter(start: date, end: date):
    end_exclusive = datetime.combine(end, time.max)
    return or_(
        AnalyticsSnapshot.metric_date.between(start, end),
        (AnalyticsSnapshot.metric_date.is_(None)
         & AnalyticsSnapshot.period_start.is_not(None)
         & (AnalyticsSnapshot.period_start <= end_exclusive)
         & (AnalyticsSnapshot.period_end >= datetime.combine(start, time.min))),
        (AnalyticsSnapshot.metric_date.is_(None)
         & AnalyticsSnapshot.period_start.is_(None)
         & (func.coalesce(AnalyticsSnapshot.fetched_at, AnalyticsSnapshot.recorded_at) >= datetime.combine(start, time.min))
         & (func.coalesce(AnalyticsSnapshot.fetched_at, AnalyticsSnapshot.recorded_at) <= end_exclusive)),
    )


def _latest_snapshot_key(row: AnalyticsSnapshot) -> tuple:
    fetched = row.fetched_at or row.recorded_at or datetime.min
    return (_as_naive_utc(fetched), row.id)


def _select_unique_observations(rows: list[AnalyticsSnapshot]) -> list[AnalyticsSnapshot]:
    daily: dict[tuple[int, date], AnalyticsSnapshot] = {}
    aggregate: dict[int, AnalyticsSnapshot] = {}
    for row in rows:
        if row.published_pin_id is None:
            continue
        if row.metric_date is not None:
            key = (row.published_pin_id, row.metric_date)
            if key not in daily or _latest_snapshot_key(row) > _latest_snapshot_key(daily[key]):
                daily[key] = row
        else:
            key = row.published_pin_id
            if key not in aggregate or _latest_snapshot_key(row) > _latest_snapshot_key(aggregate[key]):
                aggregate[key] = row
    daily_pin_ids = {pin_id for pin_id, _ in daily}
    return sorted(
        [*daily.values(), *(row for pin_id, row in aggregate.items() if pin_id not in daily_pin_ids)],
        key=lambda row: (row.published_pin_id or 0, row.metric_date or date.min, row.id),
    )


def _sum_known(rows: list[AnalyticsSnapshot], field: str) -> int | None:
    values = [getattr(row, field) for row in rows]
    return sum(values) if values and all(value is not None for value in values) else None


def _mean_known(rows: list[AnalyticsSnapshot], field: str) -> float | None:
    values = [float(getattr(row, field)) for row in rows if getattr(row, field) is not None]
    return round(sum(values) / len(values), 8) if values else None


def _confidence(samples: int) -> str:
    if samples < LOW_CONFIDENCE_SAMPLES:
        return "insufficient_data"
    if samples < ADEQUATE_CONFIDENCE_SAMPLES:
        return "low_confidence"
    return "adequate_sample"


def _canonical_fingerprint(
    *, scope_key: str, start: date, end: date, snapshots: list[AnalyticsSnapshot],
    unlinked_ids: list[int], provenance_sources: list[dict[str, Any]],
) -> str:
    source = {
        "account_scope": scope_key,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "algorithm_version": PERFORMANCE_LEARNING_VERSION,
        "snapshots": [{
            "id": row.id,
            "published_pin_id": row.published_pin_id,
            "metric_date": row.metric_date.isoformat() if row.metric_date else None,
            "metrics": {
                field: str(getattr(row, field)) if getattr(row, field) is not None else None
                for field in (*_COUNT_METRICS, *_RATE_METRICS)
            },
            "fetched_at": _as_naive_utc(row.fetched_at).isoformat() if row.fetched_at else None,
            "recorded_at": _as_naive_utc(row.recorded_at).isoformat() if row.recorded_at else None,
            "period_start": _as_naive_utc(row.period_start).isoformat() if row.period_start else None,
            "period_end": _as_naive_utc(row.period_end).isoformat() if row.period_end else None,
            "collection_run_id": row.collection_run_id,
        } for row in sorted(snapshots, key=lambda item: item.id)],
        "unlinked_snapshot_ids": sorted(unlinked_ids),
        "provenance_sources": provenance_sources,
    }
    return hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _pin_metrics(rows: list[AnalyticsSnapshot]) -> dict[str, Any]:
    result: dict[str, Any] = {field: _sum_known(rows, field) for field in _COUNT_METRICS}
    result.update({field: _mean_known(rows, field) for field in _RATE_METRICS})
    impressions = result["impressions"]
    if impressions is not None and impressions > 0:
        if result["saves"] is not None:
            result["save_rate"] = round(result["saves"] / impressions, 8)
        if result["outbound_clicks"] is not None:
            result["outbound_click_rate"] = round(result["outbound_clicks"] / impressions, 8)
        if result["pin_clicks"] is not None:
            result["pin_click_rate"] = round(result["pin_clicks"] / impressions, 8)
    else:
        result["save_rate"] = None
        # Provider-reported rates remain usable when raw counts/denominators are absent.
    result["save_rate"] = result.get("save_rate")
    result["metric_coverage"] = {
        field: sum(getattr(row, field) is not None for row in rows) for field in (*_COUNT_METRICS, *_RATE_METRICS)
    }
    return result


def _aggregate(samples: list[dict[str, Any]], baseline: dict[str, Any] | None) -> dict[str, Any]:
    sums: dict[str, int | None] = {}
    for field in _COUNT_METRICS:
        values = [sample["metrics"].get(field) for sample in samples]
        known = [value for value in values if value is not None]
        sums[field] = sum(known) if known else None
    # Rates use only matched, observed numerator/denominator pairs.
    for field, numerator in (("save_rate", "saves"), ("pin_click_rate", "pin_clicks"),
                             ("outbound_click_rate", "outbound_clicks")):
        pairs = [sample["metrics"] for sample in samples
                 if sample["metrics"].get("impressions") is not None
                 and sample["metrics"].get(numerator) is not None]
        denominator = sum(item["impressions"] for item in pairs)
        value = sum(item[numerator] for item in pairs)
        derived = round(value / denominator, 8) if denominator > 0 else None
        if derived is None and field != "save_rate":
            reported = [sample["metrics"].get(field) for sample in samples if sample["metrics"].get(field) is not None]
            derived = round(sum(reported) / len(reported), 8) if reported else None
        sums[field] = derived
    engagement_values = [sample["metrics"].get("engagement_rate") for sample in samples
                         if sample["metrics"].get("engagement_rate") is not None]
    sums["engagement_rate"] = round(sum(engagement_values) / len(engagement_values), 8) if engagement_values else None
    rate_names = ("save_rate", "pin_click_rate", "outbound_click_rate")
    numerators = {"save_rate": "saves", "pin_click_rate": "pin_clicks", "outbound_click_rate": "outbound_clicks"}
    count = len(samples)
    relative = []
    rate_sample_counts = {
        field: sum(sample["metrics"].get("impressions") is not None
                   and sample["metrics"].get(numerator) is not None for sample in samples)
        for field, numerator in numerators.items()
    }
    if baseline and count >= MIN_BASELINE_SAMPLES and baseline.get("sample_count", 0) >= MIN_BASELINE_SAMPLES:
        for field in rate_names:
            value, base = sums.get(field), baseline.get(field)
            if (rate_sample_counts[field] >= MIN_BASELINE_SAMPLES
                    and baseline.get("rate_sample_counts", {}).get(field, 0) >= MIN_BASELINE_SAMPLES
                    and value is not None and base is not None and base > 0):
                relative.append(max(0.0, min(100.0, 50.0 * value / base)))
    return {
        "sample_count": count,
        "confidence": _confidence(count),
        "first_observed": min((day for sample in samples for day in sample.get("metric_dates", [])), default=None),
        "last_observed": max((day for sample in samples for day in sample.get("metric_dates", [])), default=None),
        "metrics": sums,
        "observed_performance_score": round(sum(relative) / len(relative), 2) if relative else None,
        "score_type": "computed_relative_index" if relative else "unavailable_without_comparable_baseline",
        "baseline_sample_count": baseline.get("sample_count", 0) if baseline else 0,
        "baseline_rates": {field: baseline.get(field) for field in rate_names} if baseline else None,
        "metric_sample_counts": {
            field: sum(sample["metrics"].get(field) is not None for sample in samples)
            for field in (*_COUNT_METRICS, *_RATE_METRICS)
        },
        "rate_sample_counts": rate_sample_counts,
        "aggregate_metric_semantics": "count totals sum only non-null per-Pin observations; metric_sample_counts reports their coverage",
        "recommendation_context": _recommendation_context(sums, baseline, count, rate_sample_counts),
        "rate_method": {
            "save_rate": "sum(saves) / sum(impressions) over pins where both values are observed; denominator > 0",
            "pin_click_rate": "sum(pin_clicks) / sum(impressions) over pins where both values are observed; denominator > 0",
            "outbound_click_rate": "sum(outbound_clicks) / sum(impressions) over pins where both values are observed; denominator > 0",
            "engagement_rate": "arithmetic mean of provider-stored non-null snapshot rates",
        },
    }


def _recommendation_context(
    metrics: dict[str, Any], baseline: dict[str, Any] | None, count: int,
    rate_sample_counts: dict[str, int],
) -> list[dict[str, str]]:
    if count < MIN_BASELINE_SAMPLES or not baseline or baseline["sample_count"] < MIN_BASELINE_SAMPLES:
        return []
    context = []
    for field in ("save_rate", "pin_click_rate", "outbound_click_rate"):
        if (rate_sample_counts.get(field, 0) < MIN_BASELINE_SAMPLES
                or baseline.get("rate_sample_counts", {}).get(field, 0) < MIN_BASELINE_SAMPLES):
            continue
        value, base = metrics.get(field), baseline.get(field)
        if value is None or base is None or base <= 0:
            continue
        if value > base:
            context.append({"type": "inferred", "signal": f"historically_higher_{field}_than_scope_baseline"})
        elif value < base:
            context.append({"type": "inferred", "signal": f"historically_lower_{field}_than_scope_baseline"})
    return context


def _segments_for(sample: dict[str, Any]) -> dict[str, set[str]]:
    output = {key: set() for key in ("keyword", "semantic_group", "intent", "audience", "use_case", "creative_angle", "quality_profile", "board", "season", "holiday")}
    intel = sample.get("intelligence")
    for item in (intel.keyword_items if intel else []) or []:
        if not isinstance(item, dict) or not item.get("valid"):
            continue
        kind = item.get("keyword_type")
        normalized = item.get("normalized")
        if normalized:
            output["keyword"].add(normalized)
        if item.get("semantic_group"):
            output["semantic_group"].add(item["semantic_group"])
        if item.get("search_intent") and item["search_intent"] != "unknown":
            output["intent"].add(item["search_intent"])
        if normalized and kind == "AUDIENCE":
            output["audience"].add(normalized)
        if normalized and kind == "USE_CASE":
            output["use_case"].add(normalized)
    generation = sample.get("generation")
    snapshot = generation.output_snapshot if generation and isinstance(generation.output_snapshot, dict) else {}
    seo = snapshot.get("seo_metadata") if isinstance(snapshot.get("seo_metadata"), dict) else {}
    for intent in seo.get("search_intents", []) if isinstance(seo.get("search_intents"), list) else []:
        if isinstance(intent, str) and intent:
            output["intent"].add(intent)
    angle = seo.get("creative_angle")
    if isinstance(angle, str) and angle.strip():
        output["creative_angle"].add(angle.strip().casefold())
    quality = sample.get("quality")
    if quality:
        score = quality.overall_score
        output["quality_profile"].add("low" if score < 60 else "mid" if score < 80 else "high")
    publication = sample["publication"]
    if publication.board_id is not None:
        output["board"].add(str(publication.board_id))
    seasonal = sample.get("seasonal")
    if seasonal:
        calendar = seasonal.calendar_snapshot or {}
        season = calendar.get("season")
        if isinstance(season, dict) and season.get("name"):
            output["season"].add(f"{seasonal.region_code}:{season['name']}")
        for event in calendar.get("events", []) if isinstance(calendar.get("events"), list) else []:
            if isinstance(event, dict) and event.get("event_key") and event.get("keyword_matches"):
                output["holiday"].add(f"{seasonal.region_code}:{event['event_key']}")
    return output


class PerformanceLearningService:
    """Recalculate immutable, idempotent internal signals from stored snapshots only."""

    def recalculate(
        self,
        db: Session,
        *,
        account_id: int | None = None,
        account_identifier: str | None = None,
        window_start: date,
        window_end: date,
        now: datetime | None = None,
    ) -> SEOPerformanceLearning:
        if window_start > window_end:
            raise ValueError("window_start must not be after window_end")
        if account_id is None and not account_identifier:
            raise ValueError("Provide account_id or account_identifier to keep learning account-scoped")
        if account_id is not None and not account_identifier:
            account = db.get(PinterestAccount, account_id)
            account_identifier = account.account_identifier if account else None
        scope_key = f"identity:{account_identifier}" if account_identifier else f"id:{account_id}"
        account_scope = (
            or_(PublishedPinterestPin.account_id == account_id,
                PublishedPinterestPin.account_identifier_snapshot == account_identifier)
            if account_identifier else PublishedPinterestPin.account_id == account_id
        )
        # Make caller-staged collector snapshots visible without committing its transaction.
        db.flush()
        now = _as_naive_utc(now or datetime.now(timezone.utc))
        rows = list(db.scalars(
            select(AnalyticsSnapshot)
            .join(PublishedPinterestPin, AnalyticsSnapshot.published_pin_id == PublishedPinterestPin.id)
            .where(account_scope, _date_filter(window_start, window_end))
        ).all())
        selected = _select_unique_observations(rows)
        snapshot_ids = sorted(row.id for row in selected)
        uniquely_published_local_pins = (
            select(PublishedPinterestPin.pin_id)
            .where(account_scope, PublishedPinterestPin.pin_id.is_not(None))
            .group_by(PublishedPinterestPin.pin_id)
            .having(func.count(PublishedPinterestPin.id) == 1)
        )
        unlinked_rows = list(db.scalars(
            select(AnalyticsSnapshot)
            .join(Pin, Pin.id == AnalyticsSnapshot.pin_id)
            .where(
                AnalyticsSnapshot.published_pin_id.is_(None),
                Pin.id.in_(uniquely_published_local_pins),
                _date_filter(window_start, window_end),
            )
        ).all())
        unlinked_snapshot_ids = sorted(row.id for row in unlinked_rows)
        pub_ids = sorted({row.published_pin_id for row in selected if row.published_pin_id is not None})
        pubs = {row.id: row for row in db.scalars(
            select(PublishedPinterestPin).where(PublishedPinterestPin.id.in_(pub_ids or [-1]))
        ).all()}
        generation_ids = sorted({
            candidate
            for publication in pubs.values()
            for candidate in [publication.seo_generation_id or (publication.metadata_snapshot or {}).get("seo_generation_id")]
            if isinstance(candidate, int) and not isinstance(candidate, bool)
        })
        generations = {row.id: row for row in db.scalars(select(SEOGeneration).where(SEOGeneration.id.in_(generation_ids or [-1]))).all()}
        intelligence = {row.seo_generation_id: row for row in db.scalars(
            select(SEOKeywordIntelligence).where(SEOKeywordIntelligence.seo_generation_id.in_(generation_ids or [-1]))
        ).all()}
        quality = {row.seo_generation_id: row for row in db.scalars(
            select(SEOQualityAssessment).where(SEOQualityAssessment.seo_generation_id.in_(generation_ids or [-1]))
        ).all()}
        recommendations = list(db.scalars(
            select(PinterestBoardRecommendation).where(
                PinterestBoardRecommendation.seo_generation_id.in_(generation_ids or [-1])
            ).order_by(PinterestBoardRecommendation.calculated_at.desc(), PinterestBoardRecommendation.id.desc())
        ).all())
        recommendation_map = {}
        for row in recommendations:
            if row.board_id is not None:
                recommendation_map.setdefault((row.seo_generation_id, row.board_id), []).append(row)
        seasonal_rows = list(db.scalars(
            select(SEOTrendSeasonalAssessment).where(
                SEOTrendSeasonalAssessment.seo_generation_id.in_(generation_ids or [-1]),
                SEOTrendSeasonalAssessment.reference_date <= window_end,
            ).order_by(SEOTrendSeasonalAssessment.reference_date.desc(), SEOTrendSeasonalAssessment.id.desc())
        ).all())
        seasonal = defaultdict(list)
        for row in seasonal_rows:
            seasonal[row.seo_generation_id].append(row)

        provenance_sources = []
        for publication in sorted(pubs.values(), key=lambda item: item.id):
            candidate = publication.seo_generation_id or (publication.metadata_snapshot or {}).get("seo_generation_id")
            generation = generations.get(candidate) if isinstance(candidate, int) and not isinstance(candidate, bool) else None
            pub_time = _as_naive_utc(publication.published_at)
            source_season = next(
                (item for item in seasonal.get(generation.id, []) if item.reference_date <= pub_time.date()), None
            ) if generation else None
            source_board_recommendation = next(
                (item for item in recommendation_map.get((generation.id, publication.board_id), [])
                 if _as_naive_utc(item.calculated_at) <= pub_time), None
            ) if generation else None
            provenance_sources.append({
                "published_pin_id": publication.id,
                "seo_generation_id": generation.id if generation else None,
                "keyword_intelligence_id": intelligence[generation.id].id if generation and generation.id in intelligence else None,
                "quality_assessment_id": quality[generation.id].id if generation and generation.id in quality else None,
                "board_id": publication.board_id,
                "board_recommendation_id": source_board_recommendation.id if source_board_recommendation else None,
                "seasonal_assessment_id": source_season.id if source_season else None,
            })
        fingerprint = _canonical_fingerprint(
            scope_key=scope_key, start=window_start, end=window_end,
            snapshots=selected, unlinked_ids=unlinked_snapshot_ids,
            provenance_sources=provenance_sources,
        )
        existing = db.scalar(select(SEOPerformanceLearning).where(
            SEOPerformanceLearning.algorithm_version == PERFORMANCE_LEARNING_VERSION,
            SEOPerformanceLearning.source_fingerprint == fingerprint,
        ))
        if existing:
            return existing

        pin_rows: dict[int, list[AnalyticsSnapshot]] = defaultdict(list)
        for row in selected:
            if row.published_pin_id is not None:
                pin_rows[row.published_pin_id].append(row)
        samples: list[dict[str, Any]] = []
        missing_provenance = 0
        unlinked_snapshots = len(unlinked_snapshot_ids)
        for pub_id, observations in sorted(pin_rows.items()):
            publication = pubs.get(pub_id)
            if publication is None:
                continue
            generation_id = publication.seo_generation_id or (publication.metadata_snapshot or {}).get("seo_generation_id")
            generation = generations.get(generation_id) if isinstance(generation_id, int) else None
            intel = intelligence.get(generation.id) if generation else None
            if generation is None or intel is None:
                missing_provenance += 1
            metrics = _pin_metrics(observations)
            if all(metrics.get(field) is None for field in (*_COUNT_METRICS, *_RATE_METRICS)):
                continue
            pub_time = _as_naive_utc(publication.published_at)
            generation_seasonal = seasonal.get(generation.id, []) if generation else []
            seasonal_for_publication = next(
                (item for item in generation_seasonal if item.reference_date <= pub_time.date()), None
            )
            generation_board_recs = recommendation_map.get((generation.id, publication.board_id), []) if generation else []
            board_rec_for_publication = next(
                (item for item in generation_board_recs if _as_naive_utc(item.calculated_at) <= pub_time), None
            )
            sample = {
                "published_pin_id": pub_id,
                "seo_generation_id": generation.id if generation else None,
                "source_snapshot_ids": sorted(row.id for row in observations),
                "metric_dates": sorted(row.metric_date.isoformat() for row in observations if row.metric_date),
                "metrics": metrics,
                "publication": publication,
                "generation": generation,
                "intelligence": intel,
                "quality": quality.get(generation.id) if generation else None,
                "seasonal": seasonal_for_publication,
                "board_recommendation": board_rec_for_publication,
            }
            sample["segments"] = _segments_for(sample)
            samples.append(sample)

        baseline = _aggregate(samples, None)
        baseline_rates = baseline["metrics"]
        baseline_record = {
            "sample_count": baseline["sample_count"],
            "save_rate": baseline_rates.get("save_rate"),
            "pin_click_rate": baseline_rates.get("pin_click_rate"),
            "outbound_click_rate": baseline_rates.get("outbound_click_rate"),
            "rate_sample_counts": baseline.get("rate_sample_counts", {}),
        }
        board_ids = sorted({sample["publication"].board_id for sample in samples if sample["publication"].board_id})
        board_names = {board.id: board.name for board in db.scalars(
            select(PinterestBoard).where(PinterestBoard.id.in_(board_ids or [-1]))
        ).all()}
        dimensions: dict[str, list[dict[str, Any]]] = {}
        for dimension in ("keyword", "semantic_group", "intent", "audience", "use_case", "creative_angle", "quality_profile", "board", "season", "holiday"):
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for sample in samples:
                for value in sorted(sample["segments"][dimension]):
                    grouped[value].append(sample)
            entries = []
            for value, members in sorted(grouped.items()):
                aggregate = _aggregate(members, baseline_record)
                aggregate["dimension"] = dimension
                aggregate["value"] = value
                if dimension == "board":
                    recommendation_scores = [
                        item["board_recommendation"].match_score for item in members
                        if item.get("board_recommendation") is not None
                    ]
                    aggregate["board_name"] = board_names.get(int(value))
                    aggregate["computed_match_signal"] = round(sum(recommendation_scores) / len(recommendation_scores), 2) if recommendation_scores else None
                    aggregate["observed_performance_signal"] = aggregate["metrics"]
                entries.append(aggregate)
            dimensions[dimension] = entries

        sample_count = len(samples)
        status = "completed" if sample_count >= MIN_BASELINE_SAMPLES else "insufficient_data"
        if not samples and (missing_provenance or unlinked_snapshots):
            status = "missing_provenance"
        elif samples and missing_provenance == len(samples):
            status = "missing_provenance"
        payload = {
            "calculation_type": "deterministic_observed_heuristic",
            "sample_unit": "distinct_published_pin",
            "snapshot_selection": "latest_fetched_per_published_pin_and_metric_date; aggregate snapshots excluded when daily rows exist",
            "confidence_thresholds": {
                "insufficient_data_below": LOW_CONFIDENCE_SAMPLES,
                "low_confidence_below": ADEQUATE_CONFIDENCE_SAMPLES,
                "adequate_sample_from": ADEQUATE_CONFIDENCE_SAMPLES,
                "source": "internal_heuristic_v1",
            },
            "baseline": baseline,
            "dimensions": dimensions,
            "observations": [{
                "published_pin_id": sample["published_pin_id"],
                "seo_generation_id": sample["seo_generation_id"],
                "source_snapshot_ids": sample["source_snapshot_ids"],
                "metric_dates": sample["metric_dates"],
                "board_id": sample["publication"].board_id,
                "board_recommendation_id": sample["board_recommendation"].id if sample["board_recommendation"] else None,
                "seasonal_assessment_id": sample["seasonal"].id if sample["seasonal"] else None,
                "quality_assessment_id": sample["quality"].id if sample["quality"] else None,
                "keyword_intelligence_id": sample["intelligence"].id if sample["intelligence"] else None,
                "external_trend_status": (
                    (sample["seasonal"].external_trend_snapshot or {}).get("status", "unknown")
                    if sample["seasonal"] else "not_available"
                ),
                "metrics": sample["metrics"],
                "provenance_status": "known" if sample["generation"] and sample["intelligence"] else "missing_provenance",
            } for sample in samples],
            "missing_provenance_sample_count": missing_provenance,
            "unlinked_snapshot_count": unlinked_snapshots,
            "unlinked_snapshot_ids": unlinked_snapshot_ids,
            "score_definition": "mean of available save/pin-click/outbound-click rate ratios versus account baseline, baseline rate > 0; ratio is 50 * segment_rate / baseline_rate clamped to 0..100",
            "score_label": "internal_computed_relative_index_not_pinterest_ranking",
            "recommendation_semantics": {"observed": "stored analytics facts", "inferred": "descriptive comparison to this account/window baseline; no action is applied"},
            "external_calls": False,
        }
        result = SEOPerformanceLearning(
            account_id=account_id,
            account_identifier_snapshot=account_identifier,
            window_start=window_start,
            window_end=window_end,
            calculated_at=now,
            algorithm_version=PERFORMANCE_LEARNING_VERSION,
            status=status,
            sample_count=sample_count,
            source_fingerprint=fingerprint,
            source_snapshot_ids=snapshot_ids,
            calculation_metadata={"selected_snapshot_count": len(snapshot_ids), "missing_provenance_sample_count": missing_provenance},
            result_snapshot=payload,
        )
        db.add(result)
        db.flush()
        return result


def recalculate_performance_learning(db: Session, **kwargs: Any) -> SEOPerformanceLearning:
    """Convenience entry point for a future controlled batch job; does not schedule it."""
    return PerformanceLearningService().recalculate(db, **kwargs)
