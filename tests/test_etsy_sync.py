from __future__ import annotations

import httpx
import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import EtsyAccount, EtsyListing, EtsySyncRun, Pin, PinCreative, Product
from app.models.core import PinStatus
from app.services.ai_content import AIContentService
from app.services.etsy import ETSY_API_BASE_URL, EtsyApiService, EtsyIntegrationError


def _account(db):
    account = EtsyAccount(shop_name="Sync shop", shop_identifier="123", is_active=True)
    db.add(account)
    db.commit()
    return account


def _listing_data(**overrides):
    data = {
        "listing_id": 1001,
        "title": "Floral Phone Case",
        "description": "A floral phone case",
        "url": "https://example.test/listing/1001",
        "price": {"amount": 2499, "divisor": 100, "currency_code": "USD"},
        "quantity": 2,
        "tags": ["phone case", "floral"],
        "state": "active",
    }
    data.update(overrides)
    return data


def _mock_listing_fetches(monkeypatch, service, listings, images):
    monkeypatch.setattr(service, "fetch_active_listings", lambda _: listings)
    monkeypatch.setattr(service, "fetch_listing_images", lambda listing_id: images[str(listing_id)])


def test_new_listing_creates_product_mockups_and_audit_record(monkeypatch):
    db = SessionLocal()
    try:
        account = _account(db)
        service = EtsyApiService(db, account)
        _mock_listing_fetches(monkeypatch, service, [_listing_data()], {"1001": ["https://img.test/one.jpg"]})
        monkeypatch.setattr(AIContentService, "generate", lambda *_: pytest.fail("sync must not call AI"))

        result = service.sync()

        listing = db.query(EtsyListing).filter_by(listing_id="1001").one()
        assert result.processed_listings == 1
        assert result.new_products == 1
        assert result.new_mockup_creatives == 1
        assert listing.product.title == "Floral Phone Case"
        assert db.query(PinCreative).filter_by(product_id=listing.product_id, source_type="mockup").count() == 1
        assert db.query(EtsySyncRun).one().status == "success"
    finally:
        db.close()


def test_sync_is_idempotent_and_adds_only_a_new_image_creative(monkeypatch):
    db = SessionLocal()
    try:
        account = _account(db)
        service = EtsyApiService(db, account)
        data = _listing_data()
        _mock_listing_fetches(monkeypatch, service, [data], {"1001": ["https://img.test/one.jpg"]})
        assert service.sync().new_mockup_creatives == 1
        assert service.sync().new_products == 0
        assert db.query(PinCreative).count() == 1

        _mock_listing_fetches(monkeypatch, service, [data], {"1001": ["https://img.test/one.jpg", "https://img.test/two.jpg"]})
        result = service.sync()
        assert result.new_mockup_creatives == 1
        assert result.changed_products == 1
        assert db.query(PinCreative).count() == 2
    finally:
        db.close()


def test_text_price_and_tag_changes_update_listing_without_duplicate_creatives(monkeypatch):
    db = SessionLocal()
    try:
        account = _account(db)
        service = EtsyApiService(db, account)
        _mock_listing_fetches(monkeypatch, service, [_listing_data()], {"1001": ["https://img.test/one.jpg"]})
        service.sync()
        changed = _listing_data(title="Updated Floral Phone Case", description="Updated description", tags=["phone case", "botanical"], quantity=4)
        _mock_listing_fetches(monkeypatch, service, [changed], {"1001": ["https://img.test/one.jpg"]})

        result = service.sync()
        listing = db.query(EtsyListing).filter_by(listing_id="1001").one()
        assert result.changed_products == 1
        assert result.new_mockup_creatives == 0
        assert listing.title == "Updated Floral Phone Case"
        assert listing.tags == ["phone case", "botanical"]
        assert db.query(PinCreative).count() == 1
    finally:
        db.close()


def test_missing_active_listing_is_inactivated_and_unsent_pin_is_cancelled(monkeypatch):
    db = SessionLocal()
    try:
        account = _account(db)
        service = EtsyApiService(db, account)
        _mock_listing_fetches(monkeypatch, service, [_listing_data()], {"1001": ["https://img.test/one.jpg"]})
        service.sync()
        listing = db.query(EtsyListing).one()
        creative = db.query(PinCreative).one()
        pin = Pin(product_id=listing.product_id, creative_id=creative.id, title="queued", status=PinStatus.SCHEDULED.value)
        db.add(pin)
        db.commit()
        _mock_listing_fetches(monkeypatch, service, [], {})

        result = service.sync()
        db.refresh(listing)
        db.refresh(pin)
        assert result.inactive_listings == 1
        assert listing.state == "inactive"
        assert pin.status == PinStatus.CANCELLED.value
    finally:
        db.close()


def test_active_listing_pagination_collects_all_pages(monkeypatch):
    db = SessionLocal()
    try:
        service = EtsyApiService(db, _account(db))
        pages = [
            {"results": [{"listing_id": index} for index in range(100)]},
            {"results": [{"listing_id": 100}]},
        ]
        monkeypatch.setattr(service, "_get", lambda *_args, **_kwargs: pages.pop(0))
        assert len(service.fetch_active_listings("123")) == 101
    finally:
        db.close()


def test_rate_limit_retries_without_logging_credentials(monkeypatch):
    db = SessionLocal()
    try:
        monkeypatch.setattr(settings, "etsy_api_key", "test-key")
        monkeypatch.setattr(settings, "etsy_shared_secret", "test-secret")
        delays = []
        service = EtsyApiService(db, _account(db), access_token="test-token", sleep=delays.append)
        responses = [
            httpx.Response(429, headers={"Retry-After": "1"}, request=httpx.Request("GET", f"{ETSY_API_BASE_URL}/x")),
            httpx.Response(200, json={"results": []}, request=httpx.Request("GET", f"{ETSY_API_BASE_URL}/x")),
        ]
        monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: responses.pop(0))
        assert service._get("/x") == {"results": []}
        assert delays == [1.0]
    finally:
        db.close()


def test_transient_etsy_error_is_recorded_without_deleting_existing_data(monkeypatch):
    db = SessionLocal()
    try:
        account = _account(db)
        product = Product(title="Existing")
        listing = EtsyListing(account=account, product=product, listing_id="old", title="Existing", state="active")
        db.add(listing)
        db.commit()
        service = EtsyApiService(db, account)
        monkeypatch.setattr(service, "fetch_active_listings", lambda _: (_ for _ in ()).throw(EtsyIntegrationError("temporary failure")))

        with pytest.raises(EtsyIntegrationError):
            service.sync()
        assert db.query(EtsyListing).filter_by(listing_id="old").one().state == "active"
        assert db.query(EtsySyncRun).order_by(EtsySyncRun.id.desc()).first().status == "failed"
    finally:
        db.close()
