"""Deterministic evaluation of SEO options using PinPilot's existing signals.

Candidate history is stored in the selected SEOGeneration snapshot, so the
optimizer does not introduce a parallel persistence model or migration.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.models import PinCreative, PinterestAccount, PinterestBoard
from app.services.board_intelligence import _profile_match, analyze_board_metadata
from app.services.keyword_intelligence import analyze_keyword_set, normalize_keyword, semantic_tokens
from app.services.opportunity_engine import (
    _account_learning_rows,
    _learning_signals,
    _matching_learning_evidence,
    score_opportunity,
)
from app.services.seo_quality import calculate_seo_quality
from app.services.trend_seasonal import analyze_trend_seasonal_context


SEO_CANDIDATE_RANKING_VERSION = "seo_candidate_rank_v1"
_CANDIDATE_SIMILARITY_PENALTY_MAX = 12


def _candidate_id(snapshot: dict[str, Any], generated_at: str) -> str:
    canonical = generated_at + "\n" + json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"seo-candidate-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def _candidate_tokens(snapshot: dict[str, Any]) -> set[str]:
    seo = snapshot.get("seo_metadata") if isinstance(snapshot.get("seo_metadata"), dict) else {}
    values = [snapshot.get("title", ""), seo.get("primary_keyword", ""), seo.get("creative_angle", "")]
    values.extend(seo.get("secondary_keywords", []) if isinstance(seo.get("secondary_keywords"), list) else [])
    values.extend(seo.get("long_tail_keywords", []) if isinstance(seo.get("long_tail_keywords"), list) else [])
    return set().union(*(semantic_tokens(value) for value in values if isinstance(value, str)))


def _similarity(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def evaluate_seo_candidates(
    db,
    *,
    product: Any,
    context: Any,
    creative_type: str,
    candidates: list[dict[str, Any]],
    provider: str,
    model: str,
    generated_at: str,
    previous_creatives: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Return ranked, explainable candidate records without external calls."""
    accounts = list(db.scalars(select(PinterestAccount).where(
        PinterestAccount.is_active.is_(True)
    ).order_by(PinterestAccount.id)))
    # Learning evidence is account-local. Ambiguous account scope remains neutral.
    learning_rows = _account_learning_rows(db, accounts[0].id) if len(accounts) == 1 else []
    learning_signals = _learning_signals(learning_rows)
    # A candidate has no account assignment yet. Only use board context when
    # there is exactly one active Pinterest account; combining boards from
    # multiple accounts would persist cross-account recommendations in SEO
    # candidate provenance.
    boards = list(db.scalars(
        select(PinterestBoard).where(
            PinterestBoard.account_id == accounts[0].id
        ).order_by(PinterestBoard.board_id, PinterestBoard.id)
    )) if len(accounts) == 1 else []
    # Build current metadata profiles once per board using the existing Board SEO analyzer.
    board_profiles = {
        board.id: analyze_board_metadata(
            board.name, board.description, source=board.source or "local_board_metadata"
        )
        for board in boards
    }

    existing = list(db.scalars(select(PinCreative).where(
        PinCreative.product_id == product.id
    ).order_by(PinCreative.id)))
    existing_primary = []
    type_history_count = 0
    angle_history = []
    for creative in existing:
        seo = creative.seo_metadata if isinstance(creative.seo_metadata, dict) else {}
        primary = normalize_keyword(str(seo.get("primary_keyword") or ""))
        terms = set(semantic_tokens(primary or ""))
        terms.update(*(semantic_tokens(value) for value in (creative.keywords or []) if isinstance(value, str)))
        if terms:
            existing_primary.append(terms)
        if creative.creative_type == creative_type:
            type_history_count += 1
        angle = str(seo.get("creative_angle") or "")
        if angle:
            angle_history.append(angle)

    evaluated: list[dict[str, Any]] = []
    all_candidate_tokens = [_candidate_tokens(item["snapshot"]) for item in candidates]
    for index, item in enumerate(candidates):
        snapshot = item["snapshot"]
        seo = snapshot.get("seo_metadata", {})
        candidate_index = int(item.get("candidate_index", index))
        candidate_id = _candidate_id(snapshot, generated_at)
        intel = analyze_keyword_set(
            seo,
            title=snapshot.get("title", ""),
            description=snapshot.get("description", ""),
            product_tags=context.tags,
        )
        quality = calculate_seo_quality(snapshot, intel)
        core_keywords = [keyword for keyword in intel["keyword_items"]
                         if keyword.get("valid") and not keyword.get("duplicate")
                         and keyword.get("keyword_type") in {"PRIMARY", "SECONDARY", "LONG_TAIL"}]
        primary = next((keyword for keyword in core_keywords if keyword.get("keyword_type") == "PRIMARY"), None)
        primary = primary or next(iter(core_keywords), {"raw": "", "normalized": "", "quality": {}, "relevance": {"score": None}, "search_intent": "unknown"})

        transient_generation = type("CandidateGeneration", (), {
            "output_snapshot": snapshot,
            "completed_at": date.fromisoformat(generated_at[:10]),
        })()
        transient_intel = type("CandidateIntelligence", (), {"keyword_items": intel["keyword_items"]})()
        board_matches = []
        for board in boards:
            calculated = board_profiles[board.id]
            profile = type("CandidateBoardProfile", (), {
                "keyword_items": calculated["keyword_items"],
                "search_intents": calculated["search_intents"],
                "audience_signals": calculated["audience_signals"],
                "use_case_signals": calculated["use_case_signals"],
                "topic_signals": calculated["topic_signals"],
            })()
            match = _profile_match(transient_generation, transient_intel, profile)
            board_matches.append((match["score"], board, match))
        board_matches.sort(key=lambda row: (-row[0], row[1].account_id, row[1].board_id.casefold(), row[1].id))
        best_board = board_matches[0] if board_matches and board_matches[0][0] > 0 else None
        board_score = best_board[0] if best_board else None
        board_id = best_board[1].id if best_board else None

        region = settings.seo_calendar_region
        seasonal = analyze_trend_seasonal_context(
            transient_generation,
            transient_intel,
            reference_date=date.fromisoformat(generated_at[:10]),
            region_code=region,
        )
        season_name = seasonal.get("season", {}).get("name") if isinstance(seasonal.get("season"), dict) else None
        learning = _matching_learning_evidence(
            learning_signals,
            keyword=primary,
            creative_type=creative_type,
            angle=str(seo.get("creative_angle") or ""),
            board_id=board_id,
            season=season_name,
            region_code=seasonal.get("region_code"),
            audience_values=seo.get("audience_keywords", []),
            use_case_values=seo.get("use_case_keywords", []),
        )
        keyword_overlap = max((_similarity(_candidate_tokens(snapshot), terms) for terms in existing_primary), default=0.0)
        exact_prior = any(
            normalize_keyword(str(seo.get("primary_keyword") or "")) == normalize_keyword(item.get("primary_keyword", ""))
            or normalize_keyword(str(seo.get("creative_angle") or "")) == normalize_keyword(item.get("creative_angle", ""))
            for item in previous_creatives
        )
        peer_similarity = max((
            _similarity(all_candidate_tokens[index], tokens)
            for peer_index, tokens in enumerate(all_candidate_tokens)
            if peer_index != index
        ), default=0.0)
        duplicate_penalty = round(peer_similarity * _CANDIDATE_SIMILARITY_PENALTY_MAX)
        opportunity = score_opportunity(
            keyword_item=primary,
            quality_score=quality["score"]["overall"],
            board_score=board_score,
            seasonal_score=seasonal["score"]["value"],
            creative_type=creative_type,
            type_history_count=type_history_count,
            keyword_overlap=keyword_overlap,
            angle_history_count=sum(
                1 for angle in angle_history
                if _similarity(semantic_tokens(str(seo.get("creative_angle") or "")), semantic_tokens(angle)) >= 0.6
            ),
            observed_performance_score=learning.get("score"),
            performance_adjustment=learning.get("adjustment", 0.0),
            performance_status=learning.get("status"),
        )
        final_score = max(0, int(opportunity["score"]) - duplicate_penalty)
        validation_status = quality["validation"]["status"]
        rejection_reasons = []
        if validation_status == "FAIL":
            rejection_reasons.append("Mevcut SEO kalite doğrulaması FAIL verdi.")
        if exact_prior:
            rejection_reasons.append("Primary keyword veya creative angle mevcut kreatiflerle tekrarlanıyor.")

        record = {
            "candidate_id": candidate_id,
            "candidate_index": candidate_index,
            "generated_at": generated_at,
            "generation_method": "ai_provider_structured_candidates",
            "provider": provider,
            "model": model,
            "prompt_version": item.get("prompt_version"),
            "schema_version": item.get("schema_version"),
            "snapshot": snapshot,
            "evaluation": {
                "ranking_version": SEO_CANDIDATE_RANKING_VERSION,
                "opportunity_score": opportunity["score"],
                "final_score": final_score,
                "score_origin": opportunity["score_origin"],
                "components": opportunity["components"],
                "weights": opportunity["weights"],
                "positive_signals": opportunity["positive_signals"],
                "negative_signals": opportunity["negative_signals"],
                "candidate_similarity_penalty": duplicate_penalty,
                "candidate_similarity": round(peer_similarity, 4),
                "quality": quality,
                "keyword_intelligence": intel,
                "best_board": ({"id": best_board[1].id, "name": best_board[1].name, "score": best_board[0], "breakdown": best_board[2]} if best_board else None),
                "board_matches": [{"id": row[1].id, "name": row[1].name, "score": row[0], "breakdown": row[2]} for row in board_matches],
                "seasonal": seasonal,
                "performance_learning": {
                    "status": learning["status"],
                    "confidence": learning["confidence"],
                    "sample_count": learning["sample_count"],
                    "adjustment": learning["adjustment"],
                    "source_learning_ids": learning["source_learning_ids"],
                    "source_snapshot_ids": learning["source_snapshot_ids"],
                    "used_in_ai_prompt": False,
                },
                "valid": validation_status != "FAIL" and not exact_prior,
                "rejection_reasons": rejection_reasons,
            },
            "selected": False,
            "ranking_position": None,
            "selection_reason": None,
        }
        evaluated.append(record)

    eligible = [row for row in evaluated if row["evaluation"]["valid"]]
    eligible.sort(key=lambda row: (-row["evaluation"]["final_score"], row["candidate_id"]))
    complete_ranking = sorted(
        evaluated,
        key=lambda row: (
            not row["evaluation"]["valid"],
            -row["evaluation"]["final_score"],
            row["candidate_id"],
        ),
    )
    for rank, row in enumerate(complete_ranking, start=1):
        row["ranking_position"] = rank
    if eligible:
        selected = eligible[0]
        selected["selected"] = True
        positives = selected["evaluation"]["positive_signals"]
        selected["selection_reason"] = " ".join(positives[:2]) if positives else "En yüksek deterministik SEO aday skoru ve geçerli kalite doğrulaması nedeniyle seçildi."
    return evaluated
