"""Provider-neutral Pinterest image generation using the OpenAI Images API."""

from __future__ import annotations

import base64
import mimetypes
import uuid
from pathlib import Path
from typing import Protocol

import httpx

from app.config import PROJECT_ROOT, settings


class AIImageError(Exception):
    """Raised when an image provider cannot produce an image."""


class AIImageProvider(Protocol):
    def generate(self, image_url: str, prompt: str) -> bytes: ...


class MockAIImageProvider:
    """No-network provider used by tests and local development."""

    def generate(self, image_url: str, prompt: str) -> bytes:
        raise AIImageError("Gerçek görsel üretimi için AI_IMAGE_PROVIDER=openai olarak ayarlanmalı.")


class OpenAIImageProvider:
    def __init__(self) -> None:
        key = settings.openai_api_key or settings.ai_api_key
        if not key:
            raise AIImageError("OPENAI_API_KEY veya AI_API_KEY yapılandırılmamış.")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise AIImageError("OpenAI SDK kurulu değil. requirements.txt içindeki openai paketini yükleyin.") from exc
        self.client = OpenAI(api_key=key)

    def generate(self, image_url: str, prompt: str) -> bytes:
        try:
            response = httpx.get(image_url, timeout=30, follow_redirects=True)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
            extension = mimetypes.guess_extension(content_type) or ".jpg"
            image_file = (f"product{extension}", response.content, content_type)
            result = self.client.images.edit(
                model=settings.openai_image_model,
                image=image_file,
                prompt=prompt,
                size="1024x1536",
                quality="low", 
            )
            encoded = result.data[0].b64_json
            if not encoded:
                raise AIImageError("OpenAI görsel yanıtı boş döndü.")
            return base64.b64decode(encoded)
        except AIImageError:
            raise
        except Exception as exc:
            raise AIImageError(f"OpenAI görsel üretimi başarısız: {exc}") from exc


def get_image_provider() -> AIImageProvider:
    return OpenAIImageProvider() if settings.ai_image_provider.lower() == "openai" else MockAIImageProvider()


def save_generated_image(image_bytes: bytes) -> str:
    relative_dir = Path(settings.generated_media_dir)
    absolute_dir = PROJECT_ROOT / relative_dir
    absolute_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.png"
    path = absolute_dir / filename
    path.write_bytes(image_bytes)
    return "/media/generated/" + filename
