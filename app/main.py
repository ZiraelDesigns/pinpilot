from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session, joinedload

from app.config import PROJECT_ROOT, settings
from app.analytics_migrations import upgrade_analytics_schema
from app.database import Base, engine, get_db
from app.models import EtsyAccount, EtsySyncRun, Pin, PinCreative, PinGenerationJob, PinterestAccount, PinterestBoard, Product, SEOGeneration
from app.models.core import PinCreativeSourceType, PinStatus
from app.routers.etsy import router as etsy_router
from app.routers.pinterest import router as pinterest_router
from app.routers.creatives import router as creatives_router
from app.routers.auth import router as auth_router
from app.services.analytics_dashboard import DashboardFilters, get_dashboard_data
from app.services.opportunity_engine import get_next_best_pin_opportunities
from app.services.seo_performance_learning import get_performance_learning_dashboard
from app.services.experiments import experiment_summaries
from app.routers.experiments import router as experiments_router
from app.routers.pipeline import router as pipeline_router
from app.services.ai_pipeline import dashboard_pipeline_status
from app.security import AuthPrincipal, get_principal


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    upgrade_analytics_schema(engine)
    inspector = inspect(engine)
    if "pin_creatives" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("pin_creatives")}
        missing_columns = {
            "image_path": "VARCHAR(2048)",
            "source_image_url": "VARCHAR(2048)",
            "seo_metadata": "JSON",
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
        for name, definition in {
            "retry_count": "INTEGER NOT NULL DEFAULT 0",
            "next_attempt_at": "DATETIME",
            "locked_at": "DATETIME",
            "worker_id": "VARCHAR(64)",
        }.items():
            if name not in job_columns:
                with engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE pin_generation_jobs ADD COLUMN {name} {definition}"))
    if "products" in inspector.get_table_names():
        product_columns = {column["name"] for column in inspector.get_columns("products")}
        for name, definition in {
            "ai_generation_locked_at": "DATETIME",
            "ai_generation_worker_id": "VARCHAR(64)",
        }.items():
            if name not in product_columns:
                with engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE products ADD COLUMN {name} {definition}"))
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")
media_dir = PROJECT_ROOT / settings.generated_media_dir.split("/", 1)[0]
media_dir.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")
app.include_router(etsy_router)
app.include_router(pinterest_router)
app.include_router(creatives_router)
app.include_router(experiments_router)
app.include_router(pipeline_router)
app.include_router(auth_router)
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}


@app.get("/privacy-policy.html", response_class=FileResponse)
def privacy_policy() -> FileResponse:
    """Serve the repository's public privacy policy through the existing app."""
    return FileResponse(PROJECT_ROOT / "privacy-policy.html", media_type="text/html")


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    principal: AuthPrincipal | None = Depends(get_principal),
    etsy_message: str | None = None,
    etsy_error: str | None = None,
    pinterest_message: str | None = None,
    pinterest_error: str | None = None,
    period: str = "30",
    start_date: str | None = None,
    end_date: str | None = None,
    account_id: int | None = None,
    product_id: int | None = None,
    creative_type: str | None = None,
    source_type: str | None = None,
    board_id: int | None = None,
    page: int = 1,
    db: Session = Depends(get_db),
):
    if principal is None or principal.role != "admin":
        return RedirectResponse(url="/auth/login", status_code=303)

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
    ai_job_counts = {
        status: db.scalar(select(func.count()).select_from(PinGenerationJob).where(
            PinGenerationJob.status == status
        )) or 0
        for status in ("pending", "processing", "completed", "failed")
    }
    etsy_account = db.query(EtsyAccount).filter_by(is_active=True).first()
    etsy_sync_run = (
        db.query(EtsySyncRun).filter_by(account_id=etsy_account.id).order_by(EtsySyncRun.started_at.desc()).first()
        if etsy_account else None
    )
    pinterest_account = db.query(PinterestAccount).filter_by(is_active=True).first()
    pinterest_boards = (
        db.query(PinterestBoard).filter_by(account_id=pinterest_account.id).order_by(PinterestBoard.name).all()
        if pinterest_account
        else []
    )
    products = db.query(Product).order_by(Product.title).all()
    creatives = db.query(PinCreative).order_by(PinCreative.created_at.desc()).all()
    creative_ids = [creative.id for creative in creatives]
    seo_candidate_summaries = {}
    if creative_ids:
        generations = db.scalars(
            select(SEOGeneration)
            .where(SEOGeneration.creative_id.in_(creative_ids), SEOGeneration.status == "completed")
            .order_by(SEOGeneration.started_at.desc(), SEOGeneration.id.desc())
        )
        for generation in generations:
            if generation.creative_id in seo_candidate_summaries:
                continue
            snapshot = generation.output_snapshot if isinstance(generation.output_snapshot, dict) else {}
            selection = snapshot.get("seo_candidate_selection")
            if isinstance(selection, dict):
                seo_candidate_summaries[generation.creative_id] = selection
    today = datetime.now().date()
    portfolio_rows = db.scalars(
        select(Pin).where(
            Pin.scheduled_for >= datetime.combine(today, datetime.min.time()),
            Pin.scheduled_for < datetime.combine(today + timedelta(days=1), datetime.min.time()),
            Pin.portfolio_snapshot.is_not(None),
        ).options(joinedload(Pin.creative)).order_by(Pin.scheduled_for, Pin.id).limit(15)
    ).all()
    today_portfolio = [
        pin for pin in portfolio_rows if isinstance(pin.portfolio_snapshot, dict)
    ]
    today_portfolio_summary = next((
        pin.portfolio_snapshot.get("portfolio_summary")
        for pin in today_portfolio
        if isinstance(pin.portfolio_snapshot.get("portfolio_summary"), dict)
    ), {})
    portfolio_warnings = []
    for key, label in (
        ("keyword", "Anahtar kelime"),
        ("creative_type", "Kreatif türü"),
        ("creative_angle", "Kreatif açısı"),
    ):
        distribution = today_portfolio_summary.get(
            {"keyword": "keyword_distribution", "creative_type": "creative_type_distribution",
             "creative_angle": "creative_angle_distribution"}[key], {}
        )
        if isinstance(distribution, dict) and distribution:
            value, count = min(distribution.items(), key=lambda item: (-item[1], str(item[0])))
            if count >= 3 and count / max(today_portfolio_summary.get("selected_count", 0), 1) >= 0.5:
                portfolio_warnings.append(f"{label}: {value} ({count} Pin)")
    filter_error = None
    if period in {"7", "30", "90"}:
        analytics_end = today
        analytics_start = today - timedelta(days=int(period) - 1)
    elif period == "custom":
        try:
            analytics_start = date.fromisoformat(start_date or "")
            analytics_end = date.fromisoformat(end_date or "")
            if analytics_end < analytics_start:
                raise ValueError
        except ValueError:
            analytics_start = today - timedelta(days=29)
            analytics_end = today
            filter_error = "Custom date range is invalid; showing the last 30 days."
    else:
        period = "30"
        analytics_start = today - timedelta(days=29)
        analytics_end = today
    analytics_page = max(1, min(page, 100000))
    analytics_filters = DashboardFilters(
        start=analytics_start,
        end=analytics_end,
        account_id=account_id,
        product_id=product_id,
        creative_type=creative_type,
        source_type=source_type,
        board_id=board_id,
        page=analytics_page,
    )
    analytics = get_dashboard_data(db, analytics_filters)
    experiments = experiment_summaries(db)
    next_best_pins = get_next_best_pin_opportunities(db, account_id=account_id)
    performance_learning = get_performance_learning_dashboard(db, account_id=account_id)
    pipeline_status = dashboard_pipeline_status(db)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "counts": counts,
            "queue_counts": queue_counts,
            "ai_job_counts": ai_job_counts,
            "etsy_account": etsy_account,
            "etsy_sync_run": etsy_sync_run,
            "etsy_message": etsy_message,
            "etsy_error": etsy_error,
            "pinterest_account": pinterest_account,
            "pinterest_boards": pinterest_boards,
            "pinterest_message": pinterest_message,
            "pinterest_error": pinterest_error,
            "products": products,
            "creatives": creatives,
            "seo_candidate_summaries": seo_candidate_summaries,
            "analytics": analytics,
            "analytics_filters": analytics_filters,
            "analytics_period": period,
            "analytics_start_date": analytics_start.isoformat(),
            "analytics_end_date": analytics_end.isoformat(),
            "analytics_filter_error": filter_error,
            "experiments": experiments,
            "next_best_pins": next_best_pins,
            "today_portfolio": today_portfolio,
            "today_portfolio_summary": today_portfolio_summary,
            "portfolio_warnings": portfolio_warnings,
            "performance_learning": performance_learning,
            "pipeline_status": pipeline_status,
            "authenticated": bool(principal and principal.role == "admin"),
            "csrf_token": principal.csrf_token if principal and principal.role == "admin" else "",
        },
    )
