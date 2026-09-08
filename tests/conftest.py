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

from app.database import Base, engine  # noqa: E402
import app.models  # noqa: E402,F401 - registers ORM tables before create_all.


@pytest.fixture(autouse=True)
def isolated_database():
    """Give every test a fresh schema without ever touching pinpilot.db."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    try:
        yield
    finally:
        Base.metadata.drop_all(bind=engine)
