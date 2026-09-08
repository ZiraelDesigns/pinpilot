"""Pinterest API v5 OAuth and read-only account/board services."""

from __future__ import annotations

import base64
import secrets
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PinterestAccount, PinterestBoard, PinterestOAuthCredential, PinterestOAuthState

PINTEREST_AUTHORIZE_URL = "https://www.pinterest.com/oauth/"
PINTEREST_TOKEN_URL = "https://api.pinterest.com/v5/oauth/token"
PINTEREST_API_BASE_URL = "https://api.pinterest.com/v5"
# Explicitly limited to the scopes requested for the planned PinPilot workflow.
PINTEREST_SCOPES = ("boards:read", "pins:read", "pins:write")


class PinterestIntegrationError(Exception):
    """A safe, user-facing error from Pinterest setup or the upstream API."""


class PinterestConfigurationError(PinterestIntegrationError):
    pass


def _require(value: str | None, label: str) -> str:
    if not value:
        raise PinterestConfigurationError(f"Pinterest bağlantısı için {label} yapılandırılmamış.")
    return value


class PinterestTokenService:
    """Encrypt and renew Pinterest tokens without exposing them to routes or logs."""

    def __init__(self, db: Session):
        self.db = db

    @property
    def _fernet(self) -> Fernet:
        key = _require(settings.pinterest_token_encryption_key, "PINTEREST_TOKEN_ENCRYPTION_KEY")
        try:
            return Fernet(key.encode())
        except (TypeError, ValueError) as exc:
            raise PinterestConfigurationError("PINTEREST_TOKEN_ENCRYPTION_KEY geçerli bir Fernet anahtarı değil.") from exc

    def save(self, account: PinterestAccount, token_data: dict[str, Any]) -> PinterestOAuthCredential:
        try:
            access_token = token_data["access_token"]
            refresh_token = token_data["refresh_token"]
        except KeyError as exc:
            raise PinterestIntegrationError("Pinterest geçerli token bilgisi döndürmedi.") from exc
        credential = account.credential or PinterestOAuthCredential(account=account)
        credential.access_token_encrypted = self._fernet.encrypt(access_token.encode()).decode()
        credential.refresh_token_encrypted = self._fernet.encrypt(refresh_token.encode()).decode()
        credential.expires_at = datetime.utcnow() + timedelta(seconds=int(token_data.get("expires_in", 2592000)))
        refresh_lifetime = token_data.get("refresh_token_expires_in")
        credential.refresh_expires_at = (
            datetime.utcnow() + timedelta(seconds=int(refresh_lifetime)) if refresh_lifetime else None
        )
        credential.scopes = token_data.get("scope", " ".join(PINTEREST_SCOPES))
        self.db.add(credential)
        return credential

    def access_token(self, account: PinterestAccount) -> str:
        credential = account.credential
        if not credential:
            raise PinterestIntegrationError("Bu Pinterest hesabı için yetkilendirme bilgisi bulunamadı.")
        if credential.expires_at <= datetime.utcnow() + timedelta(days=1):
            self.refresh(account)
        try:
            return self._fernet.decrypt(credential.access_token_encrypted.encode()).decode()
        except InvalidToken as exc:
            raise PinterestIntegrationError("Saklanan Pinterest erişim bilgisi okunamadı. Lütfen yeniden bağlanın.") from exc

    def refresh(self, account: PinterestAccount) -> None:
        credential = account.credential
        if not credential:
            raise PinterestIntegrationError("Yenilenecek Pinterest yetkilendirmesi bulunamadı.")
        try:
            refresh_token = self._fernet.decrypt(credential.refresh_token_encrypted.encode()).decode()
        except InvalidToken as exc:
            raise PinterestIntegrationError("Saklanan Pinterest yenileme bilgisi okunamadı. Lütfen yeniden bağlanın.") from exc
        data = PinterestOAuthService(self.db).request_token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token, "scope": ",".join(PINTEREST_SCOPES)}
        )
        self.save(account, data)
        self.db.commit()


class PinterestOAuthService:
    def __init__(self, db: Session):
        self.db = db

    def authorization_url(self) -> str:
        client_id = _require(settings.pinterest_client_id, "PINTEREST_CLIENT_ID")
        redirect_uri = _require(settings.pinterest_redirect_uri, "PINTEREST_REDIRECT_URI")
        _require(settings.pinterest_client_secret, "PINTEREST_CLIENT_SECRET")
        _require(settings.pinterest_token_encryption_key, "PINTEREST_TOKEN_ENCRYPTION_KEY")
        state = secrets.token_urlsafe(32)
        self.db.add(PinterestOAuthState(state=state, expires_at=datetime.utcnow() + timedelta(minutes=10)))
        self.db.commit()
        parameters = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": ",".join(PINTEREST_SCOPES),
            "state": state,
        }
        return f"{PINTEREST_AUTHORIZE_URL}?{urlencode(parameters)}"

    def consume_state(self, state: str) -> PinterestOAuthState:
        saved_state = self.db.query(PinterestOAuthState).filter_by(state=state).one_or_none()
        if not saved_state or saved_state.consumed_at or saved_state.expires_at < datetime.utcnow():
            raise PinterestIntegrationError("Pinterest bağlantı isteği geçersiz veya süresi dolmuş. Lütfen yeniden deneyin.")
        saved_state.consumed_at = datetime.utcnow()
        self.db.commit()
        return saved_state

    def _basic_header(self) -> str:
        client_id = _require(settings.pinterest_client_id, "PINTEREST_CLIENT_ID")
        secret = _require(settings.pinterest_client_secret, "PINTEREST_CLIENT_SECRET")
        return "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()

    def request_token(self, payload: dict[str, str]) -> dict[str, Any]:
        try:
            response = httpx.post(
                PINTEREST_TOKEN_URL,
                data=payload,
                headers={"Authorization": self._basic_header(), "Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise PinterestIntegrationError("Pinterest yetkilendirmesi tamamlanamadı. Uygulama bilgilerini ve izinleri kontrol edin.") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise PinterestIntegrationError("Pinterest yetkilendirme servisine ulaşılamadı. Lütfen tekrar deneyin.") from exc

    def exchange_code(self, code: str) -> dict[str, Any]:
        return self.request_token({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _require(settings.pinterest_redirect_uri, "PINTEREST_REDIRECT_URI"),
            "continuous_refresh": "true",
        })


class PinterestApiService:
    """Only GET requests are available. Pin creation/publishing is intentionally absent."""

    def __init__(self, db: Session, account: PinterestAccount, access_token: str | None = None):
        self.db = db
        self.account = account
        self._access_token = access_token
        self.tokens = PinterestTokenService(db)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._access_token or self.tokens.access_token(self.account)
        try:
            response = httpx.get(
                f"{PINTEREST_API_BASE_URL}{path}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                params=params,
                timeout=20,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                retry_after = exc.response.headers.get("Retry-After")
                suffix = f" {retry_after} saniye sonra tekrar deneyin." if retry_after else " Lütfen daha sonra tekrar deneyin."
                raise PinterestIntegrationError("Pinterest istek sınırına ulaşıldı." + suffix) from exc
            if exc.response.status_code == 401:
                raise PinterestIntegrationError("Pinterest yetkilendirmesi geçersiz. Lütfen hesabı yeniden bağlayın.") from exc
            if exc.response.status_code == 403:
                raise PinterestIntegrationError("Pinterest bu işlem için gerekli izni vermedi.") from exc
            raise PinterestIntegrationError("Pinterest verileri okunamadı. Lütfen daha sonra tekrar deneyin.") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise PinterestIntegrationError("Pinterest'e şu anda ulaşılamıyor. Lütfen daha sonra tekrar deneyin.") from exc

    def fetch_account(self) -> dict[str, Any]:
        return self._get("/user_account")

    def fetch_boards(self) -> list[dict[str, Any]]:
        boards: list[dict[str, Any]] = []
        bookmark: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if bookmark:
                params["bookmark"] = bookmark
            page = self._get("/boards", params)
            boards.extend(page.get("items", []))
            bookmark = page.get("bookmark")
            if not bookmark:
                return boards

    def sync_boards(self) -> int:
        count = 0
        for data in self.fetch_boards():
            board_id = str(data["id"])
            board = self.db.query(PinterestBoard).filter_by(board_id=board_id).one_or_none()
            if not board:
                board = PinterestBoard(account=self.account, board_id=board_id, name=data.get("name", "Pinterest board"))
                self.db.add(board)
            board.name = data.get("name", "Pinterest board")
            board.description = data.get("description")
            board.privacy = data.get("privacy")
            count += 1
        self.db.commit()
        return count
