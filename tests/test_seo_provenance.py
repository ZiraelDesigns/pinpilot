import json
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import (
    AnalyticsSnapshot,
    Pin,
    PinCreative,
    Product,
    PinterestAccount,
    PublishedPinterestPin,
    SEOGeneration,
)
from app.models.core import PinCreativeType
from app.services.ai_content import AIContentError, AIContentService
from app.services.pinterest_publisher import PinterestPinPublishResult, PinterestPublisher


def _response(keyword: str, angle: str) -> str:
    return json.dumps({
        "title": f"{keyword.title()} for Calm Evenings",
        "description": f"Explore {keyword} for calm evening use and thoughtful shoppers.",
        "call_to_action": "See details",
        "seo": {
            "primary_keyword": keyword,
            "secondary_keywords": ["soy candle", "evening candle"],
            "long_tail_keywords": [f"{keyword} for calm evening use"],
            "audience_keywords": ["thoughtful shoppers"],
            "use_case_keywords": ["calm evening use"],
            "search_intents": ["product_search"],
            "creative_angle": angle,
        },
    })


class ProvenanceProvider:
    provider_name = "test-provider"
    model_name = "test-model-v7"

    def __init__(self):
        self.responses = iter([
            _response("handmade candle", "calm evening ritual"),
            _response("soy candle", "quiet reading corner"),
        ])

    def generate_json(self, _prompt):
        return next(self.responses)


class FailingProvider:
    provider_name = "test-provider"
    model_name = "test-model-v7"

    def generate_json(self, _prompt):
        raise RuntimeError("Bearer private-token api_key=hidden")


class FakePublisher:
    publishing_enabled = True

    def publish_pin(self, _account, _request):
        return PinterestPinPublishResult(
            external_pin_id="publisher-provenance-pin",
            published_at=datetime(2026, 9, 30, 12),
        )


def test_successful_seo_generation_persists_version_model_timestamp_and_keyword_snapshot():
    with SessionLocal() as db:
        product = Product(title="Handmade Candle", description="A soy candle for calm evenings")
        db.add(product)
        db.flush()

        creative = AIContentService(db, ProvenanceProvider()).generate(
            product, PinCreativeType.PRODUCT_FOCUS, 1
        )[0]
        db.commit()
        provenance = db.query(SEOGeneration).one()

        assert provenance.id is not None
        assert provenance.creative_id == creative.id
        assert provenance.product_id == product.id
        assert provenance.provider == "test-provider"
        assert provenance.model_name == "test-model-v7"
        assert provenance.prompt_version == "pinterest_seo_v2"
        assert provenance.schema_version == "pinterest_seo_metadata_v2"
        assert provenance.status == "completed"
        assert provenance.started_at <= provenance.completed_at
        assert provenance.output_snapshot["title"] == creative.title
        assert provenance.output_snapshot["seo_metadata"]["primary_keyword"] == "handmade candle"
        assert provenance.output_snapshot["keywords"] == creative.keywords
        assert provenance.error_category is None


def test_successive_generations_keep_independent_output_history_after_creative_edits():
    with SessionLocal() as db:
        product = Product(title="Handmade Candle", description="A soy candle for calm evenings")
        db.add(product)
        db.flush()
        service = AIContentService(db, ProvenanceProvider())
        first = service.generate(product, PinCreativeType.PRODUCT_FOCUS, 1, force_new=True)[0]
        db.commit()
        first_snapshot = db.query(SEOGeneration).filter_by(creative_id=first.id).one()
        original_title = first_snapshot.output_snapshot["title"]

        first.title = "Manually edited title"
        first.seo_metadata = {"primary_keyword": "manually edited"}
        second = service.generate(product, PinCreativeType.PRODUCT_FOCUS, 1, force_new=True)[0]
        db.commit()

        rows = db.query(SEOGeneration).order_by(SEOGeneration.id).all()
        assert len(rows) == 2
        assert rows[0].creative_id == first.id and rows[1].creative_id == second.id
        assert rows[0].output_snapshot["title"] == original_title
        assert rows[0].output_snapshot["seo_metadata"]["primary_keyword"] == "handmade candle"
        assert rows[1].output_snapshot["seo_metadata"]["primary_keyword"] == "soy candle"


def test_failed_provider_attempt_is_persisted_without_credentials_or_error_text():
    with SessionLocal() as db:
        product = Product(title="Handmade Candle", description="A soy candle for calm evenings")
        db.add(product)
        db.flush()
        with pytest.raises(AIContentError):
            AIContentService(db, FailingProvider()).generate(
                product, PinCreativeType.PRODUCT_FOCUS, 1
            )

        failed = db.query(SEOGeneration).one()
        assert failed.product_id == product.id
        assert failed.creative_id is None
        assert failed.status == "failed"
        assert failed.provider == "test-provider"
        assert failed.model_name == "test-model-v7"
        assert failed.error_category == "provider_error"
        assert failed.output_snapshot is None
        assert "private-token" not in repr(failed.output_snapshot)
        assert "hidden" not in repr(failed.output_snapshot)


def test_seo_generation_links_through_publication_to_analytics_snapshot():
    with SessionLocal() as db:
        product = Product(title="Handmade Candle", description="A soy candle for calm evenings")
        db.add(product)
        db.flush()
        creative = AIContentService(db, ProvenanceProvider()).generate(
            product, PinCreativeType.PRODUCT_FOCUS, 1
        )[0]
        db.flush()
        generation = db.query(SEOGeneration).filter_by(creative_id=creative.id).one()
        pin = Pin(
            product_id=product.id,
            creative_id=creative.id,
            title=creative.title,
            description=creative.description,
            image_path="/media/generated/test.png",
            destination_url="https://shop.example.test/item",
        )
        db.add(pin)
        db.flush()
        metadata = PublishedPinterestPin.capture_metadata(pin)
        published = PublishedPinterestPin(
            pin=pin,
            external_pin_id="external-test-pin",
            published_at=datetime(2026, 9, 30),
            seo_generation_id=generation.id,
            metadata_snapshot=metadata,
        )
        snapshot = AnalyticsSnapshot(
            pin=pin,
            published_pin=published,
            impressions=0,
            metric_date=datetime(2026, 9, 30).date(),
        )
        db.add(snapshot)
        db.commit()

        assert metadata["seo_generation_id"] == generation.id
        assert metadata["seo_provenance_status"] == "known"
        assert published.seo_generation.id == generation.id
        assert snapshot.published_pin.seo_generation.output_snapshot["seo_metadata"]["primary_keyword"] == "handmade candle"


def test_publisher_carries_generation_id_into_confirmed_publication():
    with SessionLocal() as db:
        product = Product(title="Handmade Candle", description="A soy candle for calm evenings")
        db.add(product)
        db.flush()
        creative = AIContentService(db, ProvenanceProvider()).generate(
            product, PinCreativeType.PRODUCT_FOCUS, 1
        )[0]
        db.flush()
        generation = db.query(SEOGeneration).filter_by(creative_id=creative.id).one()
        pin = Pin(
            product_id=product.id,
            creative_id=creative.id,
            title=creative.title,
            description=creative.description,
            image_path="https://media.example.test/image.png",
            destination_url="https://shop.example.test/item",
            status="scheduled",
        )
        account = PinterestAccount(
            account_name="test account", account_identifier="test-account", is_active=True
        )
        db.add_all([pin, account])
        db.commit()

        published = PinterestPublisher(db, FakePublisher()).publish_pin(pin.id, account.id)

        assert published.seo_generation_id == generation.id
        assert published.metadata_snapshot["seo_generation_id"] == generation.id


def test_legacy_creative_without_provenance_remains_valid_and_marked_unknown():
    with SessionLocal() as db:
        product = Product(title="Legacy product")
        db.add(product)
        db.flush()
        creative = PinCreative(
            product=product,
            creative_type="product_focus",
            title="Legacy title",
            description="Legacy description",
            keywords=["legacy"],
            seo_metadata={"primary_keyword": "legacy product"},
            call_to_action="View",
            generation_key="legacy-provenance-test",
        )
        db.add(creative)
        db.flush()
        pin = Pin(product=product, creative=creative, title=creative.title, description=creative.description)
        db.add(pin)
        db.flush()
        metadata = PublishedPinterestPin.capture_metadata(pin)

        assert db.query(SEOGeneration).count() == 0
        assert metadata["seo_generation_id"] is None
        assert metadata["seo_provenance_status"] == "unknown"
        assert creative.seo_metadata["primary_keyword"] == "legacy product"
