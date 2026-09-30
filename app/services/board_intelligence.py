"""Deterministic Pinterest board SEO profiles and SEO-generation matching.

This module consumes only board metadata already stored locally and immutable
SEO-generation snapshots. It never calls Pinterest or sends a publishing request.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    PinterestAccount,
    PinterestBoard,
    PinterestBoardRecommendation,
    PinterestBoardSEOProfile,
    SEOGeneration,
    SEOKeywordIntelligence,
)
from app.services.keyword_intelligence import analyze_keyword_set, normalize_keyword, semantic_tokens


BOARD_SEO_VERSION = "pinterest_board_seo_v1"
BOARD_MATCH_VERSION = "board_match_v1"
BOARD_METADATA_VERSION = "pinterest_board_metadata_v1"
BOARD_SOURCE_API = "pinterest_api_v5"
BOARD_SOURCE_LOCAL = "local_board_metadata"
MATCH_TOP_N = 3

_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into",
    "is", "it", "of", "on", "or", "the", "to", "with", "your", "you", "ideas",
    "inspiration", "inspirations", "collection", "collections",
}
_AUDIENCE_TERMS = {
    "athletes", "beginners", "brides", "children", "kids", "men", "moms", "parents",
    "runners", "shoppers", "students", "teachers", "women",
}
_INTENT_MARKERS = {
    "gift_intent": {"gift", "gifts", "gifting", "giftable"},
    "audience_intent": {"women", "men", "kids", "children", "beginners", "students", "teachers"},
    "use_case_intent": {"workout", "workouts", "recipe", "recipes", "outfit", "outfits", "room", "classroom"},
    "aesthetic_style_intent": {"style", "aesthetic", "minimalist", "decor", "design", "visual"},
}
_TOPIC_MARKERS = {
    "home_decor": {"home", "decor", "room", "interior", "living"},
    "fashion": {"fashion", "clothing", "outfit", "shirt", "dress", "jewelry"},
    "fitness": {"fitness", "workout", "gym", "running", "yoga"},
    "food": {"food", "recipe", "recipes", "cooking", "baking"},
    "wedding": {"wedding", "bride", "bridal", "ceremony"},
    "crafts": {"craft", "crafts", "diy", "sewing", "knitting", "printable"},
    "travel": {"travel", "trip", "vacation", "destination"},
    "beauty": {"beauty", "skincare", "makeup", "hair"},
}


def _tokens(value: str) -> list[str]:
    normalized = normalize_keyword(value) or ""
    return [token for token in normalized.split()
            if token not in _STOP_WORDS and 1 < len(token) <= 160]


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        normalized = normalize_keyword(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(raw.strip())
    return result


def _ngrams(tokens: list[str], sizes=(2, 3)) -> list[str]:
    phrases: list[str] = []
    for size in sizes:
        phrases.extend(" ".join(tokens[index:index + size]) for index in range(max(0, len(tokens) - size + 1)))
    return phrases


def _phrase_in_text(phrase: str, text: str) -> bool:
    needle = _tokens(phrase)
    haystack = _tokens(text)
    return bool(needle) and any(
        haystack[index:index + len(needle)] == needle
        for index in range(max(0, len(haystack) - len(needle) + 1))
    )


def _infer_labels(tokens: set[str]) -> tuple[list[str], list[str], list[str], list[str]]:
    intents = [label for label, markers in _INTENT_MARKERS.items() if tokens & markers]
    audience = sorted(tokens & _AUDIENCE_TERMS)
    use_cases = sorted(tokens & (_INTENT_MARKERS["use_case_intent"] | {
        "party", "classroom", "office", "bedroom", "kitchen", "gift", "gifting", "everyday",
    }))
    topics = sorted(label for label, markers in _TOPIC_MARKERS.items() if tokens & markers)
    return intents, audience, use_cases, topics


def analyze_board_metadata(
    name: str,
    description: str | None,
    *,
    source: str = BOARD_SOURCE_LOCAL,
) -> dict[str, Any]:
    """Return deterministic, explainable terms from actual board name/description."""
    clean_name = (name or "").strip()[:255]
    clean_description = (description or "").strip()[:5000]
    name_tokens = _tokens(clean_name)
    description_tokens = _tokens(clean_description)
    all_tokens = (name_tokens + description_tokens)[:1000]
    unique_terms = _unique(all_tokens)

    # The full board title is the primary candidate. Short description phrases
    # are capped to keep processing bounded and are derived only from observed text.
    secondary = [term for term in unique_terms if term not in set(name_tokens)][:64]
    phrases = _unique(_ngrams(name_tokens, (2, 3)) + _ngrams(description_tokens, (2, 3)))[:32]
    seo_metadata = {
        "primary_keyword": clean_name,
        "secondary_keywords": secondary,
        "long_tail_keywords": phrases,
        "audience_keywords": [],
        "use_case_keywords": [],
        "search_intents": [],
    }
    intents, audience, use_cases, topics = _infer_labels(set(all_tokens))
    seo_metadata["audience_keywords"] = audience
    seo_metadata["use_case_keywords"] = use_cases
    seo_metadata["search_intents"] = intents
    # Reuse the existing normalization, classification, relevance and semantic
    # grouping implementation instead of introducing a second keyword algorithm.
    intelligence = analyze_keyword_set(
        seo_metadata,
        title=clean_name,
        description=clean_description,
    )
    keyword_items = []
    for item in intelligence["keyword_items"]:
        term = item["normalized"] or ""
        in_name = _phrase_in_text(term, clean_name)
        in_description = _phrase_in_text(term, clean_description)
        if in_name and in_description:
            term_source = "board_name_and_description"
        elif in_name or item["source_field"] == "primary_keyword":
            term_source = "board_name"
        else:
            term_source = "board_description"
        keyword_items.append({
            "raw_term": item["raw"],
            "normalized_term": item["normalized"],
            "classification": item["keyword_type"],
            "intent": item["search_intent"],
            "semantic_group": item.get("semantic_group"),
            "source": term_source,
            "computed_status": "computed" if item["valid"] else "unavailable",
            "valid": item["valid"],
            "duplicate": item["duplicate"],
            "relevance": item["relevance"],
            "quality": item["quality"],
        })
    return {
        "algorithm_version": BOARD_SEO_VERSION,
        "source": source,
        "calculation_type": "deterministic_computed",
        "input_snapshot": {
            "name": clean_name,
            "description": clean_description,
        },
        "normalized_terms": unique_terms,
        "keyword_items": keyword_items,
        "search_intents": intents or ["unknown"],
        "audience_signals": audience,
        "use_case_signals": use_cases,
        "topic_signals": topics,
        "external_signals": {
            key: {"status": "not_collected", "value": None, "source": None}
            for key in ("search_volume", "popularity", "competition", "trend")
        },
    }


def _board_metadata_fingerprint(board: PinterestBoard, source: str) -> str:
    canonical = "\n".join((source, (board.name or "").strip()[:255], (board.description or "").strip()[:5000]))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ensure_board_seo_profiles(
    db: Session,
    boards: list[PinterestBoard],
) -> list[PinterestBoardSEOProfile]:
    """Bulk-load matching cached profiles and create only missing versions."""
    if not boards:
        return []
    prepared = []
    for board in boards:
        source = board.source or BOARD_SOURCE_LOCAL
        prepared.append((board, source, _board_metadata_fingerprint(board, source)))
    board_ids = [board.id for board, _, _ in prepared]
    existing_rows = db.scalars(select(PinterestBoardSEOProfile).where(
        PinterestBoardSEOProfile.board_id.in_(board_ids),
        PinterestBoardSEOProfile.algorithm_version == BOARD_SEO_VERSION,
    ))
    existing = {
        (row.board_id, row.metadata_fingerprint): row
        for row in existing_rows
    }
    profiles = []
    for board, source, fingerprint in prepared:
        profile = existing.get((board.id, fingerprint))
        if profile is None:
            calculated = analyze_board_metadata(board.name, board.description, source=source)
            profile = PinterestBoardSEOProfile(
                board=board,
                source=source,
                algorithm_version=BOARD_SEO_VERSION,
                computed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                metadata_fingerprint=fingerprint,
                calculation_type=calculated["calculation_type"],
                input_snapshot=calculated["input_snapshot"],
                normalized_terms=calculated["normalized_terms"],
                keyword_items=calculated["keyword_items"],
                search_intents=calculated["search_intents"],
                audience_signals=calculated["audience_signals"],
                use_case_signals=calculated["use_case_signals"],
                topic_signals=calculated["topic_signals"],
                external_signals=calculated["external_signals"],
            )
            db.add(profile)
        profiles.append(profile)
    db.flush()
    return profiles


def ensure_board_seo_profile(
    db: Session,
    board: PinterestBoard,
) -> PinterestBoardSEOProfile:
    """Reuse an immutable profile for unchanged observed metadata."""
    return ensure_board_seo_profiles(db, [board])[0]


def _as_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _jaccard(left: set[str], right: set[str]) -> float | None:
    if not left or not right:
        return None
    return len(left & right) / len(left | right)


def _profile_match(
    generation: SEOGeneration,
    intelligence: SEOKeywordIntelligence,
    profile: PinterestBoardSEOProfile,
) -> dict[str, Any]:
    snapshot = generation.output_snapshot if isinstance(generation.output_snapshot, dict) else {}
    seo = snapshot.get("seo_metadata") if isinstance(snapshot.get("seo_metadata"), dict) else {}
    pin_items = [item for item in (intelligence.keyword_items or [])
                 if isinstance(item, dict) and item.get("valid")]
    board_items = [item for item in (profile.keyword_items or [])
                   if isinstance(item, dict) and item.get("valid")]
    pin_phrases = {item.get("normalized") for item in pin_items if item.get("normalized")}
    board_phrases = {item.get("normalized_term") for item in board_items if item.get("normalized_term")}
    exact_overlap = sorted(pin_phrases & board_phrases)
    pin_tokens = {token for phrase in pin_phrases for token in semantic_tokens(phrase)}
    board_tokens = {token for phrase in board_phrases for token in semantic_tokens(phrase)}
    keyword_jaccard = _jaccard(pin_phrases, board_phrases)
    semantic_jaccard = _jaccard(pin_tokens, board_tokens)
    keyword_score = round((100 * keyword_jaccard) if keyword_jaccard is not None else 0)
    semantic_score = round((100 * semantic_jaccard) if semantic_jaccard is not None else 0)

    pin_intents = {item.get("search_intent") for item in pin_items
                   if item.get("search_intent") not in (None, "unknown")}
    board_intents = {intent for intent in (profile.search_intents or []) if intent != "unknown"}
    intent_score = 100 if pin_intents & board_intents else 0
    intent_available = bool(pin_intents and board_intents)

    pin_audience = {token for phrase in _as_strings(seo.get("audience_keywords")) for token in _tokens(phrase)}
    board_audience = set(profile.audience_signals or [])
    audience_overlap = _jaccard(pin_audience, board_audience)
    audience_score = round(100 * audience_overlap) if audience_overlap is not None else 0

    pin_use_cases = {token for phrase in _as_strings(seo.get("use_case_keywords")) for token in _tokens(phrase)}
    board_use_cases = set(profile.use_case_signals or [])
    use_case_overlap = _jaccard(pin_use_cases, board_use_cases)
    use_case_score = round(100 * use_case_overlap) if use_case_overlap is not None else 0

    pin_context_text = " ".join([
        str(snapshot.get("title") or ""), str(snapshot.get("description") or ""),
        str(seo.get("creative_angle") or ""),
        " ".join(_as_strings(seo.get("primary_keyword"))),
        " ".join(_as_strings(seo.get("secondary_keywords"))),
        " ".join(_as_strings(seo.get("long_tail_keywords"))),
    ])
    pin_context_tokens = set(_tokens(pin_context_text))
    pin_topics = {label for label, markers in _TOPIC_MARKERS.items() if pin_context_tokens & markers}
    board_topics = set(profile.topic_signals or [])
    topic_overlap = _jaccard(pin_topics, board_topics)
    topic_score = round(100 * topic_overlap) if topic_overlap is not None else 0

    raw_components = {
        "keyword_overlap": {"score": keyword_score, "available": keyword_jaccard is not None, "weight": 0.35},
        "semantic_similarity": {"score": semantic_score, "available": semantic_jaccard is not None, "weight": 0.25},
        "intent_compatibility": {"score": intent_score, "available": intent_available, "weight": 0.15},
        "audience_compatibility": {"score": audience_score, "available": audience_overlap is not None, "weight": 0.10},
        "use_case_compatibility": {"score": use_case_score, "available": use_case_overlap is not None, "weight": 0.10},
        "topic_relevance": {"score": topic_score, "available": topic_overlap is not None, "weight": 0.05},
    }
    available_weight = sum(item["weight"] for item in raw_components.values() if item["available"])
    overall = round(sum(
        item["score"] * item["weight"] for item in raw_components.values() if item["available"]
    ) / available_weight) if available_weight else 0

    positive: list[str] = []
    negative: list[str] = []
    if exact_overlap:
        positive.append("shared_keywords:" + ", ".join(exact_overlap[:5]))
    if semantic_score >= 35:
        positive.append("related_topic_terms_overlap")
    if intent_available and intent_score:
        positive.append("compatible_search_intent")
    if audience_score:
        positive.append("matching_audience_terms")
    if use_case_score:
        positive.append("matching_use_case_terms")
    if topic_score:
        positive.append("matching_topic_category")
    if keyword_score < 20:
        negative.append("weak_exact_keyword_overlap")
    if intent_available and not intent_score:
        negative.append("search_intent_mismatch")
    if pin_audience and board_audience and not audience_score:
        negative.append("audience_mismatch")
    if pin_topics and board_topics and not topic_score:
        negative.append("unrelated_topic_category")
    if not exact_overlap and semantic_score < 20:
        negative.append("weak_keyword_and_semantic_overlap")
    return {
        "score": overall,
        "components": raw_components,
        "available_weight": round(available_weight, 4),
        "positive_signals": positive,
        "negative_signals": negative,
        "evidence": {
            "shared_keyword_phrases": exact_overlap,
            "pin_intents": sorted(pin_intents),
            "board_intents": sorted(board_intents),
            "pin_audience_terms": sorted(pin_audience),
            "board_audience_terms": sorted(board_audience),
            "pin_use_case_terms": sorted(pin_use_cases),
            "board_use_case_terms": sorted(board_use_cases),
            "pin_topic_categories": sorted(pin_topics),
            "board_topic_categories": sorted(board_topics),
        },
        "algorithm_version": BOARD_MATCH_VERSION,
        "score_origin": "computed_heuristic_not_pinterest_ranking",
    }


def recommend_boards_for_generation(
    db: Session,
    seo_generation_id: int,
    *,
    account_id: int | None = None,
    top_n: int = MATCH_TOP_N,
) -> list[PinterestBoardRecommendation]:
    """Persist stable recommendations from locally cached boards only."""
    generation = db.get(SEOGeneration, seo_generation_id)
    if generation is None or generation.status != "completed" or not isinstance(generation.output_snapshot, dict):
        return []
    intelligence = db.scalar(select(SEOKeywordIntelligence).where(
        SEOKeywordIntelligence.seo_generation_id == generation.id
    ))
    if intelligence is None or intelligence.status != "completed":
        return []
    boards_query = (
        select(PinterestBoard)
        .join(PinterestAccount, PinterestAccount.id == PinterestBoard.account_id)
        .options(selectinload(PinterestBoard.account))
        .where(PinterestAccount.is_active.is_(True))
        .order_by(PinterestBoard.account_id, PinterestBoard.board_id)
    )
    if account_id is not None:
        boards_query = boards_query.where(PinterestBoard.account_id == account_id)
    boards = list(db.scalars(boards_query))
    if not boards:
        return []
    profiles = ensure_board_seo_profiles(db, boards)
    scope_key = f"account:{account_id}" if account_id is not None else "all_accounts"
    profile_ids = sorted(profile.id for profile in profiles)
    cohort_payload = f"{scope_key}|top:{max(1, top_n)}|" + ",".join(map(str, profile_ids))
    cohort_fingerprint = hashlib.sha256(cohort_payload.encode("utf-8")).hexdigest()
    cached = list(db.scalars(select(PinterestBoardRecommendation).where(
        PinterestBoardRecommendation.seo_generation_id == generation.id,
        PinterestBoardRecommendation.algorithm_version == BOARD_MATCH_VERSION,
        PinterestBoardRecommendation.scope_key == scope_key,
        PinterestBoardRecommendation.cohort_fingerprint == cohort_fingerprint,
    ).order_by(PinterestBoardRecommendation.rank, PinterestBoardRecommendation.id)))
    if len(cached) == len(profiles):
        return cached

    board_by_id = {board.id: board for board in boards}
    scored = []
    for profile in profiles:
        board = board_by_id[profile.board_id]
        result = _profile_match(generation, intelligence, profile)
        scored.append((result["score"], board, profile, result))
    scored.sort(key=lambda item: (-item[0], item[1].account_id, item[1].board_id.casefold(), item[1].id))
    recommendations: list[PinterestBoardRecommendation] = []
    eligible_rank = 0
    for rank, (score, board, profile, result) in enumerate(scored, start=1):
        if score <= 0 or not result["positive_signals"]:
            status = "rejected"
        else:
            eligible_rank += 1
            status = "recommended" if eligible_rank <= max(1, top_n) else "candidate"
        recommendation = PinterestBoardRecommendation(
            seo_generation_id=generation.id,
            board=board,
            board_profile=profile,
            account_identifier_snapshot=board.account.account_identifier if board.account else None,
            external_board_id_snapshot=board.board_id,
            board_name_snapshot=board.name,
            source="local_deterministic_match",
            algorithm_version=BOARD_MATCH_VERSION,
            scope_key=scope_key,
            cohort_fingerprint=cohort_fingerprint,
            calculated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            match_score=score,
            rank=rank,
            status=status,
            match_breakdown=result,
            positive_signals=result["positive_signals"],
            negative_signals=result["negative_signals"],
        )
        db.add(recommendation)
        recommendations.append(recommendation)
    db.flush()
    return recommendations


def recommend_boards_for_all_accounts(
    db: Session,
    seo_generation_id: int,
    *,
    top_n: int = MATCH_TOP_N,
) -> list[PinterestBoardRecommendation]:
    """Create separate account-scoped cohorts for every locally cached board account."""
    account_ids = list(db.scalars(
        select(PinterestBoard.account_id)
        .join(PinterestAccount, PinterestAccount.id == PinterestBoard.account_id)
        .where(PinterestAccount.is_active.is_(True))
        .distinct()
        .order_by(PinterestBoard.account_id)
    ))
    results = []
    for account_id in account_ids:
        results.extend(recommend_boards_for_generation(
            db, seo_generation_id, account_id=account_id, top_n=top_n
        ))
    return results


def publisher_board_candidates(
    db: Session,
    seo_generation_id: int,
    account_id: int,
    *,
    limit: int = MATCH_TOP_N,
) -> list[PinterestBoardRecommendation]:
    """Read persisted, account-scoped recommendations for a future publisher.

    This is a query interface only; it does not invoke the Pinterest publisher.
    """
    current_cohort = db.scalar(select(PinterestBoardRecommendation.cohort_fingerprint).where(
        PinterestBoardRecommendation.seo_generation_id == seo_generation_id,
        PinterestBoardRecommendation.scope_key == f"account:{account_id}",
    ).order_by(PinterestBoardRecommendation.calculated_at.desc(),
               PinterestBoardRecommendation.id.desc()).limit(1))
    if current_cohort is None:
        return []
    return list(db.scalars(select(PinterestBoardRecommendation)
        .join(PinterestBoard, PinterestBoard.id == PinterestBoardRecommendation.board_id)
        .where(
            PinterestBoardRecommendation.seo_generation_id == seo_generation_id,
            PinterestBoardRecommendation.scope_key == f"account:{account_id}",
            PinterestBoardRecommendation.cohort_fingerprint == current_cohort,
            PinterestBoardRecommendation.status == "recommended",
            PinterestBoard.account_id == account_id,
            PinterestAccount.is_active.is_(True),
        ).join(PinterestAccount, PinterestAccount.id == PinterestBoard.account_id)
        .order_by(PinterestBoardRecommendation.rank, PinterestBoardRecommendation.id).limit(limit)))
