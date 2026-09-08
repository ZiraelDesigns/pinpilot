from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from urllib.parse import parse_qs, urlparse

from app.config import settings
from app.main import app
from app.models import EtsyAccount
from app.services.etsy import EtsyApiService, EtsyOAuthService, EtsyTokenService, _parse_price


def _configure_etsy(monkeypatch):
    monkeypatch.setattr(settings, "etsy_api_key", "test-key")
    monkeypatch.setattr(settings, "etsy_shared_secret", "test-secret")
    monkeypatch.setattr(settings, "etsy_redirect_uri", "https://example.test/etsy/callback")
    monkeypatch.setattr(settings, "etsy_token_encryption_key", Fernet.generate_key().decode())


def test_authorization_redirect_uses_pkce_and_minimal_scopes(monkeypatch):
    _configure_etsy(monkeypatch)
    with TestClient(app) as client:
        response = client.get("/etsy/connect", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://www.etsy.com/oauth/connect?")
    assert "scope=shops_r+listings_r" in response.headers["location"]
    assert "code_challenge_method=S256" in response.headers["location"]


def test_token_service_encrypts_tokens(monkeypatch):
    _configure_etsy(monkeypatch)
    with TestClient(app):
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            account = EtsyAccount(shop_name="Test shop", shop_identifier="999", is_active=True)
            db.add(account)
            db.flush()
            service = EtsyTokenService(db)
            credential = service.save(account, {"access_token": "999.access", "refresh_token": "999.refresh", "expires_in": 3600})
            db.commit()
            assert credential.access_token_encrypted != "999.access"
            assert service.access_token(account) == "999.access"
        finally:
            if "account" in locals():
                db.delete(account)
            db.commit()
            db.close()


def test_price_parser_supports_etsy_money():
    price, currency = _parse_price({"amount": 1299, "divisor": 100, "currency_code": "USD"})
    assert str(price) == "12.99"
    assert currency == "USD"


def test_callback_rejects_unknown_state():
    with TestClient(app) as client:
        response = client.get("/etsy/callback?code=code&state=invalid", follow_redirects=False)

    assert response.status_code == 303
    assert "etsy_error=" in response.headers["location"]


def test_callback_uses_mocked_etsy_responses_without_network(monkeypatch):
    _configure_etsy(monkeypatch)
    monkeypatch.setattr(
        EtsyOAuthService,
        "exchange_code",
        lambda *_: {"access_token": "123.access", "refresh_token": "123.refresh", "expires_in": 3600},
    )
    monkeypatch.setattr(EtsyApiService, "fetch_shop", lambda *_: {"shop_id": 456, "shop_name": "Mock Etsy Shop"})
    with TestClient(app) as client:
        connect = client.get("/etsy/connect", follow_redirects=False)
        state = parse_qs(urlparse(connect.headers["location"]).query)["state"][0]
        callback = client.get(f"/etsy/callback?code=mock-code&state={state}", follow_redirects=False)

    assert callback.status_code == 303
    assert "etsy_message=" in callback.headers["location"]
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        account = db.query(EtsyAccount).filter_by(shop_identifier="456").one()
        assert account.credential is not None
        db.delete(account)
        db.commit()
    finally:
        db.close()
