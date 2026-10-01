"""Fail-closed single-admin browser sessions and CSRF validation."""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings

SESSION_COOKIE = "pinpilot_session"
LOGIN_CSRF_COOKIE = "pinpilot_login_csrf"
SESSION_SALT = "pinpilot-admin-session-v1"
LOGIN_CSRF_SALT = "pinpilot-login-csrf-v1"
MIN_SESSION_TTL_SECONDS = 300
MAX_SESSION_TTL_SECONDS = 86400


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


@dataclass(frozen=True)
class AuthPrincipal:
    username: str
    role: str
    csrf_token: str


def auth_is_configured() -> bool:
    username = settings.app_auth_username
    password = settings.app_auth_password
    secret = settings.app_session_secret_key
    return bool(
        username
        and password
        and len(password) >= 16
        and secret
        and len(secret.encode("utf-8")) >= 32
        and MIN_SESSION_TTL_SECONDS <= settings.app_session_ttl_seconds <= MAX_SESSION_TTL_SECONDS
    )


def _serializer(salt: str) -> URLSafeTimedSerializer:
    if not auth_is_configured():
        raise RuntimeError("Authentication is not configured")
    return URLSafeTimedSerializer(settings.app_session_secret_key, salt=salt)


def issue_login_csrf() -> tuple[str, str]:
    nonce = secrets.token_urlsafe(32)
    return nonce, _serializer(LOGIN_CSRF_SALT).dumps({"nonce": nonce})


def verify_login_csrf(cookie_value: str | None, submitted: str | None) -> bool:
    if (
        not isinstance(cookie_value, str)
        or not isinstance(submitted, str)
        or not cookie_value
        or not submitted
        or not auth_is_configured()
    ):
        return False
    try:
        payload = _serializer(LOGIN_CSRF_SALT).loads(cookie_value, max_age=600)
    except BadSignature:
        return False
    nonce = payload.get("nonce") if isinstance(payload, dict) else None
    return isinstance(nonce, str) and _constant_time_equal(nonce, submitted)


def create_session_token(username: str, *, role: str = "admin") -> tuple[str, str]:
    csrf_token = secrets.token_urlsafe(32)
    token = _serializer(SESSION_SALT).dumps(
        {"sub": username, "role": role, "csrf": csrf_token}
    )
    return token, csrf_token


def get_principal(request: Request) -> AuthPrincipal | None:
    if not auth_is_configured():
        return None
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        payload = _serializer(SESSION_SALT).loads(
            token, max_age=settings.app_session_ttl_seconds
        )
    except BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    username = payload.get("sub")
    role = payload.get("role")
    csrf_token = payload.get("csrf")
    if (
        not isinstance(username, str)
        or not _constant_time_equal(username, settings.app_auth_username or "")
        or not isinstance(role, str)
        or not isinstance(csrf_token, str)
        or len(csrf_token) < 32
    ):
        return None
    return AuthPrincipal(username=username, role=role, csrf_token=csrf_token)


async def _submitted_csrf(request: Request) -> str | None:
    header_value = request.headers.get("x-csrf-token")
    if header_value:
        return header_value
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return None
    try:
        body = (await request.body()).decode("utf-8")
    except (UnicodeDecodeError, RuntimeError):
        return None
    return parse_qs(body, keep_blank_values=True).get("_csrf", [None])[0]


async def require_admin_csrf(request: Request) -> AuthPrincipal:
    """Authorize the sole configured admin and require CSRF on every mutation."""
    principal = get_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Giriş gerekli.")
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="Bu işlem için yetkiniz yok.")
    submitted = await _submitted_csrf(request)
    if not submitted or not _constant_time_equal(principal.csrf_token, submitted):
        raise HTTPException(status_code=403, detail="CSRF doğrulaması başarısız.")
    return principal
