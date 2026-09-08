"""Provider-neutral structured Pinterest content generation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from app.config import settings
from app.models import EtsyListing, PinCreative, PinGenerationJob, Product
from app.models.core import PinCreativeSourceType, PinCreativeStatus, PinCreativeType


class AIContentError(Exception):
    """A safe error to show when structured content cannot be generated."""


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

    def generate_json(self, prompt: str) -> str:
        payload = json.loads(prompt.rsplit("INPUT_JSON=", 1)[1])
        product = payload["product"]
        creative_type = payload["creative_type"]
        variation = payload["variation"]

        labels = {
            "product_focus": "Ürünün öne çıkan detayları",
            "lifestyle": "Günlük hayata uyum sağlayan fikir",
            "problem_solution": "Pratik bir çözüm fikri",
            "gift_idea": "Düşünceli hediye fikri",
            "minimalist": "Sade ve zamansız seçim",
        }

        keywords = product["tags"] or _keywords_from_title(product["title"])

        return json.dumps(
            {
                "title": f"{product['title']} — {labels[creative_type]} {variation}",
                "description": (
                    f"{product['title']} için "
                    f"{labels[creative_type].lower()}. "
                    f"{product['description'][:220]}"
                ).strip(),
                "keywords": keywords[:6],
                "call_to_action": "Detayları inceleyin",
            },
            ensure_ascii=False,
        )


class GeminiAIContentProvider:
    """Google Gemini provider for Pinterest title/description/keyword generation."""

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
            raise AIContentError(
                f"Gemini içerik üretimi başarısız: {exc}"
            ) from exc

        text = getattr(response, "text", None)

        if not text:
            raise AIContentError(
                "Gemini geçerli bir içerik döndürmedi."
            )

        return text


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

    def product_context(self, product: Product) -> ProductContext:
        listing = (
            self.db.query(EtsyListing)
            .filter_by(product_id=product.id)
            .one_or_none()
        )

        title = (product.title or "").strip()
        description = (product.description or "").strip()

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
    ) -> list[PinCreative]:
        """
        Generate one or more Pinterest creatives for an Etsy product.

        Each creative consists of:

        1. Gemini-generated Pinterest SEO content.
        2. OpenAI-generated Pinterest image when AI_IMAGE_PROVIDER=openai.
        3. Etsy product destination URL.
        """

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

        if len(existing_keys) >= desired_count:
            return []

        created: list[PinCreative] = []

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

            except Exception as exc:
                raise AIContentError(
                    f"Görsel sağlayıcısı başlatılamadı: {exc}"
                ) from exc

        for variation in range(1, desired_count + 1):
            key = self._generation_key(
                product.id,
                creative_type.value,
                variation,
            )

            legacy_key = self._legacy_ai_generation_key(product.id, creative_type.value, variation)
            if key in existing_keys or legacy_key in existing_keys:
                continue

            # ---------------------------------------------------------
            # 1. Generate Pinterest SEO content with Gemini
            # ---------------------------------------------------------

            generated = self._parse_generated_json(
                (self.provider or get_ai_provider()).generate_json(
                    self._prompt(
                        context,
                        creative_type.value,
                        variation,
                    )
                )
            )

            # ---------------------------------------------------------
            # 2. Create PinCreative database object
            # ---------------------------------------------------------

            creative = PinCreative(
                product=product,
                creative_type=creative_type.value,
                title=generated.title,
                description=generated.description,
                keywords=generated.keywords,
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

            # Flush so SQLAlchemy assigns an ID before the image
            # generation process is completed.
            self.db.flush()

            # ---------------------------------------------------------
            # 3. Generate Pinterest image with OpenAI
            # ---------------------------------------------------------

            if image_provider is not None and context.images:
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
                    self.db.rollback()

                    raise AIContentError(
                        f"Pinterest görseli oluşturulamadı: {exc}"
                    ) from exc

            created.append(creative)

        self.db.commit()

        return created

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
            created.append(PinCreative(
                product=product,
                creative_type=PinCreativeType.PRODUCT_FOCUS.value,
                title=title[:255],
                description=(
                    f"View the original Etsy mockup for {context.title}. "
                    f"Explore product details, materials, and ordering information."
                ),
                keywords=keywords,
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
        # A just-synced Product has not necessarily been committed yet.
        self.db.flush()
        existing = self.db.query(PinGenerationJob.id).filter(
            PinGenerationJob.product_id == product.id,
            PinGenerationJob.status.in_(("pending", "running")),
        ).first()
        if existing:
            return None
        job = PinGenerationJob(product_id=product.id, status="pending")
        self.db.add(job)
        return job

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
    ) -> str:
        """Build a Gemini prompt for one Pinterest SEO creative."""

        data = {
            "product": {
                "title": context.title,
                "description": context.description,
                "tags": context.tags,
                "price": context.price,
                "url": context.url,
                "images": context.images,
            },
            "creative_type": creative_type,
            "variation": variation,
        }

        return (
            "Create one high-quality Pinterest SEO creative for this Etsy product. "
            "Return JSON only with exactly these fields: "
            "title, description, keywords, call_to_action. "

            "The Pinterest title should be natural, attractive and optimized "
            "for Pinterest search without keyword stuffing. "

            "The description should clearly explain why someone would be "
            "interested in the product and naturally include relevant search terms. "

            "Use natural readable English because the target Pinterest audience "
            "is primarily English-speaking. "

            "Do not use spam, excessive hashtags, fake claims, fake discounts, "
            "fake reviews or unsupported product features. "

            "Use relevant keywords only. "

            "Return 5 to 12 concise Pinterest search keywords. "

            "Make this variation meaningfully different from other variations "
            "of the same product. "

            "The call_to_action should be short and natural, such as "
            "\"Shop the product\", \"See details\" or \"Explore the mug\". "

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

    @staticmethod
    def _parse_generated_json(raw: str) -> GeneratedCreative:
        try:
            data = json.loads(raw)

        except (TypeError, json.JSONDecodeError) as exc:
            raise AIContentError(
                "AI sağlayıcısı geçerli JSON döndürmedi."
            ) from exc

        if not isinstance(data, dict):
            raise AIContentError(
                "AI yanıtı beklenen JSON nesnesi formatında değil."
            )

        for key in (
            "title",
            "description",
            "call_to_action",
        ):
            value = data.get(key)

            if not isinstance(value, str) or not value.strip():
                raise AIContentError(
                    f"AI yanıtında '{key}' alanı eksik veya geçersiz."
                )

        keywords_data = data.get("keywords")

        if (
            not isinstance(keywords_data, list)
            or not keywords_data
            or not all(
                isinstance(item, str) and item.strip()
                for item in keywords_data
            )
        ):
            raise AIContentError(
                "AI yanıtındaki keywords alanı geçersiz."
            )

        keywords = list(
            dict.fromkeys(
                item.strip()
                for item in keywords_data
            )
        )[:12]

        return GeneratedCreative(
            title=data["title"].strip()[:255],
            description=data["description"].strip(),
            keywords=keywords,
            call_to_action=data["call_to_action"].strip()[:255],
        )


def _keywords_from_title(title: str) -> list[str]:
    return list(
        dict.fromkeys(
            word.lower()
            for word in re.findall(r"[\w-]{3,}", title)
        )
    )[:6]
