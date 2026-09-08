from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT, settings
from app.database import Base, engine, get_db
from app.models import EtsyAccount, Pin, PinCreative, PinterestAccount, PinterestBoard, Product
from app.models.core import PinStatus
from app.routers.etsy import router as etsy_router
from app.routers.pinterest import router as pinterest_router
from app.routers.creatives import router as creatives_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    if "pin_creatives" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("pin_creatives")}
        missing_columns = {
            "image_path": "VARCHAR(2048)",
            "source_image_url": "VARCHAR(2048)",
            # Existing image-generation records predate source types and are AI records.
            "source_type": "VARCHAR(16) NOT NULL DEFAULT 'ai'",
        }
        for name, definition in missing_columns.items():
            if name in columns:
                continue
            with engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE pin_creatives ADD COLUMN {name} {definition}"))
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")
media_dir = PROJECT_ROOT / settings.generated_media_dir.split("/", 1)[0]
media_dir.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")
app.include_router(etsy_router)
app.include_router(pinterest_router)
app.include_router(creatives_router)
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    etsy_message: str | None = None,
    etsy_error: str | None = None,
    pinterest_message: str | None = None,
    pinterest_error: str | None = None,
    db: Session = Depends(get_db),
):
    counts = {
        "products": db.scalar(select(func.count()).select_from(Product)) or 0,
        "generated_pins": db.scalar(
            select(func.count()).select_from(Pin).where(Pin.status == PinStatus.GENERATED.value)
        ) or 0,
        "scheduled_pins": db.scalar(
            select(func.count()).select_from(Pin).where(Pin.status == PinStatus.SCHEDULED.value)
        ) or 0,
        "published_pins": db.scalar(
            select(func.count()).select_from(Pin).where(Pin.status == PinStatus.PUBLISHED.value)
        ) or 0,
    }
    etsy_account = db.query(EtsyAccount).filter_by(is_active=True).first()
    pinterest_account = db.query(PinterestAccount).filter_by(is_active=True).first()
    pinterest_boards = (
        db.query(PinterestBoard).filter_by(account_id=pinterest_account.id).order_by(PinterestBoard.name).all()
        if pinterest_account
        else []
    )
    products = db.query(Product).order_by(Product.title).all()
    creatives = db.query(PinCreative).order_by(PinCreative.created_at.desc()).all()
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "counts": counts,
            "etsy_account": etsy_account,
            "etsy_message": etsy_message,
            "etsy_error": etsy_error,
            "pinterest_account": pinterest_account,
            "pinterest_boards": pinterest_boards,
            "pinterest_message": pinterest_message,
            "pinterest_error": pinterest_error,
            "products": products,
            "creatives": creatives,
        },
    )
