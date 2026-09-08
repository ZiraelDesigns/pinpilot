import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "PinPilot"}


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
                response = client.get("/")
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
