"""Provider-neutral Pinterest analytics contracts and local database collector.

There is deliberately no HTTP implementation in this module. A provider receives
public account/Pin identifiers only; OAuth credentials are not part of its interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AnalyticsCollectionRun,
    AnalyticsSnapshot,
    PinterestAccount,
    PinterestAccountAnalyticsSnapshot,
    PublishedPinterestPin,
)


class AnalyticsProviderError(Exception):
    """Base class for safe-to-classify provider failures."""


class TemporaryAnalyticsProviderError(AnalyticsProviderError):
    """The provider failed temporarily; no automatic retry is performed here."""


class AnalyticsRateLimitError(AnalyticsProviderError):
    """The provider reported a rate limit condition."""


class AnalyticsAuthenticationError(AnalyticsProviderError):
    """Provider authentication was rejected."""


class AnalyticsConfigurationError(AnalyticsProviderError):
    """The provider or account is not configured for this operation."""


class AnalyticsNormalizationError(ValueError):
    """A provider DTO does not meet PinPilot's internal analytics contract."""


@dataclass(frozen=True)
class PinAnalyticsDTO:
    """Normalized metrics for one external Pin; absent and null metrics stay None."""

    external_pin_id: str
    metric_date: date | None = None
    impressions: int | None = None
    saves: int | None = None
    pin_clicks: int | None = None
    outbound_clicks: int | None = None
    engagements: int | None = None
    engagement_rate: Decimal | None = None
    pin_click_rate: Decimal | None = None
    outbound_click_rate: Decimal | None = None
    metric_schema_version: str | None = None


@dataclass(frozen=True)
class AccountAnalyticsDTO:
    """Normalized account-scope metrics, independent of Pin-scope metrics."""

    metric_date: date | None = None
    profile_visits: int | None = None
    follows: int | None = None
    total_audience: int | None = None
    engaged_audience: int | None = None
    metric_schema_version: str | None = None


class PinterestAnalyticsProvider(Protocol):
    """Minimal interface implemented by a future, separately approved provider."""

    def fetch_pin_analytics(
        self,
        account_identifier: str,
        external_pin_id: str,
        period_start: datetime,
        period_end: datetime,
    ) -> PinAnalyticsDTO: ...

    def fetch_account_analytics(
        self,
        account_identifier: str,
        period_start: datetime,
        period_end: datetime,
    ) -> AccountAnalyticsDTO | None: ...


@dataclass(frozen=True)
class AnalyticsCollectionFailure:
    scope: str
    target: str
    category: str


@dataclass(frozen=True)
class AnalyticsCollectionResult:
    run_id: int
    status: str
    pin_snapshots_written: int
    account_snapshot_written: bool
    skipped_pins: int
    failures: tuple[AnalyticsCollectionFailure, ...]


def _normalize_count(name: str, value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AnalyticsNormalizationError(f"{name} must be a non-negative integer or null")
    return value


def _normalize_rate(name: str, value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise AnalyticsNormalizationError(f"{name} must be a non-negative decimal or null")
    try:
        normalized = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AnalyticsNormalizationError(f"{name} must be a non-negative decimal or null") from exc
    if not normalized.is_finite() or normalized < 0:
        raise AnalyticsNormalizationError(f"{name} must be a non-negative decimal or null")
    return normalized


def _normalize_metric_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, date) or isinstance(value, datetime):
        raise AnalyticsNormalizationError("metric_date must be a date or null")
    return value


def normalize_pin_analytics(value: PinAnalyticsDTO, expected_pin_id: str) -> PinAnalyticsDTO:
    """Validate and canonicalize Pin data without inventing unavailable metrics."""
    if not isinstance(value, PinAnalyticsDTO):
        raise AnalyticsNormalizationError("provider returned an invalid Pin analytics DTO")
    if not isinstance(value.external_pin_id, str) or not value.external_pin_id.strip():
        raise AnalyticsNormalizationError("external_pin_id is required")
    if value.external_pin_id != expected_pin_id:
        raise AnalyticsNormalizationError("provider response does not match the requested Pin")
    if value.metric_schema_version is not None and not isinstance(value.metric_schema_version, str):
        raise AnalyticsNormalizationError("metric_schema_version must be a string or null")
    return PinAnalyticsDTO(
        external_pin_id=value.external_pin_id,
        metric_date=_normalize_metric_date(value.metric_date),
        impressions=_normalize_count("impressions", value.impressions),
        saves=_normalize_count("saves", value.saves),
        pin_clicks=_normalize_count("pin_clicks", value.pin_clicks),
        outbound_clicks=_normalize_count("outbound_clicks", value.outbound_clicks),
        engagements=_normalize_count("engagements", value.engagements),
        engagement_rate=_normalize_rate("engagement_rate", value.engagement_rate),
        pin_click_rate=_normalize_rate("pin_click_rate", value.pin_click_rate),
        outbound_click_rate=_normalize_rate("outbound_click_rate", value.outbound_click_rate),
        metric_schema_version=value.metric_schema_version,
    )


def normalize_account_analytics(value: AccountAnalyticsDTO) -> AccountAnalyticsDTO:
    """Validate account-scope data separately from Pin metrics."""
    if not isinstance(value, AccountAnalyticsDTO):
        raise AnalyticsNormalizationError("provider returned an invalid account analytics DTO")
    if value.metric_schema_version is not None and not isinstance(value.metric_schema_version, str):
        raise AnalyticsNormalizationError("metric_schema_version must be a string or null")
    return AccountAnalyticsDTO(
        metric_date=_normalize_metric_date(value.metric_date),
        profile_visits=_normalize_count("profile_visits", value.profile_visits),
        follows=_normalize_count("follows", value.follows),
        total_audience=_normalize_count("total_audience", value.total_audience),
        engaged_audience=_normalize_count("engaged_audience", value.engaged_audience),
        metric_schema_version=value.metric_schema_version,
    )


def _failure_category(error: Exception) -> str:
    if isinstance(error, AnalyticsRateLimitError):
        return "rate_limit"
    if isinstance(error, (AnalyticsAuthenticationError, AnalyticsConfigurationError)):
        return "authentication_or_configuration"
    if isinstance(error, AnalyticsNormalizationError):
        return "normalization_or_validation"
    if isinstance(error, TemporaryAnalyticsProviderError):
        return "temporary_provider_error"
    if isinstance(error, AnalyticsProviderError):
        return "provider_error"
    return "unexpected_provider_error"


class AnalyticsCollector:
    """Collect analytics through an injected provider and persist appendable snapshots."""

    def __init__(self, db: Session, provider: PinterestAnalyticsProvider):
        self.db = db
        self.provider = provider

    def collect(
        self,
        account_id: int,
        period_start: datetime,
        period_end: datetime,
        *,
        run_id: int | None = None,
        include_pins: bool = True,
        include_account: bool = True,
    ) -> AnalyticsCollectionResult:
        account = self.db.get(PinterestAccount, account_id)
        if account is None:
            raise ValueError("Pinterest account not found")

        if run_id is None:
            run = AnalyticsCollectionRun(
                account=account,
                account_identifier_snapshot=account.account_identifier,
                started_at=datetime.now(UTC).replace(tzinfo=None),
                status="running",
                period_start=period_start,
                period_end=period_end,
                scope=self._scope(include_pins, include_account),
            )
            self.db.add(run)
            self.db.commit()
            self.db.refresh(run)
        else:
            run = self.db.get(AnalyticsCollectionRun, run_id)
            if run is None or run.account_id != account.id:
                raise ValueError("Analytics collection run does not belong to this account")
            if run.period_start != period_start or run.period_end != period_end:
                raise ValueError("Retry period must match the original collection run")
            run.status = "running"
            run.finished_at = None
            run.error_summary = None
            run.scope = self._scope(include_pins, include_account)
            self.db.commit()

        failures: list[AnalyticsCollectionFailure] = []
        snapshots_written = 0
        skipped_pins = 0
        account_snapshot_written = False
        now = datetime.now(UTC).replace(tzinfo=None)

        if period_start >= period_end:
            failures.append(AnalyticsCollectionFailure("collection", "period", "normalization_or_validation"))
        elif not account.account_identifier:
            failures.append(AnalyticsCollectionFailure(
                "collection", "account", "authentication_or_configuration"
            ))
        else:
            if include_pins:
                publications = self.db.scalars(
                    select(PublishedPinterestPin)
                    .where(
                        PublishedPinterestPin.account_id == account.id,
                        PublishedPinterestPin.published_at <= period_end,
                    )
                    .order_by(PublishedPinterestPin.id)
                ).all()
                for publication in publications:
                    if not publication.external_pin_id:
                        skipped_pins += 1
                        continue
                    try:
                        response = self.provider.fetch_pin_analytics(
                            account.account_identifier,
                            publication.external_pin_id,
                            period_start,
                            period_end,
                        )
                        metrics = normalize_pin_analytics(response, publication.external_pin_id)
                        self._upsert_pin_snapshot(run, publication, metrics, period_start, period_end, now)
                        snapshots_written += 1
                    except Exception as exc:
                        failures.append(AnalyticsCollectionFailure(
                            "pin", str(publication.id), _failure_category(exc)
                        ))

            if include_account:
                try:
                    response = self.provider.fetch_account_analytics(
                        account.account_identifier,
                        period_start,
                        period_end,
                    )
                    if response is not None:
                        metrics = normalize_account_analytics(response)
                        self._upsert_account_snapshot(
                            run, account, metrics, period_start, period_end, now
                        )
                        account_snapshot_written = True
                except Exception as exc:
                    failures.append(AnalyticsCollectionFailure(
                        "account", str(account.id), _failure_category(exc)
                    ))

        run.status = "failed" if failures else "completed"
        run.finished_at = datetime.now(UTC).replace(tzinfo=None)
        run.error_summary = self._safe_error_summary(failures)
        self.db.commit()
        return AnalyticsCollectionResult(
            run_id=run.id,
            status=run.status,
            pin_snapshots_written=snapshots_written,
            account_snapshot_written=account_snapshot_written,
            skipped_pins=skipped_pins,
            failures=tuple(failures),
        )

    @staticmethod
    def _scope(include_pins: bool, include_account: bool) -> str:
        if include_pins and include_account:
            return "all"
        if include_pins:
            return "pin"
        if include_account:
            return "account"
        return "none"

    @staticmethod
    def _safe_error_summary(failures: list[AnalyticsCollectionFailure]) -> str | None:
        if not failures:
            return None
        # Never persist provider exception messages: they may contain credentials.
        return "; ".join(
            f"{failure.scope}:{failure.target}:{failure.category}" for failure in failures
        )[:1000]

    def _upsert_pin_snapshot(
        self,
        run: AnalyticsCollectionRun,
        publication: PublishedPinterestPin,
        metrics: PinAnalyticsDTO,
        period_start: datetime,
        period_end: datetime,
        fetched_at: datetime,
    ) -> None:
        snapshot = self.db.scalar(
            select(AnalyticsSnapshot).where(
                AnalyticsSnapshot.collection_run_id == run.id,
                AnalyticsSnapshot.published_pin_id == publication.id,
                AnalyticsSnapshot.period_start == period_start,
                AnalyticsSnapshot.period_end == period_end,
            )
        )
        if snapshot is None:
            snapshot = AnalyticsSnapshot(
                collection_run_id=run.id,
                published_pin_id=publication.id,
                pin_id=publication.pin_id,
                period_start=period_start,
                period_end=period_end,
            )
            self.db.add(snapshot)
        snapshot.metric_date = metrics.metric_date
        snapshot.fetched_at = fetched_at
        snapshot.recorded_at = fetched_at
        snapshot.impressions = metrics.impressions
        snapshot.saves = metrics.saves
        snapshot.pin_clicks = metrics.pin_clicks
        snapshot.outbound_clicks = metrics.outbound_clicks
        snapshot.engagements = metrics.engagements
        snapshot.engagement_rate = metrics.engagement_rate
        snapshot.pin_click_rate = metrics.pin_click_rate
        snapshot.outbound_click_rate = metrics.outbound_click_rate
        snapshot.metric_schema_version = metrics.metric_schema_version

    def _upsert_account_snapshot(
        self,
        run: AnalyticsCollectionRun,
        account: PinterestAccount,
        metrics: AccountAnalyticsDTO,
        period_start: datetime,
        period_end: datetime,
        fetched_at: datetime,
    ) -> None:
        snapshot = self.db.scalar(
            select(PinterestAccountAnalyticsSnapshot).where(
                PinterestAccountAnalyticsSnapshot.collection_run_id == run.id,
                PinterestAccountAnalyticsSnapshot.account_id == account.id,
                PinterestAccountAnalyticsSnapshot.period_start == period_start,
                PinterestAccountAnalyticsSnapshot.period_end == period_end,
            )
        )
        if snapshot is None:
            snapshot = PinterestAccountAnalyticsSnapshot(
                collection_run_id=run.id,
                account_id=account.id,
                period_start=period_start,
                period_end=period_end,
            )
            self.db.add(snapshot)
        snapshot.metric_date = metrics.metric_date
        snapshot.fetched_at = fetched_at
        snapshot.profile_visits = metrics.profile_visits
        snapshot.follows = metrics.follows
        snapshot.total_audience = metrics.total_audience
        snapshot.engaged_audience = metrics.engaged_audience
        snapshot.metric_schema_version = metrics.metric_schema_version
