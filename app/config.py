from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Application settings loaded from OS variables or the project-root .env file."""

    app_name: str = "PinPilot"
    database_url: str = "sqlite:///./pinpilot.db"
    # Mutating dashboard/API access is disabled until all admin auth settings exist.
    app_auth_username: str | None = None
    app_auth_password: str | None = None
    app_session_secret_key: str | None = None
    app_session_ttl_seconds: int = 28800
    app_session_cookie_secure: bool = True
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
    # Keep publishing explicitly disabled until a separately approved activation.
    pinterest_publish_enabled: bool = False
    # Analytics collection remains opt-in until Pinterest access is explicitly approved.
    pinterest_analytics_collection_enabled: bool = False
    # Optional ISO-3166-1 alpha-2 SEO calendar region; unset means no country assumption.
    seo_calendar_region: str | None = None
    # Set to https://api-sandbox.pinterest.com/v5 to use Pinterest Sandbox.
    pinterest_api_base_url: str = "https://api.pinterest.com/v5"
    pinterest_api_timeout_seconds: float = 20.0
    # "mock" is the safe default for local development and automated tests.
    ai_provider: str = "mock"
    ai_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    # Number of SEO options requested for each creative; kept bounded for cost/safety.
    seo_candidate_count: int = Field(default=3, ge=1, le=10)
    ai_image_provider: str = "mock"
    openai_image_model: str = "gpt-image-2"
    generated_media_dir: str = "media/generated"
    # Public HTTPS origin used when a future Pinterest request needs an absolute
    # URL for a locally generated image. Keep unset for local HTTP development.
    public_base_url: str | None = None
    # This absolute path makes `uvicorn app.main:app` independent of its launch directory.
    # OS environment variables still take precedence over values in this file.
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8")


settings = Settings()
