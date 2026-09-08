"""Read-only Etsy Open API v3 integration services."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.config import settings
from app.models import EtsyAccount, EtsyListing, EtsyOAuthCredential, EtsyOAuthState, Product

ETSY_AUTHORIZE_URL = "https://www.etsy.com/oauth/connect"
ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
ETSY_API_BASE_URL = "https://openapi.etsy.com/v3/application"
ETSY_SCOPES = ("shops_r", "listings_r")


class EtsyIntegrationError(Exception):
    """An expected Etsy setup, authorization, or upstream API error."""


class EtsyConfigurationError(EtsyIntegrationError):
    pass


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

    def __init__(self, db: Session, account: EtsyAccount, access_token: str | None = None):
        self.db = db
        self.account = account
        self._access_token = access_token
        self.tokens = EtsyTokenService(db)

    def _headers(self) -> dict[str, str]:
        api_key = _require(settings.etsy_api_key, "ETSY_API_KEY")
        secret = _require(settings.etsy_shared_secret, "ETSY_SHARED_SECRET")
        access_token = self._access_token or self.tokens.access_token(self.account)
        return {"x-api-key": f"{api_key}:{secret}", "Authorization": f"Bearer {access_token}"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = httpx.get(f"{ETSY_API_BASE_URL}{path}", headers=self._headers(), params=params, timeout=20)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise EtsyIntegrationError("Etsy verileri okunamadı. Bağlantıyı ve izinleri kontrol edin.") from exc
        except httpx.HTTPError as exc:
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
        if not self.account.shop_identifier:
            raise EtsyIntegrationError("Etsy mağaza bilgisi eksik. Lütfen hesabı yeniden bağlayın.")
        count = 0
        for data in self.fetch_active_listings(self.account.shop_identifier):
            listing_id = str(data["listing_id"])
            listing = self.db.query(EtsyListing).filter_by(listing_id=listing_id).one_or_none()
            if not listing:
                product = Product(title=data.get("title", "Etsy listing"))
                listing = EtsyListing(account=self.account, product=product, listing_id=listing_id, title=product.title, state="active")
                self.db.add(listing)
            images = self.fetch_listing_images(listing_id)
            price, currency = _parse_price(data.get("price"))
            listing.title = data.get("title", "Etsy listing")
            listing.description = data.get("description")
            listing.url = data.get("url")
            listing.price = price
            listing.currency = currency
            listing.quantity = data.get("quantity")
            listing.tags = data.get("tags") or []
            listing.images = images
            listing.state = data.get("state", "active")
            listing.product.title = listing.title
            listing.product.description = listing.description
            listing.product.url = listing.url
            listing.product.image_url = images[0] if images else None
            # Add existing Etsy mockups to the local Pin pool without any real AI calls.
            from app.services.ai_content import AIContentService

            pool_service = AIContentService(self.db)
            pool_service.ensure_mockup_creatives(listing.product, listing)
            pool_service.prepare_ai_generation_job(listing.product)
            count += 1
        self.db.commit()
        return count


def _parse_price(raw: Any) -> tuple[Decimal | None, str | None]:
    if not raw:
        return None, None
    if isinstance(raw, dict):
        amount, divisor = raw.get("amount"), raw.get("divisor", 1)
        return (Decimal(amount) / Decimal(divisor) if amount is not None else None, raw.get("currency_code"))
    return Decimal(str(raw)), None
