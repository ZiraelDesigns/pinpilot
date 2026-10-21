import tempfile
from datetime import datetime, time
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, SessionLocal, get_db
from app.main import app, dashboard as dashboard_route
from app.models import Pin, PinCreative, Product
from app.models.core import PinCreativeStatus, PinCreativeType, PinStatus
from app.security import AuthPrincipal


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ZiraelAutomation Content Studio"}


def test_privacy_policy_is_served_from_public_route():
    with TestClient(app) as client:
        response = client.get("/privacy-policy.html")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Privacy Policy" in response.text
    assert "https://ziraelautomation.vesvada.store/privacy-policy.html" in response.text
    assert "pinpilot.vesvada.store" not in response.text
    assert "ZiraelAutomation Content Studio" in response.text
    assert "ZiraelDesigns Content Studio" not in response.text
    assert "ziraelamz@gmail.com" in response.text
    assert "personal project and private workspace" in response.text
    assert "not a registered company name" in response.text
    assert "no company or other legal entity is claimed" in response.text
    assert "operator's full legal name is not included" in response.text
    assert "have not been determined here" in response.text
    assert "selected Pins and publication schedule" in response.text
    assert "automatic production publishing is currently disabled" in response.text.casefold()
    assert "user_accounts:read" in response.text
    assert "boards:read" in response.text
    assert "pins:write" in response.text
    assert "does not request <code>pins:read</code>" in response.text
    assert "October 21, 2026" in response.text
    assert "No fixed automatic retention period" in response.text
    assert "legal identity" in response.text
    assert 'href="/static/public-info.css"' in response.text
    assert "pinpilot" not in response.text.casefold()


def test_public_homepage_uses_ziraelautomation_brand_and_links_privacy_policy():
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "ZiraelAutomation Content Studio" in response.text
    assert "ZiraelDesigns" not in response.text
    assert "pinpilot" not in response.text.casefold()
    assert "Pinterest content planning" in response.text
    assert "Pin titles, descriptions, and keyword metadata" in response.text
    assert "selected Pinterest Pins" in response.text
    assert "chosen schedule" in response.text
    assert "automatic production publishing is not yet enabled" in response.text.casefold()
    assert "completion of the scheduled publisher integration" in response.text
    assert 'property="og:title" content="ZiraelAutomation Content Studio' in response.text
    assert 'name="twitter:title" content="ZiraelAutomation Content Studio' in response.text
    normalized_text = " ".join(response.text.split()).casefold()
    assert "automatic production publishing is not yet enabled" in normalized_text
    assert "production ai generation pipeline and production analytics collection are also currently disabled" in normalized_text
    assert 'href="/privacy-policy.html"' in response.text
    assert "ziraelamz@gmail.com" in response.text
    assert 'href="/static/public-info.css"' in response.text


def test_public_information_styles_are_local_and_responsive():
    with TestClient(app) as client:
        response = client.get("/static/public-info.css")

    assert response.status_code == 200
    assert "@media (max-width: 760px)" in response.text
    assert "@media (prefers-reduced-motion: reduce)" in response.text
    assert "fonts.googleapis.com" not in response.text


def test_public_openapi_metadata_uses_public_brand():
    with TestClient(app) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "ZiraelAutomation Content Studio"
    assert "pinpilot" not in response.text.casefold()


def test_dashboard_shows_empty_counts():
    with tempfile.TemporaryDirectory() as tmpdir:
        database_path = Path(tmpdir) / "test.db"
        test_engine = create_engine(
            f"sqlite:///{database_path}",
            connect_args={"check_same_thread": False},
        )
        TestingSessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=test_engine,
        )

        Base.metadata.create_all(bind=test_engine)

        def override_get_db():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db

        try:
            with TestClient(app) as client:
                response = client.get("/dashboard")
        finally:
            app.dependency_overrides.clear()
            test_engine.dispose()

    assert response.status_code == 200
    assert "PinPilot" in response.text
    assert 'id="products-count">0<' in response.text
    assert 'id="generated-pins-count">0<' in response.text
    assert 'id="scheduled-pins-count">0<' in response.text
    assert 'id="published-pins-count">0<' in response.text
    assert 'id="today-prepared-pins-count">0<' in response.text
    assert 'id="mockup-scheduled-pins-count">0<' in response.text
    assert 'id="ai-scheduled-pins-count">0<' in response.text
    assert 'id="pending-ai-jobs-count">0<' in response.text


def test_dashboard_renders_persisted_content_portfolio():
    from starlette.requests import Request

    db = SessionLocal()
    product = Product(title="Portfolio product")
    db.add(product)
    db.flush()
    creative = PinCreative(
        product_id=product.id, creative_type=PinCreativeType.LIFESTYLE.value,
        title="Portfolio creative", description="Test description", keywords=["quiet garden"],
        call_to_action="Explore", image_path="https://images.example.test/portfolio.jpg",
        source_type="mockup", destination_url="https://example.test/item",
        status=PinCreativeStatus.DRAFT.value, generation_key=f"dashboard-portfolio:{product.id}",
    )
    db.add(creative)
    db.flush()
    db.add(Pin(
        product_id=product.id, creative_id=creative.id, title="Quiet garden decor",
        status=PinStatus.SCHEDULED.value,
        scheduled_for=datetime.combine(datetime.now().date(), time(hour=15)),
        portfolio_snapshot={
            "version": "smart_content_portfolio_v1", "final_score": 84,
            "opportunity_score": 78, "selection_reason": "Yüksek pano uyumu; kontrollü keşif seçimi",
            "creative_type": PinCreativeType.LIFESTYLE.value, "creative_angle": "calm home",
            "primary_keyword": "quiet garden decor", "board": {"id": 1, "name": "Garden ideas"},
            "exploration": True, "learned": False, "seasonal": True,
            "portfolio_summary": {
                "selected_count": 1, "total_candidates": 4, "keyword_diversity": 1,
                "keyword_distribution": {"quiet garden decor": 1},
                "creative_type_distribution": {"lifestyle": 1},
                "creative_angle_distribution": {"calm home": 1},
                "board_distribution": {"1": 1}, "exploration_count": 1,
                "learned_positive_count": 0, "seasonal_count": 1,
            },
        },
    ))
    db.commit()
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    try:
        response = dashboard_route(
            request=request,
            db=db,
            principal=AuthPrincipal(username="test-admin", role="admin", csrf_token="test-csrf-token"),
        )
        body = response.body.decode()
    finally:
        db.close()

    assert response.status_code == 200
    assert "Bugünün İçerik Portföyü" in body
    assert "Quiet garden decor" in body
    assert "84 / 100" in body
    assert "Garden ideas" in body
    assert "Keşif" in body
