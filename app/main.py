from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT, settings
from app.database import Base, engine, get_db
from app.models import EtsyAccount, Pin, PinCreative, PinGenerationJob, PinterestAccount, PinterestBoard, Product
from app.models.core import PinCreativeSourceType, PinStatus
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
    if "pins" in inspector.get_table_names():
        pin_columns = {column["name"] for column in inspector.get_columns("pins")}
        if "creative_id" not in pin_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE pins ADD COLUMN creative_id INTEGER"))
        # Legacy Pin rows with no creative link remain valid while new records
        # cannot reuse a creative.
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_pins_creative_id_unique "
                "ON pins (creative_id) WHERE creative_id IS NOT NULL"
            ))
    if "pin_generation_jobs" in inspector.get_table_names():
        job_columns = {column["name"] for column in inspector.get_columns("pin_generation_jobs")}
        if "requested_count" not in job_columns:
            with engine.begin() as connection:
                connection.execute(text(
                    "ALTER TABLE pin_generation_jobs "
                    "ADD COLUMN requested_count INTEGER NOT NULL DEFAULT 1"
                ))
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
    today_start = datetime.combine(datetime.now().date(), datetime.min.time())
    today_end = today_start + timedelta(days=1)
    queue_counts = {
        "today_prepared": db.scalar(
            select(func.count()).select_from(Pin).where(
                Pin.scheduled_for >= today_start,
                Pin.scheduled_for < today_end,
                Pin.status.in_((PinStatus.SCHEDULED.value, PinStatus.PUBLISHED.value)),
            )
        ) or 0,
        "scheduled": counts["scheduled_pins"],
        "mockup": db.scalar(
            select(func.count()).select_from(Pin).join(PinCreative).where(
                Pin.scheduled_for >= today_start,
                Pin.scheduled_for < today_end,
                PinCreative.source_type == PinCreativeSourceType.MOCKUP.value,
            )
        ) or 0,
        "ai": db.scalar(
            select(func.count()).select_from(Pin).join(PinCreative).where(
                Pin.scheduled_for >= today_start,
                Pin.scheduled_for < today_end,
                PinCreative.source_type == PinCreativeSourceType.AI.value,
            )
        ) or 0,
        "pending_ai_jobs": db.scalar(
            select(func.count()).select_from(PinGenerationJob).where(
                PinGenerationJob.status == "pending"
            )
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
            "queue_counts": queue_counts,
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
