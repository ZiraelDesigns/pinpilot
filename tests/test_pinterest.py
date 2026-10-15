from urllib.parse import parse_qs, urlparse
from datetime import datetime, timedelta
from html import unescape

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
import pytest

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import PinterestAccount, PinterestBoard
from app.services.pinterest import PinterestApiService, PinterestIntegrationError, PinterestOAuthService, PinterestTokenService
from app.services.pinterest_api import PinterestApiClient, PinterestRateLimited


def _configure_pinterest(monkeypatch):
    monkeypatch.setattr(settings, "pinterest_client_id", "test-client")
    monkeypatch.setattr(settings, "pinterest_client_secret", "test-secret")
    monkeypatch.setattr(settings, "pinterest_redirect_uri", "https://example.test/pinterest/callback")
    monkeypatch.setattr(settings, "pinterest_token_encryption_key", Fernet.generate_key().decode())


def test_authorization_redirect_uses_state_and_requested_scopes(monkeypatch):
    _configure_pinterest(monkeypatch)
    with TestClient(app) as client:
        response = client.post("/pinterest/connect", follow_redirects=False)

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://www.pinterest.com/oauth/")
    assert query["scope"] == ["user_accounts:read,boards:read,pins:read,pins:write"]
    assert len(query["state"][0]) >= 32


def test_token_service_encrypts_pinterest_tokens(monkeypatch):
    _configure_pinterest(monkeypatch)
    with TestClient(app):
        db = SessionLocal()
        try:
            account = PinterestAccount(account_name="Test", account_identifier="test-account", is_active=True)
            db.add(account)
            db.flush()
            credential = PinterestTokenService(db).save(
                account, {"access_token": "pina_access", "refresh_token": "pinr_refresh", "expires_in": 2592000}
            )
            db.commit()
            assert credential.access_token_encrypted != "pina_access"
            assert PinterestTokenService(db).access_token(account) == "pina_access"
        finally:
            if "account" in locals():
                db.delete(account)
            db.commit()
            db.close()


def test_token_refresh_uses_existing_encrypted_refresh_credential_and_refresh_expiry(monkeypatch):
    _configure_pinterest(monkeypatch)
    requested = {}
    refresh_expiry = datetime.utcnow() + timedelta(days=55)

    def mocked_refresh(_service, payload):
        requested.update(payload)
        return {
            "access_token": "pina_refreshed_access",
            "refresh_token": "pinr_rotated_refresh",
            "expires_in": 2592000,
            "refresh_token_expires_at": refresh_expiry.timestamp(),
        }

    monkeypatch.setattr(PinterestOAuthService, "request_token", mocked_refresh)
    db = SessionLocal()
    account = PinterestAccount(account_name="Refresh", account_identifier="refresh-test", is_active=True)
    db.add(account)
    db.flush()
    PinterestTokenService(db).save(
        account,
        {"access_token": "pina_old_access", "refresh_token": "pinr_old_refresh", "expires_in": 1},
    )
    db.commit()
    try:
        token = PinterestTokenService(db).access_token(account)
        assert token == "pina_refreshed_access"
        assert requested["grant_type"] == "refresh_token"
        assert requested["refresh_token"] == "pinr_old_refresh"
        assert account.credential.refresh_expires_at == datetime.utcfromtimestamp(refresh_expiry.timestamp())
        assert account.credential.refresh_token_encrypted != "pinr_rotated_refresh"
    finally:
        db.delete(account)
        db.commit()
        db.close()


def test_callback_uses_mocked_pinterest_responses_without_network(monkeypatch):
    _configure_pinterest(monkeypatch)
    monkeypatch.setattr(
        PinterestOAuthService,
        "exchange_code",
        lambda *_: {"access_token": "pina_access", "refresh_token": "pinr_refresh", "expires_in": 2592000},
    )
    monkeypatch.setattr(
        PinterestApiService,
        "fetch_account",
        lambda *_: {"username": "mock-business", "business_name": "Mock Business"},
    )
    with TestClient(app) as client:
        connect = client.post("/pinterest/connect", follow_redirects=False)
        state = parse_qs(urlparse(connect.headers["location"]).query)["state"][0]
        callback = client.get(f"/pinterest/callback?code=mock-code&state={state}", follow_redirects=False)

    assert callback.status_code == 303
    callback_location = urlparse(callback.headers["location"])
    callback_message = parse_qs(callback_location.query)["pinterest_message"][0]
    assert callback_location.path == "/dashboard"
    db = SessionLocal()
    try:
        account = db.query(PinterestAccount).filter_by(account_identifier="mock-business").one()
        assert account.credential is not None
        db.delete(account)
        db.commit()
    finally:
        db.close()
    with TestClient(app) as client:
        assert callback_message in unescape(client.get(callback.headers["location"]).text)


def test_pinterest_connection_sync_and_callback_results_redirect_to_dashboard(monkeypatch):
    _configure_pinterest(monkeypatch)

    def unavailable_authorization_url(*_args):
        raise PinterestIntegrationError("Pinterest OAuth unavailable & retry")

    monkeypatch.setattr(PinterestOAuthService, "authorization_url", unavailable_authorization_url)
    with TestClient(app) as client:
        connection_error = client.post("/pinterest/connect", follow_redirects=False)
        error_location = urlparse(connection_error.headers["location"])
        error_message = parse_qs(error_location.query)["pinterest_error"][0]
        assert connection_error.status_code == 303
        assert error_location.path == "/dashboard"
        assert error_message == "Pinterest OAuth unavailable & retry"
        assert error_message in unescape(client.get(connection_error.headers["location"]).text)

        missing_account = client.post("/pinterest/boards/sync", follow_redirects=False)
        missing_location = urlparse(missing_account.headers["location"])
        missing_message = parse_qs(missing_location.query)["pinterest_error"][0]
        assert missing_account.status_code == 303
        assert missing_location.path == "/dashboard"
        assert missing_message == "Önce bir Pinterest hesabı bağlayın."
        assert missing_message in unescape(client.get(missing_account.headers["location"]).text)

        from app.database import SessionLocal

        with SessionLocal() as db:
            db.add(PinterestAccount(
                account_name="Sync test", account_identifier="sync-test", is_active=True,
            ))
            db.commit()
        monkeypatch.setattr(PinterestApiService, "sync_boards", lambda *_: 4)
        sync_success = client.post("/pinterest/boards/sync", follow_redirects=False)
        success_location = urlparse(sync_success.headers["location"])
        success_message = parse_qs(success_location.query)["pinterest_message"][0]
        assert sync_success.status_code == 303
        assert success_location.path == "/dashboard"
        assert success_message == "4 Pinterest board eşitlendi."
        assert success_message in unescape(client.get(sync_success.headers["location"]).text)

        callback_error = client.get(
            "/pinterest/callback?error=access_denied&error_description=Review+cancelled",
            follow_redirects=False,
        )
        callback_location = urlparse(callback_error.headers["location"])
        callback_message = parse_qs(callback_location.query)["pinterest_error"][0]
        assert callback_error.status_code == 303
        assert callback_location.path == "/dashboard"
        assert callback_message == "Review cancelled"
        assert callback_message in unescape(client.get(callback_error.headers["location"]).text)


def test_local_boards_endpoint_returns_board_id_and_name():
    with TestClient(app):
        db = SessionLocal()
        account = PinterestAccount(account_name="Board test", account_identifier="board-test", is_active=True)
        board = PinterestBoard(account=account, board_id="12345", name="Ideas")
        db.add_all([account, board])
        db.commit()
        try:
            with TestClient(app) as client:
                response = client.get("/pinterest/boards")
            assert response.status_code == 200
            assert response.json()["items"] == [{"id": "12345", "name": "Ideas"}]
        finally:
            db.delete(account)
            db.commit()
            db.close()


def test_rate_limit_error_includes_retry_after_without_exposing_token(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "30"}))
    db = SessionLocal()
    try:
        api_client = PinterestApiClient(
            "never-log-this", http_client=httpx.Client(transport=transport)
        )
        service = PinterestApiService(db, PinterestAccount(account_name="Test"), api_client=api_client)
        with pytest.raises(PinterestIntegrationError, match="30 saniye"):
            service.fetch_boards()
    finally:
        db.close()
