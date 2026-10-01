import logging

import httpx
import pytest
from pydantic import ValidationError

from app.config import settings
from app.services.pinterest_api import (
    PinterestApiClient,
    PinterestApiConfigurationError,
    PinterestAuthenticationError,
    PinterestBoardNotFound,
    PinterestCreatePinPayload,
    PinterestImageUrlError,
    PinterestInvalidMedia,
    PinterestInvalidPayload,
    PinterestPermissionError,
    PinterestPinNotFound,
    PinterestRateLimited,
    PinterestTemporaryError,
    PinterestUnknownApiError,
    PinterestUpdatePinPayload,
    pinterest_image_url,
)


def _client(handler, *, base_url="https://api.pinterest.com/v5", token="test-access-token"):
    transport = httpx.MockTransport(handler)
    return PinterestApiClient(token, base_url=base_url, http_client=httpx.Client(transport=transport))


def test_create_pin_payload_is_typed_and_uses_documented_image_url_shape():
    payload = PinterestCreatePinPayload(
        board_id="board-123",
        title="Handmade ceramic mug",
        description="A handmade mug.",
        link="https://etsy.example.test/listing/1",
        alt_text="Blue handmade ceramic mug",
        media_source={"source_type": "image_url", "url": "https://cdn.example.test/mug.png"},
    )
    assert payload.model_dump(exclude_none=True) == {
        "board_id": "board-123",
        "title": "Handmade ceramic mug",
        "description": "A handmade mug.",
        "link": "https://etsy.example.test/listing/1",
        "alt_text": "Blue handmade ceramic mug",
        "media_source": {
            "source_type": "image_url",
            "url": "https://cdn.example.test/mug.png",
            "is_standard": True,
        },
    }
    with pytest.raises(ValidationError):
        PinterestCreatePinPayload(
            title="missing board", media_source={"url": "https://cdn.example.test/image.png"}
        )


@pytest.mark.parametrize(
    ("field", "maximum", "over_limit"),
    [("title", 100, 101), ("description", 800, 801)],
)
def test_create_pin_payload_enforces_pinterest_text_limits(field, maximum, over_limit):
    base = {
        "board_id": "board-123",
        "title": "ğ" * 99,
        "description": "İ" * 799,
        "media_source": {"source_type": "image_url", "url": "https://cdn.example.test/pin.png"},
    }
    base[field] = "ğ" * maximum
    payload = PinterestCreatePinPayload(**base)
    assert len(getattr(payload, field)) == maximum
    base[field] = "ğ" * over_limit
    with pytest.raises(ValidationError):
        PinterestCreatePinPayload(**base)


@pytest.mark.parametrize(
    ("field", "maximum"), [("title", 100), ("description", 800)],
)
def test_update_pin_payload_enforces_pinterest_text_limits(field, maximum):
    from app.services.pinterest_api import PinterestUpdatePinPayload

    assert len(getattr(PinterestUpdatePinPayload(**{field: "ğ" * maximum}), field)) == maximum
    with pytest.raises(ValidationError):
        PinterestUpdatePinPayload(**{field: "ğ" * (maximum + 1)})


def test_api_base_url_allows_only_official_production_and_sandbox_hosts():
    assert PinterestApiClient("token", base_url="https://api-sandbox.pinterest.com/v5").base_url == (
        "https://api-sandbox.pinterest.com/v5"
    )
    for unsafe in (
        "http://api.pinterest.com/v5",
        "https://evil.example/v5",
        "https://api.pinterest.com/v4",
        "https://user@api.pinterest.com/v5",
    ):
        with pytest.raises(PinterestApiConfigurationError):
            PinterestApiClient("token", base_url=unsafe)


def test_local_generated_media_uses_public_base_url(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://pins.example.test/")
    assert pinterest_image_url("/media/generated/mock.png") == "https://pins.example.test/media/generated/mock.png"


@pytest.mark.parametrize(
    "image_reference",
    [None, "/media/private/image.png", "C:\\private\\image.png", "file:///tmp/image.png",
     "/media/generated/../../private.png", "/media/generated/nested/image.png", "/media/generated/",
     "http://cdn.example.test/image.png", "https://localhost/image.png", "https://127.0.0.1/image.png"],
)
def test_local_or_non_public_image_reference_is_rejected(image_reference, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://pins.example.test")
    with pytest.raises(PinterestImageUrlError):
        pinterest_image_url(image_reference)


def test_get_current_user_and_board_endpoints_are_routed_through_mock_transport():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/user_account"):
            return httpx.Response(200, json={"username": "mock-user"})
        return httpx.Response(200, json={"id": "board-1", "name": "Ideas"})

    client = _client(handler)
    assert client.get_current_user() == {"username": "mock-user"}
    assert client.get_board("board-1") == {"id": "board-1", "name": "Ideas"}
    assert seen == [("GET", "/v5/user_account"), ("GET", "/v5/boards/board-1")]


def test_list_boards_follows_documented_bookmark_pagination():
    requests = []

    def handler(request):
        requests.append(dict(request.url.params))
        if len(requests) == 1:
            return httpx.Response(200, json={"items": [{"id": "b1"}], "bookmark": "next-page"})
        return httpx.Response(200, json={"items": [{"id": "b2"}], "bookmark": None})

    assert [row["id"] for row in _client(handler).list_boards()] == ["b1", "b2"]
    assert requests == [{"page_size": "100"}, {"page_size": "100", "bookmark": "next-page"}]


def test_create_get_update_delete_pin_methods_use_api_v5_paths():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.read()))
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method == "POST":
            return httpx.Response(201, json={"id": "pin-1", "created_at": "2026-09-29T10:00:00Z", "board_id": "b1"})
        return httpx.Response(200, json={"id": "pin-1", "created_at": "2026-09-29T10:00:00Z", "board_id": "b1"})

    client = _client(handler)
    payload = PinterestCreatePinPayload(
        board_id="b1", title="Title", media_source={"url": "https://cdn.example.test/image.png"}
    )
    assert client.create_pin(payload).id == "pin-1"
    assert client.get_pin("pin-1").board_id == "b1"
    assert client.update_pin("pin-1", PinterestUpdatePinPayload(title="Updated")).id == "pin-1"
    client.delete_pin("pin-1")
    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "/v5/pins"), ("GET", "/v5/pins/pin-1"), ("PATCH", "/v5/pins/pin-1"),
        ("DELETE", "/v5/pins/pin-1"),
    ]
    assert b'"source_type":"image_url"' in calls[0][2]


@pytest.mark.parametrize(
    ("status", "path", "error_type", "body", "headers"),
    [
        (401, "/pins", PinterestAuthenticationError, {"message": "auth failed"}, {}),
        (403, "/pins", PinterestPermissionError, {"message": "scope denied"}, {}),
        (404, "/boards/b1", PinterestBoardNotFound, {"message": "missing"}, {}),
        (404, "/pins/p1", PinterestPinNotFound, {"message": "missing"}, {}),
        (400, "/pins", PinterestInvalidMedia, {"message": "invalid media source"}, {}),
        (422, "/pins", PinterestInvalidPayload, {"message": "invalid title"}, {}),
        (429, "/pins", PinterestRateLimited, {"message": "slow down"}, {"Retry-After": "30"}),
        (503, "/pins", PinterestTemporaryError, {"message": "unavailable"}, {}),
        (418, "/pins", PinterestUnknownApiError, {"message": "unexpected"}, {}),
    ],
)
def test_http_statuses_map_to_distinct_safe_exceptions(status, path, error_type, body, headers):
    client = _client(lambda request: httpx.Response(status, json=body, headers=headers))
    with pytest.raises(error_type) as caught:
        client._request("GET", path)
    if error_type is PinterestRateLimited:
        assert caught.value.retryable is True
        assert caught.value.retry_after == "30"
        assert "30 saniye" in str(caught.value)
    else:
        assert caught.value.retryable is (error_type is PinterestTemporaryError)


def test_transport_timeout_is_retryable_and_no_automatic_retry_occurs():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(PinterestTemporaryError):
        _client(handler).get_current_user()
    assert calls == 1


def test_safe_error_logging_never_includes_access_or_client_secret(caplog, monkeypatch):
    secret = "client-secret-do-not-log"
    token = "pina-never-log-access-token"
    monkeypatch.setattr(settings, "pinterest_client_secret", secret)
    client = _client(
        lambda request: httpx.Response(
            400,
            json={"message": f"bad {token}; {secret}; Bearer {token}"},
        ),
        token=token,
    )
    caplog.set_level(logging.WARNING)
    with pytest.raises(PinterestInvalidPayload):
        client._request("POST", "/pins")
    output = caplog.text
    assert token not in output
    assert secret not in output
    assert "[REDACTED]" in output
