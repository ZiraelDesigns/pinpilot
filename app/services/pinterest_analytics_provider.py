"""Pinterest v5 adapter used by the opt-in analytics collection entry point.

The scheduled script ``scripts/collect_pinterest_analytics.py`` constructs this
provider only after checking ``PINTEREST_ANALYTICS_COLLECTION_ENABLED``. The
optional systemd oneshot/timer units provide the schedule, but are installed and
enabled separately; the provider is not invoked by dashboard requests or normal
application startup. Normalized collection and persistence remain in the
provider-neutral collector and its existing database models.
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


def _daily_metric_rows(
    bundle: dict[str, object],
    period_start: datetime,
    period_end: datetime,
    metric_fields: dict[str, str],
    *,
    external_pin_id: str | None = None,
) -> tuple[PinAnalyticsDTO | AccountAnalyticsDTO, ...] | None:
    """Normalize documented daily_metrics rows; None means summary-only response."""
    if "daily_metrics" not in bundle:
        return None
    daily_metrics = bundle["daily_metrics"]
    if not isinstance(daily_metrics, list):
        raise AnalyticsNormalizationError("Pinterest daily_metrics is not an array")

    start_date, end_date = _as_utc_date(period_start), _as_utc_date(period_end)
    result: list[PinAnalyticsDTO | AccountAnalyticsDTO] = []
    seen_dates: set[date] = set()
    for row in daily_metrics:
        if not isinstance(row, dict):
            raise AnalyticsNormalizationError("Pinterest daily metric row is not an object")
        # A day is persisted only when Pinterest marks that row ready. Other
        # statuses mean there is no usable measurement yet, not a zero value.
        if row.get("data_status") != "READY":
            continue
        raw_date = row.get("date")
        if not isinstance(raw_date, str):
            raise AnalyticsNormalizationError("Pinterest daily metric date is missing")
        try:
            metric_date = date.fromisoformat(raw_date)
        except ValueError:
            raise AnalyticsNormalizationError("Pinterest daily metric date is invalid") from None
        if not start_date <= metric_date <= end_date:
            raise AnalyticsNormalizationError("Pinterest daily metric date is outside the requested period")
        if metric_date in seen_dates:
            raise AnalyticsNormalizationError("Pinterest returned duplicate daily metric dates")
        seen_dates.add(metric_date)
        metrics = row.get("metrics")
        if not isinstance(metrics, dict):
            raise AnalyticsNormalizationError("Pinterest daily metric values are not an object")
        values = {
            field: _metric_value(metrics.get(api_name), field)
            for api_name, field in metric_fields.items()
        }
        if external_pin_id is not None:
            result.append(PinAnalyticsDTO(
                external_pin_id=external_pin_id,
                metric_date=metric_date,
                **values,
                metric_schema_version="pinterest_v5_organic_daily",
            ))
        else:
            result.append(AccountAnalyticsDTO(
                metric_date=metric_date,
                **values,
                metric_schema_version="pinterest_v5_organic_daily",
            ))
    return tuple(result)


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
    ) -> PinAnalyticsDTO | tuple[PinAnalyticsDTO, ...]:
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
        elif not isinstance(item, dict):
            raise AnalyticsNormalizationError("Pinterest Pin analytics item is not an object")
        else:
            daily_rows = _daily_metric_rows(
                item, period_start, period_end, _PIN_METRIC_FIELDS,
                external_pin_id=external_pin_id,
            )
            if daily_rows is not None:
                return daily_rows
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
    ) -> AccountAnalyticsDTO | tuple[AccountAnalyticsDTO, ...] | None:
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
            daily_rows = _daily_metric_rows(
                bundles[0], period_start, period_end, _ACCOUNT_METRIC_FIELDS
            )
            if daily_rows is not None:
                return daily_rows
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
