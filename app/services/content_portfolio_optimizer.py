"""Deterministic greedy selection for a balanced daily Pin candidate portfolio."""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.services.keyword_intelligence import semantic_tokens


PORTFOLIO_VERSION = "smart_content_portfolio_v1"
DEFAULT_DAILY_PORTFOLIO_SIZE = 15
EXPLORATION_SHARE = 0.20
_DIVERSITY_PENALTIES = {
    "keyword": 10.0,
    "cluster": 8.0,
    "creative_type": 7.0,
    "creative_angle": 7.0,
    "board": 5.0,
}


def _jaccard(left: str, right: str) -> float:
    a, b = semantic_tokens(left), semantic_tokens(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def _feature_counts(candidates: list[dict[str, Any]], field: str) -> Counter[str]:
    return Counter(str(item[field]) for item in candidates if item.get(field) not in (None, ""))


def optimize_content_portfolio(
    candidates: list[dict[str, Any]], *, target: int = DEFAULT_DAILY_PORTFOLIO_SIZE,
    recent_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Select candidates greedily while preserving opportunity and priority tiers.

    Each input item is a scheduler-ready creative enriched by the existing
    Opportunity Engine. Missing scores are neutral (50), never treated as failed
    performance. Ties are resolved using stable feature values and candidate ID.
    """
    if target < 0:
        raise ValueError("target cannot be negative")
    history = recent_history or []
    history_counts = {
        field: Counter(str(row[field]) for row in history if row.get(field) not in (None, ""))
        for field in _DIVERSITY_PENALTIES
    }
    remaining = [dict(item) for item in candidates]
    selected: list[dict[str, Any]] = []
    exploration_limit = max(1, int(target * EXPLORATION_SHARE)) if target else 0

    def sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            tuple(item.get("priority_tier", (0, 0))),
            -float(item.get("opportunity_score") if item.get("opportunity_score") is not None else 50),
            str(item.get("keyword") or "").casefold(),
            str(item.get("creative_type") or ""),
            str(item.get("creative_angle") or "").casefold(),
            str(item.get("candidate_id", "")),
        )

    remaining.sort(key=sort_key)
    while remaining and len(selected) < target:
        # Preserve established source/status preference lanes in the scheduler;
        # optimize diversity within the highest-priority lane currently available.
        current_tier = min(tuple(item.get("priority_tier", (0, 0))) for item in remaining)
        tier_candidates = [item for item in remaining if tuple(item.get("priority_tier", (0, 0))) == current_tier]
        ranked: list[tuple[float, tuple[Any, ...], dict[str, Any], dict[str, float], bool]] = []
        for item in tier_candidates:
            opportunity = float(item.get("opportunity_score") if item.get("opportunity_score") is not None else 50)
            evidence = item.get("performance_learning") or {}
            learned_status = evidence.get("status", item.get("performance_status", "unknown"))
            learned_adjustment = float(evidence.get("adjustment", 0) or 0)
            if learned_status not in {"positive", "negative"} or item.get("opportunity_learning_applied") is True:
                learned_adjustment = 0.0

            penalties: dict[str, float] = {}
            saturation = item.get("saturation") or {}
            for field, weight in _DIVERSITY_PENALTIES.items():
                value = item.get(field)
                count = sum(selected_item.get(field) == value for selected_item in selected) if value else 0
                historic_count = int(saturation.get(field, history_counts[field].get(str(value), 0)) or 0)
                penalties[field] = min(24.0, count * weight + min(10.0, historic_count * 1.5))
            comparison_rows = [*selected, *history]
            similarity = max((max(
                _jaccard(str(item.get("keyword") or ""), str(chosen.get("keyword") or "")),
                _jaccard(str(item.get("creative_angle") or ""), str(chosen.get("creative_angle") or "")),
            ) for chosen in comparison_rows), default=0.0)
            penalties["cannibalization"] = round(min(18.0, similarity * 18), 2)
            diversity_contribution = -round(sum(penalties.values()), 2)

            exploration_signals = [
                (float(item[field]), weight)
                for field, weight in (("quality_score", .50), ("board_fit", .30), ("seasonal_score", .20))
                if item.get(field) is not None
            ]
            signal_weight = sum(weight for _, weight in exploration_signals)
            exploration_priority = (
                sum(value * weight for value, weight in exploration_signals) / signal_weight
                if signal_weight else 0.0
            ) - float(saturation.get("total", 0) or 0) * 3
            learning_unknown = learned_status in {"unknown", "insufficient_data", "uncertain"}
            exploration = (
                learning_unknown
                and exploration_priority >= 55
                and len([entry for entry in selected if entry.get("portfolio", {}).get("exploration")]) < exploration_limit
            )
            exploration_bonus = min(5.0, max(0.0, (exploration_priority - 50) * .10)) if exploration else 0.0
            final_score = max(0.0, min(100.0, opportunity + learned_adjustment + diversity_contribution + exploration_bonus))
            tie = sort_key(item)
            ranked.append((final_score, tie, item, penalties, exploration))

        ranked.sort(key=lambda row: (-row[0], row[1]))
        final_score, _, chosen, penalties, exploration = ranked[0]
        chosen_evidence = chosen.get("performance_learning") or {}
        portfolio_learning_contribution = float(chosen_evidence.get("adjustment", 0) or 0)
        if (chosen_evidence.get("status") not in {"positive", "negative"}
                or chosen.get("opportunity_learning_applied") is True):
            portfolio_learning_contribution = 0.0
        chosen_saturation = chosen.get("saturation") or {}
        chosen_exploration_signals = [
            (float(chosen[field]), weight)
            for field, weight in (("quality_score", .50), ("board_fit", .30), ("seasonal_score", .20))
            if chosen.get(field) is not None
        ]
        chosen_signal_weight = sum(weight for _, weight in chosen_exploration_signals)
        chosen_exploration_priority = (
            sum(value * weight for value, weight in chosen_exploration_signals) / chosen_signal_weight
            if chosen_signal_weight else 0.0
        ) - float(chosen_saturation.get("total", 0) or 0) * 3
        exploration_contribution = (
            min(5.0, max(0.0, (chosen_exploration_priority - 50) * .10)) if exploration else 0.0
        )
        chosen["portfolio"] = {
            "version": PORTFOLIO_VERSION,
            "final_score": round(final_score, 2),
            "opportunity_score": chosen.get("opportunity_score"),
            "diversity_contribution": round(-sum(penalties.values()), 2),
            "learning_contribution": round(portfolio_learning_contribution, 2),
            "penalties": penalties,
            "exploration": exploration,
            "learned": (chosen.get("performance_learning") or {}).get("status") in {"positive", "negative"},
            "seasonal": chosen.get("seasonal_score") is not None and chosen.get("seasonal_score", 0) > 0,
            "selection_reason": _selection_reason(chosen, penalties, exploration),
            "score_lineage": {
                "opportunity_score": chosen.get("opportunity_score"),
                "opportunity_components": chosen.get("opportunity_components", {}),
                "opportunity_learning_applied": chosen.get("opportunity_learning_applied", False),
                "portfolio_learning_contribution": round(portfolio_learning_contribution, 2),
                "diversity_contribution": round(-sum(penalties.values()), 2),
                "exploration_contribution": round(exploration_contribution, 2),
                "final_score": round(final_score, 2),
            },
        }
        selected.append(chosen)
        remaining.remove(chosen)

    summary = _portfolio_summary(selected, len(candidates))
    return {"selected": selected, "summary": summary, "version": PORTFOLIO_VERSION}


def _selection_reason(item: dict[str, Any], penalties: dict[str, float], exploration: bool) -> str:
    reasons = [f"Opportunity puanı {item.get('opportunity_score') if item.get('opportunity_score') is not None else 'bilinmiyor'}"]
    status = (item.get("performance_learning") or {}).get("status", "unknown")
    if status == "positive":
        reasons.append("hesaba özel olumlu öğrenme")
    elif status == "negative":
        reasons.append("geçmiş negatif sinyale rağmen farklılık/kalite dengesi")
    if item.get("quality_score") is not None and item["quality_score"] >= 75:
        reasons.append("SEO kalite sinyali güçlü")
    if item.get("board_fit") is not None and item["board_fit"] >= 70:
        reasons.append("pano uyumu güçlü")
    if exploration:
        reasons.append("kontrollü keşif seçimi")
    if item.get("seasonal_score"):
        reasons.append("mevsimsel fırsat")
    if sum(penalties.values()):
        reasons.append("tekrar ve doygunluk cezaları uygulandı")
    return "; ".join(reasons)


def _portfolio_summary(selected: list[dict[str, Any]], total_candidates: int) -> dict[str, Any]:
    keyword_counts = _feature_counts(selected, "keyword")
    type_distribution = dict(sorted(_feature_counts(selected, "creative_type").items()))
    angle_distribution = dict(sorted(_feature_counts(selected, "creative_angle").items()))
    board_distribution = dict(sorted(_feature_counts(selected, "board").items()))
    dominant = {
        "keyword": max(keyword_counts.items(), key=lambda item: (-item[1], item[0]))[0] if keyword_counts else None,
        "creative_type": max(type_distribution.items(), key=lambda item: (-item[1], item[0]))[0] if type_distribution else None,
        "creative_angle": max(angle_distribution.items(), key=lambda item: (-item[1], item[0]))[0] if angle_distribution else None,
    }
    return {
        "selected_count": len(selected),
        "total_candidates": total_candidates,
        "keyword_diversity": len(keyword_counts),
        "keyword_distribution": dict(sorted(keyword_counts.items())),
        "creative_type_distribution": type_distribution,
        "creative_angle_distribution": angle_distribution,
        "board_distribution": board_distribution,
        "exploration_count": sum(bool(item.get("portfolio", {}).get("exploration")) for item in selected),
        "learned_count": sum(bool(item.get("portfolio", {}).get("learned")) for item in selected),
        "learned_positive_count": sum((item.get("performance_learning") or {}).get("status") == "positive" for item in selected),
        "seasonal_count": sum(bool(item.get("portfolio", {}).get("seasonal")) for item in selected),
        "dominant": dominant,
    }
