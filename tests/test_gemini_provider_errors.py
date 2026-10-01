from types import SimpleNamespace
import socket
import ssl

import httpx
import pytest
from google.genai.errors import APIError

from app.services.ai_content import (
    AIProviderRequestError,
    GeminiAIContentProvider,
    _classify_gemini_exception,
)
from app.services.ai_worker import is_retryable_error


@pytest.mark.parametrize(
    ("exception", "category", "status", "provider_code", "retryable"),
    [
        (httpx.ConnectTimeout("sensitive timeout details"), "timeout", None, None, True),
        (socket.gaierror("secret-like DNS detail"), "dns_error", None, None, True),
        (ssl.SSLError("secret-like TLS detail"), "tls_error", None, None, False),
        (httpx.ConnectError("secret-like transport detail"), "network_error", None, None, True),
        (APIError(401, {"error": {"status": "UNAUTHENTICATED", "message": "credential must stay private"}}), "authentication_error", 401, "UNAUTHENTICATED", False),
        (APIError(403, {"error": {"status": "PERMISSION_DENIED", "message": "credential must stay private"}}), "permission_error", 403, "PERMISSION_DENIED", False),
        (APIError(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "credential must stay private"}}), "rate_limit", 429, "RESOURCE_EXHAUSTED", True),
        (APIError(404, {"error": {"status": "NOT_FOUND", "message": "credential must stay private"}}), "model_or_endpoint_not_found", 404, "NOT_FOUND", False),
        (APIError(503, {"error": {"status": "UNAVAILABLE", "message": "credential must stay private"}}), "provider_server_error", 503, "UNAVAILABLE", True),
    ],
)
def test_gemini_transport_and_http_errors_are_classified_without_raw_details(
    exception, category, status, provider_code, retryable
):
    result = _classify_gemini_exception(exception)

    assert result.category == category
    assert result.http_status == status
    assert result.provider_code == provider_code
    assert result.retryable is retryable
    assert is_retryable_error(result) is retryable
    assert "credential must stay private" not in str(result)
    assert "sensitive timeout details" not in str(result)
    assert "secret-like DNS detail" not in str(result)
    assert "secret-like TLS detail" not in str(result)


def test_gemini_provider_logs_only_safe_classification_fields(caplog):
    secret_marker = "DO_NOT_LOG_GEMINI_SECRET"
    sdk_error = APIError(
        429,
        {"error": {"status": "RESOURCE_EXHAUSTED", "message": secret_marker}},
    )
    provider = GeminiAIContentProvider.__new__(GeminiAIContentProvider)
    provider._client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **_kwargs: (_ for _ in ()).throw(sdk_error))
    )

    with caplog.at_level("WARNING"):
        with pytest.raises(AIProviderRequestError) as error:
            provider.generate_json("synthetic prompt")

    assert error.value.category == "rate_limit"
    assert error.value.http_status == 429
    assert error.value.provider_code == "RESOURCE_EXHAUSTED"
    assert "429" in caplog.text
    assert "RESOURCE_EXHAUSTED" in caplog.text
    assert secret_marker not in caplog.text
    assert secret_marker not in str(error.value)
