import pytest

from app.config import settings
from app.services.ai_image import PublicMediaUrlError, public_media_url


def test_public_media_url_uses_configured_https_origin(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://pins.example.test/")

    assert public_media_url("/media/generated/example.png") == (
        "https://pins.example.test/media/generated/example.png"
    )


def test_public_media_url_rejects_missing_or_non_https_origin(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", None)
    with pytest.raises(PublicMediaUrlError, match="HTTPS"):
        public_media_url("/media/generated/example.png")

    monkeypatch.setattr(settings, "public_base_url", "http://pins.example.test")
    with pytest.raises(PublicMediaUrlError, match="HTTPS"):
        public_media_url("/media/generated/example.png")


def test_public_media_url_rejects_paths_outside_generated_media(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://pins.example.test")

    with pytest.raises(PublicMediaUrlError, match="Yalnızca"):
        public_media_url("/media/private-file.png")
