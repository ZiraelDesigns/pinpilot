"""Pinterest API v5 OAuth and read-only account/board services."""

from __future__ import annotations

import base64
import secrets
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PinterestAccount, PinterestBoard, PinterestOAuthCredential, PinterestOAuthState
from app.services.pinterest_api import PinterestApiClient, PinterestIntegrationError

PINTEREST_AUTHORIZE_URL = "https://www.pinterest.com/oauth/"
PINTEREST_TOKEN_URL = "https://api.pinterest.com/v5/oauth/token"
# Explicitly limited to the scopes requested for the planned PinPilot workflow.
PINTEREST_SCOPES = ("user_accounts:read", "boards:read", "pins:read", "pins:write")


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
        refresh_expires_at = token_data.get("refresh_token_expires_at")
        if refresh_expires_at:
            credential.refresh_expires_at = datetime.utcfromtimestamp(float(refresh_expires_at))
        else:
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
    """Compatibility facade over the injectable, provider-neutral HTTP adapter."""

    def __init__(
        self,
        db: Session,
        account: PinterestAccount,
        access_token: str | None = None,
        *,
        api_client: PinterestApiClient | None = None,
    ):
        self.db = db
        self.account = account
        self._access_token = access_token
        self.tokens = PinterestTokenService(db)
        self.api_client = api_client

    def _client(self) -> PinterestApiClient:
        if self.api_client is None:
            token = self._access_token or self.tokens.access_token(self.account)
            self.api_client = PinterestApiClient(token)
        return self.api_client

    def fetch_account(self) -> dict[str, Any]:
        return self._client().get_current_user()

    def fetch_boards(self) -> list[dict[str, Any]]:
        return self._client().list_boards()

    def fetch_board(self, board_id: str) -> dict[str, Any]:
        return self._client().get_board(board_id)

    def fetch_pin_analytics(
        self,
        pin_id: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, Any]:
        return self._client().get_pin_analytics(pin_id, start_date, end_date)

    def fetch_account_analytics(
        self,
        start_date: date,
        end_date: date,
    ) -> dict[str, Any]:
        return self._client().get_user_account_analytics(start_date, end_date)

    def sync_boards(self) -> int:
        from app.services.board_intelligence import (
            BOARD_METADATA_VERSION,
            BOARD_SOURCE_API,
            ensure_board_seo_profiles,
        )

        count = 0
        fetched_at = datetime.utcnow()
        existing_boards = {
            board.board_id: board
            for board in self.db.query(PinterestBoard).filter_by(account_id=self.account.id).all()
        }
        touched_boards: dict[str, PinterestBoard] = {}
        for data in self.fetch_boards():
            external_id = data.get("id")
            name = data.get("name")
            if not isinstance(external_id, (str, int)) or not str(external_id).strip():
                continue
            if not isinstance(name, str) or not name.strip():
                continue
            board_id = str(external_id)
            if board_id in touched_boards:
                continue
            board = existing_boards.get(board_id)
            if not board:
                board = PinterestBoard(
                    account=self.account,
                    board_id=board_id,
                    name=name.strip(),
                )
                self.db.add(board)
                existing_boards[board_id] = board
            board.name = name.strip()
            board.description = data.get("description") if isinstance(data.get("description"), str) else None
            board.privacy = data.get("privacy") if isinstance(data.get("privacy"), str) else None
            board.source = BOARD_SOURCE_API
            board.fetched_at = fetched_at
            board.metadata_version = BOARD_METADATA_VERSION
            board.updated_at = fetched_at
            touched_boards[board_id] = board
            count += 1
        self.db.flush()
        ensure_board_seo_profiles(self.db, list(touched_boards.values()))
        self.db.commit()
        return count
