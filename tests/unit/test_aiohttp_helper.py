from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_request


@pytest.mark.asyncio
async def test_aiohttp_request_with_json_data_sends_correct_format() -> None:
    """Test that json_data parameter sends data as JSON with correct encoding"""
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="POST",
            url="https://example.com/api",
            json_data={"key": "value", "number": 42},
        )

    # Verify the request was called with correct parameters
    mock_session.request.assert_called_once()
    assert captured_args[0] == "POST"  # method is first positional argument
    assert captured_request_kwargs["url"] == "https://example.com/api"
    # Verify json parameter was used (aiohttp will handle JSON encoding)
    assert "json" in captured_request_kwargs
    assert captured_request_kwargs["json"] == {"key": "value", "number": 42}
    # Verify data parameter was NOT used
    assert "data" not in captured_request_kwargs


@pytest.mark.asyncio
async def test_aiohttp_request_with_data_sends_as_form_data() -> None:
    """Test that data parameter sends data as form-encoded when Content-Type is not application/json"""
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="POST",
            url="https://example.com/api",
            data={"field1": "value1", "field2": "value2"},
        )

    # Verify data parameter was used (aiohttp will handle form encoding)
    assert "data" in captured_request_kwargs
    assert captured_request_kwargs["data"] == {"field1": "value1", "field2": "value2"}
    # Verify json parameter was NOT used
    assert "json" not in captured_request_kwargs


@pytest.mark.asyncio
async def test_aiohttp_request_with_data_and_json_content_type_uses_json() -> None:
    """Test that data parameter with application/json Content-Type uses JSON encoding"""
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="POST",
            url="https://example.com/api",
            headers={"Content-Type": "application/json"},
            data={"name": "test", "value": 42},
        )

    # Verify json parameter was used (Content-Type is application/json)
    assert "json" in captured_request_kwargs
    assert captured_request_kwargs["json"] == {"name": "test", "value": 42}
    assert "data" not in captured_request_kwargs


@pytest.mark.asyncio
async def test_aiohttp_request_with_data_and_form_urlencoded_uses_data() -> None:
    """Test that data parameter with form-urlencoded Content-Type uses data encoding"""
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="POST",
            url="https://example.com/api",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"username": "user", "password": "pass"},
        )

    # Verify data parameter was used (form-urlencoded Content-Type)
    assert "data" in captured_request_kwargs
    assert captured_request_kwargs["data"] == {"username": "user", "password": "pass"}
    # Verify headers were passed correctly
    assert captured_request_kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert "json" not in captured_request_kwargs


@pytest.mark.asyncio
async def test_aiohttp_request_with_data_and_json_content_type_case_insensitive() -> None:
    """Test that Content-Type header check is case-insensitive for application/json"""
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        # Test lowercase content-type
        await aiohttp_request(
            method="POST",
            url="https://example.com/api",
            headers={"content-type": "application/json"},
            data={"key": "value"},
        )

    # Verify json parameter was used (content-type is application/json, case-insensitive)
    assert "json" in captured_request_kwargs
    assert captured_request_kwargs["json"] == {"key": "value"}
    assert "data" not in captured_request_kwargs


@pytest.mark.asyncio
async def test_aiohttp_request_with_data_and_json_content_type_with_charset() -> None:
    """Test that Content-Type with charset still matches application/json"""
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="POST",
            url="https://example.com/api",
            headers={"Content-Type": "application/json; charset=utf-8"},
            data={"key": "value"},
        )

    # Verify json parameter was used (Content-Type contains application/json)
    assert "json" in captured_request_kwargs
    assert captured_request_kwargs["json"] == {"key": "value"}
    assert "data" not in captured_request_kwargs


@pytest.mark.asyncio
async def test_aiohttp_request_headers_passed_correctly() -> None:
    """Test that custom headers are passed correctly to the request"""
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    custom_headers = {
        "Authorization": "Bearer token123",
        "X-Custom-Header": "custom-value",
        "Content-Type": "application/json",
    }

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="POST",
            url="https://example.com/api",
            headers=custom_headers,
            json_data={"key": "value"},
        )

    # Verify headers were passed correctly
    assert captured_request_kwargs["headers"] == custom_headers


@pytest.mark.asyncio
async def test_aiohttp_request_cookies_passed_correctly() -> None:
    """Test that cookies are passed correctly to the request"""
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    cookies = {"session_id": "abc123", "user_id": "456"}

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="GET",
            url="https://example.com/api",
            cookies=cookies,
        )

    # Verify cookies were passed correctly
    assert captured_request_kwargs["cookies"] == cookies


@pytest.mark.asyncio
async def test_aiohttp_request_method_uppercased() -> None:
    """Test that HTTP method is uppercased correctly"""
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="post",  # lowercase
            url="https://example.com/api",
            json_data={"key": "value"},
        )

    # Verify method was uppercased
    assert captured_args[0] == "POST"


@pytest.mark.asyncio
async def test_aiohttp_request_get_method_no_body_sent() -> None:
    """Test that GET requests do not send body data"""
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"method": "GET"})
    mock_response.text = AsyncMock(return_value='{"method": "GET"}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="GET",
            url="https://example.com/api",
            data={"should": "not_be_sent"},
        )

    # Verify no body data was sent for GET request
    assert "json" not in captured_request_kwargs
    assert "data" not in captured_request_kwargs
    assert captured_args[0] == "GET"


@pytest.mark.asyncio
async def test_aiohttp_request_priority_json_data_over_data() -> None:
    """Test parameter priority: json_data takes precedence over data"""
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"result": "json_data_used"})
    mock_response.text = AsyncMock(return_value='{"result": "json_data_used"}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="POST",
            url="https://example.com/api",
            json_data={"priority": "json"},
            data={"should": "not_be_used"},
        )

    # Verify only json parameter was used
    assert "json" in captured_request_kwargs
    assert captured_request_kwargs["json"] == {"priority": "json"}
    assert "data" not in captured_request_kwargs


@pytest.mark.asyncio
async def test_aiohttp_request_response_json_decoding() -> None:
    """Test decoding JSON response (returns dict)"""
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json; charset=utf-8"}
    mock_response.json = AsyncMock(return_value={"users": [{"id": 1, "name": "Alice"}]})
    mock_response.text = AsyncMock(return_value='{"users": [{"id": 1, "name": "Alice"}]}')

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        status, headers, body = await aiohttp_request(
            method="GET",
            url="https://example.com/api/users",
        )

    assert status == 200
    assert isinstance(body, dict)
    assert "users" in body
    assert len(body["users"]) == 1
    assert body["users"][0]["id"] == 1
    assert body["users"][0]["name"] == "Alice"


@pytest.mark.asyncio
async def test_aiohttp_request_response_text_decoding() -> None:
    """Test decoding non-JSON response (returns str)"""
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "text/html; charset=utf-8"}
    # Simulate JSON parsing failure, return text
    mock_response.json = AsyncMock(
        side_effect=aiohttp.ContentTypeError(
            request_info=MagicMock(),
            history=(),
            message="Attempt to decode JSON with unexpected mimetype: text/html",
        )
    )
    mock_response.text = AsyncMock(return_value="<html><body>Hello World</body></html>")

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        status, headers, body = await aiohttp_request(
            method="GET",
            url="https://example.com/page",
        )

    assert status == 200
    assert isinstance(body, str)
    assert body == "<html><body>Hello World</body></html>"


@pytest.mark.asyncio
async def test_aiohttp_request_response_headers_dict() -> None:
    """Test that response headers are correctly converted to dict"""
    mock_response = AsyncMock()
    mock_response.status = 201
    mock_response.headers = {
        "Content-Type": "application/json",
        "X-Custom-Header": "custom-value",
        "Set-Cookie": "session=abc123",
    }
    mock_response.json = AsyncMock(return_value={"created": True})
    mock_response.text = AsyncMock(return_value='{"created": true}')

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        status, headers, body = await aiohttp_request(
            method="POST",
            url="https://example.com/api",
        )

    assert status == 201
    assert isinstance(headers, dict)
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Custom-Header"] == "custom-value"
    assert headers["Set-Cookie"] == "session=abc123"


@pytest.mark.asyncio
async def test_aiohttp_request_follow_redirects() -> None:
    """Test that follow_redirects parameter is passed correctly"""
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="GET",
            url="https://example.com/api",
            follow_redirects=False,
        )

    # Verify allow_redirects parameter was set correctly
    assert captured_request_kwargs["allow_redirects"] is False


@pytest.mark.asyncio
async def test_aiohttp_request_proxy_passed_correctly() -> None:
    """Test that proxy parameter is passed correctly"""
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    proxy_url = "http://proxy.example.com:8080"

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="GET",
            url="https://example.com/api",
            proxy=proxy_url,
        )

    # Verify proxy was passed correctly
    assert captured_request_kwargs["proxy"] == proxy_url


@pytest.mark.asyncio
async def test_aiohttp_request_with_files_uses_multipart() -> None:
    """Test that files parameter sends data as multipart/form-data"""
    import os
    import tempfile

    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    # Create a temporary file for testing
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as tmp_file:
        tmp_file.write("test file content")
        tmp_file_path = tmp_file.name

    try:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.json = AsyncMock(return_value={"success": True})
        mock_response.text = AsyncMock(return_value='{"success": true}')
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        # type: ignore[no-untyped-def]
        def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
            captured_args.extend(args)
            captured_request_kwargs.update(kwargs)
            return mock_response

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.request = MagicMock(side_effect=capture_request)

        with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
            await aiohttp_request(
                method="POST",
                url="https://example.com/api/upload",
                files={"file": tmp_file_path},
            )

        # Verify data parameter was used (should be FormData for multipart)
        assert "data" in captured_request_kwargs
        # FormData object should be passed
        assert isinstance(captured_request_kwargs["data"], aiohttp.FormData)
        # Verify Content-Type header was removed (aiohttp will set it for multipart)
        assert "Content-Type" not in captured_request_kwargs["headers"]
        assert "content-type" not in captured_request_kwargs["headers"]
    finally:
        # Clean up temporary file
        if os.path.exists(tmp_file_path):
            os.unlink(tmp_file_path)


@pytest.mark.asyncio
async def test_aiohttp_request_with_files_and_data_combines_in_multipart() -> None:
    """Test that files and data can be combined in multipart/form-data"""
    import os
    import tempfile

    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    # Create a temporary file for testing
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as tmp_file:
        tmp_file.write("test file content")
        tmp_file_path = tmp_file.name

    try:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.json = AsyncMock(return_value={"success": True})
        mock_response.text = AsyncMock(return_value='{"success": true}')
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        # type: ignore[no-untyped-def]
        def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
            captured_args.extend(args)
            captured_request_kwargs.update(kwargs)
            return mock_response

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.request = MagicMock(side_effect=capture_request)

        with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
            await aiohttp_request(
                method="POST",
                url="https://example.com/api/upload",
                data={"field1": "value1", "field2": "value2"},
                files={"file": tmp_file_path},
            )

        # Verify data parameter was used (should be FormData for multipart)
        assert "data" in captured_request_kwargs
        assert isinstance(captured_request_kwargs["data"], aiohttp.FormData)
        # Verify Content-Type header was removed
        assert "Content-Type" not in captured_request_kwargs["headers"]
    finally:
        # Clean up temporary file
        if os.path.exists(tmp_file_path):
            os.unlink(tmp_file_path)


@pytest.mark.asyncio
async def test_aiohttp_request_with_files_raises_file_not_found() -> None:
    """Test that files parameter raises FileNotFoundError for non-existent files"""
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(return_value=mock_response)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(FileNotFoundError, match="File not found"):
            await aiohttp_request(
                method="POST",
                url="https://example.com/api/upload",
                files={"file": "/nonexistent/path/to/file.txt"},
            )


@pytest.mark.asyncio
async def test_aiohttp_request_with_multiple_files() -> None:
    """Test that multiple files can be uploaded in a single request"""
    import os
    import tempfile

    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    # Create temporary files for testing
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as tmp_file1:
        tmp_file1.write("file 1 content")
        tmp_file_path1 = tmp_file1.name

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".pdf") as tmp_file2:
        tmp_file2.write("file 2 content")
        tmp_file_path2 = tmp_file2.name

    try:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.json = AsyncMock(return_value={"success": True})
        mock_response.text = AsyncMock(return_value='{"success": true}')
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        # type: ignore[no-untyped-def]
        def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
            captured_args.extend(args)
            captured_request_kwargs.update(kwargs)
            return mock_response

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.request = MagicMock(side_effect=capture_request)

        with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
            await aiohttp_request(
                method="POST",
                url="https://example.com/api/upload",
                files={"document": tmp_file_path1, "attachment": tmp_file_path2},
            )

        # Verify data parameter was used (should be FormData for multipart)
        assert "data" in captured_request_kwargs
        assert isinstance(captured_request_kwargs["data"], aiohttp.FormData)
    finally:
        # Clean up temporary files
        for file_path in [tmp_file_path1, tmp_file_path2]:
            if os.path.exists(file_path):
                os.unlink(file_path)


@pytest.mark.asyncio
async def test_aiohttp_request_files_priority_over_data() -> None:
    """Test that files parameter takes priority and uses multipart even if data is provided"""
    import os
    import tempfile

    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    # Create a temporary file for testing
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as tmp_file:
        tmp_file.write("test file content")
        tmp_file_path = tmp_file.name

    try:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.json = AsyncMock(return_value={"success": True})
        mock_response.text = AsyncMock(return_value='{"success": true}')
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        # type: ignore[no-untyped-def]
        def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
            captured_args.extend(args)
            captured_request_kwargs.update(kwargs)
            return mock_response

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.request = MagicMock(side_effect=capture_request)

        with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
            await aiohttp_request(
                method="POST",
                url="https://example.com/api/upload",
                headers={"Content-Type": "application/json"},
                data={"key": "value"},
                files={"file": tmp_file_path},
            )

        # Verify data parameter was used (should be FormData for multipart)
        assert "data" in captured_request_kwargs
        assert isinstance(captured_request_kwargs["data"], aiohttp.FormData)
        # Verify json parameter was NOT used (files take priority)
        assert "json" not in captured_request_kwargs
        # Verify Content-Type header was removed (aiohttp will set it for multipart)
        assert "Content-Type" not in captured_request_kwargs["headers"]
    finally:
        # Clean up temporary file
        if os.path.exists(tmp_file_path):
            os.unlink(tmp_file_path)


@pytest.mark.asyncio
async def test_aiohttp_request_data_dict_with_non_json_content_type_uses_data() -> None:
    """Test that data (dict) with non-JSON Content-Type uses form encoding, not JSON"""
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="POST",
            url="https://example.com/api",
            headers={"Content-Type": "application/xml"},
            data={"key": "value", "number": 42},
        )

    # Verify data parameter was used (not json), even though data is a dict
    # This ensures form encoding is used, which might cause server decoding issues
    # if server expects JSON but receives form-encoded data
    assert "data" in captured_request_kwargs
    assert captured_request_kwargs["data"] == {"key": "value", "number": 42}
    assert "json" not in captured_request_kwargs
    assert captured_request_kwargs["headers"]["Content-Type"] == "application/xml"


@pytest.mark.asyncio
async def test_aiohttp_request_data_string_with_json_content_type_uses_json() -> None:
    """Test that data (string) with application/json Content-Type uses json parameter

    Note: This might cause issues if the string is not valid JSON, as aiohttp's
    json parameter expects serializable objects, not strings.
    """
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="POST",
            url="https://example.com/api",
            headers={"Content-Type": "application/json"},
            data='{"raw": "json_string"}',  # String, not dict
        )

    # Verify json parameter was used (Content-Type is application/json)
    # This might cause issues because aiohttp's json parameter expects
    # serializable objects, not strings
    assert "json" in captured_request_kwargs
    assert captured_request_kwargs["json"] == '{"raw": "json_string"}'
    assert "data" not in captured_request_kwargs


@pytest.mark.asyncio
async def test_aiohttp_request_data_string_with_form_content_type_uses_data() -> None:
    """Test that data (string) with form-urlencoded Content-Type uses data parameter

    This scenario might cause server decoding issues if the server expects
    form-encoded key-value pairs but receives a raw string.
    """
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="POST",
            url="https://example.com/api",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data='{"raw": "json_string"}',  # String, not dict
        )

    # Verify data parameter was used (form-urlencoded Content-Type)
    # This might cause server decoding issues if server expects form-encoded
    # key-value pairs but receives a raw JSON string
    assert "data" in captured_request_kwargs
    assert captured_request_kwargs["data"] == '{"raw": "json_string"}'
    assert "json" not in captured_request_kwargs
    assert captured_request_kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


@pytest.mark.asyncio
async def test_aiohttp_request_data_dict_with_text_content_type_uses_data() -> None:
    """Test that data (dict) with text/plain Content-Type uses form encoding

    This scenario might cause server decoding issues if the server expects
    plain text but receives form-encoded data.
    """
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="POST",
            url="https://example.com/api",
            headers={"Content-Type": "text/plain"},
            data={"key": "value", "number": 42},
        )

    # Verify data parameter was used (not json), even though data is a dict
    # This might cause server decoding issues if server expects plain text
    # but receives form-encoded data
    assert "data" in captured_request_kwargs
    assert captured_request_kwargs["data"] == {"key": "value", "number": 42}
    assert "json" not in captured_request_kwargs
    assert captured_request_kwargs["headers"]["Content-Type"] == "text/plain"


@pytest.mark.asyncio
async def test_aiohttp_request_data_list_with_json_content_type_uses_json() -> None:
    """Test that data (list) with application/json Content-Type uses json parameter"""
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="POST",
            url="https://example.com/api",
            headers={"Content-Type": "application/json"},
            data=[{"item": 1}, {"item": 2}],  # List, not dict
        )

    # Verify json parameter was used (Content-Type is application/json)
    assert "json" in captured_request_kwargs
    assert captured_request_kwargs["json"] == [{"item": 1}, {"item": 2}]
    assert "data" not in captured_request_kwargs


@pytest.mark.asyncio
async def test_aiohttp_request_data_list_with_form_content_type_uses_data() -> None:
    """Test that data (list) with form-urlencoded Content-Type uses data parameter

    This scenario might cause server decoding issues if the server expects
    form-encoded key-value pairs but receives a list.
    """
    captured_args: list[Any] = []
    captured_request_kwargs: dict[str, Any] = {}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json = AsyncMock(return_value={"success": True})
    mock_response.text = AsyncMock(return_value='{"success": true}')
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    # type: ignore[no-untyped-def]
    def capture_request(*args: Any, **kwargs: Any) -> AsyncMock:
        captured_args.extend(args)
        captured_request_kwargs.update(kwargs)
        return mock_response

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.request = MagicMock(side_effect=capture_request)

    with patch("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", return_value=mock_session):
        await aiohttp_request(
            method="POST",
            url="https://example.com/api",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=[{"item": 1}, {"item": 2}],  # List, not dict
        )

    # Verify data parameter was used (form-urlencoded Content-Type)
    # This might cause server decoding issues if server expects form-encoded
    # key-value pairs but receives a list
    assert "data" in captured_request_kwargs
    assert captured_request_kwargs["data"] == [{"item": 1}, {"item": 2}]
    assert "json" not in captured_request_kwargs
    assert captured_request_kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
