"""Provider-neutral publishing coordination for future Pinterest API publishing.

No HTTP/API implementation is included here. The configured default provider is
deliberately disabled until Pinterest publishing has been approved and verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.pinterest_seo_limits import pinterest_seo_limit_violations
from app.models import (
    Pin,
    PinterestAccount,
    PinterestBoard,
    PinterestPublishIntent,
    PinterestPublishIntentStatus,
    PublishedPinterestPin,
    SEOABVariant,
)
from app.models.core import PinStatus


class PinterestPublishingError(Exception):
    """Base class for safe publishing-coordination errors."""


class PinterestPublishingUnavailable(PinterestPublishingError):
    """Raised while production Pinterest publishing is intentionally disabled."""


class PinterestPublishRejected(PinterestPublishingError):
    """The provider confirms that it rejected the request before creating a Pin."""


class PinterestPublishOutcomeUnknown(PinterestPublishingError):
    """The provider may have created the Pin; automatic retry is unsafe."""


class PinterestPublishAlreadyInProgress(PinterestPublishOutcomeUnknown):
    """Another process owns the durable publication claim."""


@dataclass(frozen=True)
class PinterestPinPublishRequest:
    """Internal publish payload; provider adapters map this to their API schema."""

    idempotency_key: str
    title: str
    description: str | None
    image_reference: str | None
    destination_url: str | None
    board_external_id: str | None
    alt_text: str | None = None


@dataclass(frozen=True)
class PinterestPinPublishResult:
    """Normalized provider result; never a raw Pinterest response object."""

    external_pin_id: str
    published_at: datetime


class PinterestPinPublishingProvider(Protocol):
    """Small boundary between the coordinator and a future Pinterest adapter."""

    publishing_enabled: bool

    def publish_pin(
        self,
        account: PinterestAccount,
        request: PinterestPinPublishRequest,
    ) -> PinterestPinPublishResult: ...


class DisabledPinterestPinPublishingProvider:
    """Fail-closed production default: it performs no network calls."""

    publishing_enabled = False

    def publish_pin(
        self,
        account: PinterestAccount,
        request: PinterestPinPublishRequest,
    ) -> PinterestPinPublishResult:
        raise PinterestPublishingUnavailable(
            "Pinterest Pin publishing is unavailable until the API integration is approved."
        )


class PinterestApiPublishingProvider:
    """Map PinPilot publish requests to the API v5 adapter.

    It is fail-closed by default. The adapter is only reachable through this
    provider when PINTEREST_PUBLISH_ENABLED is explicitly enabled; no current
    scheduler or worker instantiates this class.
    """

    @property
    def publishing_enabled(self) -> bool:
        return settings.pinterest_publish_enabled

    def __init__(self, db: Session, *, api_client_factory=None):
        self.db = db
        self.api_client_factory = api_client_factory

    def publish_pin(
        self,
        account: PinterestAccount,
        request: PinterestPinPublishRequest,
    ) -> PinterestPinPublishResult:
        if not self.publishing_enabled:
            raise PinterestPublishingUnavailable(
                "Pinterest Pin publishing is unavailable until the API integration is approved."
            )
        if not request.board_external_id:
            raise PinterestPublishRejected("Pinterest publishing requires a board selected from this account.")

        from app.services.pinterest import PinterestTokenService
        from app.services.pinterest_api import (
            PinterestApiClient,
            PinterestApiError,
            PinterestInvalidResponse,
            PinterestImageUrlError,
            PinterestRateLimited,
            PinterestTemporaryError,
            PinterestCreatePinPayload,
            pinterest_image_url,
        )

        try:
            image_url = pinterest_image_url(request.image_reference)
            payload = PinterestCreatePinPayload(
                board_id=request.board_external_id,
                title=request.title,
                description=request.description,
                link=request.destination_url,
                alt_text=request.alt_text,
                media_source={"source_type": "image_url", "url": image_url, "is_standard": True},
            )
        except (PinterestImageUrlError, ValueError) as exc:
            raise PinterestPublishRejected(str(exc)) from None

        try:
            if self.api_client_factory is not None:
                client = self.api_client_factory(account)
            else:
                access_token = PinterestTokenService(self.db).access_token(account)
                client = PinterestApiClient(access_token)
            remote_pin = client.create_pin(payload)
        except PinterestTemporaryError:
            # A timeout/5xx after POST may occur after Pinterest created the Pin.
            raise PinterestPublishOutcomeUnknown(
                "Pinterest may have created the Pin; reconcile before retrying."
            ) from None
        except PinterestInvalidResponse:
            raise PinterestPublishOutcomeUnknown(
                "Pinterest may have created the Pin but returned an invalid response; reconcile before retrying."
            ) from None
        except PinterestRateLimited as exc:
            # No automatic retry here. The durable intent can be manually retried.
            retry_after = f" Retry-After: {exc.retry_after}." if exc.retry_after else ""
            raise PinterestPublishRejected("Pinterest rate limited the publish request." + retry_after) from None
        except PinterestApiError as exc:
            raise PinterestPublishRejected(str(exc)) from None
        except Exception:
            # Never expose arbitrary client, token, or HTTP exception text.
            raise PinterestPublishOutcomeUnknown(
                "Pinterest publish result could not be confirmed; reconcile before retrying."
            ) from None

        try:
            published_at = datetime.fromisoformat(remote_pin.created_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            raise PinterestPublishOutcomeUnknown(
                "Pinterest may have created the Pin but returned an invalid timestamp; reconcile before retrying."
            ) from None
        return PinterestPinPublishResult(external_pin_id=remote_pin.id, published_at=published_at)


class PinterestPublisher:
    """Coordinate one durable publication intent and its confirmed result.

    An intent is committed before calling the provider. A process restart that
    finds a still-publishing intent will not resend it. A definitive provider
    rejection may be retried with the same key; an ambiguous result is held for
    reconciliation instead of risking a duplicate external Pin.
    """

    def __init__(
        self,
        db: Session,
        provider: PinterestPinPublishingProvider | None = None,
        *,
        now=None,
    ):
        self.db = db
        self.provider = provider or DisabledPinterestPinPublishingProvider()
        self.now = now or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    def publish_pin(
        self,
        pin_id: int,
        account_id: int,
        board_id: int | None = None,
        *,
        seo_ab_variant_id: int | None = None,
    ) -> PublishedPinterestPin:
        """Publish a local Pin, optionally using one explicitly selected SEO variant.

        Production remains fail-closed through the existing provider flag. Variant
        attribution is recorded only after a provider-confirmed Published Pin.
        """
        if not self.provider.publishing_enabled:
            raise PinterestPublishingUnavailable(
                "Pinterest Pin publishing is unavailable until the API integration is approved."
            )

        pin = self.db.get(Pin, pin_id)
        account = self.db.get(PinterestAccount, account_id)
        if pin is None or account is None or not account.is_active:
            raise PinterestPublishingError("A valid local Pin and active Pinterest account are required.")
        if not account.account_identifier:
            raise PinterestPublishingError("The Pinterest account has no stable external account identifier.")
        board = self.db.get(PinterestBoard, board_id) if board_id is not None else None
        if board_id is not None and (board is None or board.account_id != account.id):
            raise PinterestPublishingError("The selected Pinterest board does not belong to this account.")
        if isinstance(self.provider, PinterestApiPublishingProvider) and board is None:
            raise PinterestPublishRejected("Pinterest publishing requires a board selected from this account.")

        variant = None
        if seo_ab_variant_id is not None:
            variant = self.db.get(SEOABVariant, seo_ab_variant_id)
            if variant is None:
                raise PinterestPublishingError("The requested SEO A/B variant was not found.")
            experiment = variant.experiment
            if variant.variant_type != "KEYWORD_FOCUS" or variant.status != "READY" or variant.quality_status == "FAIL":
                raise PinterestPublishRejected("Only a quality-approved supported SEO variant can be published.")
            if experiment.status != "RUNNING":
                raise PinterestPublishRejected("SEO A/B variant publishing requires an explicitly RUNNING experiment.")
            source_generation = experiment.source_generation
            creative = pin.creative
            if source_generation.creative_id is None and source_generation.product_id is None:
                raise PinterestPublishRejected("The variant source generation has no verified local product or creative link.")
            if source_generation.creative_id is not None and (
                creative is None or creative.id != source_generation.creative_id
            ):
                raise PinterestPublishRejected("The variant source generation does not belong to this local Pin creative.")
            if source_generation.product_id is not None and pin.product_id != source_generation.product_id:
                raise PinterestPublishRejected("The variant source generation does not belong to this local Pin product.")

        publish_title = (variant.output_snapshot.get("title") if variant else None) or pin.title
        publish_description = (
            variant.output_snapshot.get("description") if variant else pin.description
        )

        intent = self.db.scalar(select(PinterestPublishIntent).where(
            PinterestPublishIntent.pin_id == pin.id,
            PinterestPublishIntent.account_identifier_snapshot == account.account_identifier,
        ))
        retry_claim_committed = False
        if intent is None:
            legacy_publication = self.db.scalar(select(PublishedPinterestPin).where(
                PublishedPinterestPin.pin_id == pin.id,
                PublishedPinterestPin.account_id == account.id,
                PublishedPinterestPin.external_pin_id.is_not(None),
            ))
            if legacy_publication is not None:
                if variant is not None:
                    raise PinterestPublishingError("An existing unattributed publication cannot be assigned to an SEO variant.")
                return legacy_publication
            if not self._pin_is_publishable(pin):
                raise PinterestPublishingError("Only a locally scheduled Pin can be published.")
            self._validate_seo_text(publish_title, publish_description)
        if intent is not None:
            if intent.seo_ab_variant_id != seo_ab_variant_id:
                raise PinterestPublishingError("A Pin publish intent cannot be reused for a different SEO variant.")
            if intent.status == PinterestPublishIntentStatus.PUBLISHED.value:
                publication = intent.published_pin or self.db.get(PublishedPinterestPin, intent.published_pin_id)
                if publication is not None:
                    if variant is not None:
                        try:
                            from app.services.seo_ab_variations import link_verified_variant_publication

                            link_verified_variant_publication(self.db, variant.id, publication.id)
                            self.db.commit()
                        except ValueError:
                            intent.status = PinterestPublishIntentStatus.UNKNOWN.value
                            intent.error_summary = "Variant publication attribution could not be verified; reconcile before retrying."
                            self.db.commit()
                            raise PinterestPublishOutcomeUnknown(intent.error_summary) from None
                    return publication
                raise PinterestPublishOutcomeUnknown(
                    "The completed publication record is unavailable; reconcile it before retrying."
                )
            if intent.status in (
                PinterestPublishIntentStatus.PUBLISHING.value,
                PinterestPublishIntentStatus.UNKNOWN.value,
            ):
                raise PinterestPublishAlreadyInProgress(
                    "A previous Pinterest publish attempt may still be active; reconcile it before retrying."
                )
            if intent.status != PinterestPublishIntentStatus.FAILED.value:
                raise PinterestPublishingError("The Pinterest publish intent has an unsupported state.")
            if intent.board_id != board_id:
                raise PinterestPublishingError("A failed Pin publish retry must use the original board selection.")
            if not self._pin_is_publishable(pin):
                raise PinterestPublishingError("Only a locally scheduled Pin can be retried.")
            self._validate_seo_text(publish_title, publish_description)
            claim = self.db.execute(
                update(PinterestPublishIntent)
                .where(
                    PinterestPublishIntent.id == intent.id,
                    PinterestPublishIntent.status == PinterestPublishIntentStatus.FAILED.value,
                )
                .values(
                    status=PinterestPublishIntentStatus.PUBLISHING.value,
                    error_summary=None,
                    last_attempt_at=self.now(),
                )
            )
            if claim.rowcount != 1:
                self.db.rollback()
                raise PinterestPublishAlreadyInProgress(
                    "Another process already claimed this failed Pinterest publish intent."
                )
            self.db.commit()
            intent = self.db.get(PinterestPublishIntent, intent.id)
            retry_claim_committed = True
        else:
            intent = PinterestPublishIntent(
                pin=pin,
                account=account,
                board=board,
                seo_ab_variant=variant,
                account_identifier_snapshot=account.account_identifier,
                status=PinterestPublishIntentStatus.PUBLISHING.value,
                last_attempt_at=self.now(),
            )
            self.db.add(intent)

        if not retry_claim_committed:
            try:
                # The unique Pin/account identity is the cross-process claim.
                self.db.commit()
            except IntegrityError:
                self.db.rollback()
                raise PinterestPublishAlreadyInProgress(
                    "Another process already owns this Pin's publish intent."
                ) from None

        request = PinterestPinPublishRequest(
            idempotency_key=intent.idempotency_key,
            title=publish_title,
            description=publish_description,
            image_reference=pin.image_path,
            destination_url=pin.destination_url,
            board_external_id=board.board_id if board else None,
            alt_text=None,
        )
        try:
            result = self.provider.publish_pin(account, request)
        except PinterestPublishRejected:
            intent.status = PinterestPublishIntentStatus.FAILED.value
            intent.error_summary = "Pinterest rejected the request before creating a Pin."
            self.db.commit()
            raise
        except Exception as exc:
            # Do not include provider text: it could contain tokens or response data.
            intent.status = PinterestPublishIntentStatus.UNKNOWN.value
            intent.error_summary = "The result of the Pinterest publish request is unknown; reconcile before retrying."
            self.db.commit()
            if isinstance(exc, PinterestPublishOutcomeUnknown):
                raise
            raise PinterestPublishOutcomeUnknown(intent.error_summary) from None

        if (
            not isinstance(result, PinterestPinPublishResult)
            or not isinstance(result.external_pin_id, str)
            or not result.external_pin_id.strip()
            or len(result.external_pin_id) > 128
            or not isinstance(result.published_at, datetime)
        ):
            intent.status = PinterestPublishIntentStatus.UNKNOWN.value
            intent.error_summary = "Pinterest returned an invalid publication identity; reconcile before retrying."
            self.db.commit()
            raise PinterestPublishOutcomeUnknown(intent.error_summary)

        duplicate = self.db.scalar(select(PublishedPinterestPin).where(
            PublishedPinterestPin.account_id == account.id,
            PublishedPinterestPin.external_pin_id == result.external_pin_id,
        ))
        if duplicate is not None:
            if duplicate.pin_id == pin.id:
                if variant is not None:
                    try:
                        # Recover only an exact previously-confirmed variant publication.
                        if (duplicate.metadata_snapshot or {}).get("seo_ab_variant_id") != variant.id:
                            raise ValueError("variant provenance does not match")
                        intent.published_pin = duplicate
                        intent.status = PinterestPublishIntentStatus.PUBLISHED.value
                        self.db.flush()
                        from app.services.seo_ab_variations import link_verified_variant_publication

                        link_verified_variant_publication(self.db, variant.id, duplicate.id)
                    except ValueError:
                        intent.status = PinterestPublishIntentStatus.UNKNOWN.value
                        intent.error_summary = "An existing Pinterest Pin did not confirm this variant attribution."
                        self.db.commit()
                        raise PinterestPublishOutcomeUnknown(intent.error_summary) from None
                intent.published_pin = duplicate
                intent.status = PinterestPublishIntentStatus.PUBLISHED.value
                pin.status = PinStatus.PUBLISHED.value
                pin.published_at = duplicate.published_at
                self.db.commit()
                return duplicate
            intent.status = PinterestPublishIntentStatus.UNKNOWN.value
            intent.error_summary = "Pinterest returned an external Pin ID already assigned to another local Pin."
            self.db.commit()
            raise PinterestPublishOutcomeUnknown(intent.error_summary)

        published_at = result.published_at
        if published_at.tzinfo is not None:
            published_at = published_at.astimezone(timezone.utc).replace(tzinfo=None)
        metadata_snapshot = PublishedPinterestPin.capture_metadata(pin)
        if variant is not None:
            variant_snapshot = variant.output_snapshot or {}
            variant_seo = variant_snapshot.get("seo_metadata") if isinstance(variant_snapshot.get("seo_metadata"), dict) else {}
            metadata_snapshot.update({
                "creative_title": variant_snapshot.get("title") or metadata_snapshot.get("creative_title"),
                "creative_description": variant_snapshot.get("description") or metadata_snapshot.get("creative_description"),
                "seo_metadata": deepcopy(variant_seo),
                "primary_keyword": variant_seo.get("primary_keyword"),
                "creative_angle": variant_seo.get("creative_angle"),
                "seo_generation_id": variant.experiment.source_generation_id,
                "seo_provenance_status": "known",
                "seo_ab_variant_id": variant.id,
                "seo_ab_variant_key": variant.variant_key,
                "seo_ab_experiment_id": variant.experiment_id,
                "seo_ab_change_set": deepcopy(variant.change_set or {}),
                "seo_ab_provenance": {
                    "source_generation_id": variant.experiment.source_generation_id,
                    "source_keyword_intelligence_id": variant.provenance_snapshot.get("source_keyword_intelligence_id"),
                    "source_quality_assessment_id": variant.provenance_snapshot.get("source_quality_assessment_id"),
                    "board_recommendation_ids": deepcopy(variant.provenance_snapshot.get("board_recommendation_ids", [])),
                    "seasonal_assessment_ids": deepcopy(variant.provenance_snapshot.get("seasonal_assessment_ids", [])),
                    "performance_learning": deepcopy(variant.provenance_snapshot.get("performance_learning", [])),
                    "variation_algorithm_version": variant.algorithm_version,
                    "provider": variant.provider,
                    "external_calls": False,
                },
            })
        publication = PublishedPinterestPin(
            pin=pin,
            account=account,
            board=board,
            account_identifier_snapshot=account.account_identifier,
            external_pin_id=result.external_pin_id,
            seo_generation_id=metadata_snapshot.get("seo_generation_id"),
            published_at=published_at,
            metadata_snapshot=metadata_snapshot,
        )
        self.db.add(publication)
        intent.published_pin = publication
        intent.status = PinterestPublishIntentStatus.PUBLISHED.value
        intent.error_summary = None
        pin.status = PinStatus.PUBLISHED.value
        pin.published_at = published_at
        try:
            self.db.flush()
            if variant is not None:
                from app.services.seo_ab_variations import link_verified_variant_publication

                link_verified_variant_publication(self.db, variant.id, publication.id)
            self.db.commit()
        except IntegrityError:
            # The external side effect may already have happened. Preserve the
            # durable publishing claim and require reconciliation; never resend.
            self.db.rollback()
            current_intent = self.db.scalar(select(PinterestPublishIntent).where(
                PinterestPublishIntent.pin_id == pin_id,
                PinterestPublishIntent.account_identifier_snapshot == account.account_identifier,
            ))
            if current_intent is not None:
                current_intent.status = PinterestPublishIntentStatus.UNKNOWN.value
                current_intent.error_summary = (
                    "Pinterest may have created the Pin, but local confirmation failed; reconcile before retrying."
                )
                self.db.commit()
            raise PinterestPublishOutcomeUnknown(
                "Pinterest may have created the Pin, but local confirmation failed; reconcile before retrying."
            ) from None
        return publication

    def _pin_is_publishable(self, pin: Pin) -> bool:
        if pin.status == PinStatus.SCHEDULED.value:
            return True
        if pin.status != PinStatus.PUBLISHED.value:
            return False
        return self.db.scalar(select(PublishedPinterestPin.id).where(
            PublishedPinterestPin.pin_id == pin.id,
        ).limit(1)) is not None

    @staticmethod
    def _validate_seo_text(title: str | None, description: str | None) -> None:
        limit_errors = pinterest_seo_limit_violations(title, description)
        if limit_errors:
            # Keep the original text intact for correction/audit and reject
            # before claiming a new/retry intent or invoking any provider.
            raise PinterestPublishRejected(" ".join(item["message"] for item in limit_errors))
