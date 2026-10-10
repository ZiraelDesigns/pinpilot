"""Keep the test suite independent from a developer or VPS .env file."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


# This runs before test modules import the application settings. It prevents a
# production DATABASE_URL or real AI provider configured in .env from changing
# the test fixture pool or causing a remote request.
TEST_DATABASE = Path(tempfile.gettempdir()) / f"pinpilot-pytest-{os.getpid()}.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DATABASE.as_posix()}"
os.environ["AI_PROVIDER"] = "mock"
os.environ["AI_IMAGE_PROVIDER"] = "mock"
os.environ["PINTEREST_ANALYTICS_COLLECTION_ENABLED"] = "false"
for name in ("APP_AUTH_USERNAME", "APP_AUTH_PASSWORD", "APP_SESSION_SECRET_KEY"):
    os.environ[name] = ""

from app.database import Base, engine, SessionLocal  # noqa: E402
from app.analytics_migrations import upgrade_analytics_schema  # noqa: E402
import app.models  # noqa: E402,F401 - registers ORM tables before create_all.
from app.main import app  # noqa: E402
from app.security import AuthPrincipal, get_principal, require_admin_csrf, require_admin_read  # noqa: E402
from app.services.ai_pipeline import set_pipeline_enabled  # noqa: E402


@pytest.fixture(autouse=True)
def legacy_route_tests_use_an_authorized_admin():
    """Keep non-auth route tests focused; dedicated security tests remove this override."""
    principal = AuthPrincipal(
        username="test-admin", role="admin", csrf_token="test-csrf-token"
    )
    app.dependency_overrides[require_admin_csrf] = lambda: AuthPrincipal(
        username=principal.username, role=principal.role, csrf_token=principal.csrf_token
    )
    app.dependency_overrides[require_admin_read] = lambda: principal
    app.dependency_overrides[get_principal] = lambda: principal
    yield
    app.dependency_overrides.pop(require_admin_csrf, None)
    app.dependency_overrides.pop(require_admin_read, None)
    app.dependency_overrides.pop(get_principal, None)


@pytest.fixture(autouse=True)
def isolated_database():
    """Give every test a fresh schema without ever touching pinpilot.db."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    upgrade_analytics_schema(engine)
    # Legacy generation-focused tests explicitly opt into the pipeline while
    # remaining isolated from real providers by the mock provider environment.
    # Fail-closed defaults are covered separately by migration and missing-state tests.
    db = SessionLocal()
    try:
        set_pipeline_enabled(db, True)
    finally:
        db.close()
    try:
        yield
    finally:
        Base.metadata.drop_all(bind=engine)
