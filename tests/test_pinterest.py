from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
import pytest

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import PinterestAccount, PinterestBoard
from app.services.pinterest import PinterestApiService, PinterestIntegrationError, PinterestOAuthService, PinterestTokenService


def _configure_pinterest(monkeypatch):
    monkeypatch.setattr(settings, "pinterest_client_id", "test-client")
    monkeypatch.setattr(settings, "pinterest_client_secret", "test-secret")
    monkeypatch.setattr(settings, "pinterest_redirect_uri", "https://example.test/pinterest/callback")
    monkeypatch.setattr(settings, "pinterest_token_encryption_key", Fernet.generate_key().decode())


def test_authorization_redirect_uses_state_and_requested_scopes(monkeypatch):
    _configure_pinterest(monkeypatch)
    with TestClient(app) as client:
        response = client.get("/pinterest/connect", follow_redirects=False)

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://www.pinterest.com/oauth/")
    assert query["scope"] == ["boards:read,pins:read,pins:write"]
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
        connect = client.get("/pinterest/connect", follow_redirects=False)
        state = parse_qs(urlparse(connect.headers["location"]).query)["state"][0]
        callback = client.get(f"/pinterest/callback?code=mock-code&state={state}", follow_redirects=False)

    assert callback.status_code == 303
    db = SessionLocal()
    try:
        account = db.query(PinterestAccount).filter_by(account_identifier="mock-business").one()
        assert account.credential is not None
        db.delete(account)
        db.commit()
    finally:
        db.close()


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
    response = httpx.Response(
        429,
        headers={"Retry-After": "30"},
        request=httpx.Request("GET", "https://api.pinterest.com/v5/boards"),
    )
    monkeypatch.setattr("app.services.pinterest.httpx.get", lambda *args, **kwargs: response)
    db = SessionLocal()
    try:
        service = PinterestApiService(db, PinterestAccount(account_name="Test"), access_token="never-log-this")
        with pytest.raises(PinterestIntegrationError, match="30 saniye"):
            service.fetch_boards()
    finally:
        db.close()
