"""Pinterest API v5 HTTP adapter and request/response validation.

The adapter is deliberately passive: constructing it performs no request. The
publisher remains disabled unless an operator explicitly enables it, and no
application worker currently wires this adapter into its run loop.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.config import settings
from app.pinterest_seo_limits import (
    PINTEREST_PIN_DESCRIPTION_MAX_LENGTH,
    PINTEREST_PIN_TITLE_MAX_LENGTH,
)

logger = logging.getLogger(__name__)

PINTEREST_API_HOSTS = {"api.pinterest.com", "api-sandbox.pinterest.com"}


class PinterestIntegrationError(Exception):
    """Base class for safe Pinterest OAuth and upstream API failures."""


class PinterestApiError(PinterestIntegrationError):
    """Base exception for Pinterest API adapter errors."""

    retryable = False


class PinterestAuthenticationError(PinterestApiError):
    """The access token is missing, expired, or invalid (HTTP 401)."""


class PinterestPermissionError(PinterestApiError):
    """Pinterest refused an operation or its required scope (HTTP 403)."""


class PinterestResourceNotFound(PinterestApiError):
    """A requested remote resource does not exist (HTTP 404)."""


class PinterestBoardNotFound(PinterestResourceNotFound):
    """The selected external board is unavailable."""


class PinterestPinNotFound(PinterestResourceNotFound):
    """The requested external Pin is unavailable."""


class PinterestInvalidMedia(PinterestApiError):
    """The image source is invalid or unavailable to Pinterest."""


class PinterestInvalidPayload(PinterestApiError):
    """Pinterest rejected request fields (HTTP 400/422)."""


class PinterestRateLimited(PinterestApiError):
    """Pinterest rate limit (HTTP 429); no automatic retry is performed."""

    retryable = True

    def __init__(self, message: str, retry_after: str | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PinterestTemporaryError(PinterestApiError):
    """Network or Pinterest 5xx error; a create result may be ambiguous."""

    retryable = True


class PinterestUnknownApiError(PinterestApiError):
    """An unclassified non-retryable upstream error."""


class PinterestApiConfigurationError(PinterestApiError):
    """An unsafe or unsupported API base URL configuration."""


class PinterestInvalidResponse(PinterestApiError):
    """A successful upstream response did not match the required schema."""


class PinterestImageUrlError(ValueError):
    """An image is not represented by a publicly reachable HTTPS URL."""


def _validate_public_https_url(value: str, label: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            raise ValueError
        if host.lower() == "localhost" or host.lower().endswith((".localhost", ".local", ".internal")):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        if not parsed.path:
            raise ValueError
    except ValueError:
        raise PinterestImageUrlError(f"{label} HTTPS üzerinden herkese açık bir URL olmalı.") from None
    return value


class PinterestImageUrlMediaSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: Literal["image_url"] = "image_url"
    url: str
    is_standard: bool = True

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_public_https_url(value, "Görsel")


class PinterestCreatePinPayload(BaseModel):
    """Validated subset of Pinterest's documented Create Pin image payload."""

    model_config = ConfigDict(extra="forbid")

    board_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str | None = None
    link: str | None = None
    alt_text: str | None = None
    media_source: PinterestImageUrlMediaSource

    @field_validator("board_id", "title")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("alan boş olamaz")
        return value

    # Pinterest's published Pin text specification is an application-level
    # guard; it is separate from the HTTP adapter's API request/response schema.
    @field_validator("title")
    @classmethod
    def enforce_title_limit(cls, value: str) -> str:
        if len(value) > PINTEREST_PIN_TITLE_MAX_LENGTH:
            raise ValueError(f"Pinterest Pin title exceeds {PINTEREST_PIN_TITLE_MAX_LENGTH} characters")
        return value

    @field_validator("description", "alt_text")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("description")
    @classmethod
    def enforce_description_limit(cls, value: str | None) -> str | None:
        if value and len(value) > PINTEREST_PIN_DESCRIPTION_MAX_LENGTH:
            raise ValueError(
                f"Pinterest Pin description exceeds {PINTEREST_PIN_DESCRIPTION_MAX_LENGTH} characters"
            )
        return value

    @field_validator("link")
    @classmethod
    def validate_link(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("link mutlak HTTP(S) URL olmalı")
        return value


class PinterestUpdatePinPayload(BaseModel):
    """Documented mutable Pin fields; at least one field must be supplied."""

    model_config = ConfigDict(extra="forbid")

    board_id: str | None = None
    title: str | None = None
    description: str | None = None
    link: str | None = None
    alt_text: str | None = None
    media_source: PinterestImageUrlMediaSource | None = None

    @model_validator(mode="after")
    def require_update(self):
        if not self.model_dump(exclude_none=True):
            raise ValueError("en az bir Pin alanı verilmelidir")
        return self

    @field_validator("board_id", "title", "description", "alt_text")
    @classmethod
    def normalize_update_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("alan boş olamaz")
        return value

    @field_validator("title")
    @classmethod
    def enforce_update_title_limit(cls, value: str | None) -> str | None:
        if value is not None and len(value) > PINTEREST_PIN_TITLE_MAX_LENGTH:
            raise ValueError(f"Pinterest Pin title exceeds {PINTEREST_PIN_TITLE_MAX_LENGTH} characters")
        return value

    @field_validator("description")
    @classmethod
    def enforce_update_description_limit(cls, value: str | None) -> str | None:
        if value is not None and len(value) > PINTEREST_PIN_DESCRIPTION_MAX_LENGTH:
            raise ValueError(
                f"Pinterest Pin description exceeds {PINTEREST_PIN_DESCRIPTION_MAX_LENGTH} characters"
            )
        return value

    @field_validator("link")
    @classmethod
    def validate_link(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("link mutlak HTTP(S) URL olmalı")
        return value


@dataclass(frozen=True)
class PinterestPinResponse:
    id: str
    created_at: str | None = None
    board_id: str | None = None
    data: dict[str, Any] | None = None


# Organic metrics documented for the Pinterest v5 Pin analytics endpoint.
# Keep this request set explicit so no ad-only or undocumented metric is sent.
PINTEREST_ORGANIC_PIN_METRICS = (
    "IMPRESSION",
    "SAVE",
    "PIN_CLICK",
    "OUTBOUND_CLICK",
    "ENGAGEMENT",
    "ENGAGEMENT_RATE",
    "PIN_CLICK_RATE",
    "OUTBOUND_CLICK_RATE",
)

PINTEREST_ORGANIC_ACCOUNT_METRICS = (
    "PROFILE_VISIT",
    "FOLLOW",
    "TOTAL_AUDIENCE",
    "ENGAGED_AUDIENCE",
)


def pinterest_image_url(image_reference: str | None) -> str:
    """Resolve a local generated media path or accept a public HTTPS image URL."""
    if not image_reference:
        raise PinterestImageUrlError("Pinterest için yayınlanabilir görsel bulunamadı.")
    if image_reference.startswith("/media/generated/"):
        decoded_reference = unquote(image_reference)
        parts = decoded_reference.split("/")
        if (
            len(parts) != 4
            or not parts[-1]
            or ".." in parts
            or "." in parts
            or "\\" in decoded_reference
            or "?" in decoded_reference
            or "#" in decoded_reference
        ):
            raise PinterestImageUrlError("Yerel görsel yolu geçerli bir generated media dosyası değil.")
        from app.services.ai_image import PublicMediaUrlError, public_media_url

        try:
            return _validate_public_https_url(public_media_url(image_reference), "Görsel")
        except PublicMediaUrlError as exc:
            raise PinterestImageUrlError(str(exc)) from None
    if image_reference.startswith("/") or ":\\" in image_reference or image_reference.startswith("file:"):
        raise PinterestImageUrlError("Yerel dosya yolları Pinterest'e gönderilemez.")
    return _validate_public_https_url(image_reference, "Görsel")


class PinterestApiClient:
    """Small injectable API v5 HTTP client. It never retries automatically."""

    def __init__(
        self,
        access_token: str,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        http_client: httpx.Client | None = None,
    ):
        if not access_token or not access_token.strip():
            raise PinterestAuthenticationError("Pinterest access token bulunamadı.")
        self._access_token = access_token
        self.base_url = self._validated_base_url(base_url or settings.pinterest_api_base_url)
        self.timeout = timeout if timeout is not None else settings.pinterest_api_timeout_seconds
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise PinterestApiConfigurationError("Pinterest API timeout pozitif bir sayı olmalı.")
        self._client = http_client or httpx.Client()

    @staticmethod
    def _validated_base_url(value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in PINTEREST_API_HOSTS
            or parsed.path.rstrip("/") != "/v5"
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise PinterestApiConfigurationError(
                "Pinterest API base URL yalnızca resmi v5 production veya Sandbox adresi olabilir."
            )
        return value.rstrip("/")

    def _request(self, method: str, path: str, *, params=None, json_body=None) -> dict[str, Any] | None:
        try:
            response = self._client.request(
                method,
                f"{self.base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
        except httpx.TimeoutException:
            raise PinterestTemporaryError("Pinterest API isteği zaman aşımına uğradı.") from None
        except httpx.TransportError:
            raise PinterestTemporaryError("Pinterest API bağlantısı geçici olarak kurulamadı.") from None

        if response.status_code >= 400:
            self._raise_api_error(response, path)
        if response.status_code == 204 or not response.content:
            return None
        try:
            data = response.json()
        except ValueError:
            raise PinterestInvalidResponse("Pinterest API geçerli JSON yanıtı döndürmedi.") from None
        if not isinstance(data, dict):
            raise PinterestInvalidResponse("Pinterest API yanıt biçimi beklenen nesne değil.")
        return data

    def _raise_api_error(self, response: httpx.Response, path: str) -> None:
        body_text = ""
        error_code: str | None = None
        try:
            body = response.json()
            if isinstance(body, dict):
                message = body.get("message")
                error_code = str(body.get("code", "")) or None
                if isinstance(message, str):
                    body_text = message
        except ValueError:
            pass
        body_text = self._redact(body_text)[:500]
        logger.warning("Pinterest API error status=%s detail=%s", response.status_code, body_text or "no safe detail")
        status = response.status_code
        if status == 401:
            raise PinterestAuthenticationError("Pinterest erişim token'ı geçersiz veya süresi dolmuş.") from None
        if status == 403:
            raise PinterestPermissionError("Pinterest bu işlem için gerekli izni vermedi.") from None
        if status == 404:
            if path.startswith("/boards/"):
                raise PinterestBoardNotFound("Pinterest board bulunamadı.") from None
            if path.startswith("/pins/"):
                raise PinterestPinNotFound("Pinterest Pin bulunamadı.") from None
            raise PinterestResourceNotFound("Pinterest kaynağı bulunamadı.") from None
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            safe_retry = retry_after if retry_after and retry_after.isdigit() else None
            message = (
                f"Pinterest istek sınırına ulaşıldı. {safe_retry} saniye sonra tekrar deneyin."
                if safe_retry
                else "Pinterest istek sınırına ulaşıldı."
            )
            raise PinterestRateLimited(
                message, safe_retry
            ) from None
        if status >= 500:
            raise PinterestTemporaryError("Pinterest geçici sunucu hatası döndürdü.") from None
        if status in {400, 422}:
            lower = f"{body_text} {error_code or ''}".lower()
            if "media" in lower or "image" in lower or "source_type" in lower:
                raise PinterestInvalidMedia("Pinterest görsel kaynağını kabul etmedi.") from None
            raise PinterestInvalidPayload("Pinterest gönderilen Pin alanlarını kabul etmedi.") from None
        raise PinterestUnknownApiError(f"Pinterest API isteği reddetti (HTTP {status}).") from None

    def _redact(self, value: str) -> str:
        redacted = value.replace(self._access_token, "[REDACTED]") if self._access_token else value
        if settings.pinterest_client_secret:
            redacted = redacted.replace(settings.pinterest_client_secret, "[REDACTED]")
        redacted = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", redacted)
        redacted = re.sub(r"\bpin[arc][A-Za-z0-9._~-]{8,}", "[REDACTED_TOKEN]", redacted)
        return redacted

    def get_current_user(self) -> dict[str, Any]:
        return self._request("GET", "/user_account") or {}

    def list_boards(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        bookmark: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if bookmark:
                params["bookmark"] = bookmark
            page = self._request("GET", "/boards", params=params) or {}
            items = page.get("items", [])
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise PinterestInvalidResponse("Pinterest board listesi beklenen biçimde değil.")
            results.extend(items)
            bookmark = page.get("bookmark")
            if not bookmark:
                return results

    def get_board(self, board_id: str) -> dict[str, Any]:
        return self._request("GET", f"/boards/{_path_id(board_id)}") or {}

    def create_pin(self, payload: PinterestCreatePinPayload) -> PinterestPinResponse:
        data = self._request("POST", "/pins", json_body=payload.model_dump(exclude_none=True)) or {}
        external_id = data.get("id")
        created_at = data.get("created_at")
        if not isinstance(external_id, str) or not external_id.strip() or not isinstance(created_at, str):
            raise PinterestInvalidResponse("Pinterest Create Pin yanıtında Pin kimliği veya oluşturulma zamanı eksik.")
        return PinterestPinResponse(
            id=external_id,
            created_at=created_at,
            board_id=data.get("board_id") if isinstance(data.get("board_id"), str) else None,
            data=data,
        )

    def get_pin(self, pin_id: str) -> PinterestPinResponse:
        data = self._request("GET", f"/pins/{_path_id(pin_id)}") or {}
        external_id = data.get("id")
        if not isinstance(external_id, str) or not external_id.strip():
            raise PinterestInvalidResponse("Pinterest Get Pin yanıtında Pin kimliği eksik.")
        return PinterestPinResponse(
            id=external_id,
            created_at=data.get("created_at") if isinstance(data.get("created_at"), str) else None,
            board_id=data.get("board_id") if isinstance(data.get("board_id"), str) else None,
            data=data,
        )

    def get_pin_analytics(
        self,
        pin_id: str,
        start_date: date,
        end_date: date,
        *,
        metric_types: tuple[str, ...] = PINTEREST_ORGANIC_PIN_METRICS,
    ) -> dict[str, Any]:
        """Fetch one Pin's documented organic analytics for a UTC date range.

        Pinterest returns a map keyed by Pin identifier. The detailed response
        is left as JSON here; the analytics provider validates and normalizes it
        before it can reach ORM snapshots.
        """
        _validate_analytics_dates(start_date, end_date)
        if not metric_types or any(metric not in PINTEREST_ORGANIC_PIN_METRICS for metric in metric_types):
            raise PinterestInvalidPayload("Pinterest Pin analytics metric list is invalid.")
        return self._request(
            "GET",
            f"/pins/{_path_id(pin_id)}/analytics",
            params={
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "metric_types": ",".join(metric_types),
            },
        ) or {}

    def get_user_account_analytics(
        self,
        start_date: date,
        end_date: date,
        *,
        metric_types: tuple[str, ...] = PINTEREST_ORGANIC_ACCOUNT_METRICS,
    ) -> dict[str, Any]:
        """Fetch account analytics for the authenticated Pinterest user."""
        _validate_analytics_dates(start_date, end_date)
        if not metric_types or any(metric not in PINTEREST_ORGANIC_ACCOUNT_METRICS for metric in metric_types):
            raise PinterestInvalidPayload("Pinterest account analytics metric list is invalid.")
        return self._request(
            "GET",
            "/user_account/analytics",
            params={
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "metric_types": ",".join(metric_types),
            },
        ) or {}

    def delete_pin(self, pin_id: str) -> None:
        self._request("DELETE", f"/pins/{_path_id(pin_id)}")

    def update_pin(self, pin_id: str, payload: PinterestUpdatePinPayload) -> PinterestPinResponse:
        data = self._request(
            "PATCH", f"/pins/{_path_id(pin_id)}", json_body=payload.model_dump(exclude_none=True)
        ) or {}
        external_id = data.get("id")
        if not isinstance(external_id, str) or not external_id.strip():
            raise PinterestInvalidResponse("Pinterest Update Pin yanıtında Pin kimliği eksik.")
        return PinterestPinResponse(
            id=external_id,
            created_at=data.get("created_at") if isinstance(data.get("created_at"), str) else None,
            board_id=data.get("board_id") if isinstance(data.get("board_id"), str) else None,
            data=data,
        )


def _path_id(value: str) -> str:
    from urllib.parse import quote

    if not value or not value.strip():
        raise ValueError("Pinterest external ID cannot be empty")
    return quote(value.strip(), safe="")


def _validate_analytics_dates(start_date: date, end_date: date) -> None:
    if not isinstance(start_date, date) or isinstance(start_date, datetime):
        raise PinterestInvalidPayload("Pinterest analytics start_date must be a date.")
    if not isinstance(end_date, date) or isinstance(end_date, datetime):
        raise PinterestInvalidPayload("Pinterest analytics end_date must be a date.")
    if end_date < start_date or (end_date - start_date).days > 90:
        raise PinterestInvalidPayload("Pinterest analytics date range must be ordered and at most 90 days.")
    utc_today = datetime.now(timezone.utc).date()
    if start_date < utc_today - timedelta(days=90):
        raise PinterestInvalidPayload("Pinterest analytics start_date cannot be more than 90 days old.")


def validate_create_payload(data: dict[str, Any]) -> PinterestCreatePinPayload:
    try:
        return PinterestCreatePinPayload.model_validate(data)
    except (ValidationError, ValueError) as exc:
        detail = exc.errors()[0].get("msg", "invalid") if isinstance(exc, ValidationError) else "invalid"
        raise PinterestInvalidPayload(f"Pinterest Pin alanları geçersiz: {detail}") from None
