"""Read-only Etsy Open API v3 integration services."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.config import settings
from app.models import EtsyAccount, EtsyListing, EtsyOAuthCredential, EtsyOAuthState, EtsySyncRun, Pin, Product
from app.models.core import PinStatus

ETSY_AUTHORIZE_URL = "https://www.etsy.com/oauth/connect"
ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
ETSY_API_BASE_URL = "https://openapi.etsy.com/v3/application"
ETSY_SCOPES = ("shops_r", "listings_r")


class EtsyIntegrationError(Exception):
    """An expected Etsy setup, authorization, or upstream API error."""


class EtsyConfigurationError(EtsyIntegrationError):
    pass


@dataclass(frozen=True)
class EtsySyncResult:
    processed_listings: int
    new_products: int
    changed_products: int
    new_mockup_creatives: int
    inactive_listings: int


def _require(value: str | None, label: str) -> str:
    if not value:
        raise EtsyConfigurationError(f"Etsy bağlantısı için {label} yapılandırılmamış.")
    return value


class EtsyTokenService:
    """Encrypt, persist, and refresh tokens without returning token values to routes."""

    def __init__(self, db: Session):
        self.db = db

    @property
    def _fernet(self) -> Fernet:
        key = _require(settings.etsy_token_encryption_key, "ETSY_TOKEN_ENCRYPTION_KEY")
        try:
            return Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise EtsyConfigurationError("ETSY_TOKEN_ENCRYPTION_KEY geçerli bir Fernet anahtarı değil.") from exc

    def save(self, account: EtsyAccount, token_data: dict[str, Any]) -> EtsyOAuthCredential:
        try:
            access_token = token_data["access_token"]
            refresh_token = token_data["refresh_token"]
        except KeyError as exc:
            raise EtsyIntegrationError("Etsy yetkilendirmesi geçerli token bilgisi döndürmedi.") from exc
        credential = account.credential or EtsyOAuthCredential(account=account)
        credential.access_token_encrypted = self._fernet.encrypt(access_token.encode()).decode()
        credential.refresh_token_encrypted = self._fernet.encrypt(refresh_token.encode()).decode()
        credential.expires_at = datetime.utcnow() + timedelta(seconds=int(token_data.get("expires_in", 3600)))
        credential.scopes = token_data.get("scope", " ".join(ETSY_SCOPES))
        self.db.add(credential)
        return credential

    def access_token(self, account: EtsyAccount) -> str:
        credential = account.credential
        if not credential:
            raise EtsyIntegrationError("Bu Etsy hesabı için yetkilendirme bilgisi bulunamadı.")
        if credential.expires_at <= datetime.utcnow() + timedelta(minutes=2):
            self.refresh(account)
        try:
            return self._fernet.decrypt(credential.access_token_encrypted.encode()).decode()
        except InvalidToken as exc:
            raise EtsyIntegrationError("Saklanan Etsy erişim bilgisi okunamadı. Lütfen yeniden bağlanın.") from exc

    def refresh(self, account: EtsyAccount) -> None:
        credential = account.credential
        if not credential:
            raise EtsyIntegrationError("Yenilenecek Etsy yetkilendirmesi bulunamadı.")
        try:
            refresh_token = self._fernet.decrypt(credential.refresh_token_encrypted.encode()).decode()
        except InvalidToken as exc:
            raise EtsyIntegrationError("Saklanan Etsy yenileme bilgisi okunamadı. Lütfen yeniden bağlanın.") from exc
        data = EtsyOAuthService(self.db).request_token(
            {"grant_type": "refresh_token", "client_id": _require(settings.etsy_api_key, "ETSY_API_KEY"), "refresh_token": refresh_token}
        )
        self.save(account, data)
        self.db.commit()


class EtsyOAuthService:
    def __init__(self, db: Session):
        self.db = db

    def authorization_url(self) -> str:
        api_key = _require(settings.etsy_api_key, "ETSY_API_KEY")
        redirect_uri = _require(settings.etsy_redirect_uri, "ETSY_REDIRECT_URI")
        _require(settings.etsy_shared_secret, "ETSY_SHARED_SECRET")
        _require(settings.etsy_token_encryption_key, "ETSY_TOKEN_ENCRYPTION_KEY")
        verifier = secrets.token_urlsafe(64)[:128]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)
        self.db.add(EtsyOAuthState(state=state, code_verifier=verifier, expires_at=datetime.utcnow() + timedelta(minutes=10)))
        self.db.commit()
        return f"{ETSY_AUTHORIZE_URL}?{urlencode({'response_type': 'code', 'client_id': api_key, 'redirect_uri': redirect_uri, 'scope': ' '.join(ETSY_SCOPES), 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})}"

    def consume_state(self, state: str) -> EtsyOAuthState:
        oauth_state = self.db.query(EtsyOAuthState).filter_by(state=state).one_or_none()
        if not oauth_state or oauth_state.consumed_at or oauth_state.expires_at < datetime.utcnow():
            raise EtsyIntegrationError("Etsy bağlantı isteği geçersiz veya süresi dolmuş. Lütfen yeniden deneyin.")
        oauth_state.consumed_at = datetime.utcnow()
        self.db.commit()
        return oauth_state

    def request_token(self, payload: dict[str, str]) -> dict[str, Any]:
        try:
            response = httpx.post(ETSY_TOKEN_URL, data=payload, timeout=15)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EtsyIntegrationError("Etsy yetkilendirmesi tamamlanamadı. Lütfen tekrar deneyin.") from exc

    def exchange_code(self, code: str, verifier: str) -> dict[str, Any]:
        return self.request_token({
            "grant_type": "authorization_code",
            "client_id": _require(settings.etsy_api_key, "ETSY_API_KEY"),
            "redirect_uri": _require(settings.etsy_redirect_uri, "ETSY_REDIRECT_URI"),
            "code": code,
            "code_verifier": verifier,
        })


class EtsyApiService:
    """Only GET requests to Etsy are implemented in this service."""

    def __init__(
        self, db: Session, account: EtsyAccount, access_token: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.db = db
        self.account = account
        self._access_token = access_token
        self.tokens = EtsyTokenService(db)
        self._sleep = sleep

    def _headers(self) -> dict[str, str]:
        api_key = _require(settings.etsy_api_key, "ETSY_API_KEY")
        secret = _require(settings.etsy_shared_secret, "ETSY_SHARED_SECRET")
        access_token = self._access_token or self.tokens.access_token(self.account)
        return {"x-api-key": f"{api_key}:{secret}", "Authorization": f"Bearer {access_token}"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read Etsy safely, retrying only rate-limit and transient upstream failures."""
        for attempt in range(3):
            try:
                response = httpx.get(
                    f"{ETSY_API_BASE_URL}{path}", headers=self._headers(), params=params, timeout=20
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in (429, 500, 502, 503, 504) and attempt < 2:
                    retry_after = exc.response.headers.get("Retry-After")
                    delay = min(float(retry_after), 30.0) if retry_after and retry_after.isdigit() else float(2 ** attempt)
                    self._sleep(delay)
                    continue
                if status == 429:
                    raise EtsyIntegrationError("Etsy istek sınırına ulaşıldı. Daha sonra tekrar deneyin.") from exc
                if status in (401, 403):
                    raise EtsyIntegrationError("Etsy yetkilendirmesi geçersiz. Lütfen hesabı yeniden bağlayın.") from exc
                raise EtsyIntegrationError("Etsy verileri okunamadı. Bağlantıyı ve izinleri kontrol edin.") from exc
            except httpx.HTTPError as exc:
                if attempt < 2:
                    self._sleep(float(2 ** attempt))
                    continue
                raise EtsyIntegrationError("Etsy'ye şu anda ulaşılamıyor. Lütfen daha sonra tekrar deneyin.") from exc

    def fetch_shop(self, user_id: str) -> dict[str, Any]:
        return self._get(f"/users/{user_id}/shops")

    def fetch_active_listings(self, shop_id: str) -> list[dict[str, Any]]:
        listings: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._get(f"/shops/{shop_id}/listings/active", {"limit": 100, "offset": offset})
            results = page.get("results", [])
            listings.extend(results)
            if len(results) < 100:
                return listings
            offset += len(results)

    def fetch_listing_images(self, listing_id: str) -> list[str]:
        result = self._get(f"/listings/{listing_id}/images")
        return [image["url_fullxfull"] for image in result.get("results", []) if image.get("url_fullxfull")]

    def sync_active_listings(self) -> int:
        """Backward-compatible count used by the manual dashboard route."""
        return self.sync().processed_listings

    def sync(self) -> EtsySyncResult:
        """Synchronize one shop without provider calls or destructive record deletion."""
        if not self.account.shop_identifier:
            raise EtsyIntegrationError("Etsy mağaza bilgisi eksik. Lütfen hesabı yeniden bağlayın.")
        run = EtsySyncRun(account=self.account, status="running")
        self.db.add(run)
        self.db.commit()
        try:
            result = self._sync_active_listings()
            run.status = "success"
            run.finished_at = datetime.utcnow()
            run.processed_listings = result.processed_listings
            run.new_products = result.new_products
            run.changed_products = result.changed_products
            run.new_mockup_creatives = result.new_mockup_creatives
            run.inactive_listings = result.inactive_listings
            self.db.commit()
            return result
        except EtsyIntegrationError as exc:
            self.db.rollback()
            run.status = "failed"
            run.finished_at = datetime.utcnow()
            run.error_message = str(exc)[:1000]
            self.db.add(run)
            self.db.commit()
            raise
        except Exception:
            self.db.rollback()
            run.status = "failed"
            run.finished_at = datetime.utcnow()
            run.error_message = "Etsy sync beklenmeyen bir hata nedeniyle tamamlanamadı."
            self.db.add(run)
            self.db.commit()
            raise

    def _sync_active_listings(self) -> EtsySyncResult:
        from app.services.ai_content import AIContentService

        active_data = self.fetch_active_listings(self.account.shop_identifier or "")
        seen_listing_ids: set[str] = set()
        processed = new_products = changed_products = new_mockups = 0
        pool_service = AIContentService(self.db)

        for data in active_data:
            listing_id = str(data["listing_id"])
            seen_listing_ids.add(listing_id)
            listing = self.db.query(EtsyListing).filter_by(listing_id=listing_id).one_or_none()
            is_new = listing is None
            if not listing:
                product = Product(title=data.get("title") or "Etsy listing")
                listing = EtsyListing(
                    account=self.account, product=product, listing_id=listing_id,
                    title=product.title, state="active",
                )
                self.db.add(listing)
                self.db.flush()

            images = _unique_strings(self.fetch_listing_images(listing_id))
            price, currency = _parse_price(data.get("price"))
            incoming = {
                "title": data.get("title") or "Etsy listing",
                "description": data.get("description"), "url": data.get("url"),
                "price": price, "currency": currency, "quantity": data.get("quantity"),
                "tags": _unique_strings(data.get("tags") or []), "images": images,
                "state": data.get("state") or "active",
            }
            changed_fields = {
                field for field, value in incoming.items()
                if getattr(listing, field) != value
            }
            for field, value in incoming.items():
                setattr(listing, field, value)
            product = listing.product
            if product:
                product.title = listing.title
                product.description = listing.description
                product.url = listing.url
                product.image_url = images[0] if images else None
            self.db.flush()
            created_mockups = pool_service.ensure_mockup_creatives(product, listing) if product else []
            new_mockups += len(created_mockups)
            if product and (is_new or changed_fields - {"images"}):
                pool_service.prepare_ai_generation_job(product)
            if is_new:
                new_products += 1
            elif changed_fields:
                changed_products += 1
            processed += 1

        inactive_count = self._mark_missing_listings_inactive(seen_listing_ids)
        return EtsySyncResult(processed, new_products, changed_products, new_mockups, inactive_count)

    def _mark_missing_listings_inactive(self, seen_listing_ids: set[str]) -> int:
        """Keep historical records while preventing unpublished queue items from going live."""
        stale = self.db.query(EtsyListing).filter(
            EtsyListing.account_id == self.account.id,
            EtsyListing.state != "inactive",
            EtsyListing.listing_id.not_in(seen_listing_ids) if seen_listing_ids else True,
        ).all()
        for listing in stale:
            listing.state = "inactive"
            if listing.product_id:
                self.db.query(Pin).filter(
                    Pin.product_id == listing.product_id,
                    Pin.status == PinStatus.SCHEDULED.value,
                ).update({"status": PinStatus.CANCELLED.value, "scheduled_for": None}, synchronize_session=False)
        return len(stale)


def _parse_price(raw: Any) -> tuple[Decimal | None, str | None]:
    if not raw:
        return None, None
    if isinstance(raw, dict):
        amount, divisor = raw.get("amount"), raw.get("divisor", 1)
        return (Decimal(amount) / Decimal(divisor) if amount is not None else None, raw.get("currency_code"))
    return Decimal(str(raw)), None


def _unique_strings(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip()))
