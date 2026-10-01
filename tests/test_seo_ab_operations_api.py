from datetime import date, datetime
import re
from uuid import uuid4

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    AnalyticsSnapshot,
    Pin,
    PinCreative,
    PinterestAccount,
    PinterestPublishIntent,
    PinterestPublishIntentStatus,
    Product,
    PublishedPinterestPin,
    SEOABVariantPublication,
    SEOGeneration,
)
from app.models.core import PinStatus
from app.routers.experiments import get_seo_ab_publisher
from app.security import require_admin_csrf
from app.database import get_db
from app.services.keyword_intelligence import ensure_keyword_intelligence
from app.services.pinterest_publisher import PinterestPinPublishResult, PinterestPublisher


def _generation(*, low_quality=False):
    with SessionLocal() as db:
        product = Product(title="SEO A/B API product", url=f"https://shop.example.test/{uuid4().hex}")
        db.add(product)
        db.flush()
        metadata = {
            "primary_keyword": "botanical soy candle",
            "secondary_keywords": ["natural wax candle", "botanical home decor"],
            "long_tail_keywords": ["botanical candle for a reading room"],
            "audience_keywords": ["thoughtful home shoppers"],
            "use_case_keywords": ["calm reading room"],
            "search_intents": ["product_search"],
            "creative_angle": "botanical reading nook",
        }
        creative = PinCreative(
            product=product,
            creative_type="product_focus",
            title="Botanical Soy Candle for a Calm Reading Room and Botanical Home Decor",
            description="Bring a botanical soy candle into your calm reading room for quiet evenings. Explore this thoughtful botanical home decor accent and view the product details today.",
            keywords=["botanical soy candle", "natural wax candle"],
            seo_metadata=metadata,
            call_to_action="Explore",
            image_path="https://media.example.test/ab-api.png",
            source_type="ai",
            destination_url=product.url,
            generation_key=f"seo-ab-api-{uuid4().hex}",
        )
        db.add(creative)
        db.flush()
        snapshot = {
            "title": "Botanical Soy Candle for a Calm Reading Room and Botanical Home Decor",
            "description": "Bring a botanical soy candle into your calm reading room for quiet evenings. Explore this thoughtful botanical home decor accent and view the product details today.",
            "seo_metadata": metadata,
        }
        if low_quality:
            snapshot = {"title": "", "description": "", "seo_metadata": metadata}
        generation = SEOGeneration(
            product_id=product.id,
            creative_id=creative.id,
            started_at=datetime(2026, 9, 1),
            completed_at=datetime(2026, 9, 1),
            provider="mock",
            model_name="mock-model",
            prompt_version="seo-prompt-test",
            schema_version="seo-schema-test",
            status="completed",
            output_snapshot=snapshot,
        )
        db.add(generation)
        db.flush()
        ensure_keyword_intelligence(db, generation)
        db.commit()
        return generation.id, product.id, creative.id, snapshot


def _create_draft(client, generation_id):
    return client.post(
        "/experiments/seo-ab",
        json={"source_generation_id": generation_id, "hypothesis": "Measure an existing keyword focus"},
    )


def _csrf_login(monkeypatch, client):
    monkeypatch.setattr(settings, "app_auth_username", "owner")
    monkeypatch.setattr(settings, "app_auth_password", "a-long-test-password-123")
    monkeypatch.setattr(settings, "app_session_secret_key", "s" * 48)
    monkeypatch.setattr(settings, "app_session_ttl_seconds", 28800)
    monkeypatch.setattr(settings, "app_session_cookie_secure", True)
    app.dependency_overrides.pop(require_admin_csrf, None)
    page = client.get("/auth/login")
    token = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/auth/login",
        data={"username": "owner", "password": "a-long-test-password-123", "_csrf": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    dashboard = client.get("/")
    return re.search(r'<meta name="csrf-token" content="([^"]+)"', dashboard.text).group(1)


def test_seo_ab_operations_require_admin_and_csrf(monkeypatch):
    generation_id, *_ = _generation()
    app.dependency_overrides.pop(require_admin_csrf, None)
    with TestClient(app, base_url="https://testserver") as client:
        unauthenticated = _create_draft(client, generation_id)
        assert unauthenticated.status_code == 401
        assert client.get("/experiments/seo-ab").status_code == 401
        csrf = _csrf_login(monkeypatch, client)
        assert client.get("/experiments/seo-ab").status_code == 200
        missing = _create_draft(client, generation_id)
        wrong = client.post(
            "/experiments/seo-ab",
            json={"source_generation_id": generation_id, "hypothesis": "Measure an existing keyword focus"},
            headers={"X-CSRF-Token": "wrong"},
        )
        assert missing.status_code == wrong.status_code == 403
        accepted = client.post(
            "/experiments/seo-ab",
            json={"source_generation_id": generation_id, "hypothesis": "Measure an existing keyword focus"},
            headers={"X-CSRF-Token": csrf},
        )
        assert accepted.status_code == 201
        assert accepted.json()["status"] == "DRAFT"
        assert client.get("/health").status_code == 200


def test_draft_and_keyword_focus_variant_api_are_idempotent_and_preserve_source():
    generation_id, _, _, original_snapshot = _generation()
    with TestClient(app) as client:
        draft = _create_draft(client, generation_id)
        again = _create_draft(client, generation_id)
        assert draft.status_code == again.status_code == 201
        experiment_id = draft.json()["id"]
        assert again.json()["id"] == experiment_id
        variant_payload = {"variant_type": "KEYWORD_FOCUS", "candidate_keyword": "botanical home decor"}
        first = client.post(f"/experiments/seo-ab/{experiment_id}/variants", json=variant_payload)
        again_variant = client.post(
            f"/experiments/seo-ab/{experiment_id}/variants",
            json={**variant_payload, "candidate_keyword": " BOTANICAL  HOME DECOR "},
        )
        assert first.status_code == again_variant.status_code == 201
        assert first.json()["id"] == again_variant.json()["id"]
        assert first.json()["quality_status"] in {"PASS", "WARN"}
        assert first.json()["change_set"]["new_keywords_created"] is False
        invalid = client.post(
            f"/experiments/seo-ab/{experiment_id}/variants",
            json={"candidate_keyword": "invented keyword"},
        )
        assert invalid.status_code == 400
    with SessionLocal() as db:
        generation = db.get(SEOGeneration, generation_id)
        assert generation.output_snapshot == original_snapshot


def test_quality_fail_cannot_enter_ready_or_reach_publish_boundary():
    generation_id, *_ = _generation(low_quality=True)
    with TestClient(app) as client:
        draft = _create_draft(client, generation_id)
        experiment_id = draft.json()["id"]
        variant = client.post(
            f"/experiments/seo-ab/{experiment_id}/variants",
            json={"candidate_keyword": "botanical home decor"},
        )
        assert variant.status_code == 201
        assert variant.json()["status"] == "QUALITY_FAIL"
        assert variant.json()["quality_status"] == "FAIL"
        ready = client.patch(
            f"/experiments/seo-ab/{experiment_id}/status", json={"target_status": "READY"},
        )
        assert ready.status_code == 409
        monkeypatch_disabled = settings.pinterest_publish_enabled
        assert monkeypatch_disabled is False
        publish = client.post(
            f"/experiments/seo-ab/{experiment_id}/variants/{variant.json()['id']}/publish",
            json={"pin_id": 1, "account_id": 1},
        )
        assert publish.status_code == 409
        with SessionLocal() as db:
            assert db.scalar(select(PinterestPublishIntent)) is None


def test_publish_disabled_creates_no_intent_and_variant_attribution_comparison_learning_chain(monkeypatch):
    generation_id, product_id, creative_id, _ = _generation()
    with TestClient(app) as client:
        experiment = _create_draft(client, generation_id).json()
        experiment_id = experiment["id"]
        variant_response = client.post(
            f"/experiments/seo-ab/{experiment_id}/variants",
            json={"candidate_keyword": "botanical home decor"},
        )
        variant_id = variant_response.json()["id"]
        assert client.patch(f"/experiments/seo-ab/{experiment_id}/status", json={"target_status": "READY"}).status_code == 200
        assert client.patch(f"/experiments/seo-ab/{experiment_id}/status", json={"target_status": "RUNNING"}).status_code == 200

        with SessionLocal() as db:
            account = PinterestAccount(account_name="A/B API account", account_identifier=f"ab-api-{uuid4().hex}", is_active=True)
            pin = Pin(
                product_id=product_id,
                creative_id=creative_id,
                title="Original title",
                description="Original description",
                image_path="https://media.example.test/ab-api.png",
                destination_url="https://shop.example.test/item",
                status=PinStatus.SCHEDULED.value,
                scheduled_for=datetime(2026, 9, 30, 9),
            )
            db.add_all([account, pin])
            db.commit()
            account_id, pin_id = account.id, pin.id

        monkeypatch.setattr(settings, "pinterest_publish_enabled", False)
        disabled = client.post(
            f"/experiments/seo-ab/{experiment_id}/variants/{variant_id}/publish",
            json={"pin_id": pin_id, "account_id": account_id},
        )
        assert disabled.status_code == 423
        with SessionLocal() as db:
            assert db.scalar(select(PinterestPublishIntent)) is None

        class FakeProvider:
            publishing_enabled = True

            def publish_pin(self, account, request):
                return PinterestPinPublishResult("mock-variant-pin", datetime(2026, 9, 30, 12))

        def fake_publisher(db=Depends(get_db)):
            return PinterestPublisher(db, FakeProvider())

        monkeypatch.setattr(settings, "pinterest_publish_enabled", True)
        app.dependency_overrides[get_seo_ab_publisher] = fake_publisher
        try:
            published = client.post(
                f"/experiments/seo-ab/{experiment_id}/variants/{variant_id}/publish",
                json={"pin_id": pin_id, "account_id": account_id},
            )
            assert published.status_code == 200
            published_id = published.json()["published_pin_id"]
            with SessionLocal() as db:
                link = db.scalar(select(SEOABVariantPublication).where(
                    SEOABVariantPublication.published_pin_id == published_id,
                ))
                assert link is not None and link.variant_id == variant_id
                assert db.scalar(select(PinterestPublishIntent).where(
                    PinterestPublishIntent.seo_ab_variant_id == variant_id,
                    PinterestPublishIntent.status == PinterestPublishIntentStatus.PUBLISHED.value,
                )) is not None
                db.add(AnalyticsSnapshot(
                    published_pin_id=published_id,
                    pin_id=pin_id,
                    metric_date=date(2026, 9, 30),
                    fetched_at=datetime(2026, 10, 1),
                    impressions=0,
                    saves=0,
                    pin_clicks=None,
                    outbound_clicks=None,
                    engagements=None,
                    metric_schema_version="pinterest_v5_organic_daily",
                ))
                other_account = PinterestAccount(
                    account_name="Other account", account_identifier=f"other-{uuid4().hex}", is_active=True,
                )
                other_pin = Pin(
                    product_id=product_id,
                    title="Other account baseline",
                    description="Other account baseline",
                    status=PinStatus.PUBLISHED.value,
                )
                db.add_all([other_account, other_pin])
                db.flush()
                other_publication = PublishedPinterestPin(
                    pin_id=other_pin.id,
                    account_id=other_account.id,
                    account_identifier_snapshot=other_account.account_identifier,
                    external_pin_id="other-account-baseline",
                    seo_generation_id=generation_id,
                    published_at=datetime(2026, 9, 29),
                    metadata_snapshot={"seo_generation_id": generation_id},
                )
                db.add(other_publication)
                db.flush()
                db.add(AnalyticsSnapshot(
                    published_pin_id=other_publication.id,
                    pin_id=other_pin.id,
                    metric_date=date(2026, 9, 30),
                    fetched_at=datetime(2026, 10, 1),
                    impressions=999,
                    metric_schema_version="pinterest_v5_organic_daily",
                ))
                db.commit()
                other_account_id = other_account.id

            mixed = client.post(
                f"/experiments/seo-ab/{experiment_id}/comparisons",
                json={"period_start": "2026-09-01", "period_end": "2026-09-30", "metric_name": "impressions"},
            )
            assert mixed.status_code == 400
            compared = client.post(
                f"/experiments/seo-ab/{experiment_id}/comparisons",
                json={"period_start": "2026-09-01", "period_end": "2026-09-30", "metric_name": "impressions", "account_id": account_id},
            )
            assert compared.status_code == 200
            data = compared.json()["result"]
            variant_metrics = next(row for row in data["variants"] if row["variant_id"] == variant_id)
            assert variant_metrics["metrics"]["impressions"] == 0
            assert variant_metrics["metrics"]["pin_clicks"] is None
            assert data["winner_selected"] is False
            assert data["baseline"]["metrics"]["impressions"] is None
            assert other_account_id != account_id

            learning = client.post(
                "/experiments/seo-ab/learning/run",
                json={"account_id": account_id, "window_start": "2026-09-01", "window_end": "2026-09-30"},
            )
            repeated_learning = client.post(
                "/experiments/seo-ab/learning/run",
                json={"account_id": account_id, "window_start": "2026-09-01", "window_end": "2026-09-30"},
            )
            assert learning.status_code == repeated_learning.status_code == 201
            assert learning.json()["id"] == repeated_learning.json()["id"]
            assert learning.json()["result"]["external_calls"] is False
            assert any(
                item["value"] == variant_metrics["variant_key"]
                for item in learning.json()["result"]["dimensions"]["variant"]
            )
        finally:
            app.dependency_overrides.pop(get_seo_ab_publisher, None)
