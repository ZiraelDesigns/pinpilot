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
    parsed = AIContentService._parse_generated_json(json.dumps({"title": "A title", "description": "Helpful text", "keywords": ["gift", "gift", "idea"], "call_to_action": "View item"}))
    assert parsed.keywords == ["gift", "idea"]


def test_duplicate_request_avoids_additional_provider_calls():
    with TestClient(app):
        db = SessionLocal()
        try:
            product = _product(db)
            provider = StaticProvider(json.dumps({"title": "Unique title", "description": "Helpful description", "keywords": ["candle"], "call_to_action": "See more"}))
            service = AIContentService(db, provider)
            assert len(service.generate(product, PinCreativeType.GIFT_IDEA, 1)) == 1
            assert service.generate(product, PinCreativeType.GIFT_IDEA, 1) == []
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
    generated = GeneratedCreative("Cup title", "Cup description", ["cup"], "Shop")
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
