"""Deterministic, local-data-only recommendations for the next Pin to create.

Scores are internal opportunity heuristics, not Pinterest ranking or trend scores.
The engine only consumes persisted SEO/board/seasonal/learning snapshots and never
calls an AI or external provider.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    PinCreative,
    PinterestAccount,
    SEOGeneration,
    SEOKeywordIntelligence,
    SEOQualityAssessment,
    SEOTrendSeasonalAssessment,
    PinterestBoardRecommendation,
    SEOPerformanceLearning,
)
from app.models.core import PinCreativeType
from app.services.keyword_intelligence import normalize_keyword, semantic_tokens
from app.services.seo_performance_learning import ADEQUATE_CONFIDENCE_SAMPLES


OPPORTUNITY_SCORE_VERSION = "next_best_pin_v1"
_CREATIVE_ANGLES = {
    PinCreativeType.PRODUCT_FOCUS.value: "product details",
    PinCreativeType.LIFESTYLE.value: "everyday style",
    PinCreativeType.PROBLEM_SOLUTION.value: "practical use",
    PinCreativeType.GIFT_IDEA.value: "thoughtful gifting",
    PinCreativeType.MINIMALIST.value: "minimalist style",
}
_TYPE_LABELS = {
    PinCreativeType.PRODUCT_FOCUS.value: "Ürün odaklı",
    PinCreativeType.LIFESTYLE.value: "Yaşam tarzı",
    PinCreativeType.PROBLEM_SOLUTION.value: "Sorun ve çözüm",
    PinCreativeType.GIFT_IDEA.value: "Hediye fikri",
    PinCreativeType.MINIMALIST.value: "Minimalist",
}
_ANGLE_LABELS = {
    "product details": "Ürün detayları",
    "everyday style": "Günlük kullanım ve stil",
    "practical use": "Pratik kullanım",
    "thoughtful gifting": "Özenli hediye fikri",
    "minimalist style": "Minimalist stil",
}


def score_opportunity(
    *,
    keyword_item: dict[str, Any],
    quality_score: int | None,
    board_score: int | None,
    seasonal_score: int | None,
    creative_type: str,
    type_history_count: int,
    keyword_overlap: float,
    angle_history_count: int,
    observed_performance_score: float | None = None,
    performance_adjustment: float = 0.0,
    performance_status: str | None = None,
    apply_learning: bool = True,
) -> dict[str, Any]:
    """Score one candidate using only available, explicitly sourced signals."""
    components: dict[str, Any] = {
        "keyword_quality": keyword_item.get("quality", {}).get("heuristic_quality_score"),
        "keyword_relevance": keyword_item.get("relevance", {}).get("score"),
        "seo_quality": quality_score,
        "board_fit": board_score,
        "creative_diversity": max(0, 100 - 30 * type_history_count - 15 * angle_history_count),
        "seasonal_relevance": seasonal_score,
        "performance_learning_score": observed_performance_score,
        "performance_learning_adjustment": round(performance_adjustment, 2),
        "cannibalization_penalty": round(min(40.0, max(0.0, keyword_overlap) * 40)),
    }
    weighted = [
        (components["keyword_quality"], 0.20),
        (components["keyword_relevance"], 0.15),
        (components["seo_quality"], 0.20),
        (components["board_fit"], 0.15),
        (components["creative_diversity"], 0.15),
        (components["seasonal_relevance"], 0.10),
    ]
    available = [(float(value), weight) for value, weight in weighted if value is not None]
    denominator = sum(weight for _, weight in available)
    base_score = sum(value * weight for value, weight in available) / denominator if denominator else 0.0
    applied_learning_adjustment = (
        components["performance_learning_adjustment"] if apply_learning else 0.0
    )
    score = max(0, min(100, round(base_score - components["cannibalization_penalty"]
                                  + applied_learning_adjustment)))
    signals = []
    relevance_score = keyword_item.get("relevance", {}).get("score")
    if relevance_score is not None and relevance_score >= 60:
        signals.append("Anahtar kelime ürün bağlamıyla örtüşüyor.")
    if quality_score is not None and quality_score >= 70:
        signals.append("Kaynak SEO kalite değerlendirmesi güçlü.")
    if board_score is not None and board_score >= 60:
        signals.append("Mevcut pano eşleşmesi hesaba katıldı.")
    if seasonal_score is not None and seasonal_score > 0:
        signals.append("Takvim kaynaklı mevsimsel ilgi bulundu.")
    if performance_status == "positive":
        signals.append("Hesaba özel geçmiş veride bu strateji için olumlu gözlemsel öğrenme sinyali var.")
    elif performance_status == "negative":
        signals.append("Hesaba özel geçmiş veride bu strateji için olumsuz gözlemsel öğrenme sinyali var.")
    if components["cannibalization_penalty"]:
        signals.append("Benzer mevcut içerik nedeniyle çakışma cezası uygulandı.")
    if not signals:
        signals.append("Skor yalnızca mevcut ve doğrulanmış yerel SEO sinyallerine dayanıyor.")
    negative_signals = []
    if components["keyword_relevance"] is not None and components["keyword_relevance"] < 40:
        negative_signals.append("Anahtar kelimenin ürün bağlamıyla örtüşmesi zayıf.")
    if quality_score is not None and quality_score < 50:
        negative_signals.append("Kaynak SEO kalite puanı düşük.")
    if board_score is not None and board_score < 40:
        negative_signals.append("Mevcut pano eşleşmesi zayıf.")
    if components["creative_diversity"] < 50:
        negative_signals.append("Bu kreatif türü veya açı geçmişte sık kullanılmış.")
    if components["cannibalization_penalty"]:
        negative_signals.append("Mevcut keyword içerikleriyle benzerlik tespit edildi.")
    if performance_status == "negative":
        negative_signals.append("Hesaba özel geçmiş veride bu strateji için olumsuz gözlemsel öğrenme sinyali var.")
    return {
        "score": score,
        "score_version": OPPORTUNITY_SCORE_VERSION,
        "score_origin": "computed_heuristic_not_pinterest_ranking",
        "learning_applied": apply_learning,
        "components": components,
        "weights": {
            "keyword_quality": 0.20,
            "keyword_relevance": 0.15,
            "seo_quality": 0.20,
            "board_fit": 0.15,
            "creative_diversity": 0.15,
            "seasonal_relevance": 0.10,
            "performance_learning_adjustment": "bounded +/-8 heuristic points; only adequate-sample signals",
            "missing_signals": "excluded_then_available_weights_normalized; never imputed as zero",
        },
        "positive_signals": signals,
        "negative_signals": negative_signals,
        "performance_status": performance_status or ("unknown" if observed_performance_score is None else "insufficient_data"),
    }


def _keyword_candidates(intelligence: SEOKeywordIntelligence) -> list[dict[str, Any]]:
    items = intelligence.keyword_items if isinstance(intelligence.keyword_items, list) else []
    return [
        item for item in items
        if isinstance(item, dict)
        and item.get("valid") is True
        and item.get("duplicate") is False
        and item.get("keyword_type") in {"PRIMARY", "SECONDARY", "LONG_TAIL"}
        and isinstance(item.get("raw"), str)
        and normalize_keyword(item["raw"])
    ]


def _learning_signals(rows: list[SEOPerformanceLearning]) -> dict[tuple[str, str], dict[str, Any]]:
    """Index persisted account-local strategy observations without cross-account merging."""
    signals: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        dimensions = (row.result_snapshot or {}).get("dimensions", {})
        for dimension, entries in dimensions.items() if isinstance(dimensions, dict) else []:
            for item in entries if isinstance(entries, list) else []:
                if not isinstance(item, dict):
                    continue
                value = str(item.get("value", "")).strip()
                if dimension in {"keyword", "creative_angle"}:
                    value = normalize_keyword(value) or ""
                if not value:
                    continue
                signal_status = item.get("signal_status", "insufficient_data")
                sample_count = item.get("sample_count", 0)
                score = item.get("observed_performance_score")
                eligible = (
                    sample_count >= ADEQUATE_CONFIDENCE_SAMPLES
                    and item.get("confidence") == "adequate_sample"
                    and signal_status in {"positive", "negative"}
                    and isinstance(score, (int, float))
                )
                adjustment = 0.0
                if eligible:
                    adjustment = max(-8.0, min(8.0, (float(score) - 50.0) * 0.16))
                key = (dimension, value)
                signals.setdefault(key, {
                    "score": float(score) if isinstance(score, (int, float)) else None,
                    "sample_count": sample_count,
                    "confidence": item.get("confidence", "insufficient_data"),
                    "signal_status": signal_status if eligible else "insufficient_data",
                    "adjustment": adjustment,
                    "source_learning_id": row.id,
                    "source_snapshot_ids": item.get("source_snapshot_ids", row.source_snapshot_ids),
                    "window_start": row.window_start.isoformat(),
                    "window_end": row.window_end.isoformat(),
                    "algorithm_version": row.algorithm_version,
                })
    return signals


def _account_learning_rows(db: Session, account_id: int | None) -> list[SEOPerformanceLearning]:
    """Only use one explicitly selected or sole active account's learning rows."""
    if account_id is None:
        accounts = list(db.scalars(
            select(PinterestAccount).where(PinterestAccount.is_active.is_(True)).order_by(PinterestAccount.id)
        ))
        if len(accounts) != 1:
            return []
        account_id = accounts[0].id
        account_identifier = accounts[0].account_identifier
    else:
        account = db.get(PinterestAccount, account_id)
        if account is None:
            return []
        account_identifier = account.account_identifier
    clauses = [SEOPerformanceLearning.account_id == account_id]
    if account_identifier:
        clauses.append(SEOPerformanceLearning.account_identifier_snapshot == account_identifier)
    return list(db.scalars(
        select(SEOPerformanceLearning).where(
            SEOPerformanceLearning.status == "completed",
            or_(*clauses),
        ).order_by(SEOPerformanceLearning.calculated_at.desc(), SEOPerformanceLearning.id.desc()).limit(100)
    ))


def _matching_learning_evidence(
    signals: dict[tuple[str, str], dict[str, Any]], *, keyword: dict[str, Any],
    creative_type: str, angle: str, board_id: int | None, season: str | None,
    region_code: str | None, audience_values: list[str] | None = None,
    use_case_values: list[str] | None = None,
) -> dict[str, Any]:
    candidates = [
        ("keyword", normalize_keyword(keyword.get("normalized") or keyword.get("raw", "")) or ""),
        ("creative_type", creative_type),
    ]
    semantic_group = keyword.get("semantic_group")
    if semantic_group:
        candidates.append(("semantic_group", str(semantic_group)))
    intent = keyword.get("search_intent")
    if isinstance(intent, str) and intent and intent != "unknown":
        candidates.append(("intent", intent))
    candidates.extend(("audience", value) for value in (audience_values or []) if value)
    candidates.extend(("use_case", value) for value in (use_case_values or []) if value)
    if board_id is not None:
        candidates.append(("board", str(board_id)))
    if season and region_code:
        candidates.append(("season", f"{region_code}:{season}"))
    angle_tokens = semantic_tokens(angle)
    if angle_tokens:
        for dimension, value in signals:
            prior = semantic_tokens(value) if dimension == "creative_angle" else set()
            if prior and len(angle_tokens & prior) / len(angle_tokens | prior) >= 0.6:
                candidates.append((dimension, value))
    matched = [signals[key] for key in dict.fromkeys(candidates) if key in signals]
    eligible = [item for item in matched if item["signal_status"] in {"positive", "negative"}]
    adjustment = round(sum(item["adjustment"] for item in eligible) / len(eligible), 2) if eligible else 0.0
    scores = [item["score"] for item in eligible if item["score"] is not None]
    if not matched:
        status = "unknown"
    elif not eligible:
        status = "insufficient_data"
    elif adjustment > 0.5:
        status = "positive"
    elif adjustment < -0.5:
        status = "negative"
    else:
        status = "uncertain"
    return {
        "status": status,
        "adjustment": adjustment,
        "score": round(sum(scores) / len(scores), 2) if scores else None,
        "signals": matched,
        "source_learning_ids": sorted({item["source_learning_id"] for item in matched}),
        "source_snapshot_ids": sorted({snapshot_id for item in matched for snapshot_id in item["source_snapshot_ids"]}),
        "sample_count": min((item["sample_count"] for item in (eligible or matched)), default=0),
        "confidence": "adequate_sample" if eligible else "insufficient_data" if matched else "unknown",
    }


def get_next_best_pin_opportunities(
    db: Session, *, limit: int = 6, account_id: int | None = None
) -> list[dict[str, Any]]:
    """Build ranked recommendations from existing completed generations only."""
    generations = list(db.scalars(
        select(SEOGeneration)
        .where(SEOGeneration.status == "completed", SEOGeneration.product_id.is_not(None))
        .options(joinedload(SEOGeneration.product), joinedload(SEOGeneration.creative))
        .order_by(SEOGeneration.completed_at.desc(), SEOGeneration.id.desc())
        .limit(500)
    ).unique())
    latest_by_product: dict[int, SEOGeneration] = {}
    for generation in generations:
        if generation.product_id is not None and generation.product_id not in latest_by_product:
            latest_by_product[generation.product_id] = generation
    if not latest_by_product:
        return []

    ids = [row.id for row in latest_by_product.values()]
    product_ids = list(latest_by_product)
    intelligence = {row.seo_generation_id: row for row in db.scalars(
        select(SEOKeywordIntelligence).where(SEOKeywordIntelligence.seo_generation_id.in_(ids))
    )}
    quality = {row.seo_generation_id: row for row in db.scalars(
        select(SEOQualityAssessment).where(SEOQualityAssessment.seo_generation_id.in_(ids))
    )}
    recommendations: dict[int, list[PinterestBoardRecommendation]] = {}
    recommendation_query = select(PinterestBoardRecommendation).where(
        PinterestBoardRecommendation.seo_generation_id.in_(ids),
        PinterestBoardRecommendation.status == "recommended",
    )
    recommendation_scope = account_id
    if recommendation_scope is None:
        active_account_ids = list(db.scalars(
            select(PinterestAccount.id)
            .where(PinterestAccount.is_active.is_(True))
            .order_by(PinterestAccount.id)
        ))
        # An unscoped opportunity view must not pick a board recommendation
        # from an arbitrary Pinterest account when several are connected.
        if len(active_account_ids) == 1:
            recommendation_scope = active_account_ids[0]
    if recommendation_scope is not None:
        recommendation_query = recommendation_query.where(
            PinterestBoardRecommendation.scope_key == f"account:{recommendation_scope}"
        )
        for row in db.scalars(recommendation_query.order_by(
            PinterestBoardRecommendation.match_score.desc(), PinterestBoardRecommendation.rank,
            PinterestBoardRecommendation.id,
        )):
            recommendations.setdefault(row.seo_generation_id, []).append(row)
    seasonal: dict[int, SEOTrendSeasonalAssessment] = {}
    for row in db.scalars(
        select(SEOTrendSeasonalAssessment)
        .where(SEOTrendSeasonalAssessment.seo_generation_id.in_(ids))
        .order_by(SEOTrendSeasonalAssessment.reference_date.desc(), SEOTrendSeasonalAssessment.id.desc())
    ):
        seasonal.setdefault(row.seo_generation_id, row)
    history = list(db.scalars(
        select(PinCreative).where(PinCreative.product_id.in_(product_ids))
    ))
    history_by_product: dict[int, list[PinCreative]] = {}
    for creative in history:
        history_by_product.setdefault(creative.product_id, []).append(creative)
    learning_rows = _account_learning_rows(db, account_id)
    performance = _learning_signals(learning_rows)

    opportunities: list[dict[str, Any]] = []
    for product_id, generation in latest_by_product.items():
        intel = intelligence.get(generation.id)
        product = generation.product
        if intel is None or intel.status != "completed" or product is None:
            continue
        q = quality.get(generation.id)
        if q is not None and q.validation_status == "FAIL":
            continue
        creative_history = history_by_product.get(product_id, [])
        type_counts = Counter(item.creative_type for item in creative_history)
        existing_keyword_types: set[tuple[str, str]] = set()
        existing_keyword_tokens: list[set[str]] = []
        existing_angle_tokens: list[set[str]] = []
        for creative in creative_history:
            seo = creative.seo_metadata if isinstance(creative.seo_metadata, dict) else {}
            primary = normalize_keyword(seo.get("primary_keyword", ""))
            if primary:
                existing_keyword_types.add((primary, creative.creative_type))
            keyword_values = list(creative.keywords or [])
            normalized_values = [normalize_keyword(item) for item in keyword_values if isinstance(item, str)]
            for value in [primary, *normalized_values]:
                if value:
                    existing_keyword_types.add((value, creative.creative_type))
                    existing_keyword_tokens.append(semantic_tokens(value))
            angle = seo.get("creative_angle")
            if isinstance(angle, str) and angle.strip():
                existing_angle_tokens.append(semantic_tokens(angle))

        board = (recommendations.get(generation.id) or [None])[0]
        seasonal_row = seasonal.get(generation.id)
        keyword_items = intel.keyword_items if isinstance(intel.keyword_items, list) else []
        audience_values = sorted({
            normalize_keyword(item.get("normalized", ""))
            for item in keyword_items
            if isinstance(item, dict) and item.get("valid") and item.get("keyword_type") == "AUDIENCE"
            and isinstance(item.get("normalized"), str) and normalize_keyword(item["normalized"])
        })
        use_case_values = sorted({
            normalize_keyword(item.get("normalized", ""))
            for item in keyword_items
            if isinstance(item, dict) and item.get("valid") and item.get("keyword_type") == "USE_CASE"
            and isinstance(item.get("normalized"), str) and normalize_keyword(item["normalized"])
        })
        for keyword in _keyword_candidates(intel):
            normalized = normalize_keyword(keyword["raw"])
            if not normalized:
                continue
            token_set = semantic_tokens(normalized)
            overlap = max((len(token_set & prior) / len(token_set | prior)
                           for prior in existing_keyword_tokens if token_set and prior), default=0.0)
            for creative_type, angle in _CREATIVE_ANGLES.items():
                # Exact already-created idea is suppressed, not merely down-ranked.
                if (normalized, creative_type) in existing_keyword_types:
                    continue
                candidate_angle_tokens = semantic_tokens(angle)
                angle_count = sum(
                    bool(candidate_angle_tokens and prior)
                    and len(candidate_angle_tokens & prior) / len(candidate_angle_tokens | prior) >= 0.6
                    for prior in existing_angle_tokens
                )
                season_snapshot = (seasonal_row.calendar_snapshot or {}).get("season", {}) if seasonal_row else {}
                seasonal_info = season_snapshot.get("name") if isinstance(season_snapshot, dict) else None
                learning = _matching_learning_evidence(
                    performance,
                    keyword=keyword,
                    creative_type=creative_type,
                    angle=angle,
                    board_id=board.board_id if board else None,
                    season=seasonal_info,
                    region_code=seasonal_row.region_code if seasonal_row else None,
                    audience_values=audience_values,
                    use_case_values=use_case_values,
                )
                assessment = score_opportunity(
                    keyword_item=keyword,
                    quality_score=q.overall_score if q else None,
                    board_score=board.match_score if board else None,
                    seasonal_score=seasonal_row.seasonal_score if seasonal_row else None,
                    creative_type=creative_type,
                    type_history_count=type_counts.get(creative_type, 0),
                    keyword_overlap=overlap,
                    angle_history_count=angle_count,
                    observed_performance_score=learning["score"],
                    performance_adjustment=learning["adjustment"],
                    performance_status=learning["status"],
                )
                opportunities.append({
                    **assessment,
                    "performance_learning": learning,
                    "product_id": product.id,
                    "product_title": product.title,
                    "creative_type": creative_type,
                    "creative_type_label": _TYPE_LABELS[creative_type],
                    "primary_keyword": keyword["raw"],
                    "keyword_type": keyword["keyword_type"],
                    "creative_angle": angle,
                    "creative_angle_label": _ANGLE_LABELS[angle],
                    "board_name": board.board_name_snapshot if board else None,
                    "board_match_score": board.match_score if board else None,
                    "season": seasonal_info,
                    "season_reference_date": seasonal_row.reference_date.isoformat() if seasonal_row else None,
                    "why": " ".join(assessment["positive_signals"][:2]),
                    "generation_id": generation.id,
                })
    opportunities.sort(key=lambda item: (
        -item["score"], item["product_title"].casefold(), item["primary_keyword"].casefold(),
        item["creative_type"], item["generation_id"],
    ))
    return opportunities[:max(0, limit)]


def score_existing_creative_opportunities(
    db: Session, creatives: list[PinCreative], *, account_id: int | None = None,
    apply_learning: bool = True,
) -> dict[int, dict[str, Any]]:
    """Score scheduler-ready creatives with the same opportunity scoring signals.

    These rows represent already-created local candidates, not new SEO ideas. They
    let the existing scheduler consume the Opportunity Engine without converting
    suggestions into creatives or invoking an AI provider.
    """
    if not creatives:
        return {}
    creative_ids = [item.id for item in creatives]
    product_ids = sorted({item.product_id for item in creatives})
    generations = list(db.scalars(
        select(SEOGeneration).where(
            SEOGeneration.creative_id.in_(creative_ids), SEOGeneration.status == "completed"
        ).order_by(SEOGeneration.completed_at.desc(), SEOGeneration.id.desc())
    ))
    generation_by_creative: dict[int, SEOGeneration] = {}
    for generation in generations:
        if generation.creative_id is not None:
            generation_by_creative.setdefault(generation.creative_id, generation)
    generation_ids = [row.id for row in generation_by_creative.values()]
    intelligence = {row.seo_generation_id: row for row in db.scalars(
        select(SEOKeywordIntelligence).where(SEOKeywordIntelligence.seo_generation_id.in_(generation_ids or [-1]))
    )}
    quality = {row.seo_generation_id: row for row in db.scalars(
        select(SEOQualityAssessment).where(SEOQualityAssessment.seo_generation_id.in_(generation_ids or [-1]))
    )}
    boards: dict[int, PinterestBoardRecommendation] = {}
    recommendation_query = select(PinterestBoardRecommendation).where(
        PinterestBoardRecommendation.seo_generation_id.in_(generation_ids or [-1]),
        PinterestBoardRecommendation.status == "recommended",
    )
    if account_id is not None:
        recommendation_query = recommendation_query.where(
            PinterestBoardRecommendation.scope_key == f"account:{account_id}"
        )
    for row in db.scalars(recommendation_query.order_by(
        PinterestBoardRecommendation.match_score.desc(),
        PinterestBoardRecommendation.rank, PinterestBoardRecommendation.id,
    )):
        boards.setdefault(row.seo_generation_id, row)
    seasonal: dict[int, SEOTrendSeasonalAssessment] = {}
    for row in db.scalars(
        select(SEOTrendSeasonalAssessment).where(
            SEOTrendSeasonalAssessment.seo_generation_id.in_(generation_ids or [-1])
        ).order_by(SEOTrendSeasonalAssessment.reference_date.desc(), SEOTrendSeasonalAssessment.id.desc())
    ):
        seasonal.setdefault(row.seo_generation_id, row)
    product_history = list(db.scalars(select(PinCreative).where(PinCreative.product_id.in_(product_ids))))
    history_by_product: dict[int, list[PinCreative]] = {}
    for item in product_history:
        history_by_product.setdefault(item.product_id, []).append(item)
    learning = _learning_signals(_account_learning_rows(db, account_id))
    output: dict[int, dict[str, Any]] = {}
    for creative in creatives:
        generation = generation_by_creative.get(creative.id)
        intel = intelligence.get(generation.id) if generation else None
        metadata = creative.seo_metadata if isinstance(creative.seo_metadata, dict) else {}
        raw_keyword = metadata.get("primary_keyword")
        if not isinstance(raw_keyword, str) or not raw_keyword.strip():
            raw_keyword = next((item for item in (creative.keywords or [])
                                if isinstance(item, str) and item.strip()), "")
        normalized = normalize_keyword(raw_keyword)
        keyword_item = next((item for item in (intel.keyword_items or [])
                             if isinstance(item, dict) and item.get("valid")
                             and normalize_keyword(item.get("normalized") or item.get("raw", "")) == normalized), None) if intel and normalized else None
        if keyword_item is None:
            keyword_item = {
                "raw": raw_keyword, "normalized": normalized,
                "relevance": {}, "quality": {}, "semantic_group": None,
            }
        generation_id = generation.id if generation else None
        board = boards.get(generation_id) if generation_id else None
        season_row = seasonal.get(generation_id) if generation_id else None
        season_snapshot = (season_row.calendar_snapshot or {}).get("season", {}) if season_row else {}
        season = season_snapshot.get("name") if isinstance(season_snapshot, dict) else None
        history = history_by_product.get(creative.product_id, [])
        prior_keywords: list[set[str]] = []
        prior_angles: list[set[str]] = []
        for item in history:
            if item.id == creative.id:
                continue
            seo = item.seo_metadata if isinstance(item.seo_metadata, dict) else {}
            candidates = [seo.get("primary_keyword"), *(item.keywords or [])]
            prior_keywords.extend(
                semantic_tokens(value) for value in candidates
                if isinstance(value, str) and semantic_tokens(value)
            )
            prior_angle = seo.get("creative_angle")
            if isinstance(prior_angle, str) and prior_angle.strip():
                prior_angles.append(semantic_tokens(prior_angle))
        token_set = semantic_tokens(normalized)
        overlap = max((len(token_set & prior) / len(token_set | prior)
                       for prior in prior_keywords if token_set and prior), default=0.0)
        angle = metadata.get("creative_angle") or ""
        angle_tokens = semantic_tokens(angle) if isinstance(angle, str) else set()
        angle_history_count = sum(
            bool(angle_tokens and prior) and len(angle_tokens & prior) / len(angle_tokens | prior) >= .6
            for prior in prior_angles
        )
        evidence = _matching_learning_evidence(
            learning, keyword=keyword_item, creative_type=creative.creative_type,
            angle=angle if isinstance(angle, str) else "",
            board_id=board.board_id if board else None, season=season,
            region_code=season_row.region_code if season_row else None,
            audience_values=[], use_case_values=[],
        )
        assessment = score_opportunity(
            keyword_item=keyword_item,
            quality_score=quality[generation_id].overall_score if generation_id in quality else None,
            board_score=board.match_score if board else None,
            seasonal_score=season_row.seasonal_score if season_row else None,
            creative_type=creative.creative_type,
            type_history_count=sum(item.creative_type == creative.creative_type for item in history),
            keyword_overlap=overlap,
            angle_history_count=angle_history_count,
            observed_performance_score=evidence["score"],
            performance_adjustment=evidence["adjustment"],
            performance_status=evidence["status"],
            apply_learning=apply_learning,
        )
        has_opportunity_signal = any((
            assessment["components"]["keyword_quality"] is not None,
            assessment["components"]["keyword_relevance"] is not None,
            assessment["components"]["seo_quality"] is not None,
            assessment["components"]["board_fit"] is not None,
            assessment["components"]["seasonal_relevance"] is not None,
        ))
        output[creative.id] = {
            **assessment,
            "opportunity_score": assessment["score"] if has_opportunity_signal else None,
            "product_id": creative.product_id,
            "creative_id": creative.id,
            "primary_keyword": raw_keyword if isinstance(raw_keyword, str) else "",
            "keyword_cluster": keyword_item.get("semantic_group") or normalized or None,
            "creative_type": creative.creative_type,
            "creative_angle": angle if isinstance(angle, str) else "",
            "board_id": board.board_id if board else None,
            "board_name": board.board_name_snapshot if board else None,
            "season": season,
            "performance_learning": evidence,
            "generation_id": generation_id,
        }
    return output
