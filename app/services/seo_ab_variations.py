"""Deterministic local SEO variation and observed analytics comparison.

This module never calls an AI/Pinterest provider and never publishes a Pin.
Only existing keyword candidates can be promoted into a variant.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AnalyticsSnapshot,
    PinterestAccount,
    PublishedPinterestPin,
    SEOABComparison,
    SEOABExperiment,
    SEOABVariant,
    SEOABVariantPublication,
    SEOGeneration,
    SEOKeywordIntelligence,
    SEOQualityAssessment,
    SEOTrendSeasonalAssessment,
    PinterestBoardRecommendation,
    SEOPerformanceLearning,
)
from app.services.keyword_intelligence import normalize_keyword
from app.services.seo_quality import calculate_seo_quality


EXPERIMENT_VERSION = "seo_ab_experiment_v1"
VARIATION_VERSION = "seo_variation_v1"
COMPARISON_VERSION = "seo_ab_comparison_v1"
SUPPORTED_VARIANT_TYPES = frozenset({"KEYWORD_FOCUS"})
MIN_COMPARISON_SAMPLE = 5
_COUNT_METRICS = ("impressions", "saves", "pin_clicks", "outbound_clicks", "engagements")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _snapshot(generation: SEOGeneration) -> dict[str, Any]:
    if generation.status != "completed" or not isinstance(generation.output_snapshot, dict):
        raise ValueError("A completed SEO generation with an output snapshot is required")
    # Whitelist the existing AIContentService schema; provider credentials or
    # unrelated fields must never cross into experiment snapshots.
    source = generation.output_snapshot
    keys = ("title", "description", "call_to_action", "keywords", "seo_metadata", "creative_type")
    return json.loads(json.dumps({key: source[key] for key in keys if key in source}, ensure_ascii=False))


def _source_provenance(db: Session, generation: SEOGeneration, learning_ids: list[int]) -> dict[str, Any]:
    intelligence = db.scalar(select(SEOKeywordIntelligence).where(
        SEOKeywordIntelligence.seo_generation_id == generation.id
    ))
    quality = db.scalar(select(SEOQualityAssessment).where(
        SEOQualityAssessment.seo_generation_id == generation.id
    ))
    boards = list(db.scalars(select(PinterestBoardRecommendation).where(
        PinterestBoardRecommendation.seo_generation_id == generation.id
    ).order_by(PinterestBoardRecommendation.id)))
    seasonal = list(db.scalars(select(SEOTrendSeasonalAssessment).where(
        SEOTrendSeasonalAssessment.seo_generation_id == generation.id
    ).order_by(SEOTrendSeasonalAssessment.id)))
    learnings = list(db.scalars(select(SEOPerformanceLearning).where(
        SEOPerformanceLearning.id.in_(learning_ids or [-1])
    ).order_by(SEOPerformanceLearning.id)))
    return {
        "source_generation_id": generation.id,
        "source_provider": generation.provider,
        "source_model_name": generation.model_name,
        "source_keyword_intelligence_id": intelligence.id if intelligence else None,
        "source_keyword_algorithm_version": intelligence.algorithm_version if intelligence else "unknown",
        "source_quality_assessment_id": quality.id if quality else None,
        "source_quality_version": quality.score_version if quality else "unknown",
        "board_recommendation_ids": [item.id for item in boards],
        "seasonal_assessment_ids": [item.id for item in seasonal],
        "performance_learning": [
            {
                "id": item.id,
                "algorithm_version": item.algorithm_version,
                "status": item.status,
                "signal_type": "observed" if item.status == "completed" else "inferred",
            }
            for item in learnings
        ],
        "provider": "deterministic",
        "external_calls": False,
    }


def _variant_keyword_snapshot(source_intelligence: SEOKeywordIntelligence, candidate: str, old_primary: str) -> dict[str, Any]:
    """Reclassify existing persisted signals only; do not rerun keyword analysis."""
    snapshot = {
        "status": source_intelligence.status,
        "algorithm_version": source_intelligence.algorithm_version,
        "source_keyword_intelligence_id": source_intelligence.id,
        "keyword_items": deepcopy(source_intelligence.keyword_items or []),
        "candidate_sets": deepcopy(source_intelligence.candidate_sets or []),
        "quality_summary": deepcopy(source_intelligence.quality_summary or {}),
        "external_signals": deepcopy(source_intelligence.external_signals or {}),
        "signal_origins": deepcopy(source_intelligence.signal_origins or {}),
    }
    for item in snapshot["keyword_items"]:
        if not isinstance(item, dict):
            continue
        normalized = item.get("normalized")
        if normalized == candidate and item.get("valid"):
            item["keyword_type"] = "PRIMARY"
            item["source_field"] = "variant_focus_from_" + str(item.get("source_field") or "keyword")
        elif normalized == old_primary and item.get("valid"):
            item["keyword_type"] = "SECONDARY"
            item["source_field"] = "variant_supporting_primary_keyword"
    snapshot["signal_origins"] = {
        **snapshot["signal_origins"],
        "variant_semantics": "reclassified_from_persisted_keyword_intelligence",
    }
    return snapshot


def create_seo_ab_experiment(
    db: Session,
    source_generation_id: int,
    *,
    hypothesis: str,
    hypothesis_source: str = "human_defined",
    performance_learning_ids: list[int] | None = None,
) -> SEOABExperiment:
    """Create/retrieve a stable experiment set and keyword-focus variants.

    The source SEOGeneration and its associated records are treated as immutable.
    No new phrase is generated: each treatment promotes one existing SECONDARY or
    LONG_TAIL term, retaining the prior primary as a supporting term.
    """
    if not isinstance(hypothesis, str) or not hypothesis.strip():
        raise ValueError("A non-empty experiment hypothesis is required")
    if hypothesis_source not in {"human_defined", "system_generated"}:
        raise ValueError("Unsupported hypothesis source")
    generation = db.get(SEOGeneration, source_generation_id)
    if generation is None:
        raise ValueError("SEO generation was not found")
    source = _snapshot(generation)
    intelligence = db.scalar(select(SEOKeywordIntelligence).where(
        SEOKeywordIntelligence.seo_generation_id == generation.id
    ))
    if intelligence is None or intelligence.status != "completed":
        raise ValueError("Completed Keyword Intelligence is required")

    learning_ids = sorted(set(performance_learning_ids or []))
    learning_rows = list(db.scalars(select(SEOPerformanceLearning).where(
        SEOPerformanceLearning.id.in_(learning_ids or [-1])
    )))
    if len(learning_rows) != len(learning_ids):
        raise ValueError("One or more performance learning records were not found")
    # Scope learning context to the source generation's product provenance only;
    # persisted signals are context, never a winner decision.
    learning_context = [{
        "learning_id": row.id,
        "algorithm_version": row.algorithm_version,
        "status": row.status,
        "signal_type": "observed" if row.status == "completed" else "inferred",
        "dimensions": (row.result_snapshot or {}).get("dimensions", {}),
    } for row in learning_rows]
    provenance = _source_provenance(db, generation, learning_ids)
    identity = {
        "source_generation_id": generation.id,
        "experiment_version": EXPERIMENT_VERSION,
        "variation_version": VARIATION_VERSION,
        "hypothesis": hypothesis.strip(),
        "hypothesis_source": hypothesis_source,
        "learning_ids": learning_ids,
    }
    idempotency_key = _canonical_hash(identity)
    existing = db.scalar(select(SEOABExperiment).where(
        SEOABExperiment.idempotency_key == idempotency_key
    ))
    if existing is not None:
        return existing

    experiment = SEOABExperiment(
        source_generation_id=generation.id,
        experiment_version=EXPERIMENT_VERSION,
        variation_version=VARIATION_VERSION,
        comparison_version=COMPARISON_VERSION,
        status="DRAFT",
        hypothesis=hypothesis.strip(),
        hypothesis_source=hypothesis_source,
        algorithm_version=VARIATION_VERSION,
        source_snapshot=source,
        provenance_snapshot={**provenance, "performance_learning_context": learning_context},
        idempotency_key=idempotency_key,
        created_at=_now(),
    )
    db.add(experiment)
    db.flush()

    seo = source.get("seo_metadata") if isinstance(source.get("seo_metadata"), dict) else {}
    raw_primary = seo.get("primary_keyword") if isinstance(seo.get("primary_keyword"), str) else ""
    original_primary = normalize_keyword(raw_primary)
    candidates: dict[str, dict[str, Any]] = {}
    for item in intelligence.keyword_items or []:
        if not isinstance(item, dict) or not item.get("valid"):
            continue
        kind = item.get("keyword_type")
        normalized = item.get("normalized")
        if kind not in {"SECONDARY", "LONG_TAIL"} or not isinstance(normalized, str):
            continue
        normalized = normalize_keyword(normalized)
        if not normalized or normalized == original_primary:
            continue
        candidates.setdefault(normalized, {"raw": item.get("raw") or normalized, "type": kind})

    secondary = [v for v in seo.get("secondary_keywords", []) if isinstance(v, str)] if isinstance(seo.get("secondary_keywords"), list) else []
    long_tail = [v for v in seo.get("long_tail_keywords", []) if isinstance(v, str)] if isinstance(seo.get("long_tail_keywords"), list) else []
    existing_secondary = {normalize_keyword(v) for v in secondary}
    for normalized, entry in sorted(candidates.items()):
        variant_snapshot = json.loads(json.dumps(source, ensure_ascii=False))
        variant_seo = variant_snapshot.get("seo_metadata")
        if not isinstance(variant_seo, dict):
            continue
        candidate_raw = str(entry["raw"])
        variant_seo["primary_keyword"] = candidate_raw
        if raw_primary and original_primary not in existing_secondary:
            variant_seo["secondary_keywords"] = [raw_primary, *secondary]
        else:
            variant_seo["secondary_keywords"] = list(secondary)
        if entry["type"] == "LONG_TAIL":
            variant_seo["long_tail_keywords"] = [value for value in long_tail if normalize_keyword(value) != normalized]
        variant_intelligence = _variant_keyword_snapshot(intelligence, normalized, original_primary or "")
        quality = calculate_seo_quality(variant_snapshot, variant_intelligence)
        variant_key = _canonical_hash({"experiment": idempotency_key, "primary": normalized})[:24]
        variant = SEOABVariant(
            experiment=experiment,
            variant_key=variant_key,
            variant_name=f"Keyword focus: {candidate_raw[:96]}",
            variant_type="KEYWORD_FOCUS",
            status="QUALITY_FAIL" if quality["validation"]["status"] == "FAIL" else "READY",
            output_snapshot=variant_snapshot,
            change_set={
                "changed_fields": [
                    "seo_metadata.primary_keyword", "seo_metadata.secondary_keywords",
                    *(["seo_metadata.long_tail_keywords"] if entry["type"] == "LONG_TAIL" else []),
                ],
                "from": {
                    "primary_keyword": raw_primary,
                    "secondary_keywords": secondary,
                    **({"long_tail_keywords": long_tail} if entry["type"] == "LONG_TAIL" else {}),
                },
                "to": {
                    "primary_keyword": candidate_raw,
                    "secondary_keywords": variant_seo["secondary_keywords"],
                    **({"long_tail_keywords": variant_seo["long_tail_keywords"]} if entry["type"] == "LONG_TAIL" else {}),
                },
                "candidate_source": entry["type"],
                "new_keywords_created": False,
            },
            keyword_intelligence_snapshot=variant_intelligence,
            quality_snapshot=quality,
            quality_status=quality["validation"]["status"],
            quality_score=quality["score"]["overall"],
            provenance_snapshot={
                **provenance,
                "source_snapshot_hash": _canonical_hash(source),
                "source_keyword_candidate": normalized,
                "source_keyword_type": entry["type"],
                "learning_context_ids": learning_ids,
                "learning_signal_types": [item["signal_type"] for item in learning_context],
                "created_at": _now().isoformat(),
                "algorithm_version": VARIATION_VERSION,
                "provider": "deterministic",
                "external_calls": False,
            },
            created_at=_now(),
        )
        db.add(variant)
    db.flush()
    return experiment


_TRANSITIONS = {
    "DRAFT": {"READY", "CANCELLED"},
    "READY": {"RUNNING", "PAUSED", "CANCELLED"},
    "RUNNING": {"PAUSED", "COMPLETED", "CANCELLED"},
    "PAUSED": {"RUNNING", "CANCELLED"},
    "COMPLETED": set(),
    "CANCELLED": set(),
}


def transition_seo_ab_experiment(db: Session, experiment_id: int, new_status: str) -> SEOABExperiment:
    experiment = db.get(SEOABExperiment, experiment_id)
    if experiment is None:
        raise ValueError("SEO A/B experiment was not found")
    if new_status not in _TRANSITIONS.get(experiment.status, set()):
        raise ValueError(f"Invalid SEO A/B experiment transition: {experiment.status} -> {new_status}")
    if new_status == "READY" and not any(v.status == "READY" for v in experiment.variants):
        raise ValueError("At least one non-failing variant is required before READY")
    experiment.status = new_status
    experiment.updated_at = _now()
    db.flush()
    return experiment


def link_verified_variant_publication(db: Session, variant_id: int, published_pin_id: int) -> SEOABVariantPublication:
    """Link only when the publication explicitly carries this variant ID.

    The publisher is intentionally untouched. A future controlled publisher may
    add the marker when it submits this exact immutable variant snapshot.
    """
    variant = db.get(SEOABVariant, variant_id)
    publication = db.get(PublishedPinterestPin, published_pin_id)
    if variant is None or publication is None:
        raise ValueError("Variant or published Pinterest Pin was not found")
    if variant.status != "READY":
        raise ValueError("A quality-failing or archived variant cannot be linked for publication")
    experiment = variant.experiment
    if experiment.status != "RUNNING":
        raise ValueError("Variant publication attribution requires an explicitly RUNNING experiment")
    marker = (publication.metadata_snapshot or {}).get("seo_ab_variant_id")
    generation_id = publication.seo_generation_id or (publication.metadata_snapshot or {}).get("seo_generation_id")
    if marker != variant.id or generation_id != experiment.source_generation_id:
        raise ValueError("Published Pin lacks an explicit matching variant provenance marker")
    existing = db.scalar(select(SEOABVariantPublication).where(
        SEOABVariantPublication.variant_id == variant.id,
        SEOABVariantPublication.published_pin_id == publication.id,
    ))
    if existing:
        return existing
    link = SEOABVariantPublication(variant_id=variant.id, published_pin_id=publication.id)
    db.add(link)
    db.flush()
    return link


def _aggregate_publications(db: Session, publications: list[PublishedPinterestPin], start: date, end: date, sample_metric: str) -> tuple[dict[str, Any], list[int], int]:
    pub_ids = [item.id for item in publications]
    rows = list(db.scalars(select(AnalyticsSnapshot).where(
        AnalyticsSnapshot.published_pin_id.in_(pub_ids or [-1]),
        AnalyticsSnapshot.metric_schema_version == "pinterest_v5_organic_daily",
        AnalyticsSnapshot.metric_date >= start,
        AnalyticsSnapshot.metric_date <= end,
    ).order_by(AnalyticsSnapshot.published_pin_id, AnalyticsSnapshot.metric_date, AnalyticsSnapshot.fetched_at.desc(), AnalyticsSnapshot.id.desc())))
    latest: dict[tuple[int, date], AnalyticsSnapshot] = {}
    for row in rows:
        latest.setdefault((row.published_pin_id, row.metric_date), row)
    selected = list(latest.values())
    observed = {metric: [getattr(row, metric) for row in selected if getattr(row, metric) is not None] for metric in _COUNT_METRICS}
    sums = {metric: (sum(values) if values else None) for metric, values in observed.items()}
    impressions = sums["impressions"]
    for metric, numerator in (("save_rate", sums["saves"]), ("pin_click_rate", sums["pin_clicks"]), ("outbound_click_rate", sums["outbound_clicks"])):
        sums[metric] = numerator / impressions if numerator is not None and impressions not in (None, 0) else None
    rate_rows = {key: [float(getattr(row, key)) for row in selected if getattr(row, key) is not None] for key in ("engagement_rate",)}
    sums["engagement_rate"] = sum(rate_rows["engagement_rate"]) / len(rate_rows["engagement_rate"]) if rate_rows["engagement_rate"] else None
    if sample_metric in _COUNT_METRICS:
        sampled_pub_ids = {row.published_pin_id for row in selected if getattr(row, sample_metric) is not None}
    elif sample_metric == "engagement_rate":
        sampled_pub_ids = {row.published_pin_id for row in selected if row.engagement_rate is not None}
    else:
        numerator = {"save_rate": "saves", "pin_click_rate": "pin_clicks", "outbound_click_rate": "outbound_clicks"}[sample_metric]
        sampled_pub_ids = {
            row.published_pin_id for row in selected
            if getattr(row, numerator) is not None and row.impressions is not None and row.impressions > 0
        }
    sample_count = len(sampled_pub_ids)
    return {**sums, "sample_count": sample_count, "observation_count": len(selected)}, sorted(row.id for row in selected), sample_count


def compare_seo_ab_experiment(
    db: Session,
    experiment_id: int,
    *,
    period_start: date,
    period_end: date,
    metric_name: str = "outbound_clicks",
    account_id: int | None = None,
    account_identifier: str | None = None,
) -> SEOABComparison:
    if period_start > period_end:
        raise ValueError("period_start must not be after period_end")
    if metric_name not in {*_COUNT_METRICS, "save_rate", "pin_click_rate", "outbound_click_rate", "engagement_rate"}:
        raise ValueError("Unsupported comparison metric")
    experiment = db.get(SEOABExperiment, experiment_id)
    if experiment is None:
        raise ValueError("SEO A/B experiment was not found")
    variant_publications: dict[int, list[PublishedPinterestPin]] = {}
    all_variant_pub_ids: set[int] = set()
    for variant in experiment.variants:
        links = list(db.scalars(select(SEOABVariantPublication).where(
            SEOABVariantPublication.variant_id == variant.id
        ).order_by(SEOABVariantPublication.id)))
        variant_publications[variant.id] = [link.published_pin for link in links]
        all_variant_pub_ids.update(link.published_pin_id for link in links)
    baseline_query = select(PublishedPinterestPin).where(
        PublishedPinterestPin.seo_generation_id == experiment.source_generation_id
    )
    if all_variant_pub_ids:
        baseline_query = baseline_query.where(PublishedPinterestPin.id.not_in(all_variant_pub_ids))
    baseline_pubs = list(db.scalars(baseline_query.order_by(PublishedPinterestPin.id)))
    all_publications = [*baseline_pubs, *(publication for rows in variant_publications.values() for publication in rows)]
    if account_id is not None and account_identifier is None:
        account_identifier = db.scalar(select(PublishedPinterestPin.account_identifier_snapshot).where(
            PublishedPinterestPin.account_id == account_id,
            PublishedPinterestPin.account_identifier_snapshot.is_not(None),
        ).limit(1))
    if account_id is None and account_identifier is not None:
        account_id = db.scalar(select(PinterestAccount.id).where(
            PinterestAccount.account_identifier == account_identifier
        ).limit(1))
    identity_keys = {
        publication.account_identifier_snapshot or (f"account_id:{publication.account_id}" if publication.account_id is not None else None)
        for publication in all_publications
    } - {None}
    if account_id is None and account_identifier is None and len(identity_keys) > 1:
        raise ValueError("Pinterest account scope is required when an experiment spans multiple accounts")
    if account_id is not None or account_identifier is not None:
        baseline_pubs = [item for item in baseline_pubs if (
            (account_id is not None and item.account_id == account_id)
            or (account_identifier is not None and item.account_identifier_snapshot == account_identifier)
        )]
        variant_publications = {
            key: [item for item in values if (
                (account_id is not None and item.account_id == account_id)
                or (account_identifier is not None and item.account_identifier_snapshot == account_identifier)
            )]
            for key, values in variant_publications.items()
        }
    baseline, baseline_ids, baseline_n = _aggregate_publications(db, baseline_pubs, period_start, period_end, metric_name)
    variants = []
    all_ids = set(baseline_ids)
    for variant in experiment.variants:
        metrics, snapshot_ids, sample_count = _aggregate_publications(db, variant_publications[variant.id], period_start, period_end, metric_name)
        all_ids.update(snapshot_ids)
        value = metrics.get(metric_name)
        base_value = baseline.get(metric_name)
        lift = (value - base_value) / abs(base_value) if value is not None and base_value not in (None, 0) else None
        sufficient = sample_count >= MIN_COMPARISON_SAMPLE and baseline_n >= MIN_COMPARISON_SAMPLE
        variants.append({
            "variant_id": variant.id,
            "variant_key": variant.variant_key,
            "variant_type": variant.variant_type,
            "quality_status": variant.quality_status,
            "metrics": metrics,
            "sample_count": sample_count,
            "metric_value": value,
            "baseline_value": base_value,
            "relative_lift": lift,
            "data_status": "observed" if snapshot_ids else "insufficient_data",
            "confidence": "sufficient_sample_for_descriptive_comparison" if sufficient else "insufficient_data",
            "source_snapshot_ids": snapshot_ids,
        })
    max_key = lambda item: (item["metric_value"] is not None, item["metric_value"] if item["metric_value"] is not None else -1, item["variant_key"])
    leading = max(variants, key=max_key) if variants and any(v["metric_value"] is not None for v in variants) else None
    cohort_ids = {
        "baseline_publications": [item.id for item in baseline_pubs],
        "variant_publications": {str(key): [item.id for item in value] for key, value in sorted(variant_publications.items())},
    }
    result = {
        "status": "observed" if all_ids else "insufficient_data",
        "metric_name": metric_name,
        "sample_unit": "distinct_published_pin_with_daily_snapshot",
        "snapshot_selection": "latest_fetched_daily_snapshot_per_published_pin_and_metric_date",
        "minimum_sample_for_descriptive_confidence": MIN_COMPARISON_SAMPLE,
        "account_scope": account_identifier or (f"account_id:{account_id}" if account_id is not None else (next(iter(identity_keys)) if identity_keys else None)),
        "baseline": {"metrics": baseline, "sample_count": baseline_n, "source_published_pin_ids": [p.id for p in baseline_pubs]},
        "cohort_publication_ids": cohort_ids,
        "variants": variants,
        "leading_signal": ({"variant_id": leading["variant_id"], "reason": "highest_observed_metric_only"} if leading else None),
        "winner_selected": False,
        "decision": "comparison_only_no_automatic_winner_or_optimization",
        "calculation_type": "observed_analytics_not_pinterest_ranking",
        "external_calls": False,
    }
    fingerprint = _canonical_hash({"experiment_id": experiment.id, "period_start": period_start, "period_end": period_end, "metric": metric_name, "account_scope": result["account_scope"], "snapshot_ids": sorted(all_ids), "cohort_ids": cohort_ids, "version": COMPARISON_VERSION})
    cached = db.scalar(select(SEOABComparison).where(
        SEOABComparison.experiment_id == experiment.id,
        SEOABComparison.comparison_version == COMPARISON_VERSION,
        SEOABComparison.source_fingerprint == fingerprint,
    ))
    if cached:
        return cached
    comparison = SEOABComparison(
        experiment_id=experiment.id,
        metric_name=metric_name,
        period_start=period_start,
        period_end=period_end,
        calculated_at=_now(),
        comparison_version=COMPARISON_VERSION,
        source_fingerprint=fingerprint,
        snapshot_ids=sorted(all_ids),
        result_snapshot=result,
    )
    db.add(comparison)
    db.flush()
    return comparison
