"""Explainable, deterministic Pin-level SEO scoring and validation."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SEOGeneration, SEOKeywordIntelligence, SEOQualityAssessment
from app.services.keyword_intelligence import normalize_keyword


SEO_SCORE_VERSION = "seo_full_score_v1"
SEO_VALIDATION_VERSION = "seo_validation_v1"
_CORE_TYPES = {"PRIMARY", "SECONDARY", "LONG_TAIL"}


def _tokens(value: str) -> list[str]:
    normalized = normalize_keyword(value) or ""
    return normalized.split()


def _clamp_score(value: float) -> int:
    return max(0, min(100, round(value)))


def _contains_phrase(haystack: str, phrase: str) -> bool:
    needle = _tokens(phrase)
    words = _tokens(haystack)
    if not needle or len(needle) > len(words):
        return False
    return any(words[index:index + len(needle)] == needle for index in range(len(words) - len(needle) + 1))


def calculate_seo_quality(
    output_snapshot: dict[str, Any] | None,
    keyword_intelligence: SEOKeywordIntelligence | dict[str, Any] | None,
) -> dict[str, Any]:
    """Use the persisted Keyword Intelligence record; do not recalculate its signals."""
    snapshot = output_snapshot if isinstance(output_snapshot, dict) else {}
    seo = snapshot.get("seo_metadata") if isinstance(snapshot.get("seo_metadata"), dict) else {}
    title = snapshot.get("title") if isinstance(snapshot.get("title"), str) else ""
    description = snapshot.get("description") if isinstance(snapshot.get("description"), str) else ""
    cta = snapshot.get("call_to_action") if isinstance(snapshot.get("call_to_action"), str) else ""
    if isinstance(keyword_intelligence, dict):
        kw_items = keyword_intelligence.get("keyword_items", [])
        kw_summary = keyword_intelligence.get("quality_summary", {})
    elif keyword_intelligence is not None:
        kw_items = keyword_intelligence.keyword_items or []
        kw_summary = keyword_intelligence.quality_summary or {}
    else:
        kw_items, kw_summary = [], {}
    kw_items = [item for item in kw_items if isinstance(item, dict) and item.get("valid")]
    core_items = [item for item in kw_items if item.get("keyword_type") in _CORE_TYPES]
    primary = seo.get("primary_keyword") if isinstance(seo.get("primary_keyword"), str) else ""
    secondary = seo.get("secondary_keywords") if isinstance(seo.get("secondary_keywords"), list) else []
    long_tail = seo.get("long_tail_keywords") if isinstance(seo.get("long_tail_keywords"), list) else []
    audience = seo.get("audience_keywords") if isinstance(seo.get("audience_keywords"), list) else []
    use_case = seo.get("use_case_keywords") if isinstance(seo.get("use_case_keywords"), list) else []
    intents = seo.get("search_intents") if isinstance(seo.get("search_intents"), list) else []
    angle = seo.get("creative_angle") if isinstance(seo.get("creative_angle"), str) else ""

    stored_quality = [
        item.get("quality", {}).get("heuristic_quality_score")
        for item in core_items
        if isinstance(item.get("quality"), dict)
        and isinstance(item.get("quality", {}).get("heuristic_quality_score"), (int, float))
    ]
    mean_keyword_quality = sum(stored_quality) / len(stored_quality) if stored_quality else 0
    primary_coverage = 100 if primary and any(
        item.get("keyword_type") == "PRIMARY" and item.get("normalized") for item in kw_items
    ) else 0
    secondary_coverage = 100 if any(
        item.get("keyword_type") == "SECONDARY" for item in kw_items
    ) else 0
    long_tail_coverage = 100 if any(
        item.get("keyword_type") == "LONG_TAIL" for item in kw_items
    ) else 0
    intent_compatibility = 100 if any(
        item.get("search_intent") not in (None, "unknown") for item in core_items
    ) else 0
    semantic_groups = {
        item.get("semantic_group") for item in core_items if item.get("semantic_group")
    }
    semantic_diversity = (
        100 * len(semantic_groups) / len(core_items) if core_items else 0
    )
    keyword_score = _clamp_score(
        mean_keyword_quality * 0.50
        + primary_coverage * 0.20
        + secondary_coverage * 0.10
        + long_tail_coverage * 0.10
        + intent_compatibility * 0.05
        + semantic_diversity * 0.05
    )

    primary_in_title = bool(primary and _contains_phrase(title, primary))
    title_tokens = _tokens(title)
    core_keyword_tokens = {
        token for item in core_items for token in _tokens(str(item.get("normalized") or ""))
    }
    title_coverage = (
        len(core_keyword_tokens & set(title_tokens)) / len(core_keyword_tokens)
        if core_keyword_tokens else 0
    )
    title_length_signal = 100 if 15 <= len(title.strip()) <= 255 else (50 if title.strip() else 0)
    title_score = _clamp_score(
        (100 if title.strip() else 0) * 0.25
        + (100 if primary_in_title else 0) * 0.35
        + title_coverage * 100 * 0.25
        + title_length_signal * 0.15
    )

    description_tokens = _tokens(description)
    description_coverage = (
        len(core_keyword_tokens & set(description_tokens)) / len(core_keyword_tokens)
        if core_keyword_tokens else 0
    )
    description_diversity = (
        len(set(description_tokens)) / len(description_tokens) if description_tokens else 0
    )
    description_score = _clamp_score(
        (100 if description.strip() else 0) * 0.25
        + description_coverage * 100 * 0.30
        + min(100, description_diversity * 125) * 0.20
        + (100 if len(description.strip()) >= 40 else (50 if description.strip() else 0)) * 0.15
        + (100 if cta.strip() else 0) * 0.10
    )

    metadata_presence = [
        bool(primary), bool(secondary), bool(long_tail), bool(audience),
        bool(use_case), bool(intents), bool(angle),
    ]
    metadata_score = _clamp_score(100 * sum(metadata_presence) / len(metadata_presence))
    keyword_relevance_values = [
        item.get("relevance", {}).get("score")
        for item in core_items
        if isinstance(item.get("relevance"), dict)
        and isinstance(item.get("relevance", {}).get("score"), (int, float))
    ]
    stored_relevance = (
        sum(keyword_relevance_values) / len(keyword_relevance_values)
        if keyword_relevance_values else 0
    )
    relevance_score = _clamp_score(
        stored_relevance * 0.50 + title_coverage * 100 * 0.25 + description_coverage * 100 * 0.25
    )

    negative_signals: list[str] = []
    positive_signals: list[str] = []
    warnings: list[dict[str, str]] = []
    if primary_coverage:
        positive_signals.append("primary_keyword_available")
    if primary_in_title:
        positive_signals.append("title_contains_primary_keyword")
    if long_tail_coverage:
        positive_signals.append("long_tail_keyword_coverage_available")
    if description_coverage >= 0.5:
        positive_signals.append("description_covers_core_keyword_terms")

    duplicate_count = int(kw_summary.get("duplicate_keyword_count") or 0)
    valid_count = int(kw_summary.get("valid_keyword_count") or len(kw_items))
    duplicate_ratio = duplicate_count / max(1, valid_count)
    stuffing_count = sum(
        bool(item.get("quality", {}).get("stuffing_risk"))
        for item in kw_items if isinstance(item.get("quality"), dict)
    )
    semantic_duplicate_count = sum(
        bool(item.get("quality", {}).get("duplicate_risk")) and not bool(item.get("duplicate"))
        for item in core_items if isinstance(item.get("quality"), dict)
    )
    core_zero_relevance = [
        item for item in core_items
        if isinstance(item.get("relevance"), dict) and item["relevance"].get("score") == 0
    ]
    title_words = title_tokens
    repeated_title_words = any(title_words.count(word) > 2 for word in set(title_words))
    description_density = (
        sum(1 for token in description_tokens if token in core_keyword_tokens) / len(description_tokens)
        if description_tokens else 0
    )
    repeated_phrases = sum(
        1 for item in core_items
        if item.get("normalized") and len(_tokens(str(item["normalized"]))) > 1
        and sum(_contains_phrase(text, str(item["normalized"])) for text in (title, description)) > 2
    )
    spam_penalty = min(
        40,
        round(duplicate_ratio * 20)
        + min(20, stuffing_count * 5)
        + min(10, semantic_duplicate_count * 3)
        + (10 if repeated_title_words else 0)
        + (10 if description_density > 0.5 and len(description_tokens) >= 10 else 0)
        + min(10, repeated_phrases * 5),
    )
    if duplicate_count:
        negative_signals.append("duplicate_keywords_present")
        warnings.append({"code": "duplicate_keywords", "message": "Keyword Intelligence duplicate keyword işaretleri bulundu."})
    if semantic_duplicate_count:
        negative_signals.append("near_duplicate_semantic_keywords")
        warnings.append({"code": "near_duplicate_keywords", "message": "Keyword Intelligence benzer semantic cluster işaretleri bulundu."})
    if stuffing_count or repeated_title_words or description_density > 0.5 or repeated_phrases:
        negative_signals.append("keyword_stuffing_risk")
        warnings.append({"code": "keyword_stuffing", "message": "Tekrar veya yoğun keyword kullanımı için heuristic risk sinyali bulundu."})
    if core_zero_relevance:
        negative_signals.append("core_keywords_without_context_overlap")
        warnings.append({"code": "low_keyword_relevance", "message": "Bazı ana keyword'ler kayıtlı ürün bağlamıyla eşleşmiyor."})
    if title.strip() and len(title.strip()) < 15:
        negative_signals.append("short_title_heuristic")
        warnings.append({"code": "short_title", "message": "Başlık dahili kısalık heuristic'ine göre kısa."})
    if description.strip() and len(description.strip()) < 40:
        negative_signals.append("short_description_heuristic")
        warnings.append({"code": "short_description", "message": "Açıklama dahili kısalık heuristic'ine göre kısa."})
    if len(core_items) > 12:
        negative_signals.append("large_keyword_set")
        warnings.append({"code": "large_keyword_set", "message": "Ana keyword listesi dahili kontrol eşiğini aşıyor."})
    if metadata_score < 100:
        warnings.append({"code": "incomplete_metadata", "message": "Bazı opsiyonel SEO metadata alanları boş."})

    weighted_score = (
        keyword_score * 0.30
        + title_score * 0.20
        + description_score * 0.20
        + metadata_score * 0.15
        + relevance_score * 0.15
    )
    overall = _clamp_score(weighted_score - spam_penalty)
    if overall < 40:
        negative_signals.append("low_heuristic_score")
        warnings.append({"code": "low_heuristic_score", "message": "Heuristic SEO skoru düşük; bu tek başına generation hatası değildir."})
    components = {
        "keyword_score": keyword_score,
        "title_score": title_score,
        "description_score": description_score,
        "metadata_score": metadata_score,
        "relevance_score": relevance_score,
        "spam_penalty": spam_penalty,
    }
    score_result = {
        "overall": overall,
        "components": components,
        "weights": {
            "keyword_score": 0.30,
            "title_score": 0.20,
            "description_score": 0.20,
            "metadata_score": 0.15,
            "relevance_score": 0.15,
            "spam_penalty": "subtracted_after_weighted_score",
        },
        "positive_signals": positive_signals,
        "negative_signals": negative_signals,
        "warnings": warnings,
        "calculation_version": SEO_SCORE_VERSION,
        "score_origin": "computed_heuristic_not_pinterest_ranking",
    }

    errors: list[dict[str, str]] = []
    if not title.strip():
        errors.append({"code": "missing_title", "message": "SEO başlığı boş."})
    if not description.strip():
        errors.append({"code": "missing_description", "message": "SEO açıklaması boş."})
    if not primary.strip():
        errors.append({"code": "missing_primary_keyword", "message": "Primary keyword eksik."})
    if primary.strip() and title.strip() and not primary_in_title:
        errors.append({"code": "title_primary_keyword_mismatch", "message": "Başlık primary keyword ile eşleşmiyor."})
    for field, values in (
        ("secondary_keywords", secondary), ("long_tail_keywords", long_tail),
        ("audience_keywords", audience), ("use_case_keywords", use_case),
        ("search_intents", intents),
    ):
        if not values:
            errors.append({"code": f"missing_{field}", "message": f"{field} alanı eksik."})
    if not angle.strip():
        errors.append({"code": "missing_creative_angle", "message": "Creative angle eksik."})
    if core_items and len(core_zero_relevance) / len(core_items) > 0.5:
        errors.append({"code": "core_keyword_relevance_failure", "message": "Ana keyword grubunun çoğunda bağlam eşleşmesi bulunamadı."})

    passed_checks = []
    if title.strip():
        passed_checks.append("title_present")
    if description.strip():
        passed_checks.append("description_present")
    if primary.strip():
        passed_checks.append("primary_keyword_present")
    if primary_in_title:
        passed_checks.append("title_contains_primary_keyword")
    if not duplicate_count and not semantic_duplicate_count:
        passed_checks.append("no_exact_duplicate_keywords")
    if not stuffing_count and not semantic_duplicate_count and not repeated_title_words and not repeated_phrases:
        passed_checks.append("no_detected_keyword_stuffing")
    if core_items and not core_zero_relevance:
        passed_checks.append("core_keywords_have_context_overlap")
    validation_status = "FAIL" if errors else ("WARN" if warnings else "PASS")
    validation_result = {
        "status": validation_status,
        "errors": errors,
        "warnings": warnings,
        "passed_checks": passed_checks,
        "failed_checks": [item["code"] for item in errors],
        "validation_version": SEO_VALIDATION_VERSION,
        "validation_origin": "deterministic_heuristic",
        "low_score_is_not_generation_failure": True,
    }
    return {"score": score_result, "validation": validation_result}


def ensure_seo_quality_assessment(
    db: Session,
    generation: SEOGeneration,
    keyword_intelligence: SEOKeywordIntelligence | None = None,
) -> SEOQualityAssessment:
    """Persist one immutable score/validation result per SEO generation."""
    existing = db.scalar(select(SEOQualityAssessment).where(
        SEOQualityAssessment.seo_generation_id == generation.id
    ))
    if existing is not None:
        return existing
    intelligence = keyword_intelligence or generation.keyword_intelligence
    result = calculate_seo_quality(generation.output_snapshot, intelligence)
    assessment = SEOQualityAssessment(
        seo_generation=generation,
        assessed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        score_version=SEO_SCORE_VERSION,
        validation_version=SEO_VALIDATION_VERSION,
        calculation_type="deterministic_heuristic",
        overall_score=result["score"]["overall"],
        score_breakdown=result["score"],
        validation_status=result["validation"]["status"],
        validation_result=result["validation"],
    )
    db.add(assessment)
    db.flush()
    return assessment
