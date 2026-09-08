from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Application settings loaded from OS variables or the project-root .env file."""

    app_name: str = "PinPilot"
    database_url: str = "sqlite:///./pinpilot.db"
    etsy_api_key: str | None = None
    etsy_shared_secret: str | None = None
    etsy_redirect_uri: str | None = None
    # A Fernet key used only to encrypt OAuth tokens stored in the local database.
    etsy_token_encryption_key: str | None = None
    pinterest_client_id: str | None = None
    pinterest_client_secret: str | None = None
    pinterest_redirect_uri: str | None = None
    # A separate Fernet key keeps Pinterest OAuth credentials encrypted at rest.
    pinterest_token_encryption_key: str | None = None
    # "mock" is the safe default for local development and automated tests.
    ai_provider: str = "mock"
    ai_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    ai_image_provider: str = "mock"
    openai_image_model: str = "gpt-image-2"
    generated_media_dir: str = "media/generated"
    # This absolute path makes `uvicorn app.main:app` independent of its launch directory.
    # OS environment variables still take precedence over values in this file.
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8")


settings = Settings()
