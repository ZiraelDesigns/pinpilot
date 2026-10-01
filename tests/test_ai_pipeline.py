from datetime import date
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import (
    AIDailyQuotaSlot,
    AIPipelineControl,
    EtsyAccount,
    EtsyListing,
    PinCreative,
    PinGenerationJob,
    Product,
)
from app.models.core import PinCreativeType
from app.services.ai_content import AIContentError, AIContentService, MockAIContentProvider
from app.services.ai_pipeline import (
    AIDailyQuotaExceededError,
    dashboard_pipeline_status,
    quota_counts,
    reserve_ai_capacity,
    set_pipeline_enabled,
)
from app.services.ai_worker import AIGenerationWorker
from app.services.daily_pin_scheduler import DailyPinScheduler
from app.services.etsy import EtsyApiService


def _product(db, title="Pipeline product"):
    product = Product(title=title, description="A useful handmade product", url="https://example.test/item")
    db.add(product)
    db.commit()
    return product


def _worker(factory=AIContentService):
    return AIGenerationWorker(SessionLocal, content_service_factory=factory)


def test_pipeline_defaults_off_and_toggle_is_persistent():
    with TestClient(app) as client:
        db = SessionLocal()
        db.delete(db.get(AIPipelineControl, 1))
        db.commit()
        db.close()
        status = client.get("/pipeline/status")
        assert status.status_code == 200
        assert status.json()["enabled"] is False
        assert client.post("/pipeline/toggle", json={"enabled": True}).json()["enabled"] is True
        assert client.post("/pipeline/toggle", json={"enabled": False}).json()["enabled"] is False
        assert client.get("/pipeline/status").json()["enabled"] is False
        assert client.post("/pipeline/toggle", json={"enabled": True}).json()["enabled"] is True


def test_paused_pipeline_blocks_manual_generation_and_keeps_pending_job():
    with TestClient(app) as client:
        db = SessionLocal()
        product = _product(db)
        job = PinGenerationJob(product_id=product.id, requested_count=2, status="pending")
        db.add(job)
        db.commit()
        try:
            client.post("/pipeline/toggle", json={"enabled": False})
            response = client.post("/creatives/generate", json={
                "product_id": product.id,
                "creative_type": "product_focus",
                "desired_count": 1,
            })
            assert response.status_code == 409
            assert "kapalı" in response.json()["detail"]
            assert _worker().process_once().claimed is False
            db.refresh(job)
            assert job.status == "pending"
            assert db.query(PinCreative).filter_by(source_type="ai").count() == 0
        finally:
            client.post("/pipeline/toggle", json={"enabled": True})
            db.close()


def test_paused_etsy_sync_imports_mockup_but_does_not_enqueue_ai(monkeypatch):
    with TestClient(app):
        db = SessionLocal()
        account = EtsyAccount(shop_name="Paused shop", shop_identifier="paused-shop", is_active=True)
        db.add(account)
        db.commit()
        try:
            set_pipeline_enabled(db, False)
            service = EtsyApiService(db, account)
            monkeypatch.setattr(service, "fetch_active_listings", lambda _: [{
                "listing_id": 9901, "title": "Paused shop mug", "description": "Mug",
                "url": "https://example.test/mug", "tags": ["ceramic mug"], "state": "active",
            }])
            monkeypatch.setattr(service, "fetch_listing_images", lambda _: ["https://img.test/mug.jpg"])
            service.sync()
            listing = db.query(EtsyListing).filter_by(listing_id="9901").one()
            creative = db.query(PinCreative).filter_by(product_id=listing.product_id).one()
            assert creative.source_type == "mockup"
            assert creative.seo_metadata["primary_keyword"] == "ceramic mug"
            assert db.query(PinGenerationJob).count() == 0
        finally:
            db.close()


def test_paused_scheduler_does_not_enqueue_ai_and_resume_queues_independent_quota():
    with TestClient(app):
        db = SessionLocal()
        product = _product(db)
        try:
            set_pipeline_enabled(db, False)
            off = DailyPinScheduler(db).schedule_daily(date(2035, 1, 1))
            assert off.ai_jobs_created == 0
            assert db.query(PinGenerationJob).count() == 0
            set_pipeline_enabled(db, True)
            on = DailyPinScheduler(db).schedule_daily(date(2035, 1, 2))
            job = db.query(PinGenerationJob).one()
            assert on.ai_jobs_created == 1
            assert job.requested_count == 15
        finally:
            db.close()


def test_mockup_stock_does_not_reduce_ai_capacity_or_daily_job_size():
    with TestClient(app):
        db = SessionLocal()
        product = _product(db)
        try:
            db.add_all([
                PinCreative(
                    product_id=product.id, creative_type="product_focus", title=f"Mockup {i}",
                    description="Etsy mockup", keywords=["item"], call_to_action="View",
                    source_type="mockup", generation_key=f"quota-mockup-{i}",
                )
                for i in range(100)
            ])
            db.commit()
            set_pipeline_enabled(db, True)
            assert quota_counts(db)["remaining"] == 15
            result = DailyPinScheduler(db).schedule_daily(date(2035, 1, 3))
            job = db.query(PinGenerationJob).one()
            assert result.mockups_prepared == 15
            assert job.requested_count == 15
            status = dashboard_pipeline_status(db)
            assert status["mockup_creatives"] == 100
            assert status["generated"] == 0
            assert status["remaining"] == 15
        finally:
            db.close()


def test_successful_ai_creatives_use_exactly_fifteen_slots_and_sixteenth_is_blocked():
    with TestClient(app):
        db = SessionLocal()
        product = _product(db)
        try:
            set_pipeline_enabled(db, True)
            db.add_all([
                PinCreative(
                    product_id=product.id, creative_type="product_focus", title=f"Etsy mockup {i}",
                    description="Original Etsy image", keywords=["item"], call_to_action="View",
                    source_type="mockup", generation_key=f"quota-mockup-plus-ai-{i}",
                )
                for i in range(100)
            ])
            db.commit()
            service = AIContentService(db, MockAIContentProvider())
            created = service.generate(product, PinCreativeType.PRODUCT_FOCUS, 15, force_new=True)
            assert len(created) == 15
            counts = quota_counts(db)
            assert counts["generated"] == 15
            assert counts["remaining"] == 0
            assert db.query(PinCreative).filter_by(source_type="mockup").count() == 100
            assert db.query(AIDailyQuotaSlot).filter_by(state="completed").count() == 15
            with pytest.raises(AIDailyQuotaExceededError):
                service.generate(product, PinCreativeType.PRODUCT_FOCUS, 1, force_new=True)
        finally:
            db.close()


def test_manual_generate_is_clamped_to_remaining_capacity():
    with TestClient(app) as client:
        db = SessionLocal()
        product = _product(db)
        service = AIContentService(db, MockAIContentProvider())
        set_pipeline_enabled(db, True)
        service.generate(product, PinCreativeType.PRODUCT_FOCUS, 14, force_new=True)
        try:
            response = client.post("/creatives/generate", json={
                "product_id": product.id,
                "creative_type": "lifestyle",
                "desired_count": 5,
            })
            assert response.status_code == 200
            assert response.json()["created"] == 1
            assert response.json()["remaining"] == 0
            assert quota_counts(db)["generated"] == 15
        finally:
            db.close()


def test_mockups_never_count_and_explicit_zero_creative_is_successful_ai():
    with TestClient(app):
        db = SessionLocal()
        product = _product(db)
        try:
            set_pipeline_enabled(db, True)
            db.add(PinCreative(
                product_id=product.id, creative_type="product_focus", title="Mockup",
                description="Etsy source", keywords=["item"], call_to_action="View",
                source_type="mockup", generation_key="quota-mockup-zero-test",
            ))
            db.commit()
            service = AIContentService(db, MockAIContentProvider())
            assert len(service.generate(product, PinCreativeType.PRODUCT_FOCUS, 1, force_new=True)) == 1
            assert quota_counts(db)["generated"] == 1
            assert quota_counts(db)["remaining"] == 14
        finally:
            db.close()


def test_generation_failure_releases_quota_for_retry():
    class BrokenProvider:
        def generate_json(self, _prompt):
            raise AIContentError("temporary mock failure")

    with TestClient(app):
        db = SessionLocal()
        product = _product(db)
        try:
            set_pipeline_enabled(db, True)
            with pytest.raises(AIContentError):
                AIContentService(db, BrokenProvider()).generate(
                    product, PinCreativeType.PRODUCT_FOCUS, 1, force_new=True
                )
            assert quota_counts(db)["generated"] == 0
            assert quota_counts(db)["remaining"] == 15
            assert db.query(AIDailyQuotaSlot).filter_by(state="reserved").count() == 0
            assert len(AIContentService(db, MockAIContentProvider()).generate(
                product, PinCreativeType.PRODUCT_FOCUS, 1, force_new=True
            )) == 1
        finally:
            db.close()


def test_concurrent_quota_reservations_cannot_exceed_fifteen():
    with TestClient(app):
        def reserve_ten(_):
            db = SessionLocal()
            try:
                set_pipeline_enabled(db, True)
                return len(reserve_ai_capacity(db, 10))
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            reservations = list(pool.map(reserve_ten, range(2)))
        db = SessionLocal()
        try:
            assert sum(reservations) == 15
            assert quota_counts(db)["reserved"] == 15
            assert quota_counts(db)["remaining"] == 0
        finally:
            db.close()


def test_manual_etsy_and_scheduler_demand_share_the_central_fifteen_quota():
    with TestClient(app):
        db = SessionLocal()
        product = _product(db)
        try:
            set_pipeline_enabled(db, True)
            scheduler = DailyPinScheduler(db).schedule_daily(date(2035, 1, 4))
            assert scheduler.ai_jobs_created == 1
            job = db.query(PinGenerationJob).one()
            assert job.requested_count == 15
            result = _worker(lambda session: AIContentService(session, MockAIContentProvider())).process_once()
            assert result.completed
            db.refresh(job)
            assert job.status == "completed"
            assert quota_counts(db)["generated"] == 15
            with pytest.raises(AIDailyQuotaExceededError):
                AIContentService(db, MockAIContentProvider()).generate(
                    product, PinCreativeType.LIFESTYLE, 1, force_new=True
                )
        finally:
            db.close()


def test_retry_of_completed_generation_job_does_not_create_or_count_a_duplicate():
    with TestClient(app):
        db = SessionLocal()
        product = _product(db)
        job = PinGenerationJob(product_id=product.id, requested_count=1, status="pending")
        db.add(job)
        db.commit()
        set_pipeline_enabled(db, True)
        try:
            initial = AIContentService(db, MockAIContentProvider()).generate(
                product, PinCreativeType.PRODUCT_FOCUS, 1, job_id=job.id, force_new=True
            )
            assert len(initial) == 1
            before = db.query(PinCreative).filter_by(source_type="ai").count()
            result = _worker(lambda session: AIContentService(session, MockAIContentProvider())).process_once()
            db.refresh(job)
            assert result.completed and job.status == "completed"
            assert db.query(PinCreative).filter_by(source_type="ai").count() == before
            assert quota_counts(db)["generated"] == 1
        finally:
            db.close()


def test_mockup_seo_metadata_uses_listing_fields_and_expected_schema():
    with TestClient(app):
        db = SessionLocal()
        product = _product(db, "Botanical Ceramic Mug")
        account = EtsyAccount(shop_name="SEO shop")
        listing = EtsyListing(
            account=account, product=product, listing_id="seo-mug", title=product.title,
            description="A botanical illustration mug for morning tea.",
            tags=["ceramic mug", "botanical gift"], images=["https://img.test/seo-mug.jpg"], state="active",
        )
        db.add_all([account, listing])
        db.commit()
        try:
            creative = AIContentService(db).ensure_mockup_creatives(product, listing)[0]
            metadata = creative.seo_metadata
            assert set(metadata) == {
                "primary_keyword", "secondary_keywords", "long_tail_keywords",
                "audience_keywords", "use_case_keywords", "search_intents", "creative_angle",
            }
            assert metadata["primary_keyword"] == "ceramic mug"
            assert "botanical gift" in metadata["secondary_keywords"]
            assert "morning tea" in metadata["use_case_keywords"][0]
            assert "morning tea" in metadata["long_tail_keywords"][0]
            assert creative.title.startswith("Botanical Ceramic Mug")
            assert creative.destination_url == product.url
        finally:
            db.close()


def test_dashboard_shows_pipeline_state_quota_and_mockup_exclusion():
    with TestClient(app) as client:
        db = SessionLocal()
        product = _product(db)
        creative = PinCreative(
            product_id=product.id, creative_type="product_focus", title="Mockup",
            description="Etsy mockup", keywords=["item"], call_to_action="View",
            source_type="mockup", generation_key="dashboard-mockup-pipeline",
            image_path="/media/dashboard-mockup.jpg",
        )
        db.add(creative)
        db.commit()
        db.close()
        response = client.get("/")
        assert response.status_code == 200
        assert 'id="ai-pipeline-state">AÇIK<' in response.text
        assert 'id="ai-quota-used">0/15<' in response.text
        assert 'id="ai-quota-remaining">15<' in response.text
        assert 'id="pipeline-mockup-creatives">1<' in response.text
        assert "mockup kreatifleri günlük 15 AI kreatif kotasına dahil değildir" in response.text
        assert 'id="generated-creatives-open"' in response.text
        assert '<dialog id="generated-creatives-dialog"' in response.text
        assert 'class="creative-card creative-mockup"' in response.text
        assert 'src="/media/dashboard-mockup.jpg" alt="Mockup"' in response.text
        assert 'class="creative-detail-image" src="/media/dashboard-mockup.jpg"' in response.text
        assert 'action="/creatives/1/approve"' in response.text
        assert 'action="/creatives/1/delete"' in response.text
        assert 'class="secondary edit-creative"' in response.text
        assert 'id="page-scroll-toggle"' in response.text
        assert "behavior: 'smooth'" in response.text
        assert "window.addEventListener('scroll', updatePageScrollToggle" in response.text
        assert "window.addEventListener('resize', updatePageScrollToggle)" in response.text
        assert "MutationObserver" not in response.text
        assert "generatedCreativesDialog.showModal()" in response.text
