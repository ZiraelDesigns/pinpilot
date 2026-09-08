from urllib.parse import urlencode

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import EtsyAccount
from app.services.etsy import EtsyApiService, EtsyIntegrationError, EtsyOAuthService, EtsyTokenService

router = APIRouter(prefix="/etsy", tags=["etsy"])


def _dashboard_redirect(message: str, error: bool = False) -> RedirectResponse:
    key = "etsy_error" if error else "etsy_message"
    return RedirectResponse(url=f"/?{urlencode({key: message})}", status_code=303)


@router.get("/connect")
def connect(db: Session = Depends(get_db)):
    try:
        return RedirectResponse(EtsyOAuthService(db).authorization_url(), status_code=302)
    except EtsyIntegrationError as exc:
        return _dashboard_redirect(str(exc), error=True)


@router.get("/callback")
def callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: Session = Depends(get_db),
):
    if error:
        return _dashboard_redirect(error_description or "Etsy bağlantısı kullanıcı tarafından iptal edildi.", error=True)
    if not code or not state:
        return _dashboard_redirect("Etsy yanıtında gerekli yetkilendirme bilgileri eksik.", error=True)
    try:
        oauth = EtsyOAuthService(db)
        request_state = oauth.consume_state(state)
        token_data = oauth.exchange_code(code, request_state.code_verifier)
        user_id = token_data["access_token"].split(".", 1)[0]
        # The code exchange already produced a short-lived access token. Use it only to
        # identify the shop before selecting or creating its local account record.
        provisional = EtsyAccount(shop_name="Etsy", is_active=True)
        shop = EtsyApiService(db, provisional, access_token=token_data["access_token"]).fetch_shop(user_id)
        account = db.query(EtsyAccount).filter_by(shop_identifier=str(shop["shop_id"])).one_or_none()
        if not account:
            account = EtsyAccount(shop_name="Etsy", is_active=True)
        account.shop_name = shop.get("shop_name", "Etsy mağazası")
        account.shop_identifier = str(shop["shop_id"])
        account.is_active = True
        db.add(account)
        db.flush()
        EtsyTokenService(db).save(account, token_data)
        db.commit()
        return _dashboard_redirect("Etsy hesabı başarıyla bağlandı.")
    except (EtsyIntegrationError, KeyError) as exc:
        db.rollback()
        return _dashboard_redirect(str(exc) if isinstance(exc, EtsyIntegrationError) else "Etsy yanıtı geçersiz.", error=True)


@router.post("/sync")
def sync(db: Session = Depends(get_db)):
    account = db.query(EtsyAccount).filter_by(is_active=True).first()
    if not account:
        return _dashboard_redirect("Önce bir Etsy hesabı bağlayın.", error=True)
    try:
        total = EtsyApiService(db, account).sync_active_listings()
        return _dashboard_redirect(f"{total} aktif Etsy listing'i eşitlendi.")
    except EtsyIntegrationError as exc:
        return _dashboard_redirect(str(exc), error=True)


@router.post("/disconnect")
def disconnect(db: Session = Depends(get_db)):
    account = db.query(EtsyAccount).filter_by(is_active=True).first()
    if not account:
        return _dashboard_redirect("Bağlı bir Etsy hesabı bulunamadı.", error=True)
    db.delete(account)
    db.commit()
    return _dashboard_redirect("Etsy bağlantısı ve saklanan yerel yetkilendirme bilgileri kaldırıldı.")
