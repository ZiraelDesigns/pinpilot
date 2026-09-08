from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PinStatus(str, Enum):
    DRAFT = "draft"
    GENERATED = "generated"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"


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
    pins: Mapped[list["Pin"]] = relationship(back_populates="product")
    creatives: Mapped[list["PinCreative"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class Pin(Base):
    __tablename__ = "pins"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(String(2048))
    destination_url: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(32), default=PinStatus.DRAFT.value, nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    product: Mapped[Product | None] = relationship(back_populates="pins")
    analytics: Mapped[list["AnalyticsSnapshot"]] = relationship(back_populates="pin")


class PinGenerationJob(Base):
    __tablename__ = "pin_generation_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
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


class AnalyticsSnapshot(Base):
    __tablename__ = "analytics_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    pin_id: Mapped[int | None] = mapped_column(ForeignKey("pins.id"))
    impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    saves: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    outbound_clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    pin: Mapped[Pin | None] = relationship(back_populates="analytics")
