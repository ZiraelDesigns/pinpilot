from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PinStatus(str, Enum):
    DRAFT = "draft"
    GENERATED = "generated"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    CANCELLED = "cancelled"


class PinCreativeType(str, Enum):
    PRODUCT_FOCUS = "product_focus"
    LIFESTYLE = "lifestyle"
    PROBLEM_SOLUTION = "problem_solution"
    GIFT_IDEA = "gift_idea"
    MINIMALIST = "minimalist"


class PinCreativeStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class PinCreativeSourceType(str, Enum):
    MOCKUP = "mockup"
    AI = "ai"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(2048))
    image_url: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # A short-lived database lease prevents two workers from creating the same
    # AI variation for a product at the same time.
    ai_generation_locked_at: Mapped[datetime | None] = mapped_column(DateTime)
    ai_generation_worker_id: Mapped[str | None] = mapped_column(String(64))
    pins: Mapped[list["Pin"]] = relationship(back_populates="product")
    creatives: Mapped[list["PinCreative"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class Pin(Base):
    __tablename__ = "pins"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    # A Pin may be prepared from one creative only.  The unique constraint is the
    # durable guard that prevents a creative from being scheduled twice.
    creative_id: Mapped[int | None] = mapped_column(
        ForeignKey("pin_creatives.id"), unique=True, nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(String(2048))
    destination_url: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(32), default=PinStatus.DRAFT.value, nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    product: Mapped[Product | None] = relationship(back_populates="pins")
    creative: Mapped["PinCreative | None"] = relationship(back_populates="pins")
    analytics: Mapped[list["AnalyticsSnapshot"]] = relationship(back_populates="pin")
    published_pins: Mapped[list["PublishedPinterestPin"]] = relationship(back_populates="pin")


class PinGenerationJob(Base):
    __tablename__ = "pin_generation_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    # One queued job may request more than one local creative without invoking a
    # provider from the scheduler itself.
    requested_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime)
    worker_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class PinCreative(Base):
    """Structured copy for a future Pinterest Pin; it does not publish anything."""

    __tablename__ = "pin_creatives"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    creative_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # Structured Pinterest SEO v2 signals. ``keywords`` remains the flattened,
    # backward-compatible search list used by the dashboard and scheduler.
    seo_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    call_to_action: Mapped[str] = mapped_column(String(255), nullable=False)
    image_path: Mapped[str | None] = mapped_column(String(2048))
    # `image_path` can be a locally served /media URL or the canonical public Etsy image URL.
    # `source_image_url` retains the source of a mockup for stable de-duplication and future mirroring.
    source_image_url: Mapped[str | None] = mapped_column(String(2048))
    source_type: Mapped[str] = mapped_column(
        String(16), default=PinCreativeSourceType.AI.value, nullable=False
    )
    destination_url: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(32), default=PinCreativeStatus.DRAFT.value, nullable=False)
    # Derived from product, type, and variation number to avoid repeat provider calls.
    generation_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    product: Mapped[Product] = relationship(back_populates="creatives")
    pins: Mapped[list[Pin]] = relationship(back_populates="creative")


class PinterestAccount(Base):
    __tablename__ = "pinterest_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_identifier: Mapped[str | None] = mapped_column(String(255), unique=True)
    is_active: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    credential: Mapped["PinterestOAuthCredential | None"] = relationship(
        back_populates="account", cascade="all, delete-orphan", uselist=False
    )
    boards: Mapped[list["PinterestBoard"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    published_pins: Mapped[list["PublishedPinterestPin"]] = relationship(back_populates="account")
    analytics_runs: Mapped[list["AnalyticsCollectionRun"]] = relationship(back_populates="account")
    analytics_snapshots: Mapped[list["PinterestAccountAnalyticsSnapshot"]] = relationship(back_populates="account")


class PinterestOAuthCredential(Base):
    """Encrypted OAuth credentials for a Pinterest account."""

    __tablename__ = "pinterest_oauth_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("pinterest_accounts.id"), unique=True)
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    scopes: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    account: Mapped[PinterestAccount] = relationship(back_populates="credential")


class PinterestOAuthState(Base):
    """Single-use server-side CSRF state for Pinterest OAuth."""

    __tablename__ = "pinterest_oauth_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)


class PinterestBoard(Base):
    """A locally cached board reference; it does not create or edit Pinterest boards."""

    __tablename__ = "pinterest_boards"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("pinterest_accounts.id"), nullable=False)
    board_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    privacy: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    account: Mapped[PinterestAccount] = relationship(back_populates="boards")
    published_pins: Mapped[list["PublishedPinterestPin"]] = relationship(back_populates="board")


class EtsyAccount(Base):
    __tablename__ = "etsy_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_name: Mapped[str] = mapped_column(String(255), nullable=False)
    shop_identifier: Mapped[str | None] = mapped_column(String(255), unique=True)
    is_active: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    credential: Mapped["EtsyOAuthCredential | None"] = relationship(
        back_populates="account", cascade="all, delete-orphan", uselist=False
    )
    listings: Mapped[list["EtsyListing"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    sync_runs: Mapped[list["EtsySyncRun"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class EtsyOAuthCredential(Base):
    """Encrypted OAuth tokens for one Etsy account; never expose these in an API response."""

    __tablename__ = "etsy_oauth_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("etsy_accounts.id"), unique=True)
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    scopes: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    account: Mapped[EtsyAccount] = relationship(back_populates="credential")


class EtsyOAuthState(Base):
    """Single-use, short-lived server-side state for an OAuth PKCE request."""

    __tablename__ = "etsy_oauth_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)


class EtsyListing(Base):
    """Read-only local copy of an Etsy listing and its display data."""

    __tablename__ = "etsy_listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("etsy_accounts.id"), nullable=False)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), unique=True)
    listing_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(2048))
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(8))
    quantity: Mapped[int | None] = mapped_column(Integer)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    images: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    account: Mapped[EtsyAccount] = relationship(back_populates="listings")
    product: Mapped[Product | None] = relationship()


class EtsySyncRun(Base):
    """A non-sensitive audit record for one read-only Etsy synchronization."""

    __tablename__ = "etsy_sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("etsy_accounts.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    processed_listings: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_products: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    changed_products: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_mockup_creatives: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    inactive_listings: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    account: Mapped[EtsyAccount] = relationship(back_populates="sync_runs")


class AnalyticsSnapshot(Base):
    __tablename__ = "analytics_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    pin_id: Mapped[int | None] = mapped_column(ForeignKey("pins.id"))
    impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    saves: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outbound_clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    published_pin_id: Mapped[int | None] = mapped_column(
        ForeignKey("published_pinterest_pins.id", ondelete="SET NULL"), nullable=True
    )
    collection_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("analytics_collection_runs.id", ondelete="SET NULL"), nullable=True
    )
    metric_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pin_clicks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    engagements: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    engagement_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    pin_click_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    outbound_click_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    metric_schema_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pin: Mapped[Pin | None] = relationship(back_populates="analytics")
    published_pin: Mapped["PublishedPinterestPin | None"] = relationship(back_populates="analytics")
    collection_run: Mapped["AnalyticsCollectionRun | None"] = relationship(back_populates="pin_snapshots")


class PublishedPinterestPin(Base):
    """A Pinterest publication linked to its local scheduled Pin and as-published metadata."""

    __tablename__ = "published_pinterest_pins"
    __table_args__ = (
        Index("ix_published_pins_account_published", "account_id", "published_at"),
        Index("ix_published_pins_board_published", "board_id", "published_at"),
        Index("ix_published_pins_pin_id", "pin_id"),
        Index(
            "uq_published_pins_account_external_id",
            "account_id",
            "external_pin_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    pin_id: Mapped[int | None] = mapped_column(ForeignKey("pins.id", ondelete="SET NULL"), nullable=True)
    # Nullable after account disconnect/deletion; the immutable identifier below retains provenance.
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("pinterest_accounts.id", ondelete="SET NULL"), nullable=True
    )
    board_id: Mapped[int | None] = mapped_column(
        ForeignKey("pinterest_boards.id", ondelete="SET NULL"), nullable=True
    )
    account_identifier_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_pin_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metadata_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    pin: Mapped[Pin | None] = relationship(back_populates="published_pins")
    account: Mapped[PinterestAccount | None] = relationship(back_populates="published_pins")
    board: Mapped[PinterestBoard | None] = relationship(back_populates="published_pins")
    analytics: Mapped[list[AnalyticsSnapshot]] = relationship(back_populates="published_pin")

    @classmethod
    def capture_metadata(cls, pin: Pin) -> dict:
        """Copy only analysis-relevant creative fields at publication time."""
        creative = pin.creative
        seo = deepcopy(creative.seo_metadata or {}) if creative else {}
        return {
            "product_id": creative.product_id if creative else pin.product_id,
            "creative_type": creative.creative_type if creative else None,
            "source_type": creative.source_type if creative else None,
            "creative_title": creative.title if creative else pin.title,
            "creative_description": creative.description if creative else pin.description,
            "destination_etsy_url": creative.destination_url if creative else pin.destination_url,
            "seo_metadata": seo,
            "primary_keyword": seo.get("primary_keyword"),
            "creative_angle": seo.get("creative_angle"),
            "image_media_reference": (
                pin.image_path or (creative.image_path or creative.source_image_url if creative else None)
            ),
        }


class AnalyticsCollectionRun(Base):
    """Audit row for one future analytics collection attempt; contains no credentials."""

    __tablename__ = "analytics_collection_runs"
    __table_args__ = (Index("ix_analytics_runs_account_started_status", "account_id", "started_at", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("pinterest_accounts.id", ondelete="SET NULL"), nullable=True
    )
    account_identifier_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scope: Mapped[str] = mapped_column(String(32), default="all", nullable=False)

    account: Mapped[PinterestAccount | None] = relationship(back_populates="analytics_runs")
    pin_snapshots: Mapped[list[AnalyticsSnapshot]] = relationship(back_populates="collection_run")
    account_snapshots: Mapped[list["PinterestAccountAnalyticsSnapshot"]] = relationship(
        back_populates="collection_run"
    )


class PinterestAccountAnalyticsSnapshot(Base):
    __tablename__ = "pinterest_account_analytics_snapshots"
    __table_args__ = (
        Index("ix_account_analytics_account_metric_date", "account_id", "metric_date"),
        Index(
            "uq_account_analytics_run_account_period",
            "collection_run_id",
            "account_id",
            "period_start",
            "period_end",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("pinterest_accounts.id", ondelete="SET NULL"), nullable=True
    )
    collection_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("analytics_collection_runs.id", ondelete="SET NULL"), nullable=True
    )
    metric_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    profile_visits: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    follows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_audience: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    engaged_audience: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metric_schema_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    account: Mapped[PinterestAccount | None] = relationship(back_populates="analytics_snapshots")
    collection_run: Mapped[AnalyticsCollectionRun | None] = relationship(back_populates="account_snapshots")
