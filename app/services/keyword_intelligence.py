"""Deterministic keyword analysis for immutable SEO generation snapshots.

This module performs no network or model calls. Its output is versioned and
persisted once per SEOGeneration so later reads never recompute or drift.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SEOGeneration, SEOKeywordIntelligence


KEYWORD_INTELLIGENCE_VERSION = "keyword_intelligence_v1"
MAX_KEYWORD_LENGTH = 160
_INTENTS = {
    "product_search", "gift_intent", "aesthetic_style_intent",
    "audience_intent", "use_case_intent",
}
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "into", "is", "it", "of", "on", "or", "the", "to", "with",
    "your", "you", "this", "that", "use", "best", "ideas", "style",
}
# Small, explicit equivalence groups aid deterministic clustering without
# changing the normalized/original keyword text or applying broad stemming.
_SEMANTIC_ALIASES = {
    "gym": "fitness", "workout": "fitness", "fitness": "fitness",
    "shirt": "shirt", "tee": "shirt", "tshirt": "shirt", "tshirts": "shirt",
}


def normalize_keyword(raw: str) -> str | None:
    """Normalize case, Unicode compatibility forms, punctuation and spacing."""
    if not isinstance(raw, str):
        return None
    value = unicodedata.normalize("NFKC", raw).casefold().strip()
    if not value:
        return None
    pieces: list[str] = []
    for char in value:
        category = unicodedata.category(char)
        if category[0] in {"L", "N"} or category[0] == "M":
            pieces.append(char)
        elif char.isspace():
            pieces.append(" ")
        else:
            pieces.append(" ")
    normalized = re.sub(r"\s+", " ", "".join(pieces)).strip()
    return normalized or None


def _tokens(value: str) -> set[str]:
    normalized = normalize_keyword(value) or ""
    return {token for token in normalized.split() if token not in _STOP_WORDS and len(token) > 1}


def _semantic_tokens(value: str) -> set[str]:
    return {_SEMANTIC_ALIASES.get(token, token) for token in _tokens(value)}


def _semantic_groups(items: list[dict[str, Any]]) -> None:
    valid = [index for index, item in enumerate(items) if item.get("valid") and item.get("normalized")]
    parents = {index: index for index in valid}

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parents[max(a, b)] = min(a, b)

    token_sets = {index: _semantic_tokens(items[index]["normalized"]) for index in valid}
    for offset, left in enumerate(valid):
        for right in valid[offset + 1:]:
            a, b = token_sets[left], token_sets[right]
            if not a or not b:
                continue
            similarity = len(a & b) / len(a | b)
            if similarity >= 0.6:
                union(left, right)

    clusters: dict[int, list[int]] = {}
    for index in valid:
        clusters.setdefault(find(index), []).append(index)
    ordered = sorted(clusters.values(), key=lambda group: min(group))
    for group in ordered:
        terms = sorted(set().union(*(token_sets[index] for index in group)))
        digest = hashlib.sha1(" ".join(terms).encode("utf-8")).hexdigest()[:10]
        group_id = f"semantic-{digest}"
        for index in group:
            items[index]["semantic_group"] = group_id
            items[index]["semantic_group_terms"] = terms


def analyze_keyword_set(
    seo_metadata: dict[str, Any] | None,
    *,
    title: str = "",
    description: str = "",
    product_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Build a fully deterministic keyword assessment from persisted SEO/context."""
    seo = seo_metadata if isinstance(seo_metadata, dict) else {}
    raw_entries: list[tuple[str, str, str]] = []
    primary = seo.get("primary_keyword")
    if isinstance(primary, str):
        raw_entries.append(("primary_keyword", primary, "PRIMARY"))
    for field, kind in (
        ("secondary_keywords", "SECONDARY"),
        ("long_tail_keywords", "LONG_TAIL"),
        ("audience_keywords", "AUDIENCE"),
        ("use_case_keywords", "USE_CASE"),
    ):
        values = seo.get(field)
        if isinstance(values, list):
            raw_entries.extend((field, value, kind) for value in values if isinstance(value, str))

    declared_intents = [
        value for value in seo.get("search_intents", [])
        if isinstance(value, str) and value in _INTENTS
    ] if isinstance(seo.get("search_intents", []), list) else []
    product_text = " ".join([title, description, *(product_tags or [])])
    product_tokens = _tokens(product_text)
    auxiliary_values: list[str] = []
    for field in ("audience_keywords", "use_case_keywords", "creative_angle"):
        value = seo.get(field)
        if isinstance(value, str):
            auxiliary_values.append(value)
        elif isinstance(value, list):
            auxiliary_values.extend(item for item in value if isinstance(item, str))
    auxiliary_text = " ".join(auxiliary_values)
    auxiliary_tokens = _tokens(auxiliary_text)

    items: list[dict[str, Any]] = []
    first_by_normalized: dict[str, int] = {}
    for source_field, raw, keyword_type in raw_entries:
        normalized = normalize_keyword(raw)
        invalid_reason = None
        if normalized is None:
            invalid_reason = "empty_or_punctuation_only"
        elif len(normalized) > MAX_KEYWORD_LENGTH:
            invalid_reason = "too_long"
        elif not _tokens(normalized):
            invalid_reason = "no_meaningful_terms"

        item: dict[str, Any] = {
            "raw": raw,
            "normalized": normalized,
            "keyword_type": keyword_type,
            "source_field": source_field,
            "valid": invalid_reason is None,
            "invalid_reason": invalid_reason,
            "duplicate": False,
            "duplicate_of": None,
            "topic_terms": [],
            "search_intent": "unknown",
            "relevance": {"score": 0, "method": "deterministic_context_overlap", "evidence": []},
            "quality": {},
        }
        if normalized is not None:
            if normalized in first_by_normalized:
                item["duplicate"] = True
                item["duplicate_of"] = first_by_normalized[normalized]
            else:
                first_by_normalized[normalized] = len(items)

        if item["valid"]:
            terms = _tokens(normalized or "")
            product_matches = sorted(terms & product_tokens)
            auxiliary_matches = sorted(terms & auxiliary_tokens)
            evidence = []
            if product_matches:
                evidence.append({"source": "product_title_description_or_tags", "matched_terms": product_matches})
            if auxiliary_matches:
                evidence.append({"source": "seo_audience_use_case_or_angle", "matched_terms": auxiliary_matches})
            denominator = max(1, len(terms))
            score = min(100, round((len(product_matches) * 75 + len(set(auxiliary_matches) - set(product_matches)) * 25) / denominator))
            item["topic_terms"] = product_matches
            if len(declared_intents) == 1:
                item["search_intent"] = declared_intents[0]
            else:
                normalized_tokens = set(normalized.split())
                for marker, intent in (
                    ({"gift", "gifting", "giftable"}, "gift_intent"),
                    ({"audience", "shoppers"}, "audience_intent"),
                    ({"use", "using", "workout", "everyday"}, "use_case_intent"),
                    ({"style", "aesthetic", "minimalist"}, "aesthetic_style_intent"),
                ):
                    if normalized_tokens & marker and intent in declared_intents:
                        item["search_intent"] = intent
                        break
            meaningful_count = len(terms)
            specificity = min(100, max(0, (meaningful_count - 1) * 25 + min(len(normalized), 40)))
            item["relevance"] = {
                "score": score,
                "method": "deterministic_context_overlap",
                "evidence": evidence,
            }
            item["quality"] = {
                "specificity_score": specificity,
                "intent_compatibility": "declared" if item["search_intent"] != "unknown" else "unknown",
                "duplicate_risk": False,
                "stuffing_risk": False,
                "signal_origin": "computed",
            }
        items.append(item)

    _semantic_groups(items)
    group_sizes: dict[str, int] = {}
    for item in items:
        group_id = item.get("semantic_group")
        if group_id:
            group_sizes[group_id] = group_sizes.get(group_id, 0) + 1
    valid_items = [item for item in items if item["valid"]]
    duplicate_count = sum(1 for item in valid_items if item["duplicate"])
    for item in valid_items:
        size = group_sizes.get(item.get("semantic_group"), 1)
        duplicate_risk = bool(item["duplicate"] or size > 1)
        stuffing_risk = bool(duplicate_risk and size >= 3)
        item["quality"]["duplicate_risk"] = duplicate_risk
        item["quality"]["stuffing_risk"] = stuffing_risk
        item["quality"]["semantic_uniqueness"] = "unique" if size == 1 else "near_duplicate_cluster"
        base_quality = round(
            item["relevance"]["score"] * 0.7
            + item["quality"]["specificity_score"] * 0.3
        )
        penalties = (
            (20 if item["duplicate"] else 0)
            + (10 if size > 1 and not item["duplicate"] else 0)
            + (20 if stuffing_risk else 0)
            + (15 if item["relevance"]["score"] == 0 else 0)
        )
        item["quality"]["heuristic_quality_score"] = max(0, base_quality - penalties)
        item["quality"]["quality_method"] = "weighted_context_specificity_with_duplicate_penalties_v1"

    core_items = [item for item in valid_items if item["keyword_type"] in {"PRIMARY", "SECONDARY", "LONG_TAIL"}]
    duplicate_ratio = duplicate_count / max(1, len(valid_items))
    stuffing_flags = sum(bool(item.get("quality", {}).get("stuffing_risk")) for item in valid_items)
    flags = []
    if len(core_items) > 12:
        flags.append("keyword_list_too_large")
    if duplicate_ratio > 0.25:
        flags.append("repeated_keywords")
    if any(size > 1 for size in group_sizes.values()):
        flags.append("near_duplicate_keywords")
    if stuffing_flags:
        flags.append("semantic_keyword_stuffing_risk")
    if any(item["relevance"]["score"] == 0 for item in core_items):
        flags.append("unmatched_context_terms")

    candidate_id = "candidate-" + hashlib.sha1(
        "|".join(item["normalized"] or "" for item in valid_items).encode("utf-8")
    ).hexdigest()[:10]
    candidate_set = {
        "candidate_id": candidate_id,
        "source": "seo_generation_snapshot",
        "selection": "current_generation_output",
        "keyword_item_indexes": [index for index, item in enumerate(items) if item["valid"]],
    }
    return {
        "status": "completed" if valid_items else "unavailable",
        "keyword_items": items,
        "candidate_sets": [candidate_set] if valid_items else [],
        "quality_summary": {
            "valid_keyword_count": len(valid_items),
            "unique_keyword_count": len({item["normalized"] for item in valid_items}),
            "duplicate_keyword_count": duplicate_count,
            "semantic_cluster_count": len(group_sizes),
            "core_keyword_count": len(core_items),
            "average_heuristic_quality_score": (
                round(sum(item["quality"]["heuristic_quality_score"] for item in valid_items) / len(valid_items))
                if valid_items else None
            ),
            "validation_flags": flags,
            "quality_method": "deterministic_heuristics_v1",
        },
        "external_signals": {
            "search_volume": {"status": "not_collected", "value": None, "source": None},
            "popularity": {"status": "not_collected", "value": None, "source": None},
            "competition": {"status": "not_collected", "value": None, "source": None},
            "trend": {"status": "not_collected", "value": None, "source": None},
        },
        "signal_origins": {
            "raw_keywords": "ai_generated_or_inherited_from_seo_snapshot",
            "normalization_grouping_relevance_quality": "deterministic_computed",
            "pinterest_external_data": "unavailable_not_collected",
        },
    }


def ensure_keyword_intelligence(
    db: Session,
    generation: SEOGeneration,
    *,
    title: str = "",
    description: str = "",
    product_tags: list[str] | None = None,
) -> SEOKeywordIntelligence:
    """Return the cached immutable assessment, creating it at most once per generation."""
    existing = db.scalar(select(SEOKeywordIntelligence).where(
        SEOKeywordIntelligence.seo_generation_id == generation.id
    ))
    if existing is not None:
        return existing

    snapshot = generation.output_snapshot if isinstance(generation.output_snapshot, dict) else {}
    seo = snapshot.get("seo_metadata") if isinstance(snapshot.get("seo_metadata"), dict) else {}
    result = analyze_keyword_set(
        seo,
        title=title or str(snapshot.get("title") or ""),
        description=description or str(snapshot.get("description") or ""),
        product_tags=product_tags,
    )
    row = SEOKeywordIntelligence(
        seo_generation=generation,
        computed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        algorithm_version=KEYWORD_INTELLIGENCE_VERSION,
        status=result["status"],
        keyword_items=result["keyword_items"],
        candidate_sets=result["candidate_sets"],
        quality_summary=result["quality_summary"],
        external_signals=result["external_signals"],
        signal_origins=result["signal_origins"],
    )
    db.add(row)
    db.flush()
    return row
