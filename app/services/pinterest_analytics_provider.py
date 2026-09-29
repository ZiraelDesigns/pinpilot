"""Pinterest v5 API adapter for the existing normalized analytics collector.

This module is intentionally not wired into a scheduler or application startup.
It makes API-backed collection explicitly constructible while leaving the
existing provider-neutral collector and its database model as the only storage
path.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PinterestAccount
from app.services.pinterest import PinterestApiService
from app.services.pinterest_analytics import (
    AccountAnalyticsDTO,
    AnalyticsAuthenticationError,
    AnalyticsConfigurationError,
    AnalyticsNormalizationError,
    AnalyticsRateLimitError,
    PinAnalyticsDTO,
    TemporaryAnalyticsProviderError,
)
from app.services.pinterest_api import (
    PinterestApiConfigurationError,
    PinterestApiError,
    PinterestAuthenticationError,
    PinterestInvalidPayload,
    PinterestInvalidResponse,
    PinterestIntegrationError,
    PinterestPermissionError,
    PinterestRateLimited,
    PinterestTemporaryError,
)


_PIN_METRIC_FIELDS = {
    "IMPRESSION": "impressions",
    "SAVE": "saves",
    "PIN_CLICK": "pin_clicks",
    "OUTBOUND_CLICK": "outbound_clicks",
    "ENGAGEMENT": "engagements",
    "ENGAGEMENT_RATE": "engagement_rate",
    "PIN_CLICK_RATE": "pin_click_rate",
    "OUTBOUND_CLICK_RATE": "outbound_click_rate",
}
_ACCOUNT_METRIC_FIELDS = {
    "PROFILE_VISIT": "profile_visits",
    "FOLLOW": "follows",
    "TOTAL_AUDIENCE": "total_audience",
    "ENGAGED_AUDIENCE": "engaged_audience",
}


def _as_utc_date(value: datetime) -> date:
    if not isinstance(value, datetime):
        raise AnalyticsNormalizationError("analytics period values must be datetimes")
    if value.tzinfo is not None:
        return value.astimezone(UTC).date()
    # Existing collector periods are UTC-naive database timestamps.
    return value.date()


def _metric_value(value: object, field: str) -> int | Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise AnalyticsNormalizationError(f"Pinterest metric {field} has an invalid numeric value")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise AnalyticsNormalizationError(f"Pinterest metric {field} has an invalid numeric value") from None
    if not number.is_finite() or number < 0:
        raise AnalyticsNormalizationError(f"Pinterest metric {field} has an invalid numeric value")
    if field.endswith("_rate"):
        return number
    if number != number.to_integral_value():
        raise AnalyticsNormalizationError(f"Pinterest count metric {field} is not an integer")
    return int(number)


def _summary_metrics(bundle: object) -> dict[str, object]:
    if not isinstance(bundle, dict):
        raise AnalyticsNormalizationError("Pinterest analytics item has an invalid response shape")
    summary = bundle.get("summary_metrics")
    if summary is None:
        # Pinterest may omit empty metric data; absence is not a zero.
        return {}
    if not isinstance(summary, dict):
        raise AnalyticsNormalizationError("Pinterest summary_metrics is not an object")
    return summary


def _convert_api_error(error: PinterestApiError) -> Exception:
    if isinstance(error, PinterestRateLimited):
        return AnalyticsRateLimitError("Pinterest analytics rate limit was reached")
    if isinstance(error, (PinterestAuthenticationError, PinterestPermissionError)):
        return AnalyticsAuthenticationError("Pinterest analytics authorization failed")
    if isinstance(error, PinterestApiConfigurationError):
        return AnalyticsConfigurationError("Pinterest analytics API configuration is invalid")
    if isinstance(error, PinterestTemporaryError):
        return TemporaryAnalyticsProviderError("Pinterest analytics provider failed temporarily")
    if isinstance(error, (PinterestInvalidPayload, PinterestInvalidResponse)):
        return AnalyticsNormalizationError("Pinterest analytics request or response was invalid")
    return error


class PinterestApiAnalyticsProvider:
    """Bridge the v5 HTTP adapter into the existing provider-neutral DTOs."""

    def __init__(self, service_for_account: Callable[[str], PinterestApiService]):
        self._service_for_account = service_for_account

    @classmethod
    def from_database(cls, db: Session) -> "PinterestApiAnalyticsProvider":
        """Resolve existing account credentials on explicit collector use only."""
        services: dict[str, PinterestApiService] = {}

        def service_for_account(account_identifier: str) -> PinterestApiService:
            if account_identifier in services:
                return services[account_identifier]
            accounts = db.scalars(
                select(PinterestAccount).where(
                    PinterestAccount.account_identifier == account_identifier
                )
            ).all()
            if len(accounts) != 1:
                raise AnalyticsConfigurationError("Pinterest account could not be resolved")
            service = PinterestApiService(db, accounts[0])
            services[account_identifier] = service
            return service

        return cls(service_for_account)

    def fetch_pin_analytics(
        self,
        account_identifier: str,
        external_pin_id: str,
        period_start: datetime,
        period_end: datetime,
    ) -> PinAnalyticsDTO:
        start_date, end_date = _as_utc_date(period_start), _as_utc_date(period_end)
        try:
            response = self._service_for_account(account_identifier).fetch_pin_analytics(
                external_pin_id, start_date, end_date
            )
        except PinterestApiError as error:
            raise _convert_api_error(error) from None
        except PinterestIntegrationError:
            raise AnalyticsConfigurationError("Pinterest account credentials are unavailable") from None

        if not isinstance(response, dict):
            raise AnalyticsNormalizationError("Pinterest Pin analytics response is not an object")
        item = response.get(external_pin_id)
        if item is None:
            # Pinterest analytics can omit rows when no requested metric has
            # data. This retains NULL instead of manufacturing zeroes.
            metrics: dict[str, object] = {}
        else:
            metrics = _summary_metrics(item)
        values = {
            field: _metric_value(metrics.get(api_name), field)
            for api_name, field in _PIN_METRIC_FIELDS.items()
        }
        return PinAnalyticsDTO(
            external_pin_id=external_pin_id,
            **values,
            metric_schema_version="pinterest_v5_organic",
        )

    def fetch_account_analytics(
        self,
        account_identifier: str,
        period_start: datetime,
        period_end: datetime,
    ) -> AccountAnalyticsDTO | None:
        start_date, end_date = _as_utc_date(period_start), _as_utc_date(period_end)
        try:
            response = self._service_for_account(account_identifier).fetch_account_analytics(start_date, end_date)
        except PinterestApiError as error:
            raise _convert_api_error(error) from None
        except PinterestIntegrationError:
            raise AnalyticsConfigurationError("Pinterest account credentials are unavailable") from None

        if not isinstance(response, dict):
            raise AnalyticsNormalizationError("Pinterest account analytics response is not an object")
        bundles = [
            value for value in response.values()
            if isinstance(value, dict) and ("summary_metrics" in value or "daily_metrics" in value)
        ]
        if not bundles:
            metrics: dict[str, object] = {}
        elif len(bundles) == 1:
            metrics = _summary_metrics(bundles[0])
        else:
            raise AnalyticsNormalizationError("Pinterest account analytics response is ambiguous")
        values = {
            field: _metric_value(metrics.get(api_name), field)
            for api_name, field in _ACCOUNT_METRIC_FIELDS.items()
        }
        return AccountAnalyticsDTO(
            **values,
            metric_schema_version="pinterest_v5_organic",
        )
