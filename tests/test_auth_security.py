import re

from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from app.config import settings
from app.main import app
from app.security import SESSION_COOKIE, SESSION_SALT, require_admin_csrf


def _strict_auth(monkeypatch):
    app.dependency_overrides.pop(require_admin_csrf, None)
    monkeypatch.setattr(settings, "app_auth_username", "owner")
    monkeypatch.setattr(settings, "app_auth_password", "a-long-test-password-123")
    monkeypatch.setattr(settings, "app_session_secret_key", "s" * 48)
    monkeypatch.setattr(settings, "app_session_ttl_seconds", 28800)
    monkeypatch.setattr(settings, "app_session_cookie_secure", True)


def _login(client, *, password="a-long-test-password-123"):
    page = client.get("/auth/login")
    assert page.status_code == 200
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/auth/login",
        data={"username": "owner", "password": password, "_csrf": csrf},
        follow_redirects=False,
    )
    return response


def test_unauthenticated_mutations_are_rejected_but_read_only_gets_remain_open(monkeypatch):
    _strict_auth(monkeypatch)
    with TestClient(app, base_url="https://testserver") as client:
        requests = [
            client.post("/pipeline/toggle", json={"enabled": False}),
            client.post("/creatives/generate", json={"product_id": 1, "creative_type": "product_focus", "desired_count": 1}),
            client.patch("/creatives/1", json={"title": "x", "description": "x", "keywords": ["x"], "call_to_action": "x"}),
            client.post("/creatives/1/approve"),
            client.post("/creatives/1/delete"),
            client.post("/pinterest/connect"),
            client.post("/pinterest/boards/sync"),
            client.post("/pinterest/disconnect"),
            client.post("/etsy/connect"),
            client.post("/etsy/sync"),
            client.post("/etsy/disconnect"),
            client.post("/experiments", json={
                "name": "test", "evaluation_metric": "impressions",
            }),
            client.post("/experiments/1/variants", json={"name": "treatment"}),
            client.patch("/experiments/1/status", json={"target_status": "running"}),
            client.post("/experiments/1/assignments", json={"variant_id": 1, "creative_id": 1}),
            client.post("/experiments/1/evaluations", json={
                "period_start": "2026-09-01T00:00:00", "period_end": "2026-09-02T00:00:00",
            }),
        ]
        assert [response.status_code for response in requests] == [401] * len(requests)
        assert client.get("/").status_code == 200
        assert client.get("/health").status_code == 200
        assert client.get("/pipeline/status").status_code == 200
        assert client.get("/pinterest/boards").status_code == 200
        assert client.get("/experiments").status_code == 200


def test_login_requires_login_csrf_and_sets_hardened_expiring_admin_cookie(monkeypatch):
    _strict_auth(monkeypatch)
    with TestClient(app, base_url="https://testserver") as client:
        login_page = client.get("/auth/login")
        assert login_page.status_code == 200
        missing_csrf = client.post("/auth/login", data={"username": "owner", "password": "a-long-test-password-123"})
        assert missing_csrf.status_code == 403
        malformed_csrf = client.post(
            "/auth/login",
            json={"username": "owner", "password": "a-long-test-password-123", "_csrf": 123},
        )
        assert malformed_csrf.status_code == 403

        response = _login(client)
        assert response.status_code == 303
        cookie_header = response.headers["set-cookie"].lower()
        assert "httponly" in cookie_header
        assert "secure" in cookie_header
        assert "samesite=lax" in cookie_header
        assert "max-age=28800" in cookie_header
        assert client.get("/").text.startswith("<!doctype html>")


def test_unicode_admin_credentials_are_compared_safely(monkeypatch):
    _strict_auth(monkeypatch)
    monkeypatch.setattr(settings, "app_auth_username", "yönetici")
    monkeypatch.setattr(settings, "app_auth_password", "uzun-parola-şifre-123")
    with TestClient(app, base_url="https://testserver") as client:
        page = client.get("/auth/login")
        csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            "/auth/login",
            data={"username": "yönetici", "password": "uzun-parola-şifre-123", "_csrf": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303


def test_authenticated_requests_require_matching_csrf_and_admin_role(monkeypatch):
    _strict_auth(monkeypatch)
    with TestClient(app, base_url="https://testserver") as client:
        assert _login(client).status_code == 303
        dashboard = client.get("/").text
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', dashboard).group(1)

        missing = client.post("/pipeline/toggle", json={"enabled": False})
        wrong = client.post(
            "/pipeline/toggle", json={"enabled": False}, headers={"X-CSRF-Token": "wrong-token"}
        )
        assert missing.status_code == wrong.status_code == 403

        accepted = client.post(
            "/pipeline/toggle", json={"enabled": False}, headers={"X-CSRF-Token": csrf}
        )
        assert accepted.status_code == 200
        assert accepted.json()["enabled"] is False

        viewer_payload = {"sub": "owner", "role": "viewer", "csrf": "v" * 40}
        viewer_cookie = URLSafeTimedSerializer("s" * 48, salt=SESSION_SALT).dumps(viewer_payload)
        client.cookies.set(SESSION_COOKIE, viewer_cookie, domain="testserver", path="/")
        forbidden = client.post(
            "/pipeline/toggle", json={"enabled": True}, headers={"X-CSRF-Token": "v" * 40}
        )
        assert forbidden.status_code == 403


def test_valid_csrf_hidden_form_token_allows_logout(monkeypatch):
    _strict_auth(monkeypatch)
    with TestClient(app, base_url="https://testserver") as client:
        assert _login(client).status_code == 303
        dashboard = client.get("/").text
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', dashboard).group(1)
        response = client.post("/auth/logout", data={"_csrf": csrf}, follow_redirects=False)
        assert response.status_code == 303
        assert client.post("/pipeline/toggle", json={"enabled": False}).status_code == 401


def test_missing_auth_configuration_fails_closed_without_secret_generation(monkeypatch):
    app.dependency_overrides.pop(require_admin_csrf, None)
    monkeypatch.setattr(settings, "app_auth_username", None)
    monkeypatch.setattr(settings, "app_auth_password", None)
    monkeypatch.setattr(settings, "app_session_secret_key", None)
    with TestClient(app) as client:
        assert client.get("/auth/login").status_code == 503
        assert client.post("/pipeline/toggle", json={"enabled": False}).status_code == 401
