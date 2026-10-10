from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from app.database import Base


def _utc_naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PinStatus(str, Enum):
    DRAFT = "draft"
    GENERATED = "generated"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    CANCELLED = "cancelled"


class PinterestPublishIntentStatus(str, Enum):
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    UNKNOWN = "unknown"


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
    # Optional immutable selection rationale for locally scheduled portfolio Pins.
    portfolio_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=PinStatus.DRAFT.value, nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    product: Mapped[Product | None] = relationship(back_populates="pins")
    creative: Mapped["PinCreative | None"] = relationship(back_populates="pins")
    analytics: Mapped[list["AnalyticsSnapshot"]] = relationship(back_populates="pin")
    published_pins: Mapped[list["PublishedPinterestPin"]] = relationship(back_populates="pin")
    publish_intents: Mapped[list["PinterestPublishIntent"]] = relationship(back_populates="pin")


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


class AIPipelineControl(Base):
    """Persistent singleton control for automatic and manual AI generation."""

    __tablename__ = "ai_pipeline_controls"

    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False, server_default=text("0"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_naive_now, nullable=False)


class AIDailyQuotaSlot(Base):
    """One atomically reservable successful AI-creative slot for a UTC day."""

    __tablename__ = "ai_daily_quota_slots"
    __table_args__ = (
        UniqueConstraint("quota_date", "slot_number", name="uq_ai_quota_date_slot"),
        Index("ix_ai_quota_date_state", "quota_date", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    quota_date: Mapped[date] = mapped_column(Date, nullable=False)
    slot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="available", nullable=False)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("pin_generation_jobs.id", ondelete="SET NULL"), nullable=True
    )
    creative_id: Mapped[int | None] = mapped_column(
        ForeignKey("pin_creatives.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_naive_now, nullable=False)


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
    seo_generations: Mapped[list["SEOGeneration"]] = relationship(
        back_populates="creative", order_by="SEOGeneration.started_at"
    )


class SEOGeneration(Base):
    """Immutable provenance and output snapshot for one SEO generation attempt."""

    __tablename__ = "seo_generations"
    __table_args__ = (
        Index("ix_seo_generations_creative_started", "creative_id", "started_at"),
        Index("ix_seo_generations_status_started", "status", "started_at"),
        CheckConstraint("status IN ('completed', 'failed')", name="ck_seo_generations_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    creative_id: Mapped[int | None] = mapped_column(
        ForeignKey("pin_creatives.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, default="unknown")
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    output_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    creative: Mapped[PinCreative | None] = relationship(back_populates="seo_generations")
    product: Mapped[Product | None] = relationship()
    keyword_intelligence: Mapped["SEOKeywordIntelligence | None"] = relationship(
        back_populates="seo_generation", cascade="all, delete-orphan", uselist=False
    )
    quality_assessment: Mapped["SEOQualityAssessment | None"] = relationship(
        back_populates="seo_generation", cascade="all, delete-orphan", uselist=False
    )
    board_recommendations: Mapped[list["PinterestBoardRecommendation"]] = relationship(
        back_populates="seo_generation", cascade="all, delete-orphan"
    )
    trend_seasonal_assessments: Mapped[list["SEOTrendSeasonalAssessment"]] = relationship(
        back_populates="seo_generation", cascade="all, delete-orphan"
    )


class SEOKeywordIntelligence(Base):
    """Deterministic, generation-scoped keyword assessment and candidate snapshot."""

    __tablename__ = "seo_keyword_intelligence"
    __table_args__ = (
        Index("ix_seo_keyword_intelligence_status_computed", "status", "computed_at"),
        CheckConstraint(
            "status IN ('completed', 'unavailable')",
            name="ck_seo_keyword_intelligence_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    seo_generation_id: Mapped[int] = mapped_column(
        ForeignKey("seo_generations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    keyword_items: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    candidate_sets: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    quality_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    external_signals: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    signal_origins: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    seo_generation: Mapped[SEOGeneration] = relationship(back_populates="keyword_intelligence")


class SEOQualityAssessment(Base):
    """Versioned deterministic SEO score and validation result for a generation."""

    __tablename__ = "seo_quality_assessments"
    __table_args__ = (
        Index("ix_seo_quality_assessments_score_version", "score_version"),
        CheckConstraint("overall_score >= 0 AND overall_score <= 100", name="ck_seo_quality_assessment_score"),
        CheckConstraint(
            "validation_status IN ('PASS', 'WARN', 'FAIL')",
            name="ck_seo_quality_assessment_validation_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    seo_generation_id: Mapped[int] = mapped_column(
        ForeignKey("seo_generations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    assessed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    score_version: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_version: Mapped[str] = mapped_column(String(64), nullable=False)
    calculation_type: Mapped[str] = mapped_column(String(32), nullable=False, default="deterministic_heuristic")
    overall_score: Mapped[int] = mapped_column(nullable=False)
    score_breakdown: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    validation_status: Mapped[str] = mapped_column(String(16), nullable=False)
    validation_result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    seo_generation: Mapped[SEOGeneration] = relationship(back_populates="quality_assessment")


class SEOTrendSeasonalAssessment(Base):
    """Versioned calendar-derived and provider-reported context for one SEO generation."""

    __tablename__ = "seo_trend_seasonal_assessments"
    __table_args__ = (
        UniqueConstraint(
            "seo_generation_id", "reference_date", "region_code",
            "algorithm_version", "calendar_version",
            name="uq_seo_trend_seasonal_generation_context_version",
        ),
        CheckConstraint(
            "seasonal_score >= 0 AND seasonal_score <= 100",
            name="ck_seo_trend_seasonal_score",
        ),
        Index("ix_seo_trend_seasonal_region_date", "region_code", "reference_date"),
        Index("ix_seo_trend_seasonal_generation", "seo_generation_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    seo_generation_id: Mapped[int] = mapped_column(
        ForeignKey("seo_generations.id", ondelete="CASCADE"), nullable=False
    )
    reference_date: Mapped[date] = mapped_column(Date, nullable=False)
    region_code: Mapped[str] = mapped_column(String(16), nullable=False, default="GLOBAL")
    assessed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    calendar_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="calendar")
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="calendar_derived")
    calculation_status: Mapped[str] = mapped_column(String(24), nullable=False, default="computed")
    seasonal_score: Mapped[int] = mapped_column(nullable=False, default=0)
    score_breakdown: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    calendar_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    keyword_matches: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    recommendations: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    external_trend_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    seo_generation: Mapped[SEOGeneration] = relationship(back_populates="trend_seasonal_assessments")


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
    publish_intents: Mapped[list["PinterestPublishIntent"]] = relationship(back_populates="account")


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
    __table_args__ = (
        UniqueConstraint("account_id", "board_id", name="uq_pinterest_boards_account_external_id"),
        Index("ix_pinterest_boards_board_id", "board_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("pinterest_accounts.id"), nullable=False)
    board_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    privacy: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)
    metadata_version: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    account: Mapped[PinterestAccount] = relationship(back_populates="boards")
    published_pins: Mapped[list["PublishedPinterestPin"]] = relationship(back_populates="board")
    publish_intents: Mapped[list["PinterestPublishIntent"]] = relationship(back_populates="board")
    seo_profiles: Mapped[list["PinterestBoardSEOProfile"]] = relationship(
        back_populates="board", cascade="all, delete-orphan"
    )
    recommendations: Mapped[list["PinterestBoardRecommendation"]] = relationship(back_populates="board")


class PinterestBoardSEOProfile(Base):
    """Immutable deterministic SEO profile for one observed board metadata version."""

    __tablename__ = "pinterest_board_seo_profiles"
    __table_args__ = (
        UniqueConstraint(
            "board_id", "metadata_fingerprint", "algorithm_version",
            name="uq_pinterest_board_seo_profile_version",
        ),
        Index("ix_pinterest_board_seo_profiles_board_computed", "board_id", "computed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    board_id: Mapped[int] = mapped_column(
        ForeignKey("pinterest_boards.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    metadata_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    calculation_type: Mapped[str] = mapped_column(String(32), nullable=False, default="deterministic_computed")
    input_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    normalized_terms: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    keyword_items: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    search_intents: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    audience_signals: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    use_case_signals: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    topic_signals: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    external_signals: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    board: Mapped[PinterestBoard] = relationship(back_populates="seo_profiles")
    recommendations: Mapped[list["PinterestBoardRecommendation"]] = relationship(back_populates="board_profile")


class PinterestBoardRecommendation(Base):
    """Explainable SEO-generation-to-board match; never a publish instruction."""

    __tablename__ = "pinterest_board_recommendations"
    __table_args__ = (
        UniqueConstraint(
            "seo_generation_id", "board_profile_id", "algorithm_version", "scope_key", "cohort_fingerprint",
            name="uq_pinterest_board_recommendation_generation_profile_version",
        ),
        CheckConstraint("match_score >= 0 AND match_score <= 100", name="ck_pinterest_board_recommendation_score"),
        CheckConstraint(
            "status IN ('recommended', 'candidate', 'rejected')",
            name="ck_pinterest_board_recommendation_status",
        ),
        Index("ix_pinterest_board_recommendations_generation_rank", "seo_generation_id", "rank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    seo_generation_id: Mapped[int] = mapped_column(
        ForeignKey("seo_generations.id", ondelete="CASCADE"), nullable=False
    )
    board_id: Mapped[int | None] = mapped_column(
        ForeignKey("pinterest_boards.id", ondelete="SET NULL"), nullable=True
    )
    board_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("pinterest_board_seo_profiles.id", ondelete="SET NULL"), nullable=True
    )
    account_identifier_snapshot: Mapped[str | None] = mapped_column(String(255))
    external_board_id_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    board_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False)
    cohort_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    match_score: Mapped[int] = mapped_column(nullable=False)
    rank: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    match_breakdown: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    positive_signals: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    negative_signals: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    seo_generation: Mapped[SEOGeneration] = relationship(back_populates="board_recommendations")
    board: Mapped[PinterestBoard | None] = relationship(back_populates="recommendations")
    board_profile: Mapped[PinterestBoardSEOProfile | None] = relationship(back_populates="recommendations")


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
    __table_args__ = (
        Index(
            "uq_analytics_snapshots_published_daily_metric",
            "published_pin_id",
            "metric_date",
            unique=True,
            sqlite_where=text("metric_schema_version = 'pinterest_v5_organic_daily' AND metric_date IS NOT NULL"),
            postgresql_where=text("metric_schema_version = 'pinterest_v5_organic_daily' AND metric_date IS NOT NULL"),
        ),
    )

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
    seo_generation_id: Mapped[int | None] = mapped_column(
        ForeignKey("seo_generations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metadata_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    pin: Mapped[Pin | None] = relationship(back_populates="published_pins")
    account: Mapped[PinterestAccount | None] = relationship(back_populates="published_pins")
    board: Mapped[PinterestBoard | None] = relationship(back_populates="published_pins")
    analytics: Mapped[list[AnalyticsSnapshot]] = relationship(back_populates="published_pin")
    seo_generation: Mapped[SEOGeneration | None] = relationship()

    @classmethod
    def capture_metadata(cls, pin: Pin) -> dict:
        """Copy only analysis-relevant creative fields at publication time."""
        creative = pin.creative
        seo = deepcopy(creative.seo_metadata or {}) if creative else {}
        seo_generation = (
            creative.seo_generations[-1]
            if creative and creative.seo_generations
            else None
        )
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
            "seo_generation_id": seo_generation.id if seo_generation else None,
            "seo_provenance_status": "known" if seo_generation else "unknown",
            "image_media_reference": (
                pin.image_path or (creative.image_path or creative.source_image_url if creative else None)
            ),
        }


class PinterestPublishIntent(Base):
    """Durable idempotency claim for publishing one local Pin to one account.

    This is a local coordination record, not evidence that Pinterest has a Pin.
    Only a provider-confirmed result creates a PublishedPinterestPin.
    """

    __tablename__ = "pinterest_publish_intents"
    __table_args__ = (
        UniqueConstraint(
            "pin_id",
            "account_identifier_snapshot",
            name="uq_pinterest_publish_intents_pin_account_snapshot",
        ),
        CheckConstraint(
            "status IN ('publishing', 'published', 'failed', 'unknown')",
            name="ck_pinterest_publish_intents_status",
        ),
        Index(
            "ix_pinterest_publish_intents_account_status_created",
            "account_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    pin_id: Mapped[int] = mapped_column(
        ForeignKey("pins.id", ondelete="RESTRICT"), nullable=False
    )
    # Keep the intent if a Pinterest account is disconnected, without retaining credentials.
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("pinterest_accounts.id", ondelete="SET NULL"), nullable=True
    )
    account_identifier_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    board_id: Mapped[int | None] = mapped_column(
        ForeignKey("pinterest_boards.id", ondelete="SET NULL"), nullable=True
    )
    # Optional attribution for a deliberately requested SEO A/B variant publish.
    # Legacy intents remain NULL and are never retroactively attributed.
    seo_ab_variant_id: Mapped[int | None] = mapped_column(
        ForeignKey("seo_ab_variants.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    published_pin_id: Mapped[int | None] = mapped_column(
        ForeignKey("published_pinterest_pins.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(64), default=lambda: uuid4().hex, unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default=PinterestPublishIntentStatus.PUBLISHING.value, nullable=False
    )
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_naive_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_naive_now, onupdate=_utc_naive_now, nullable=False
    )

    pin: Mapped[Pin] = relationship(back_populates="publish_intents")
    account: Mapped[PinterestAccount | None] = relationship(back_populates="publish_intents")
    board: Mapped[PinterestBoard | None] = relationship(back_populates="publish_intents")
    published_pin: Mapped[PublishedPinterestPin | None] = relationship()
    seo_ab_variant: Mapped["SEOABVariant | None"] = relationship()


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
            "uq_account_analytics_pinterest_daily_metric",
            "account_id",
            "metric_date",
            unique=True,
            sqlite_where=text("metric_schema_version = 'pinterest_v5_organic_daily' AND metric_date IS NOT NULL"),
            postgresql_where=text("metric_schema_version = 'pinterest_v5_organic_daily' AND metric_date IS NOT NULL"),
        ),
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


class SEOPerformanceLearning(Base):
    """Immutable, versioned learning output from existing Pin analytics snapshots."""

    __tablename__ = "seo_performance_learnings"
    __table_args__ = (
        UniqueConstraint("algorithm_version", "source_fingerprint", name="uq_seo_performance_learning_source"),
        CheckConstraint(
            "status IN ('completed', 'insufficient_data', 'missing_provenance')",
            name="ck_seo_performance_learning_status",
        ),
        Index("ix_seo_performance_learnings_period", "window_start", "window_end"),
        Index("ix_seo_performance_learnings_account_period", "account_id", "window_start", "window_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("pinterest_accounts.id", ondelete="SET NULL"), nullable=True
    )
    account_identifier_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    calculation_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class Experiment(Base):
    """A manually evaluated creative test; it never selects or applies a winner."""

    __tablename__ = "experiments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'running', 'completed', 'paused', 'cancelled')",
            name="ck_experiments_status",
        ),
        CheckConstraint(
            "evaluation_metric IN ('impressions', 'saves', 'outbound_clicks', 'pin_clicks', "
            "'engagements', 'engagement_rate', 'pin_click_rate', 'outbound_click_rate')",
            name="ck_experiments_evaluation_metric",
        ),
        Index("ix_experiments_status_start", "status", "start_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    hypothesis: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    evaluation_metric: Mapped[str] = mapped_column(String(32), nullable=False)
    pinterest_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("pinterest_accounts.id", ondelete="SET NULL"), nullable=True
    )
    account_identifier_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    start_at: Mapped[datetime | None] = mapped_column(DateTime)
    end_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    variants: Mapped[list["ExperimentVariant"]] = relationship(back_populates="experiment")
    assignments: Mapped[list["ExperimentAssignment"]] = relationship(
        back_populates="experiment",
        primaryjoin="Experiment.id == foreign(ExperimentAssignment.experiment_id)",
        foreign_keys="ExperimentAssignment.experiment_id",
        viewonly=True,
    )
    evaluations: Mapped[list["ExperimentEvaluation"]] = relationship(back_populates="experiment")
    pinterest_account: Mapped[PinterestAccount | None] = relationship()


class ExperimentVariant(Base):
    """A named, immutable-at-assignment experiment treatment definition."""

    __tablename__ = "experiment_variants"
    __table_args__ = (
        UniqueConstraint("experiment_id", "name", name="uq_experiment_variants_experiment_name"),
        UniqueConstraint("id", "experiment_id", name="uq_experiment_variants_id_experiment"),
        Index("ix_experiment_variants_experiment", "experiment_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    creative_type: Mapped[str | None] = mapped_column(String(32))
    source_type: Mapped[str | None] = mapped_column(String(16))
    creative_angle: Mapped[str | None] = mapped_column(String(255))
    primary_keyword: Mapped[str | None] = mapped_column(String(255))
    audience_definition: Mapped[str | None] = mapped_column(Text)
    configuration: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    experiment: Mapped[Experiment] = relationship(back_populates="variants")
    assignments: Mapped[list["ExperimentAssignment"]] = relationship(
        back_populates="variant",
        primaryjoin="and_(ExperimentVariant.id == ExperimentAssignment.variant_id, "
        "ExperimentVariant.experiment_id == ExperimentAssignment.experiment_id)",
        foreign_keys="[ExperimentAssignment.variant_id, ExperimentAssignment.experiment_id]",
        viewonly=True,
    )


class ExperimentAssignment(Base):
    """Historical link of exactly one creative or publication to one variant."""

    __tablename__ = "experiment_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["variant_id", "experiment_id"],
            ["experiment_variants.id", "experiment_variants.experiment_id"],
            ondelete="RESTRICT",
            name="fk_experiment_assignment_variant_experiment",
        ),
        ForeignKeyConstraint(
            ["creative_id"], ["pin_creatives.id"], ondelete="RESTRICT",
            name="fk_experiment_assignment_creative",
        ),
        ForeignKeyConstraint(
            ["published_pin_id"], ["published_pinterest_pins.id"], ondelete="RESTRICT",
            name="fk_experiment_assignment_published_pin",
        ),
        CheckConstraint(
            "(creative_id IS NOT NULL AND published_pin_id IS NULL) OR "
            "(creative_id IS NULL AND published_pin_id IS NOT NULL)",
            name="ck_experiment_assignment_exactly_one_target",
        ),
        UniqueConstraint("experiment_id", "creative_id", name="uq_experiment_assignment_creative"),
        UniqueConstraint("experiment_id", "published_pin_id", name="uq_experiment_assignment_published_pin"),
        Index("ix_experiment_assignments_experiment_variant", "experiment_id", "variant_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    variant_id: Mapped[int] = mapped_column(Integer, nullable=False)
    creative_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_pin_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    assignment_reason: Mapped[str | None] = mapped_column(Text)
    metadata_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    experiment: Mapped[Experiment] = relationship(
        back_populates="assignments",
        primaryjoin="Experiment.id == foreign(ExperimentAssignment.experiment_id)",
        foreign_keys=[experiment_id],
        viewonly=True,
    )
    variant: Mapped[ExperimentVariant] = relationship(
        back_populates="assignments",
        primaryjoin="and_(ExperimentAssignment.variant_id == ExperimentVariant.id, "
        "ExperimentAssignment.experiment_id == ExperimentVariant.experiment_id)",
        foreign_keys=[variant_id, experiment_id],
        viewonly=True,
    )
    creative: Mapped[PinCreative | None] = relationship()
    published_pin: Mapped[PublishedPinterestPin | None] = relationship()

    @classmethod
    def capture_metadata(
        cls,
        *,
        creative: PinCreative | None = None,
        published_pin: PublishedPinterestPin | None = None,
    ) -> dict:
        if (creative is None) == (published_pin is None):
            raise ValueError("Exactly one creative or published Pin is required")
        if published_pin is not None:
            snapshot = deepcopy(published_pin.metadata_snapshot or {})
            seo = snapshot.get("seo_metadata") or {}
            return {
                "creative_type": snapshot.get("creative_type"),
                "source_type": snapshot.get("source_type"),
                "creative_angle": snapshot.get("creative_angle") or seo.get("creative_angle"),
                "primary_keyword": snapshot.get("primary_keyword") or seo.get("primary_keyword"),
                "audience": snapshot.get("audience") or seo.get("audience_keywords"),
                "destination_url": snapshot.get("destination_etsy_url"),
                "published_pin_id": published_pin.id,
            }
        seo = deepcopy(creative.seo_metadata or {})
        return {
            "creative_type": creative.creative_type,
            "source_type": creative.source_type,
            "creative_angle": seo.get("creative_angle"),
            "primary_keyword": seo.get("primary_keyword"),
            "audience": seo.get("audience_keywords") or seo.get("audience"),
            "destination_url": creative.destination_url,
            "creative_id": creative.id,
        }


class ExperimentEvaluation(Base):
    """One evaluation specification/run; raw analytics remain in existing tables."""

    __tablename__ = "experiment_evaluations"
    __table_args__ = (
        Index("ix_experiment_evaluations_experiment_evaluated", "experiment_id", "evaluated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metric_name: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    calculation_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    snapshot_ids: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)

    experiment: Mapped[Experiment] = relationship(back_populates="evaluations")
    variant_results: Mapped[list["ExperimentEvaluationResult"]] = relationship(back_populates="evaluation")


class ExperimentEvaluationResult(Base):
    """Frozen, variant-level aggregate for an evaluation and its source snapshot IDs."""

    __tablename__ = "experiment_evaluation_results"
    __table_args__ = (
        UniqueConstraint("evaluation_id", "variant_id", name="uq_experiment_evaluation_variant"),
        Index("ix_experiment_evaluation_results_variant", "variant_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(
        ForeignKey("experiment_evaluations.id", ondelete="RESTRICT"), nullable=False
    )
    variant_id: Mapped[int] = mapped_column(
        ForeignKey("experiment_variants.id", ondelete="RESTRICT"), nullable=False
    )
    assignment_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    published_pin_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    impressions: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    saves: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pin_clicks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    outbound_clicks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    engagements: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    engagement_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    pin_click_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    outbound_click_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    impressions_denominator: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    selected_metric_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    source_snapshot_ids: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)

    evaluation: Mapped[ExperimentEvaluation] = relationship(back_populates="variant_results")
    variant: Mapped[ExperimentVariant] = relationship()


class SEOABExperiment(Base):
    """Local, non-publishing experiment based on an immutable SEO generation."""

    __tablename__ = "seo_ab_experiments"
    __table_args__ = (
        UniqueConstraint("source_generation_id", "idempotency_key", name="uq_seo_ab_experiment_source_idempotency"),
        CheckConstraint("status IN ('DRAFT', 'READY', 'RUNNING', 'PAUSED', 'COMPLETED', 'CANCELLED')", name="ck_seo_ab_experiment_status"),
        Index("ix_seo_ab_experiments_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_generation_id: Mapped[int] = mapped_column(ForeignKey("seo_generations.id", ondelete="RESTRICT"), nullable=False)
    experiment_version: Mapped[str] = mapped_column(String(64), nullable=False, default="seo_ab_experiment_v1")
    variation_version: Mapped[str] = mapped_column(String(64), nullable=False, default="seo_variation_v1")
    comparison_version: Mapped[str] = mapped_column(String(64), nullable=False, default="seo_ab_comparison_v1")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT")
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    hypothesis_source: Mapped[str] = mapped_column(String(16), nullable=False, default="human_defined")
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="local_seo_only")
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False, default="seo_variation_v1")
    source_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    provenance_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_naive_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_naive_now, onupdate=_utc_naive_now)

    source_generation: Mapped[SEOGeneration] = relationship()
    variants: Mapped[list["SEOABVariant"]] = relationship(back_populates="experiment", order_by="SEOABVariant.variant_key")
    comparisons: Mapped[list["SEOABComparison"]] = relationship(back_populates="experiment")


class SEOABVariant(Base):
    """Immutable local SEO candidate; never a publish command."""

    __tablename__ = "seo_ab_variants"
    __table_args__ = (
        UniqueConstraint("experiment_id", "variant_key", name="uq_seo_ab_variant_experiment_key"),
        CheckConstraint("variant_type IN ('KEYWORD_FOCUS')", name="ck_seo_ab_variant_supported_type"),
        CheckConstraint("status IN ('READY', 'QUALITY_FAIL', 'ARCHIVED')", name="ck_seo_ab_variant_status"),
        CheckConstraint("quality_status IN ('PASS', 'WARN', 'FAIL')", name="ck_seo_ab_variant_quality_status"),
        Index("ix_seo_ab_variants_experiment_status", "experiment_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("seo_ab_experiments.id", ondelete="RESTRICT"), nullable=False)
    variant_key: Mapped[str] = mapped_column(String(64), nullable=False)
    variant_name: Mapped[str] = mapped_column(String(128), nullable=False)
    variant_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="READY")
    output_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    change_set: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    keyword_intelligence_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    quality_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    quality_status: Mapped[str] = mapped_column(String(8), nullable=False)
    quality_score: Mapped[int] = mapped_column(Integer, nullable=False)
    provenance_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False, default="seo_variation_v1")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="deterministic")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_naive_now)

    experiment: Mapped[SEOABExperiment] = relationship(back_populates="variants")
    publications: Mapped[list["SEOABVariantPublication"]] = relationship(back_populates="variant")


class SEOABVariantPublication(Base):
    """Explicit, verified association made only after an external publication exists."""

    __tablename__ = "seo_ab_variant_publications"
    __table_args__ = (
        UniqueConstraint("variant_id", "published_pin_id", name="uq_seo_ab_variant_publication_variant_pin"),
        UniqueConstraint("published_pin_id", name="uq_seo_ab_variant_publication_pin"),
        Index("ix_seo_ab_variant_publications_variant", "variant_id"),
        Index("ix_seo_ab_variant_publications_pin", "published_pin_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("seo_ab_variants.id", ondelete="RESTRICT"), nullable=False)
    published_pin_id: Mapped[int] = mapped_column(ForeignKey("published_pinterest_pins.id", ondelete="RESTRICT"), nullable=False)
    linked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_naive_now)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="explicit_verified_link")

    variant: Mapped[SEOABVariant] = relationship(back_populates="publications")
    published_pin: Mapped[PublishedPinterestPin] = relationship()


class SEOABComparison(Base):
    """Frozen observed comparison over explicit daily analytics snapshots."""

    __tablename__ = "seo_ab_comparisons"
    __table_args__ = (
        UniqueConstraint("experiment_id", "comparison_version", "source_fingerprint", name="uq_seo_ab_comparison_fingerprint"),
        CheckConstraint("period_start <= period_end", name="ck_seo_ab_comparison_period"),
        Index("ix_seo_ab_comparisons_experiment_created", "experiment_id", "calculated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("seo_ab_experiments.id", ondelete="RESTRICT"), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(32), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_naive_now)
    comparison_version: Mapped[str] = mapped_column(String(64), nullable=False, default="seo_ab_comparison_v1")
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    result_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    experiment: Mapped[SEOABExperiment] = relationship(back_populates="comparisons")
