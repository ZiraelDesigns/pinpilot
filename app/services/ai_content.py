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


class AIValidationError(AIContentError):
    """The provider replied, but its structured SEO output was not acceptable.

    A later generation may comply with the same prompt, so workers treat this
    separately from permanent configuration or product-data failures.
    """


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
        previous_seo = self._previous_ai_seo_context(product.id)

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
                        previous_seo,
                    )
                ),
                context,
                creative_type.value,
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
            previous_seo.append(self._seo_context_item(generated.title, generated.seo_metadata))

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
