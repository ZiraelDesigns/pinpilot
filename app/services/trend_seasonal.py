"""Deterministic calendar context and a provider-neutral external-trend boundary.

Calendar-derived seasons and recurring dates are kept separate from external
trend observations. The default provider deliberately collects no Pinterest
trend data and this module never performs network requests itself.
"""

from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import SEOGeneration, SEOKeywordIntelligence, SEOQualityAssessment
from app.models import SEOTrendSeasonalAssessment
from app.services.keyword_intelligence import normalize_keyword, semantic_tokens


TREND_SEASONAL_VERSION = "trend_seasonal_v1"
SEASONAL_CALENDAR_VERSION = "pinpilot_calendar_v1"
WINDOW_STRATEGY_VERSION = "calendar_window_strategy_v1"
CALENDAR_SOURCE = "calendar"
CALENDAR_SOURCE_TYPE = "calendar_derived"
STRATEGY_SOURCE = "strategy_configured"
EXTERNAL_SOURCE_TYPE = "external_provider"
SUPPORTED_PROVIDER_STATUSES = {"collected", "unavailable", "not_collected", "stale"}

# Windows are strategy-configured defaults, not Pinterest ranking or platform rules.
DEFAULT_EVENT_WINDOWS = {
    "preparation_days": 30,
    "active_days": 1,
    "cooldown_days": 7,
}

_NORTHERN_HEMISPHERE = {"US", "TR", "CA", "GB", "DE", "FR"}
_SEASON_TERMS = {
    "spring": ("spring", "spring decor", "spring outfit", "spring gift", "spring flowers", "spring cleaning"),
    "summer": ("summer", "summer decor", "summer outfit", "summer gift", "summer beach", "beach vacation"),
    "fall": ("fall", "autumn", "fall decor", "autumn decor", "fall outfit", "autumn outfit"),
    "winter": ("winter", "winter decor", "winter outfit", "winter gift", "cold weather", "winter holiday"),
}


@dataclass(frozen=True)
class TrendObservation:
    """A provider-reported observation; it must never be inferred from calendar text."""

    term: str
    value: int | float | None = None
    status: str = "collected"
    source: str | None = None
    source_type: str = EXTERNAL_SOURCE_TYPE
    collected_at: datetime | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class TrendProviderResult:
    status: str
    source: str | None = None
    source_type: str = EXTERNAL_SOURCE_TYPE
    collected_at: datetime | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    confidence: float | None = None
    signals: tuple[TrendObservation, ...] = field(default_factory=tuple)


class TrendProvider(Protocol):
    def collect_trends(
        self, keywords: list[str], region_code: str, reference_date: date
    ) -> TrendProviderResult: ...


class NotCollectedTrendProvider:
    """Safe default. No live Pinterest or other external trend request is made."""

    def collect_trends(
        self, keywords: list[str], region_code: str, reference_date: date
    ) -> TrendProviderResult:
        return TrendProviderResult(status="not_collected")


@dataclass(frozen=True)
class CalendarEvent:
    key: str
    name: str
    regions: frozenset[str]
    date_rule: str
    keywords: tuple[str, ...]
    expected_intents: frozenset[str] = frozenset()
    audience_terms: frozenset[str] = frozenset()
    use_case_terms: frozenset[str] = frozenset()
    preparation_days: int = DEFAULT_EVENT_WINDOWS["preparation_days"]
    active_days: int = DEFAULT_EVENT_WINDOWS["active_days"]
    cooldown_days: int = DEFAULT_EVENT_WINDOWS["cooldown_days"]
    window_source: str = STRATEGY_SOURCE


def _fixed(month: int, day: int):
    return lambda year: date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    first_weekday, days_in_month = calendar.monthrange(year, month)
    day = 1 + (weekday - first_weekday) % 7 + (nth - 1) * 7
    if day > days_in_month:
        raise ValueError("Requested weekday occurrence is outside the month")
    return date(year, month, day)


def _easter_sunday(year: int) -> date:
    """Gregorian Easter computus; calendar-derived date, not trend data."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _black_friday(year: int) -> date:
    thanksgiving = _nth_weekday(year, 11, 3, 4)  # fourth Thursday in November
    return thanksgiving + timedelta(days=1)


def _cyber_monday(year: int) -> date:
    return _black_friday(year) + timedelta(days=3)


_HOLIDAYS = (
    CalendarEvent("new_year", "New Year's Day", frozenset({"US", "TR"}), "jan_1", ("new year", "new year's day", "new years day", "new years eve", "new year decor", "new year gift"), frozenset({"gift_intent", "aesthetic_style_intent"}), frozenset({"shoppers", "families"}), frozenset({"celebration", "decor", "gift"}), 30, 2, 7),
    CalendarEvent("valentines_day", "Valentine's Day", frozenset({"US", "TR"}), "feb_14", ("valentine's day", "valentines day", "valentine gift", "valentines gift", "valentine gifts", "gift for valentine", "romantic gift"), frozenset({"gift_intent", "aesthetic_style_intent"}), frozenset({"couples", "shoppers"}), frozenset({"gift", "celebration"}), 30, 1, 7),
    CalendarEvent("easter", "Easter", frozenset({"US"}), "gregorian_easter", ("easter", "easter basket", "easter decor", "easter gift", "easter egg", "bunny decor"), frozenset({"gift_intent", "aesthetic_style_intent"}), frozenset({"families", "children"}), frozenset({"celebration", "decor", "gift"}), 30, 1, 7),
    CalendarEvent("mothers_day", "Mother's Day", frozenset({"US", "TR"}), "second_sunday_may", ("mother's day", "mothers day", "mom gift", "mom gifts", "gift for mom", "mother gift", "gifts for mother"), frozenset({"gift_intent", "product_search"}), frozenset({"mothers", "shoppers"}), frozenset({"gift", "celebration"}), 30, 1, 7),
    CalendarEvent("fathers_day", "Father's Day", frozenset({"US", "TR"}), "third_sunday_june", ("father's day", "fathers day", "dad gift", "dad gifts", "gift for dad", "father gift", "gifts for father"), frozenset({"gift_intent", "product_search"}), frozenset({"fathers", "shoppers"}), frozenset({"gift", "celebration"}), 30, 1, 7),
    CalendarEvent("back_to_school_us", "Back to School", frozenset({"US"}), "us_strategy_aug_15", ("back to school", "school supplies", "teacher gift", "classroom decor", "student planner", "school planner"), frozenset({"product_search", "gift_intent"}), frozenset({"teachers", "students", "parents"}), frozenset({"school", "classroom", "gift"}), 21, 30, 14),
    CalendarEvent("back_to_school_tr", "Back to School", frozenset({"TR"}), "tr_strategy_sep_1", ("back to school", "school supplies", "teacher gift", "classroom decor", "student planner", "school planner"), frozenset({"product_search", "gift_intent"}), frozenset({"teachers", "students", "parents"}), frozenset({"school", "classroom", "gift"}), 21, 30, 14),
    CalendarEvent("halloween", "Halloween", frozenset({"US"}), "oct_31", ("halloween", "halloween decor", "halloween costume", "halloween party", "spooky decor", "trick or treat"), frozenset({"aesthetic_style_intent", "gift_intent"}), frozenset({"families", "children"}), frozenset({"decor", "party", "costume"}), 30, 1, 7),
    CalendarEvent("republic_day_tr", "Republic Day", frozenset({"TR"}), "oct_29", ("republic day turkey", "turkish republic day", "cumhuriyet bayramı"), frozenset({"aesthetic_style_intent"}), frozenset(), frozenset({"celebration", "decor"}), 14, 1, 3),
    CalendarEvent("thanksgiving", "Thanksgiving", frozenset({"US"}), "fourth_thursday_november", ("thanksgiving", "thanksgiving decor", "thanksgiving table", "thanksgiving gift", "thanksgiving dinner"), frozenset({"gift_intent", "aesthetic_style_intent"}), frozenset({"families", "hosts"}), frozenset({"dinner", "decor", "gift"}), 30, 1, 7),
    CalendarEvent("black_friday", "Black Friday", frozenset({"US"}), "day_after_us_thanksgiving", ("black friday", "black friday deals", "black friday gift"), frozenset({"product_search", "gift_intent"}), frozenset({"shoppers"}), frozenset({"shopping", "gift"}), 14, 1, 3),
    CalendarEvent("cyber_monday", "Cyber Monday", frozenset({"US"}), "monday_after_black_friday", ("cyber monday", "cyber monday deals", "cyber monday gift"), frozenset({"product_search", "gift_intent"}), frozenset({"shoppers"}), frozenset({"shopping", "gift"}), 14, 1, 3),
    CalendarEvent("christmas", "Christmas", frozenset({"US"}), "dec_25", ("christmas", "christmas gift", "christmas gifts", "christmas decor", "christmas ornament", "holiday gift"), frozenset({"gift_intent", "aesthetic_style_intent"}), frozenset({"families", "shoppers"}), frozenset({"gift", "decor", "celebration"}), 45, 3, 10),
)


def _event_date(event: CalendarEvent, year: int) -> date:
    rule = event.date_rule
    if rule == "jan_1":
        return _fixed(1, 1)(year)
    if rule == "feb_14":
        return _fixed(2, 14)(year)
    if rule == "gregorian_easter":
        return _easter_sunday(year)
    if rule == "second_sunday_may":
        return _nth_weekday(year, 5, 6, 2)
    if rule == "third_sunday_june":
        return _nth_weekday(year, 6, 6, 3)
    if rule == "us_strategy_aug_15":
        return _fixed(8, 15)(year)
    if rule == "tr_strategy_sep_1":
        return _fixed(9, 1)(year)
    if rule == "oct_31":
        return _fixed(10, 31)(year)
    if rule == "oct_29":
        return _fixed(10, 29)(year)
    if rule == "fourth_thursday_november":
        return _nth_weekday(year, 11, 3, 4)
    if rule == "day_after_us_thanksgiving":
        return _black_friday(year)
    if rule == "monday_after_black_friday":
        return _cyber_monday(year)
    if rule == "dec_25":
        return _fixed(12, 25)(year)
    raise ValueError(f"Unsupported calendar rule: {rule}")


def _normalize_region(region_code: str | None) -> str:
    if region_code is None or not region_code.strip():
        return "GLOBAL"
    region = region_code.strip().upper()
    if region != "GLOBAL" and not re.fullmatch(r"[A-Z]{2}", region):
        raise ValueError("region_code must be an ISO-style two-letter region or GLOBAL")
    return region


def _season_for(reference_date: date, region_code: str) -> dict[str, Any]:
    if region_code not in _NORTHERN_HEMISPHERE:
        return {
            "status": "unavailable", "name": None,
            "reason": "hemisphere_or_region_not_specified",
            "source": CALENDAR_SOURCE, "source_type": CALENDAR_SOURCE_TYPE,
        }
    month = reference_date.month
    if month in {3, 4, 5}:
        name, start, end = "spring", date(reference_date.year, 3, 1), date(reference_date.year, 5, 31)
    elif month in {6, 7, 8}:
        name, start, end = "summer", date(reference_date.year, 6, 1), date(reference_date.year, 8, 31)
    elif month in {9, 10, 11}:
        name, start, end = "fall", date(reference_date.year, 9, 1), date(reference_date.year, 11, 30)
    elif month == 12:
        next_year = reference_date.year + 1
        name, start, end = "winter", date(reference_date.year, 12, 1), date(next_year, 2, 29 if calendar.isleap(next_year) else 28)
    else:
        name, start, end = "winter", date(reference_date.year - 1, 12, 1), date(reference_date.year, 2, 29 if calendar.isleap(reference_date.year) else 28)
    return {
        "status": "computed", "name": name, "start_date": start.isoformat(), "end_date": end.isoformat(),
        "region_code": region_code, "source": CALENDAR_SOURCE, "source_type": CALENDAR_SOURCE_TYPE,
        "interpretation": "calendar_derived_meteorological_period_not_marketing_claim",
    }


def _token_set(value: str) -> set[str]:
    return set(semantic_tokens(value))


def _term_match(keyword: str, candidate: str) -> tuple[int, str] | None:
    normalized_keyword = normalize_keyword(keyword)
    normalized_candidate = normalize_keyword(candidate)
    if not normalized_keyword or not normalized_candidate:
        return None
    if normalized_keyword == normalized_candidate:
        return 100, "exact_normalized_match"
    keyword_tokens = _token_set(normalized_keyword)
    candidate_tokens = _token_set(normalized_candidate)
    if keyword_tokens and candidate_tokens and (
        candidate_tokens <= keyword_tokens or keyword_tokens <= candidate_tokens
    ):
        # Phrase inclusion uses controlled calendar vocabulary, not broad stemming.
        return 85, "calendar_phrase_token_match"
    if keyword_tokens and candidate_tokens:
        overlap = len(keyword_tokens & candidate_tokens) / len(keyword_tokens | candidate_tokens)
        if overlap >= 0.6:
            return 70, "shared_semantic_tokens"
    return None


def _month_quarter(reference_date: date) -> dict[str, int]:
    return {"month": reference_date.month, "quarter": (reference_date.month - 1) // 3 + 1}


def _event_records(region_code: str, reference_date: date) -> list[dict[str, Any]]:
    if region_code == "GLOBAL":
        return []
    records = []
    for event in _HOLIDAYS:
        if region_code not in event.regions:
            continue
        for year in (reference_date.year - 1, reference_date.year, reference_date.year + 1):
            event_date = _event_date(event, year)
            day_offset = (reference_date - event_date).days
            if -event.preparation_days <= day_offset < event.active_days + event.cooldown_days:
                if day_offset < 0:
                    phase = "preparation"
                    window_start = event_date - timedelta(days=event.preparation_days)
                    window_end = event_date - timedelta(days=1)
                    timing = round(60 + 40 * (event.preparation_days + day_offset) / max(1, event.preparation_days - 1))
                elif day_offset < event.active_days:
                    phase = "active"
                    window_start = event_date
                    window_end = event_date + timedelta(days=event.active_days - 1)
                    timing = 100
                else:
                    phase = "cooldown"
                    window_start = event_date + timedelta(days=event.active_days)
                    window_end = event_date + timedelta(days=event.active_days + event.cooldown_days - 1)
                    timing = round(75 - 50 * (day_offset - event.active_days + 1) / max(1, event.cooldown_days))
                records.append({
                    "event_key": event.key,
                    "name": event.name,
                    "region_code": region_code,
                    "event_date": event_date.isoformat(),
                    "rule": event.date_rule,
                    "window_phase": phase,
                    "days_from_event": day_offset,
                    "relevance_window": {
                        "start": window_start.isoformat(), "end": window_end.isoformat(),
                        "preparation_days": event.preparation_days,
                        "active_days": event.active_days,
                        "cooldown_days": event.cooldown_days,
                        "source": event.window_source,
                        "strategy_version": WINDOW_STRATEGY_VERSION,
                    },
                    "timing_score": max(0, min(100, timing)),
                    "keywords": list(event.keywords),
                    "expected_intents": sorted(event.expected_intents),
                    "audience_terms": sorted(event.audience_terms),
                    "use_case_terms": sorted(event.use_case_terms),
                    "source": CALENDAR_SOURCE,
                    "source_type": CALENDAR_SOURCE_TYPE,
                    "calendar_version": SEASONAL_CALENDAR_VERSION,
                })
    return sorted(records, key=lambda row: (row["event_date"], row["event_key"]))


def _valid_keyword_items(intelligence: SEOKeywordIntelligence) -> list[dict[str, Any]]:
    return [item for item in (intelligence.keyword_items or [])
            if isinstance(item, dict) and item.get("valid") and item.get("normalized")]


def _match_keywords(items: list[dict[str, Any]], phrases: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    matches = []
    for index, item in enumerate(items):
        best: tuple[int, str, str] | None = None
        for phrase in phrases:
            match = _term_match(str(item["normalized"]), phrase)
            if match and (best is None or match[0] > best[0] or (match[0] == best[0] and phrase < best[2])):
                best = (match[0], match[1], phrase)
        if best:
            matches.append({
                "keyword_index": index,
                "raw_keyword": item.get("raw"),
                "normalized_keyword": item.get("normalized"),
                "keyword_type": item.get("keyword_type"),
                "semantic_group": item.get("semantic_group"),
                "search_intent": item.get("search_intent", "unknown"),
                "existing_keyword_relevance": (item.get("relevance") or {}).get("score"),
                "matched_calendar_term": best[2],
                "match_score": best[0],
                "match_method": best[1],
                "source": "persisted_seo_keyword_intelligence",
            })
    return matches


def _context_compatibility(
    seo: dict[str, Any], matched_keywords: list[dict[str, Any]], event: dict[str, Any]
) -> dict[str, Any]:
    declared_intents = {item.get("search_intent") for item in matched_keywords
                        if item.get("search_intent") not in (None, "unknown")}
    expected_intents = set(event.get("expected_intents") or [])
    intent_score = (100 if declared_intents & expected_intents else 0) if declared_intents and expected_intents else None
    audience_terms = {token for value in (seo.get("audience_keywords") or []) if isinstance(value, str)
                      for token in _token_set(value)}
    event_audience = {token for value in event.get("audience_terms", []) for token in _token_set(value)}
    audience_score = (100 if audience_terms & event_audience else 0) if audience_terms and event_audience else None
    use_case_terms = {token for value in (seo.get("use_case_keywords") or []) if isinstance(value, str)
                      for token in _token_set(value)}
    event_use_cases = {token for value in event.get("use_case_terms", []) for token in _token_set(value)}
    use_case_score = (100 if use_case_terms & event_use_cases else 0) if use_case_terms and event_use_cases else None
    available = [value for value in (intent_score, audience_score, use_case_score) if value is not None]
    return {
        "intent_compatibility": intent_score,
        "audience_compatibility": audience_score,
        "use_case_compatibility": use_case_score,
        "score": round(sum(available) / len(available)) if available else None,
        "source": "existing_seo_metadata_and_keyword_intelligence",
    }


def _provider_snapshot(provider: TrendProvider, keyword_terms: list[str], region_code: str, reference_date: date) -> dict[str, Any]:
    try:
        result = provider.collect_trends(keyword_terms, region_code, reference_date)
    except Exception:
        return {
            "status": "unavailable", "source": None, "source_type": EXTERNAL_SOURCE_TYPE,
            "collected_at": None, "valid_from": None, "valid_until": None,
            "confidence": None, "signals": [],
            "origin": "external_provider_error_redacted",
        }
    if result.status not in SUPPORTED_PROVIDER_STATUSES:
        return {
            "status": "unavailable", "source": None, "source_type": EXTERNAL_SOURCE_TYPE,
            "collected_at": None, "valid_from": None, "valid_until": None,
            "confidence": None, "signals": [], "origin": "invalid_provider_status",
        }
    status = result.status
    if status == "collected" and (_safe_provider_source(result.source) is None or result.collected_at is None):
        status = "unavailable"
    elif status == "stale" and (_safe_provider_source(result.source) is None or result.collected_at is None):
        status = "unavailable"
    elif result.valid_until and result.valid_until < reference_date and status == "collected":
        status = "stale"
    signals = []
    if status in {"collected", "stale"}:
        requested_terms = {normalize_keyword(term) for term in keyword_terms}
        for signal in result.signals:
            if signal.status not in SUPPORTED_PROVIDER_STATUSES:
                continue
            normalized_term = normalize_keyword(signal.term)
            if not normalized_term or normalized_term not in requested_terms:
                continue
            signal_status = signal.status
            if status == "stale" and signal_status == "collected":
                signal_status = "stale"
            if signal_status == "collected" and (
                _safe_provider_source(signal.source) is None or signal.collected_at is None
            ):
                signal_status = "unavailable"
            elif signal_status == "stale" and (
                _safe_provider_source(signal.source) is None or signal.collected_at is None
            ):
                signal_status = "unavailable"
            elif signal.valid_until and signal.valid_until < reference_date and signal_status == "collected":
                signal_status = "stale"
            if isinstance(signal.value, float) and not math.isfinite(signal.value):
                signal_status = "unavailable"
            # Only declared data fields are serialized; provider credentials cannot enter this snapshot.
            signals.append({
                "term": normalized_term,
                "value": signal.value if signal_status in {"collected", "stale"} else None,
                "status": signal_status,
                "source": _safe_provider_source(signal.source) if signal_status in {"collected", "stale"} else None,
                "source_type": EXTERNAL_SOURCE_TYPE,
                "collected_at": signal.collected_at.astimezone(timezone.utc).isoformat() if signal.collected_at and signal.collected_at.tzinfo else (signal.collected_at.isoformat() if signal.collected_at else None),
                "valid_from": signal.valid_from.isoformat() if signal.valid_from else None,
                "valid_until": signal.valid_until.isoformat() if signal.valid_until else None,
                "confidence": _safe_confidence(signal.confidence),
            })
    return {
        "status": status,
        "source": _safe_provider_source(result.source) if status in {"collected", "stale"} else None,
        "source_type": EXTERNAL_SOURCE_TYPE,
        "collected_at": result.collected_at.astimezone(timezone.utc).isoformat() if result.collected_at and result.collected_at.tzinfo else (result.collected_at.isoformat() if result.collected_at else None),
        "valid_from": result.valid_from.isoformat() if result.valid_from else None,
        "valid_until": result.valid_until.isoformat() if result.valid_until else None,
        "confidence": _safe_confidence(result.confidence),
        "signals": signals,
        "origin": "provider_reported" if status in {"collected", "stale"} else "not_verified",
    }


def _safe_provider_source(value: str | None) -> str | None:
    """Persist only short source identifiers; never arbitrary provider/error text."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", value):
        return None
    lowered = value.casefold()
    if any(marker in lowered for marker in ("bearer", "token", "secret", "api_key", "credential", "authorization")):
        return None
    return value


def _safe_confidence(value: float | None) -> float | None:
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if value < 0 or value > 1:
        return None
    return float(value)


def analyze_trend_seasonal_context(
    generation: SEOGeneration,
    intelligence: SEOKeywordIntelligence,
    *,
    reference_date: date,
    region_code: str | None = None,
    trend_provider: TrendProvider | None = None,
) -> dict[str, Any]:
    """Build versioned calendar context using existing keyword intelligence only."""
    region = _normalize_region(region_code)
    snapshot = generation.output_snapshot if isinstance(generation.output_snapshot, dict) else {}
    seo = snapshot.get("seo_metadata") if isinstance(snapshot.get("seo_metadata"), dict) else {}
    items = _valid_keyword_items(intelligence)
    season = _season_for(reference_date, region)
    season_matches = _match_keywords(items, _SEASON_TERMS.get(season.get("name"), ()))
    events = _event_records(region, reference_date)
    event_results = []
    for event in events:
        matches = _match_keywords(items, event["keywords"])
        compatibility = _context_compatibility(seo, matches, event)
        event_results.append({
            **event,
            "keyword_matches": matches,
            "keyword_relevance_score": max((item["match_score"] for item in matches), default=0),
            "metadata_compatibility": compatibility,
            "relevance_status": "matched" if matches else "no_keyword_match",
        })

    matched_events = [event for event in event_results if event["keyword_matches"]]
    active_season_terms = bool(season_matches)
    holiday_relevance = max((event["keyword_relevance_score"] for event in matched_events), default=0)
    timing_relevance = max((event["timing_score"] for event in matched_events), default=None)
    context_scores = [event["metadata_compatibility"]["score"] for event in matched_events
                      if event["metadata_compatibility"]["score"] is not None]
    seasonal_relevance = max((item["match_score"] for item in season_matches), default=0)
    components = {
        "seasonal_relevance": {"score": seasonal_relevance, "available": season.get("status") == "computed", "weight": 0.30, "source": "calendar_and_existing_keyword_intelligence"},
        "holiday_relevance": {"score": holiday_relevance if events else None, "available": bool(events), "weight": 0.40, "source": "calendar_and_existing_keyword_intelligence"},
        "timing_relevance": {"score": timing_relevance, "available": timing_relevance is not None, "weight": 0.20, "source": "strategy_configured_relevance_window"},
        "metadata_compatibility": {"score": max(context_scores) if context_scores else None, "available": bool(context_scores), "weight": 0.10, "source": "existing_seo_metadata"},
    }
    available_weight = sum(item["weight"] for item in components.values() if item["available"])
    total_score = round(sum(item["score"] * item["weight"] for item in components.values()
                            if item["available"] and item["score"] is not None) / available_weight) if available_weight else 0
    # Provider execution goes through the redacting/validating boundary and is skipped if no query terms exist.
    trend_snapshot = _provider_snapshot(
        trend_provider or NotCollectedTrendProvider(),
        sorted({item["normalized"] for item in items if item.get("normalized")}),
        region,
        reference_date,
    ) if items else {
        "status": "not_collected", "source": None, "source_type": EXTERNAL_SOURCE_TYPE,
        "collected_at": None, "valid_from": None, "valid_until": None,
        "confidence": None, "signals": [], "origin": "no_keyword_terms",
    }
    recommendations = []
    if active_season_terms:
        recommendations.append({
            "type": "seasonal_relevance_detected",
            "message": f"Seasonal relevance detected for {season['name']}.",
            "source": CALENDAR_SOURCE,
            "source_type": CALENDAR_SOURCE_TYPE,
            "matched_keywords": [item["normalized_keyword"] for item in season_matches],
        })
    for event in matched_events:
        message = {
            "preparation": "Holiday window approaching",
            "active": "Holiday event window is active",
            "cooldown": "Holiday post-event window is active",
        }[event["window_phase"]]
        recommendations.append({
            "type": "holiday_window_" + event["window_phase"],
            "message": f"{message}: {event['name']}.",
            "event_key": event["event_key"],
            "event_date": event["event_date"],
            "source": event["source"],
            "source_type": event["source_type"],
            "window_source": event["relevance_window"]["source"],
            "matched_keywords": [item["normalized_keyword"] for item in event["keyword_matches"]],
            "metadata_compatibility": event["metadata_compatibility"],
        })
    if trend_snapshot["status"] in {"not_collected", "unavailable", "stale"}:
        recommendations.append({
            "type": "external_trend_data_" + trend_snapshot["status"],
            "message": "No verified external trend data is available for this assessment.",
            "source": trend_snapshot.get("source"),
            "source_type": EXTERNAL_SOURCE_TYPE,
        })
    warnings = []
    if region == "GLOBAL":
        warnings.append("region_not_specified_country_specific_calendar_not_applied")
    if season.get("status") == "unavailable":
        warnings.append("season_unavailable_without_region_hemisphere")
    if not items:
        warnings.append("keyword_intelligence_contains_no_valid_keywords")
    if trend_snapshot["status"] != "collected":
        warnings.append("verified_external_trend_data_" + trend_snapshot["status"])
    return {
        "algorithm_version": TREND_SEASONAL_VERSION,
        "calendar_version": SEASONAL_CALENDAR_VERSION,
        "window_strategy_version": WINDOW_STRATEGY_VERSION,
        "reference_date": reference_date.isoformat(),
        "region_code": region,
        "source": CALENDAR_SOURCE,
        "source_type": CALENDAR_SOURCE_TYPE,
        "calculation_status": "computed",
        "calculation_type": "deterministic_computed",
        "month_quarter": _month_quarter(reference_date),
        "season": season,
        "season_keyword_matches": season_matches,
        "events": event_results,
        "score": {
            "value": max(0, min(100, total_score)),
            "origin": "computed_calendar_context_not_pinterest_trend_or_ranking_score",
            "components": components,
        },
        "external_trends": trend_snapshot,
        "recommendations": recommendations,
        "warnings": warnings,
    }


def _generation_reference_date(generation: SEOGeneration) -> date:
    value = generation.completed_at
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.date()


def ensure_trend_seasonal_assessment(
    db: Session,
    generation: SEOGeneration,
    *,
    region_code: str | None = None,
    trend_provider: TrendProvider | None = None,
) -> SEOTrendSeasonalAssessment | None:
    """Persist one immutable assessment per generation/date/region/version."""
    if generation.status != "completed" or not isinstance(generation.output_snapshot, dict):
        return None
    intelligence = db.scalar(select(SEOKeywordIntelligence).where(
        SEOKeywordIntelligence.seo_generation_id == generation.id
    ))
    quality = db.scalar(select(SEOQualityAssessment).where(
        SEOQualityAssessment.seo_generation_id == generation.id
    ))
    # Do not backfill or manufacture missing upstream provenance for legacy rows.
    if intelligence is None or quality is None:
        return None
    region = _normalize_region(region_code if region_code is not None else settings.seo_calendar_region)
    reference_date = _generation_reference_date(generation)
    existing = db.scalar(select(SEOTrendSeasonalAssessment).where(
        SEOTrendSeasonalAssessment.seo_generation_id == generation.id,
        SEOTrendSeasonalAssessment.reference_date == reference_date,
        SEOTrendSeasonalAssessment.region_code == region,
        SEOTrendSeasonalAssessment.algorithm_version == TREND_SEASONAL_VERSION,
        SEOTrendSeasonalAssessment.calendar_version == SEASONAL_CALENDAR_VERSION,
    ))
    if existing is not None:
        return existing
    result = analyze_trend_seasonal_context(
        generation,
        intelligence,
        reference_date=reference_date,
        region_code=region,
        trend_provider=trend_provider,
    )
    assessment = SEOTrendSeasonalAssessment(
        seo_generation_id=generation.id,
        reference_date=reference_date,
        region_code=region,
        assessed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        algorithm_version=TREND_SEASONAL_VERSION,
        calendar_version=SEASONAL_CALENDAR_VERSION,
        source=CALENDAR_SOURCE,
        source_type=CALENDAR_SOURCE_TYPE,
        calculation_status="computed",
        seasonal_score=result["score"]["value"],
        score_breakdown=result["score"],
        calendar_snapshot={key: result[key] for key in (
            "reference_date", "region_code", "month_quarter", "season", "events",
            "window_strategy_version",
        )},
        keyword_matches={
            "season": result["season_keyword_matches"],
            "events": [{
                "event_key": event["event_key"],
                "keyword_matches": event["keyword_matches"],
                "metadata_compatibility": event["metadata_compatibility"],
            } for event in result["events"] if event["keyword_matches"]],
        },
        recommendations=result["recommendations"],
        warnings=result["warnings"],
        external_trend_snapshot=result["external_trends"],
    )
    db.add(assessment)
    db.flush()
    return assessment
