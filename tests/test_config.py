from pathlib import Path

from app.config import ENV_FILE, PROJECT_ROOT, Settings


def test_settings_dotenv_path_is_anchored_to_project_root():
    assert ENV_FILE == PROJECT_ROOT / ".env"
    assert Path(Settings.model_config["env_file"]) == ENV_FILE


def test_settings_loads_etsy_variable_names_from_an_explicit_dotenv_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ETSY_API_KEY=test-api-key\n"
        "ETSY_SHARED_SECRET=test-shared-secret\n"
        "ETSY_REDIRECT_URI=https://example.test/etsy/callback\n"
        "ETSY_TOKEN_ENCRYPTION_KEY=test-encryption-key\n",
        encoding="utf-8",
    )

    loaded = Settings(_env_file=env_file)

    assert loaded.etsy_api_key == "test-api-key"
    assert loaded.etsy_shared_secret == "test-shared-secret"
    assert loaded.etsy_redirect_uri == "https://example.test/etsy/callback"
    assert loaded.etsy_token_encryption_key == "test-encryption-key"
