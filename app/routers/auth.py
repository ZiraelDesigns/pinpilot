"""Browser login/logout endpoints for the configured single administrator."""

from __future__ import annotations

import hmac
from html import escape
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.security import (
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    auth_is_configured,
    create_session_token,
    get_principal,
    issue_login_csrf,
    require_admin_csrf,
    verify_login_csrf,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _login_page(csrf_token: str = "", message: str = "", *, status_code: int = 200) -> HTMLResponse:
    content = f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PinPilot yönetici girişi</title><link rel="stylesheet" href="/static/style.css"></head>
<body><main><section class="etsy-connection"><h1>PinPilot yönetici girişi</h1>
{f'<p class="notice error">{escape(message)}</p>' if message else ''}
<form method="post" action="/auth/login"><input type="hidden" name="_csrf" value="{escape(csrf_token)}">
<label>Kullanıcı adı <input name="username" autocomplete="username" required></label>
<label>Parola <input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">Giriş yap</button></form><p><a href="/">Panele dön</a></p></section></main></body></html>"""
    response = HTMLResponse(content, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _set_cookie(response, name: str, value: str, *, max_age: int, http_only: bool = True) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=http_only,
        secure=settings.app_session_cookie_secure,
        samesite="lax",
        path="/",
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if get_principal(request):
        return RedirectResponse("/", status_code=303)
    if not auth_is_configured():
        return _login_page(
            message=(
                "Yönetici girişi yapılandırılmamış. Sunucu yöneticisi "
                "APP_AUTH_USERNAME, APP_AUTH_PASSWORD ve APP_SESSION_SECRET_KEY "
                "değerlerini güvenli ortam yapılandırmasına eklemelidir."
            ),
            status_code=503,
        )
    csrf_token, cookie_value = issue_login_csrf()
    response = _login_page(csrf_token)
    _set_cookie(response, LOGIN_CSRF_COOKIE, cookie_value, max_age=600)
    return response


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request):
    if not auth_is_configured():
        return _login_page(
            message="Yönetici girişi yapılandırılmamış. Gerekli sunucu ayarları eksik.",
            status_code=503,
        )
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:
        if content_type == "application/json":
            data = await request.json()
            if not isinstance(data, dict):
                data = {}
            username, password, csrf_token = data.get("username"), data.get("password"), data.get("_csrf")
        elif content_type == "application/x-www-form-urlencoded":
            values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
            username = values.get("username", [None])[0]
            password = values.get("password", [None])[0]
            csrf_token = values.get("_csrf", [None])[0]
        else:
            return _login_page(message="Giriş isteği biçimi geçersiz.", status_code=415)
    except (UnicodeDecodeError, ValueError):
        return _login_page(message="Giriş isteği geçersiz.", status_code=400)

    if not verify_login_csrf(request.cookies.get(LOGIN_CSRF_COOKIE), csrf_token):
        return _login_page(message="CSRF doğrulaması başarısız.", status_code=403)
    username_ok = isinstance(username, str) and hmac.compare_digest(
        username.encode("utf-8"), (settings.app_auth_username or "").encode("utf-8")
    )
    password_ok = isinstance(password, str) and hmac.compare_digest(
        password.encode("utf-8"), (settings.app_auth_password or "").encode("utf-8")
    )
    if not (username_ok and password_ok):
        return _login_page(message="Kullanıcı adı veya parola geçersiz.", status_code=401)

    token, _csrf = create_session_token(settings.app_auth_username or "")
    response = RedirectResponse("/", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    _set_cookie(response, SESSION_COOKIE, token, max_age=settings.app_session_ttl_seconds)
    response.delete_cookie(
        LOGIN_CSRF_COOKIE,
        path="/",
        secure=settings.app_session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
def logout(_principal=Depends(require_admin_csrf)):
    response = RedirectResponse("/", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=settings.app_session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response
