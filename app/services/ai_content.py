"""Provider-neutral structured Pinterest content generation."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy.orm import Session

from app.config import settings
from app.models import EtsyListing, PinCreative, PinGenerationJob, Product, SEOGeneration
from app.models.core import PinCreativeSourceType, PinCreativeStatus, PinCreativeType
from app.services.ai_pipeline import (
    reserve_ai_capacity,
    finish_reservations,
    release_reservations,
)


class AIContentError(Exception):
    """A safe error to show when structured content cannot be generated."""


class AIValidationError(AIContentError):
    """The provider replied, but its structured SEO output was not acceptable.

    A later generation may comply with the same prompt, so workers treat this
    separately from permanent configuration or product-data failures.
    """


class AIProviderRequestError(AIContentError):
    """Sanitized provider failure metadata; never retains a raw SDK exception."""

    def __init__(
        self,
        category: str,
        *,
        http_status: int | None = None,
        provider_code: str | None = None,
        retryable: bool = False,
    ) -> None:
        self.category = category
        self.http_status = http_status
        self.provider_code = provider_code
        self.retryable = retryable
        safe_parts = [f"category={category}"]
        if http_status is not None:
            safe_parts.append(f"http_status={http_status}")
        if provider_code is not None:
            safe_parts.append(f"provider_code={provider_code}")
        super().__init__("Gemini request failed (" + ", ".join(safe_parts) + ")")


_logger = logging.getLogger(__name__)
_SAFE_GEMINI_STATUS_CODES = {
    "INVALID_ARGUMENT",
    "UNAUTHENTICATED",
    "PERMISSION_DENIED",
    "RESOURCE_EXHAUSTED",
    "NOT_FOUND",
    "FAILED_PRECONDITION",
    "INTERNAL",
    "UNAVAILABLE",
    "DEADLINE_EXCEEDED",
}


def _classify_gemini_exception(exc: Exception) -> AIProviderRequestError:
    """Map SDK/transport exceptions to safe categories without reading messages."""
    import httpx

    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__

    if any(isinstance(item, ssl.SSLError) for item in chain):
        return AIProviderRequestError("tls_error", retryable=False)
    if any(isinstance(item, socket.gaierror) for item in chain):
        return AIProviderRequestError("dns_error", retryable=True)
    if any(isinstance(item, httpx.TimeoutException) for item in chain):
        return AIProviderRequestError("timeout", retryable=True)
    if any(isinstance(item, httpx.ConnectError) for item in chain):
        return AIProviderRequestError("network_error", retryable=True)

    try:
        from google.genai.errors import APIError
    except ImportError:  # pragma: no cover - SDK is a declared dependency
        APIError = ()  # type: ignore[assignment,misc]

    api_error = next((item for item in chain if isinstance(item, APIError)), None) if APIError else None
    status_code = getattr(api_error, "code", None)
    status_code = status_code if isinstance(status_code, int) and 100 <= status_code <= 599 else None
    raw_provider_code = getattr(api_error, "status", None)
    provider_code = (
        raw_provider_code
        if isinstance(raw_provider_code, str) and raw_provider_code in _SAFE_GEMINI_STATUS_CODES
        else None
    )
    if status_code == 401:
        category, retryable = "authentication_error", False
    elif status_code == 403:
        category, retryable = "permission_error", False
    elif status_code == 429:
        category, retryable = "rate_limit", True
    elif status_code == 404:
        category, retryable = "model_or_endpoint_not_found", False
    elif status_code is not None and 500 <= status_code <= 599:
        category, retryable = "provider_server_error", True
    elif status_code is not None and 400 <= status_code <= 499:
        category, retryable = "request_rejected", False
    elif any(isinstance(item, httpx.TimeoutException) for item in chain):
        category, retryable = "timeout", True
    elif any(isinstance(item, httpx.RequestError) for item in chain):
        category, retryable = "network_error", True
    else:
        category, retryable = "unknown_provider_error", False

    return AIProviderRequestError(
        category,
        http_status=status_code,
        provider_code=provider_code,
        retryable=retryable,
    )


SEO_PROMPT_VERSION = "pinterest_seo_v2"
SEO_SCHEMA_VERSION = "pinterest_seo_metadata_v2"


@dataclass(frozen=True)
class ProductContext:
    title: str
    description: str
    tags: list[str]
    price: str | None
    url: str | None
    images: list[str]


class AIContentProvider(Protocol):
    """Implement this protocol to add a real LLM provider."""

    def generate_json(self, prompt: str) -> str: ...


class MockAIContentProvider:
    """Deterministic local provider for development and tests."""

    provider_name = "mock"
    model_name = "unknown"

    def generate_json(self, prompt: str) -> str:
        payload = json.loads(prompt.rsplit("INPUT_JSON=", 1)[1])
        product = payload["product"]
        creative_type = payload["creative_type"]
        variation = payload["variation"]

        angles = {
            "product_focus": "product details",
            "lifestyle": "everyday style",
            "problem_solution": "practical use",
            "gift_idea": "thoughtful gifting",
            "minimalist": "minimalist style",
        }
        intents = {
            "product_focus": ["product_search"],
            "lifestyle": ["aesthetic_style_intent"],
            "problem_solution": ["use_case_intent"],
            "gift_idea": ["gift_intent"],
            "minimalist": ["aesthetic_style_intent"],
        }
        candidates = product["tags"] or _keywords_from_title(product["title"])
        base_keyword = next((value for value in candidates if value.casefold() != "product"), candidates[0])
        angle = angles[creative_type]
        primary_keyword = f"{base_keyword} {angle} {variation}"
        product_type = payload.get("product_type_hint") or base_keyword

        return json.dumps(
            {
                "title": primary_keyword.title(),
                "description": (
                    f"Discover {product['title']} for {angle}. "
                    f"{product['description'][:220]}"
                ).strip(),
                "call_to_action": "See details",
                "seo": {
                    "primary_keyword": primary_keyword,
                    "secondary_keywords": [base_keyword],
                    "long_tail_keywords": [f"{base_keyword} {product_type} {angle}"],
                    "audience_keywords": ["thoughtful shoppers"],
                    "use_case_keywords": [angle],
                    "search_intents": intents[creative_type],
                    "creative_angle": f"{angle} angle {variation}",
                },
            },
            ensure_ascii=False,
        )


class GeminiAIContentProvider:
    """Google Gemini provider for Pinterest title/description/keyword generation."""

    provider_name = "gemini"

    def __init__(self) -> None:
        if not settings.gemini_api_key:
            raise AIContentError(
                "Gemini API için GEMINI_API_KEY yapılandırılmamış."
            )

        try:
            from google import genai
        except ImportError as exc:
            raise AIContentError(
                "Gemini SDK kurulu değil. google-genai paketini yükleyin."
            ) from exc

        self._client = genai.Client(api_key=settings.gemini_api_key)

    def generate_json(self, prompt: str) -> str:
        try:
            from google.genai import types

            response = self._client.models.generate_content(
                model=settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
        except Exception as exc:
            failure = _classify_gemini_exception(exc)
            # Log only allowlisted classification fields. SDK messages/response
            # bodies can contain sensitive request metadata and are never logged.
            _logger.warning(
                "Gemini request failed category=%s http_status=%s provider_code=%s retryable=%s",
                failure.category,
                failure.http_status,
                failure.provider_code,
                failure.retryable,
            )
            raise failure from None

        text = getattr(response, "text", None)

        if not text:
            raise AIContentError(
                "Gemini geçerli bir içerik döndürmedi."
            )

        return text

    @property
    def model_name(self) -> str:
        # Capture the configured model at generation time; never store credentials.
        return settings.gemini_model or "unknown"


class DisabledRemoteAIProvider:
    """Prevents accidental network calls for unsupported providers."""

    def generate_json(self, prompt: str) -> str:
        if not settings.ai_api_key:
            raise AIContentError(
                "Gerçek AI sağlayıcısı için AI_API_KEY yapılandırılmamış."
            )

        raise AIContentError(
            "Seçilen gerçek AI sağlayıcısı henüz yapılandırılmadı. "
            "AI_PROVIDER=mock veya AI_PROVIDER=gemini kullanın."
        )


def get_ai_provider() -> AIContentProvider:
    provider = settings.ai_provider.lower()

    if provider == "mock":
        return MockAIContentProvider()

    if provider == "gemini":
        return GeminiAIContentProvider()

    return DisabledRemoteAIProvider()


@dataclass(frozen=True)
class GeneratedCreative:
    title: str
    description: str
    keywords: list[str]
    call_to_action: str
    seo_metadata: dict[str, object]


class AIContentService:
    """
    Build product-aware Pinterest creatives.

    Flow:

    Etsy product
        ↓
    Gemini SEO content
        ↓
    OpenAI Pinterest image
        ↓
    PinCreative database record
        ↓
    Etsy destination URL
    """

    def __init__(
        self,
        db: Session,
        provider: AIContentProvider | None = None,
    ):
        self.db = db
        # Keep provider setup lazy: syncing Etsy mockups must not require or initialize Gemini.
        self.provider = provider
        self._active_generation: dict[str, object] | None = None

    def product_context(self, product: Product) -> ProductContext:
        listing = (
            self.db.query(EtsyListing)
            .filter_by(product_id=product.id)
            .one_or_none()
        )

        title = ((listing.title if listing else None) or product.title or "").strip()
        description = ((listing.description if listing else None) or product.description or "").strip()

        tags = listing.tags if listing else []

        images = (
            listing.images
            if listing
            else ([product.image_url] if product.image_url else [])
        )

        price = (
            f"{listing.price} {listing.currency or ''}".strip()
            if listing and listing.price is not None
            else None
        )

        if not title and not description and not tags:
            raise AIContentError(
                "Bu ürün için içerik üretmeye yetecek başlık, "
                "açıklama veya Etsy etiketi yok."
            )

        return ProductContext(
            title=title or "Ürün",
            description=description,
            tags=tags,
            price=price,
            url=product.url,
            images=images,
        )

    def generate(
        self,
        product: Product,
        creative_type: PinCreativeType,
        desired_count: int,
        *,
        job_id: int | None = None,
        force_new: bool = False,
    ) -> list[PinCreative]:
        """
        Generate one or more Pinterest creatives for an Etsy product.

        Each creative consists of:

        1. Gemini-generated Pinterest SEO content.
        2. OpenAI-generated Pinterest image when AI_IMAGE_PROVIDER=openai.
        3. Etsy product destination URL.
        """

        if not force_new:
            existing_count = self.db.query(PinCreative.id).filter_by(
                product_id=product.id,
                creative_type=creative_type.value,
                source_type=PinCreativeSourceType.AI.value,
            ).count()
            desired_count = max(0, desired_count - existing_count)
        if desired_count <= 0:
            return []
        slots = reserve_ai_capacity(self.db, desired_count, job_id=job_id)
        try:
            created = self._generate_reserved(product, creative_type, len(slots))
            finish_reservations(self.db, slots, created)
            return created
        except Exception:
            self.db.rollback()
            release_reservations(self.db, [slot.id for slot in slots])
            self._persist_failed_generation()
            raise

    def _persist_failed_generation(self) -> None:
        """Keep a sanitized failed attempt after the creative transaction rolls back."""
        attempt = self._active_generation
        self._active_generation = None
        if not attempt:
            return
        try:
            self.db.add(SEOGeneration(
                product_id=attempt["product_id"],
                started_at=attempt["started_at"],
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                provider=attempt["provider"],
                model_name=attempt["model_name"],
                prompt_version=SEO_PROMPT_VERSION,
                schema_version=SEO_SCHEMA_VERSION,
                status="failed",
                output_snapshot=attempt.get("output_snapshot"),
                error_category=attempt.get("error_category", "generation_error"),
            ))
            self.db.commit()
        except Exception:
            # Provenance persistence must not replace or expose the original error.
            self.db.rollback()

    def _generate_reserved(
        self,
        product: Product,
        creative_type: PinCreativeType,
        desired_count: int,
    ) -> list[PinCreative]:
        context = self.product_context(product)

        existing_keys = {
            key
            for (key,) in self.db.query(
                PinCreative.generation_key
            ).filter_by(
                product_id=product.id,
                creative_type=creative_type.value,
                source_type=PinCreativeSourceType.AI.value,
            )
        }

        created: list[PinCreative] = []
        previous_seo = self._previous_ai_seo_context(product.id)
        provider_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            provider = self.provider or get_ai_provider()
        except Exception:
            provider_name = settings.ai_provider.lower() or "unknown"
            model_name = settings.gemini_model if provider_name == "gemini" else "unknown"
            self._active_generation = {
                "product_id": product.id,
                "started_at": provider_started_at,
                "provider": provider_name[:64],
                "model_name": str(model_name or "unknown")[:128],
                "error_category": "configuration_error",
            }
            raise
        provider_name = getattr(provider, "provider_name", type(provider).__name__)[:64]
        model_name = getattr(provider, "model_name", "unknown")
        if callable(model_name):
            model_name = model_name()
        model_name = str(model_name or "unknown")[:128]

        image_provider = None

        # Only initialize the image provider when actual image
        # generation has been enabled.
        if (
            settings.ai_image_provider
            and settings.ai_image_provider.lower() == "openai"
        ):
            try:
                from app.services.ai_image import get_image_provider

                image_provider = get_image_provider()

            except Exception:
                raise AIContentError(
                    "Görsel sağlayıcısı başlatılamadı; yapılandırma ayrıntıları gizlendi."
                ) from None

        variation = 1
        while len(created) < desired_count:
            key = self._generation_key(
                product.id,
                creative_type.value,
                variation,
            )

            legacy_key = self._legacy_ai_generation_key(product.id, creative_type.value, variation)
            if key in existing_keys or legacy_key in existing_keys:
                variation += 1
                continue

            # ---------------------------------------------------------
            # 1. Generate Pinterest SEO content with Gemini
            # ---------------------------------------------------------

            started_at = datetime.now(timezone.utc).replace(tzinfo=None)
            self._active_generation = {
                "product_id": product.id,
                "started_at": started_at,
                "provider": provider_name,
                "model_name": model_name,
                "error_category": "provider_error",
            }
            try:
                raw_content = provider.generate_json(
                    self._prompt(context, creative_type.value, variation, previous_seo)
                )
            except Exception as exc:
                if isinstance(exc, AIProviderRequestError):
                    self._active_generation["error_category"] = f"gemini_{exc.category}"[:64]
                    raise
                provider_message = str(exc).casefold()
                if any(marker in provider_message for marker in (
                    "429", "rate limit", "resource_exhausted", "timeout", "temporar",
                    "connection", "500", "502", "503", "504",
                )):
                    raise AIContentError(
                        "AI içerik sağlayıcısı geçici olarak kullanılamıyor; ayrıntılar gizlendi."
                    ) from None
                raise AIContentError(
                    "AI içerik sağlayıcısı başarısız oldu; hata ayrıntıları güvenlik için gizlendi."
                ) from None
            self._active_generation["error_category"] = "validation_error"
            generated = self._parse_generated_json(
                raw_content,
                context,
                creative_type.value,
            )
            self._active_generation["output_snapshot"] = self._seo_output_snapshot(
                generated, creative_type.value
            )
            self._ensure_seo_is_novel(generated.seo_metadata, previous_seo)

            # ---------------------------------------------------------
            # 2. Create PinCreative database object
            # ---------------------------------------------------------

            creative = PinCreative(
                product=product,
                creative_type=creative_type.value,
                title=generated.title,
                description=generated.description,
                keywords=generated.keywords,
                seo_metadata=generated.seo_metadata,
                call_to_action=generated.call_to_action,

                # IMPORTANT:
                # Pinterest will use this URL as the destination
                # when the user clicks the Pin.
                destination_url=context.url,

                status=PinCreativeStatus.DRAFT.value,
                source_type=PinCreativeSourceType.AI.value,
                generation_key=key,
            )

            self.db.add(creative)
            existing_keys.add(key)

            # Flush so SQLAlchemy assigns an ID before the image
            # generation process is completed.
            self.db.flush()

            generation = SEOGeneration(
                product_id=product.id,
                creative=creative,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                provider=provider_name,
                model_name=model_name,
                prompt_version=SEO_PROMPT_VERSION,
                schema_version=SEO_SCHEMA_VERSION,
                status="completed",
                output_snapshot=self._seo_output_snapshot(generated, creative_type.value),
            )
            self.db.add(generation)
            self.db.flush()
            from app.services.keyword_intelligence import ensure_keyword_intelligence

            ensure_keyword_intelligence(
                self.db,
                generation,
                title=generated.title,
                description=generated.description,
                product_tags=context.tags,
            )
            from app.services.seo_quality import ensure_seo_quality_assessment

            ensure_seo_quality_assessment(self.db, generation, generation.keyword_intelligence)
            from app.services.board_intelligence import recommend_boards_for_all_accounts

            recommend_boards_for_all_accounts(self.db, generation.id)
            from app.services.trend_seasonal import ensure_trend_seasonal_assessment

            ensure_trend_seasonal_assessment(self.db, generation)
            self._active_generation = None

            # ---------------------------------------------------------
            # 3. Generate Pinterest image with OpenAI
            # ---------------------------------------------------------

            if image_provider is not None and context.images:
                # A later image failure still leaves a failed generation record with
                # the valid SEO output snapshot after the outer transaction rolls back.
                self._active_generation = {
                    "product_id": product.id,
                    "started_at": started_at,
                    "provider": provider_name,
                    "model_name": model_name,
                    "output_snapshot": self._seo_output_snapshot(generated, creative_type.value),
                    "error_category": "image_generation_error",
                }
                try:
                    from app.services.ai_image import save_generated_image

                    image_prompt = self._image_prompt(
                        context=context,
                        creative_type=creative_type.value,
                        variation=variation,
                        generated=generated,
                    )

                    image_bytes = image_provider.generate(
                        context.images[0],
                        image_prompt,
                    )

                    creative.image_path = save_generated_image(
                        image_bytes
                    )

                except Exception as exc:
                    raise AIContentError(
                        "Pinterest görseli oluşturulamadı; sağlayıcı hatası güvenlik için gizlendi."
                    ) from None

            self._active_generation = None

            created.append(creative)
            previous_seo.append(self._seo_context_item(generated.title, generated.seo_metadata))
            variation += 1

        self.db.flush()

        return created

    @staticmethod
    def _seo_output_snapshot(
        generated: GeneratedCreative, creative_type: str
    ) -> dict[str, object]:
        """Copy output fields used later for analytics attribution and audit."""
        return {
            "title": generated.title,
            "description": generated.description,
            "call_to_action": generated.call_to_action,
            "keywords": list(generated.keywords),
            "seo_metadata": dict(generated.seo_metadata),
            "creative_type": creative_type,
        }

    def ensure_mockup_creatives(self, product: Product, listing: EtsyListing) -> list[PinCreative]:
        """Add each Etsy image to the Pin pool once, without invoking an AI provider.

        Etsy synchronization must remain safe to run for a full shop: it may create
        local pool records, but it never triggers a paid text or image API request.
        """
        context = self.product_context(product)
        created: list[PinCreative] = []
        for image_url in listing.images or []:
            if not isinstance(image_url, str) or not image_url.strip():
                continue
            image_url = image_url.strip()
            key = self._mockup_generation_key(product.id, image_url)
            if self.db.query(PinCreative.id).filter_by(generation_key=key).first():
                continue
            title = f"{context.title} | Etsy product mockup"
            keywords = list(dict.fromkeys((context.tags or _keywords_from_title(context.title))))[:12]
            if not keywords:
                keywords = ["etsy product"]
            seo_metadata = self.mockup_seo_metadata(context, keywords)
            created.append(PinCreative(
                product=product,
                creative_type=PinCreativeType.PRODUCT_FOCUS.value,
                title=title[:255],
                description=(
                    f"View the original Etsy mockup for {context.title}. "
                    f"Explore product details, materials, and ordering information."
                ),
                keywords=keywords,
                seo_metadata=seo_metadata,
                call_to_action="View product details",
                # A canonical Etsy HTTPS URL is already appropriate for future public-image handling.
                image_path=image_url,
                source_image_url=image_url,
                source_type=PinCreativeSourceType.MOCKUP.value,
                destination_url=context.url,
                status=PinCreativeStatus.DRAFT.value,
                generation_key=key,
            ))
        self.db.add_all(created)
        return created

    def prepare_ai_generation_job(self, product: Product) -> PinGenerationJob | None:
        """Queue one local, non-executing AI generation job for a newly synced product.

        The scheduler may later claim this job while respecting its daily target.
        Creating this record never calls a text or image provider.
        """
        from app.services.ai_pipeline import create_generation_job

        return create_generation_job(self.db, product, 1)

    @staticmethod
    def mockup_seo_metadata(context: ProductContext, keywords: list[str]) -> dict:
        """Build grounded Etsy mockup metadata without calling a content provider."""
        title_terms = _keywords_from_title(context.title)
        description_terms = _keywords_from_title(context.description or "")
        clean = list(dict.fromkeys(
            re.sub(r"\s+", " ", item).strip(" ,.-")
            for item in [*keywords, *title_terms, *description_terms]
            if isinstance(item, str) and item.strip()
        ))
        primary = (keywords[0] if keywords else context.title.strip().lower()) or "etsy product"
        secondary = [item for item in clean if item.casefold() != primary.casefold()][:6]
        product_phrase = re.sub(r"\s+", " ", context.title).strip()
        description = re.sub(r"\s+", " ", context.description or "").strip()
        description = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)[0].strip(" .")
        use_case = re.search(r"\bfor\s+([^,.;!?]{3,64})", context.description or "", re.IGNORECASE)
        has_gift_signal = "gift" in " ".join([context.title, context.description, *keywords]).casefold()
        search_intents = ["product_search"]
        if has_gift_signal:
            search_intents.append("gift_intent")
        elif use_case:
            search_intents.append("use_case_intent")
        elif any(term in " ".join([context.title, context.description]).casefold()
                 for term in ("minimal", "modern", "vintage", "botanical", "floral")):
            search_intents.append("aesthetic_style_intent")
        return {
            "primary_keyword": primary,
            "secondary_keywords": secondary,
            "long_tail_keywords": [
                (description if description and primary.casefold() in description.casefold()
                 else (f"{primary} for {use_case.group(1).strip()}" if use_case
                       else f"{primary} {product_phrase}")).strip()[:160]
            ],
            "audience_keywords": ["gift shoppers", "home and style shoppers"],
            "use_case_keywords": [use_case.group(1).strip() if use_case else "product inspiration"],
            "search_intents": search_intents,
            "creative_angle": "Etsy product mockup and product details",
        }

    @staticmethod
    def _generation_key(
        product_id: int,
        creative_type: str,
        variation: int,
    ) -> str:
        return hashlib.sha256(
            f"ai:{product_id}:{creative_type}:{variation}".encode()
        ).hexdigest()

    @staticmethod
    def _mockup_generation_key(product_id: int, image_url: str) -> str:
        return hashlib.sha256(f"mockup:{product_id}:{image_url}".encode()).hexdigest()

    @staticmethod
    def _legacy_ai_generation_key(product_id: int, creative_type: str, variation: int) -> str:
        """Recognize records created before source types were introduced."""
        return hashlib.sha256(f"{product_id}:{creative_type}:{variation}".encode()).hexdigest()

    @staticmethod
    def _prompt(
        context: ProductContext,
        creative_type: str,
        variation: int,
        previous_creatives: list[dict[str, str]] | None = None,
    ) -> str:
        """Build a grounded, structured Pinterest SEO v2 Gemini prompt."""

        strategies = {
            "product_focus": "Focus on product-search and buying-research intent using supported details only.",
            "lifestyle": "Focus on aesthetic/style intent and a believable supported use environment.",
            "problem_solution": "Focus on one practical need only when the supplied product data supports it.",
            "gift_idea": "Focus on a plausible recipient and gifting occasion without inventing personalization.",
            "minimalist": "Focus on understated style and simple use without inventing materials or design details.",
        }

        data = {
            "product": {
                "title": context.title,
                "description": context.description,
                "tags": context.tags,
                "price": context.price,
                "url": context.url,
                # URLs are references only; do not infer visual facts from them.
                "image_urls": context.images,
            },
            "creative_type": creative_type,
            "product_type_hint": _product_type_hint(context),
            "variation": variation,
            "previous_ai_creatives": (previous_creatives or [])[-8:],
        }

        return (
            "Create one Pinterest SEO v2 creative for the supplied Etsy product. "
            "Return JSON only, with exactly title, description, call_to_action, and seo. "
            "The seo object must contain exactly primary_keyword, secondary_keywords, "
            "long_tail_keywords, audience_keywords, use_case_keywords, search_intents, "
            "and creative_angle. Keyword groups are JSON arrays of concise strings. "
            "search_intents may contain only product_search, gift_intent, "
            "aesthetic_style_intent, audience_intent, or use_case_intent. "
            "Use natural American English. Put the primary keyword naturally near the "
            "start of the title; avoid keyword stuffing, hashtags, clickbait, ranking "
            "or viral promises. Write an original, useful description connecting product, "
            "audience, and a supported use or gift situation. FACTUAL BOUNDARY: only use "
            "a product feature, material, compatibility, personalization, durability, "
            "protection, quality, longevity, or gift-suitability claim when it is explicitly "
            "supported by the supplied title, description, or tags. Never invent features, "
            "materials, personalization, discounts, reviews, compatibility, or claims. "
            "Make each long-tail keyword read like a natural American-English search query; "
            "when product_type_hint is available, every long-tail keyword must include that "
            "product type rather than a vague accessory term. Use one dominant marketing or "
            "search angle only: do not combine product, gifting, and lifestyle angles. "
            "Use five to twelve unique keyword phrases across all SEO groups. "
            "Previous AI creatives are avoidance context: never reuse their title pattern, "
            "primary_keyword, or creative_angle. Creative-type strategy: "
            + strategies.get(creative_type, strategies["product_focus"])
            + " "
            "INPUT_JSON="
            + json.dumps(data, ensure_ascii=False)
        )

    @staticmethod
    def _image_prompt(
        context: ProductContext,
        creative_type: str,
        variation: int,
        generated: GeneratedCreative,
    ) -> str:
        """
        Build the OpenAI image-edit prompt.

        The original Etsy product image is the visual source of truth.
        The model must preserve the actual product rather than redesign it.
        """

        creative_instructions = {
            "product_focus": (
                "Use a close three-quarter product view on a distinctive clean studio set, "
                "with the product as the hero and a simple shadow-led composition."
            ),
            "lifestyle": (
                "Show the product in a believable real-life use moment, using a wider "
                "eye-level camera angle and an environment appropriate to the product."
            ),
            "problem_solution": (
                "Tell a before-to-after visual story through an organized, practical use "
                "scene. Use an over-the-shoulder or top-down camera angle as appropriate."
            ),
            "gift_idea": (
                "Use a warm gifting moment with wrapping, a card, or a thoughtful reveal. "
                "Choose an intimate close scene rather than a generic product shot."
            ),
            "minimalist": (
                "Use a restrained editorial composition with generous negative space, "
                "a single complementary prop, and a deliberate off-center placement."
            ),
        }

        creative_instruction = creative_instructions.get(
            creative_type,
            creative_instructions["product_focus"],
        )

        return (
            "Create a professional Pinterest vertical product image "
            "using the provided Etsy product photo as the exact product reference. "

            "The source product image is the visual source of truth. "

            "PRESERVE THE EXACT PRODUCT IDENTITY. "
            "Preserve the exact product shape, proportions, colors, materials, "
            "artwork, logos, printed graphics and all visible printed text. "

            "If the product contains text, reproduce that text accurately. "

            "Do NOT redesign the product. "
            "Do NOT recolor it. "
            "Do NOT change its shape. "
            "Do NOT distort it. "
            "Do NOT mirror it. "
            "Do NOT replace its artwork. "
            "Do NOT invent product features. "
            "Do NOT remove important product details. "

            f"{creative_instruction} "

            "Make this variation a genuinely new visual story, not a crop, color shift, "
            "or small edit of the source photo. Change the scene, camera angle, styling, "
            "and composition in ways that suit the requested creative type. "

            "The product must remain clearly recognizable as the exact Etsy item. "

            "Use realistic lighting, realistic materials and a polished "
            "commercial photography aesthetic. "

            "Create a vertical Pinterest-friendly composition with the product "
            "prominent in the frame. "

            "Do not add fake prices, discounts, reviews, claims, "
            "watermarks or unrelated logos. "

            "Do not add unnecessary text overlays. "

            f"Pinterest creative type: {creative_type}. "
            f"Variation: {variation}. "

            f"Pinterest title context: {generated.title}. "
            f"Pinterest description context: {generated.description[:500]} "
        )

    def _previous_ai_seo_context(self, product_id: int) -> list[dict[str, str]]:
        """Return a bounded, non-sensitive diversity context for Gemini."""
        rows = self.db.query(PinCreative).filter_by(
            product_id=product_id,
            source_type=PinCreativeSourceType.AI.value,
        ).order_by(PinCreative.created_at.desc()).limit(8).all()
        return [
            self._seo_context_item(creative.title, creative.seo_metadata)
            for creative in reversed(rows)
            if creative.seo_metadata
        ]

    @staticmethod
    def _seo_context_item(title: str, seo: dict[str, object]) -> dict[str, str]:
        return {
            "title": title,
            "primary_keyword": str(seo.get("primary_keyword", "")).strip(),
            "creative_angle": str(seo.get("creative_angle", "")).strip(),
        }

    @staticmethod
    def _ensure_seo_is_novel(
        seo: dict[str, object], previous_creatives: list[dict[str, str]]
    ) -> None:
        primary = str(seo["primary_keyword"]).casefold()
        angle = str(seo["creative_angle"]).casefold()
        previous_primary = {item["primary_keyword"].casefold() for item in previous_creatives}
        previous_angles = {item["creative_angle"].casefold() for item in previous_creatives}
        if primary in previous_primary:
            raise AIValidationError("AI yanıtı bu ürün için zaten kullanılan primary keyword'ü tekrarlıyor.")
        if angle in previous_angles:
            raise AIValidationError("AI yanıtı bu ürün için zaten kullanılan creative angle'ı tekrarlıyor.")

    @staticmethod
    def _parse_generated_json(
        raw: str, context: ProductContext | None = None, creative_type: str | None = None
    ) -> GeneratedCreative:
        try:
            data = json.loads(raw)

        except (TypeError, json.JSONDecodeError) as exc:
            raise AIValidationError(
                "AI sağlayıcısı geçerli JSON döndürmedi."
            ) from exc

        if not isinstance(data, dict):
            raise AIValidationError(
                "AI yanıtı beklenen JSON nesnesi formatında değil."
            )

        for key in ("title", "description", "call_to_action"):
            value = data.get(key)

            if not isinstance(value, str) or not value.strip():
                raise AIValidationError(
                    f"AI yanıtında '{key}' alanı eksik veya geçersiz."
                )

        seo = data.get("seo")
        if not isinstance(seo, dict):
            raise AIValidationError("AI yanıtında 'seo' alanı eksik veya geçersiz.")

        primary = seo.get("primary_keyword")
        angle = seo.get("creative_angle")
        if not isinstance(primary, str) or not primary.strip():
            raise AIValidationError("AI yanıtında primary_keyword eksik veya geçersiz.")
        if not isinstance(angle, str) or not angle.strip():
            raise AIValidationError("AI yanıtında creative_angle eksik veya geçersiz.")

        groups = (
            "secondary_keywords",
            "long_tail_keywords",
            "audience_keywords",
            "use_case_keywords",
        )
        normalized_groups: dict[str, list[str]] = {}
        source_keywords: list[str] = [primary.strip()]
        for group in groups:
            values = seo.get(group)
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value.strip() for value in values
            ):
                raise AIValidationError(f"AI yanıtındaki {group} alanı geçersiz.")
            source_keywords.extend(value.strip() for value in values)
            normalized_groups[group] = list(dict.fromkeys(value.strip() for value in values))

        intents = seo.get("search_intents")
        allowed_intents = {
            "product_search", "gift_intent", "aesthetic_style_intent",
            "audience_intent", "use_case_intent",
        }
        if not isinstance(intents, list) or not intents or not all(
            isinstance(intent, str) and intent in allowed_intents for intent in intents
        ):
            raise AIValidationError("AI yanıtındaki search_intents alanı geçersiz.")

        keywords = list(dict.fromkeys(keyword.strip() for keyword in source_keywords))
        if len(source_keywords) > 20 or len(keywords) < 5 or len(keywords) > 12:
            raise AIValidationError("AI yanıtındaki keyword seti spam veya beklenen aralık dışında.")
        if any(source_keywords.count(keyword) > 2 for keyword in set(source_keywords)):
            raise AIValidationError("AI yanıtındaki keyword setinde aşırı tekrar var.")

        seo_metadata: dict[str, object] = {
            "primary_keyword": primary.strip(),
            **normalized_groups,
            "search_intents": list(dict.fromkeys(intents)),
            "creative_angle": angle.strip(),
        }
        AIContentService._validate_seo_grounding(data, seo_metadata, context, creative_type)

        return GeneratedCreative(
            title=data["title"].strip()[:255],
            description=data["description"].strip(),
            keywords=keywords,
            call_to_action=data["call_to_action"].strip()[:255],
            seo_metadata=seo_metadata,
        )

    @staticmethod
    def _validate_seo_grounding(
        data: dict[str, object], seo: dict[str, object], context: ProductContext | None,
        creative_type: str | None = None,
    ) -> None:
        """Reject obvious irrelevant and unsupported SEO claims deterministically."""
        title = str(data["title"])
        description = str(data["description"])
        if "#" in title or "#" in description:
            raise AIValidationError("AI yanıtında hashtag kullanılamaz.")
        if str(seo["primary_keyword"]).casefold() not in title.casefold():
            raise AIValidationError("Primary keyword Pinterest başlığında doğal biçimde yer almalı.")
        title_tokens = re.findall(r"[a-z0-9]+", title.casefold())
        if len(title_tokens) > 18 or any(title_tokens.count(token) > 2 for token in set(title_tokens)):
            raise AIValidationError("Pinterest başlığı gereksiz genişletilmiş veya keyword stuffing içeriyor.")
        AIContentService._validate_creative_type_angle(seo, creative_type)
        if context is None:
            return

        evidence = " ".join([context.title, context.description, *context.tags]).casefold()
        evidence_tokens = set(re.findall(r"[a-z0-9]{3,}", evidence))
        primary_tokens = set(re.findall(r"[a-z0-9]{3,}", str(seo["primary_keyword"]).casefold()))
        generic = {"product", "details", "everyday", "thoughtful", "use"}
        if not (primary_tokens - generic) & evidence_tokens:
            raise AIValidationError("AI yanıtındaki primary keyword ürün verisiyle ilgili değil.")

        output = " ".join([
            title, description, str(seo["primary_keyword"]),
            *[str(item) for group in ("secondary_keywords", "long_tail_keywords", "audience_keywords", "use_case_keywords") for item in seo[group]],
            str(seo["creative_angle"]),
        ]).casefold()
        restricted_claims = {
            "personalized": ("personalized", "personalised", "custom"),
            "handmade": ("handmade",),
            "engraved": ("engraved",),
            "sterling": ("sterling",), "gold": ("gold",), "silver": ("silver",),
            "wooden": ("wooden",), "leather": ("leather",), "organic": ("organic",),
            "vegan": ("vegan",), "waterproof": ("waterproof", "water-resistant"),
            "hypoallergenic": ("hypoallergenic",), "durable": ("durable",),
            "protective": ("protective", "protection"), "premium": ("premium",),
            "high-quality": ("high-quality", "high quality"),
            "long-lasting": ("long-lasting", "long lasting"),
            "multiple phone models": ("multiple phone models",),
            "gift": ("gift", "gifting", "giftable", "present"),
            "discount": ("discount", "sale"), "free shipping": ("free shipping",),
        }
        for claim, evidence_forms in restricted_claims.items():
            if claim in output and not any(form in evidence for form in evidence_forms):
                raise AIValidationError("AI yanıtı ürün verisiyle desteklenmeyen bir özellik iddiası içeriyor.")

        product_type = _product_type_hint(context)
        if product_type and any(
            product_type not in keyword.casefold() for keyword in seo["long_tail_keywords"]
        ):
            raise AIValidationError("Long-tail keyword ürün türünü açıkça içermeli.")

    @staticmethod
    def _validate_creative_type_angle(seo: dict[str, object], creative_type: str | None) -> None:
        """Keep one explicit, type-appropriate search angle per AI creative."""
        if not creative_type:
            return
        intents = set(seo["search_intents"])
        expected = {
            "product_focus": "product_search",
            "lifestyle": "aesthetic_style_intent",
            "problem_solution": "use_case_intent",
            "gift_idea": "gift_intent",
            "minimalist": "aesthetic_style_intent",
        }
        required = expected.get(creative_type)
        if required and required not in intents:
            raise AIValidationError("Creative type ile SEO arama niyeti uyumlu değil.")
        # Product search can legitimately include a product's visual style (for
        # example, a minimalist phone case). Gifting is a competing conversion
        # intent for product_focus and remains disallowed here.
        if creative_type == "product_focus" and "gift_intent" in intents:
            raise AIValidationError("product_focus creative tek baskın ürün açısı kullanmalı.")
        angle_words = re.findall(r"[a-z0-9]+", str(seo["creative_angle"]).casefold())
        if len(angle_words) > 10 or "," in str(seo["creative_angle"]):
            raise AIValidationError("Creative angle tek baskın bir açı olmalı.")


def _keywords_from_title(title: str) -> list[str]:
    return list(
        dict.fromkeys(
            word.lower()
            for word in re.findall(r"[\w-]{3,}", title)
        )
    )[:6]


def _product_type_hint(context: ProductContext) -> str | None:
    """Return a conservative product-type phrase when the listing states one."""
    evidence = " ".join([context.title, context.description, *context.tags]).casefold()
    known_types = (
        "phone case", "t-shirt", "tee shirt", "sweatshirt", "hoodie", "tote bag",
        "mug", "candle", "necklace", "poster", "art print", "sticker", "notebook",
    )
    return next((product_type for product_type in known_types if product_type in evidence), None)
