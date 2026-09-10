import json

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.config import settings
from app.main import app
from app.models import EtsyAccount, EtsyListing, PinCreative, PinGenerationJob, Product
from app.models.core import PinCreativeSourceType, PinCreativeType
from app.services.ai_content import AIContentError, AIContentService, MockAIContentProvider
from app.services.etsy import EtsyApiService


class StaticProvider:
    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    def generate_json(self, prompt: str) -> str:
        self.calls += 1
        return self.response


def _seo_response(**overrides):
    response = {
        "title": "Handmade Candle for Quiet Evenings",
        "description": "A handmade candle for thoughtful shoppers and quiet evening rituals.",
        "call_to_action": "See details",
        "seo": {
            "primary_keyword": "handmade candle",
            "secondary_keywords": ["soy candle"],
            "long_tail_keywords": ["handmade candle for quiet evenings"],
            "audience_keywords": ["thoughtful shoppers"],
            "use_case_keywords": ["evening ritual"],
            "search_intents": ["product_search", "use_case_intent"],
            "creative_angle": "quiet evening ritual",
        },
    }
    response.update(overrides)
    return response


def _product(db, title="Handmade Candle", description="A warm soy candle for quiet evenings"):
    product = Product(title=title, description=description, url="https://example.test/candle")
    db.add(product)
    db.commit()
    return product


def test_mock_provider_creates_structured_creative():
    with TestClient(app):
        db = SessionLocal()
        try:
            product = _product(db)
            created = AIContentService(db, MockAIContentProvider()).generate(product, PinCreativeType.PRODUCT_FOCUS, 1)
            assert len(created) == 1
            assert created[0].keywords
            assert created[0].destination_url == product.url
        finally:
            db.delete(product)
            db.commit()
            db.close()


@pytest.mark.parametrize("creative_type", list(PinCreativeType))
def test_each_creative_type_is_supported(creative_type):
    with TestClient(app):
        db = SessionLocal()
        try:
            product = _product(db, title=f"Product {creative_type.value}")
            created = AIContentService(db, MockAIContentProvider()).generate(product, creative_type, 1)
            assert created[0].creative_type == creative_type.value
        finally:
            db.delete(product)
            db.commit()
            db.close()


def test_empty_product_information_is_rejected():
    with TestClient(app):
        db = SessionLocal()
        try:
            product = _product(db, title="", description="")
            with pytest.raises(AIContentError, match="yetecek"):
                AIContentService(db, MockAIContentProvider()).generate(product, PinCreativeType.MINIMALIST, 1)
        finally:
            db.delete(product)
            db.commit()
            db.close()


def test_invalid_ai_json_is_rejected():
    with TestClient(app):
        db = SessionLocal()
        try:
            product = _product(db)
            with pytest.raises(AIContentError, match="geçerli JSON"):
                AIContentService(db, StaticProvider("not json")).generate(product, PinCreativeType.LIFESTYLE, 1)
        finally:
            db.delete(product)
            db.commit()
            db.close()


def test_json_parsing_deduplicates_keywords():
    payload = _seo_response()
    payload["seo"]["secondary_keywords"] = ["soy candle", "soy candle"]
    parsed = AIContentService._parse_generated_json(json.dumps(payload))
    assert parsed.keywords.count("soy candle") == 1


def test_duplicate_request_avoids_additional_provider_calls():
    with TestClient(app):
        db = SessionLocal()
        try:
            product = _product(db)
            provider = StaticProvider(json.dumps(_seo_response()))
            service = AIContentService(db, provider)
            assert len(service.generate(product, PinCreativeType.PRODUCT_FOCUS, 1)) == 1
            assert service.generate(product, PinCreativeType.PRODUCT_FOCUS, 1) == []
            assert provider.calls == 1
        finally:
            db.delete(product)
            db.commit()
            db.close()


def test_mockup_creatives_are_created_once_per_etsy_image_without_ai_calls():
    with TestClient(app):
        db = SessionLocal()
        try:
            product = _product(db, title="Mockup Product")
            account = EtsyAccount(shop_name="Test Etsy", shop_identifier="mockup-test")
            listing = EtsyListing(
                account=account,
                product=product,
                listing_id="mockup-listing",
                title=product.title,
                state="active",
                tags=["handmade gift"],
                images=["https://images.example.test/one.jpg", "https://images.example.test/two.jpg"],
            )
            db.add_all([account, listing])
            db.commit()
            service = AIContentService(db)
            created = service.ensure_mockup_creatives(product, listing)
            db.commit()
            assert len(created) == 2
            assert {creative.source_type for creative in created} == {PinCreativeSourceType.MOCKUP.value}
            assert {creative.source_image_url for creative in created} == set(listing.images)
            assert service.ensure_mockup_creatives(product, listing) == []
            listing.images = [*listing.images, "https://images.example.test/three.jpg"]
            db.commit()
            assert len(service.ensure_mockup_creatives(product, listing)) == 1
        finally:
            db.delete(account)
            db.delete(product)
            db.commit()
            db.close()


def test_ai_creative_uses_ai_source_and_does_not_conflict_with_mockup_key(monkeypatch):
    monkeypatch.setattr(settings, "ai_image_provider", "mock")
    with TestClient(app):
        db = SessionLocal()
        try:
            product = _product(db, title="Separate Sources")
            account = EtsyAccount(shop_name="Source Etsy", shop_identifier="source-test")
            listing = EtsyListing(
                account=account, product=product, listing_id="source-listing", title=product.title,
                state="active", images=["https://images.example.test/source.jpg"],
            )
            db.add_all([account, listing])
            db.commit()
            service = AIContentService(db, MockAIContentProvider())
            service.ensure_mockup_creatives(product, listing)
            db.commit()
            ai = service.generate(product, PinCreativeType.PRODUCT_FOCUS, 1)
            assert len(ai) == 1
            assert ai[0].source_type == PinCreativeSourceType.AI.value
        finally:
            db.delete(account)
            db.delete(product)
            db.commit()
            db.close()


def test_etsy_sync_adds_new_listing_images_to_mockup_pool_without_ai_calls(monkeypatch):
    with TestClient(app):
        db = SessionLocal()
        try:
            account = EtsyAccount(shop_name="Sync Test", shop_identifier="sync-test", is_active=True)
            db.add(account)
            db.commit()
            service = EtsyApiService(db, account)
            monkeypatch.setattr(service, "fetch_active_listings", lambda _: [{
                "listing_id": 987654,
                "title": "Synced Mockup Product",
                "description": "An Etsy item with original mockups.",
                "url": "https://example.test/products/synced",
                "price": {"amount": 1200, "divisor": 100, "currency_code": "USD"},
                "quantity": 2,
                "tags": ["etsy mockup"],
                "state": "active",
            }])
            monkeypatch.setattr(service, "fetch_listing_images", lambda _: [
                "https://images.example.test/sync-one.jpg", "https://images.example.test/sync-two.jpg"
            ])

            assert service.sync_active_listings() == 1
            listing = db.query(EtsyListing).filter_by(listing_id="987654").one()
            creatives = db.query(PinCreative).filter_by(product_id=listing.product_id).all()
            assert len(creatives) == 2
            assert all(creative.source_type == "mockup" for creative in creatives)
            assert db.query(PinGenerationJob).filter_by(product_id=listing.product_id, status="pending").count() == 1
        finally:
            db.delete(account)
            db.delete(listing.product)
            db.commit()
            db.close()


def test_image_prompts_use_distinct_scene_instructions_per_creative_type():
    # Compare prompt text directly; this performs no provider or image call.
    from app.services.ai_content import GeneratedCreative, ProductContext
    product_context = ProductContext("Cup", "Ceramic cup", [], None, None, ["https://images.example.test/cup.jpg"])
    generated = GeneratedCreative("Cup title", "Cup description", ["cup"], "Shop", _seo_response()["seo"])
    prompts = {
        creative_type: AIContentService._image_prompt(product_context, creative_type.value, 1, generated)
        for creative_type in PinCreativeType
    }
    assert len(set(prompts.values())) == len(PinCreativeType)
    assert "genuinely new visual story" in prompts[PinCreativeType.LIFESTYLE]
    assert AIContentService._image_prompt(product_context, "lifestyle", 1, generated) != AIContentService._image_prompt(product_context, "lifestyle", 2, generated)


def test_creative_api_can_approve_update_and_delete():
    with TestClient(app) as client:
        db = SessionLocal()
        product = _product(db)
        try:
            creative = AIContentService(db, MockAIContentProvider()).generate(product, PinCreativeType.MINIMALIST, 1)[0]
            creative_id = creative.id
            updated = client.patch(f"/creatives/{creative_id}", json={"title": "Edited", "description": "Edited description", "keywords": ["edited"], "call_to_action": "Open"})
            assert updated.status_code == 200
            assert client.post(f"/creatives/{creative_id}/approve", follow_redirects=False).status_code == 303
            db.expire_all()
            assert db.get(PinCreative, creative_id).status == "approved"
            assert client.post(f"/creatives/{creative_id}/delete", follow_redirects=False).status_code == 303
            db.expire_all()
            assert db.get(PinCreative, creative_id) is None
        finally:
            db.delete(product)
            db.commit()
            db.close()


def test_seo_v2_metadata_is_saved_and_keywords_are_flattened(monkeypatch):
    monkeypatch.setattr(settings, "ai_image_provider", "mock")
    with TestClient(app):
        db = SessionLocal()
        try:
            product = _product(db)
            created = AIContentService(db, StaticProvider(json.dumps(_seo_response()))).generate(
                product, PinCreativeType.PRODUCT_FOCUS, 1
            )
            creative = created[0]
            assert creative.seo_metadata["primary_keyword"] == "handmade candle"
            assert creative.keywords == [
                "handmade candle", "soy candle", "handmade candle for quiet evenings",
                "thoughtful shoppers", "evening ritual",
            ]
        finally:
            db.delete(product)
            db.commit()
            db.close()


def test_seo_v2_rejects_invalid_primary_intent_stuffing_and_unsupported_claims():
    from app.services.ai_content import ProductContext

    context = ProductContext("Handmade Candle", "A warm soy candle", ["candle"], None, None, [])
    unrelated = _seo_response()
    unrelated["title"] = "Luxury Yacht Gift"
    unrelated["seo"]["primary_keyword"] = "luxury yacht"
    with pytest.raises(AIContentError, match="ilgili değil"):
        AIContentService._parse_generated_json(json.dumps(unrelated), context)

    invalid_intent = _seo_response()
    invalid_intent["seo"]["search_intents"] = ["viral_intent"]
    with pytest.raises(AIContentError, match="search_intents"):
        AIContentService._parse_generated_json(json.dumps(invalid_intent), context)

    stuffing = _seo_response()
    stuffing["seo"]["secondary_keywords"] = ["candle"] * 4
    with pytest.raises(AIContentError, match="aşırı tekrar"):
        AIContentService._parse_generated_json(json.dumps(stuffing), context)

    unsupported = _seo_response()
    unsupported["title"] = "Personalized Handmade Candle"
    unsupported["seo"]["primary_keyword"] = "personalized handmade candle"
    with pytest.raises(AIContentError, match="desteklenmeyen"):
        AIContentService._parse_generated_json(json.dumps(unsupported), context)


def test_seo_v2_prompt_includes_type_strategy_and_previous_context():
    from app.services.ai_content import ProductContext

    context = ProductContext("Handmade Candle", "Soy candle", ["candle"], None, None, [])
    previous = [{
        "title": "Handmade Candle for Reading",
        "primary_keyword": "handmade candle reading gift",
        "creative_angle": "reading nook gift",
    }]
    prompt = AIContentService._prompt(context, "gift_idea", 2, previous)

    payload = json.loads(prompt.rsplit("INPUT_JSON=", 1)[1])
    assert payload["previous_ai_creatives"] == previous
    assert "gifting occasion" in prompt
    assert "primary_keyword" in prompt

    strategies = {
        creative_type: AIContentService._prompt(context, creative_type.value, 1)
        for creative_type in PinCreativeType
    }
    assert len(set(strategies.values())) == len(PinCreativeType)


def test_seo_v2_rejects_duplicate_primary_keyword_from_previous_creative(monkeypatch):
    monkeypatch.setattr(settings, "ai_image_provider", "mock")
    with TestClient(app):
        db = SessionLocal()
        try:
            product = _product(db)
            previous = PinCreative(
                product=product,
                creative_type=PinCreativeType.LIFESTYLE.value,
                title="Handmade Candle for Quiet Evenings",
                description="Description",
                keywords=["handmade candle"],
                seo_metadata=_seo_response()["seo"],
                call_to_action="See details",
                source_type="ai",
                status="draft",
                generation_key="seo-v2-previous",
            )
            db.add(previous)
            db.commit()
            with pytest.raises(AIContentError, match="primary keyword"):
                AIContentService(db, StaticProvider(json.dumps(_seo_response()))).generate(
                    product, PinCreativeType.PRODUCT_FOCUS, 1
                )
        finally:
            db.delete(product)
            db.commit()
            db.close()


def test_seo_v2_rejects_unsupported_product_feature_claims():
    from app.services.ai_content import ProductContext

    context = ProductContext("Floral Phone Case", "A floral phone case", ["phone case"], None, None, [])
    response = _seo_response()
    response["title"] = "Durable Floral Phone Case"
    response["description"] = "A durable protective floral phone case for everyday use."
    response["seo"]["primary_keyword"] = "durable floral phone case"
    response["seo"]["long_tail_keywords"] = ["durable floral phone case for everyday use"]
    with pytest.raises(AIContentError, match="desteklenmeyen"):
        AIContentService._parse_generated_json(json.dumps(response), context, "product_focus")


def test_seo_v2_rejects_vague_phone_case_long_tail_keyword():
    from app.services.ai_content import ProductContext

    context = ProductContext("Pressed Flower Phone Case", "A soft floral phone case", ["phone case"], None, None, [])
    response = _seo_response()
    response["title"] = "Pressed Flower Phone Case"
    response["description"] = "A soft floral phone case."
    response["seo"]["primary_keyword"] = "pressed flower phone case"
    response["seo"]["secondary_keywords"] = ["floral phone case"]
    response["seo"]["long_tail_keywords"] = ["delicate floral phone accessory"]
    with pytest.raises(AIContentError, match="ürün türünü"):
        AIContentService._parse_generated_json(json.dumps(response), context, "product_focus")


def test_product_focus_rejects_multiple_competing_angles():
    from app.services.ai_content import ProductContext

    context = ProductContext("Handmade Candle", "A warm soy candle", ["candle"], None, None, [])
    response = _seo_response()
    response["seo"]["search_intents"] = [
        "product_search", "gift_intent", "aesthetic_style_intent",
    ]
    response["seo"]["creative_angle"] = "product details, gifting, and lifestyle style"
    with pytest.raises(AIContentError, match="tek baskın"):
        AIContentService._parse_generated_json(json.dumps(response), context, "product_focus")


def test_product_focus_allows_product_search_with_supported_style_intent():
    from app.services.ai_content import ProductContext

    context = ProductContext("Wavy Line Phone Case", "Abstract wavy line phone case", ["phone case", "minimalist"], None, None, [])
    response = _seo_response()
    response["title"] = "Wavy Line Phone Case"
    response["description"] = "An abstract wavy line phone case with a minimalist style."
    response["seo"]["primary_keyword"] = "wavy line phone case"
    response["seo"]["secondary_keywords"] = ["abstract phone case"]
    response["seo"]["long_tail_keywords"] = ["wavy line phone case for minimalist style"]
    response["seo"]["search_intents"] = ["product_search", "aesthetic_style_intent"]
    response["seo"]["creative_angle"] = "abstract phone case style"

    parsed = AIContentService._parse_generated_json(json.dumps(response), context, "product_focus")
    assert parsed.seo_metadata["search_intents"] == ["product_search", "aesthetic_style_intent"]
