from urllib.parse import urlencode

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PinterestAccount, PinterestBoard
from app.services.pinterest import PinterestApiService, PinterestIntegrationError, PinterestOAuthService, PinterestTokenService

router = APIRouter(prefix="/pinterest", tags=["pinterest"])


def _dashboard_redirect(message: str, error: bool = False) -> RedirectResponse:
    key = "pinterest_error" if error else "pinterest_message"
    return RedirectResponse(url=f"/?{urlencode({key: message})}", status_code=303)


@router.get("/connect")
def connect(db: Session = Depends(get_db)):
    try:
        return RedirectResponse(PinterestOAuthService(db).authorization_url(), status_code=302)
    except PinterestIntegrationError as exc:
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
        return _dashboard_redirect(error_description or "Pinterest bağlantısı kullanıcı tarafından iptal edildi.", error=True)
    if not code or not state:
        return _dashboard_redirect("Pinterest yanıtında gerekli yetkilendirme bilgileri eksik.", error=True)
    try:
        oauth = PinterestOAuthService(db)
        oauth.consume_state(state)
        token_data = oauth.exchange_code(code)
        provisional = PinterestAccount(account_name="Pinterest", is_active=True)
        profile = PinterestApiService(db, provisional, access_token=token_data["access_token"]).fetch_account()
        identifier = str(profile.get("username") or profile.get("id") or "")
        if not identifier:
            raise PinterestIntegrationError("Pinterest hesap bilgisi geçerli bir kimlik içermiyor.")
        account = db.query(PinterestAccount).filter_by(account_identifier=identifier).one_or_none()
        if not account:
            account = PinterestAccount(account_name="Pinterest", account_identifier=identifier, is_active=True)
        account.account_name = profile.get("business_name") or profile.get("username") or "Pinterest"
        account.is_active = True
        db.add(account)
        db.flush()
        PinterestTokenService(db).save(account, token_data)
        db.commit()
        return _dashboard_redirect("Pinterest hesabı başarıyla bağlandı. Board'ları görmek için eşitleyin.")
    except (PinterestIntegrationError, KeyError) as exc:
        db.rollback()
        return _dashboard_redirect(str(exc) if isinstance(exc, PinterestIntegrationError) else "Pinterest yanıtı geçersiz.", error=True)


@router.post("/boards/sync")
def sync_boards(db: Session = Depends(get_db)):
    account = db.query(PinterestAccount).filter_by(is_active=True).first()
    if not account:
        return _dashboard_redirect("Önce bir Pinterest hesabı bağlayın.", error=True)
    try:
        total = PinterestApiService(db, account).sync_boards()
        return _dashboard_redirect(f"{total} Pinterest board eşitlendi.")
    except PinterestIntegrationError as exc:
        return _dashboard_redirect(str(exc), error=True)


@router.get("/boards")
def list_boards(db: Session = Depends(get_db)) -> JSONResponse:
    account = db.query(PinterestAccount).filter_by(is_active=True).first()
    if not account:
        return JSONResponse({"items": [], "message": "Bağlı Pinterest hesabı yok."})
    boards = db.query(PinterestBoard).filter_by(account_id=account.id).order_by(PinterestBoard.name).all()
    return JSONResponse({"items": [{"id": board.board_id, "name": board.name} for board in boards]})


@router.post("/disconnect")
def disconnect(db: Session = Depends(get_db)):
    account = db.query(PinterestAccount).filter_by(is_active=True).first()
    if not account:
        return _dashboard_redirect("Bağlı bir Pinterest hesabı bulunamadı.", error=True)
    db.delete(account)
    db.commit()
    return _dashboard_redirect("Pinterest bağlantısı ve saklanan yerel yetkilendirme bilgileri kaldırıldı.")
