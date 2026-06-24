"""Tests for skyvern.cli.core shared modules (guards, browser_ops, session_ops)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core.browser_ops import do_act, do_extract, do_navigate, do_screenshot, parse_extract_schema
from skyvern.cli.core.guards import (
    GuardError,
    check_js_password,
    check_password_prompt,
    resolve_ai_mode,
    validate_button,
    validate_wait_until,
)
from skyvern.cli.core.session_ops import do_session_close, do_session_create, do_session_list
from skyvern.client.types.extensions import Extensions

CAPTCHA_SOLVER_EXTENSION: Extensions = "captcha-solver"

# ---------------------------------------------------------------------------
# guards.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "enter your password",
        "use credential to login",
        "type the secret",
        "enter passphrase",
        "enter passcode",
        "enter your pin code",
        "type pwd here",
        "enter passwd",
    ],
)
def test_password_guard_blocks_sensitive_text(text: str) -> None:
    with pytest.raises(GuardError) as exc_info:
        check_password_prompt(text)
    assert exc_info.value.hint  # hint should always be populated


@pytest.mark.parametrize("text", ["click the submit button", "fill in the email field", ""])
def test_password_guard_allows_normal_text(text: str) -> None:
    check_password_prompt(text)  # should not raise


def test_js_password_guard() -> None:
    with pytest.raises(GuardError):
        check_js_password('input[type=password].value = "secret"')
    with pytest.raises(GuardError):
        check_js_password('.type === "password"; el.value = "x"')
    check_js_password("document.title")  # allowed


@pytest.mark.parametrize("value", ["load", "domcontentloaded", "networkidle", "commit", None])
def test_wait_until_accepts_valid(value: str | None) -> None:
    validate_wait_until(value)


def test_wait_until_rejects_invalid() -> None:
    with pytest.raises(GuardError, match="Invalid wait_until"):
        validate_wait_until("badvalue")


@pytest.mark.parametrize("value", ["left", "right", "middle", None])
def test_button_accepts_valid(value: str | None) -> None:
    validate_button(value)


def test_button_rejects_invalid() -> None:
    with pytest.raises(GuardError, match="Invalid button"):
        validate_button("double")


@pytest.mark.parametrize(
    "selector,intent,expected",
    [
        (None, "click it", ("proactive", None)),
        ("#btn", "click it", ("fallback", None)),
        ("#btn", None, (None, None)),
        (None, None, (None, "INVALID_INPUT")),
    ],
)
def test_resolve_ai_mode(selector: str | None, intent: str | None, expected: tuple) -> None:
    assert resolve_ai_mode(selector, intent) == expected


# ---------------------------------------------------------------------------
# browser_ops.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_navigate_success() -> None:
    page = MagicMock()
    page.goto = AsyncMock()
    page.url = "https://example.com/final"
    page.title = AsyncMock(return_value="Example")

    result = await do_navigate(page, "https://example.com")
    assert result.url == "https://example.com/final"
    assert result.title == "Example"


@pytest.mark.asyncio
async def test_do_navigate_passes_wait_until_through() -> None:
    page = MagicMock()
    page.goto = AsyncMock()
    page.url = "https://example.com/final"
    page.title = AsyncMock(return_value="Example")

    result = await do_navigate(page, "https://example.com", wait_until="badvalue")
    assert result.url == "https://example.com/final"
    page.goto.assert_awaited_once_with("https://example.com", timeout=30000, wait_until="badvalue")


@pytest.mark.asyncio
async def test_do_screenshot_full_page() -> None:
    page = MagicMock()
    page.screenshot = AsyncMock(return_value=b"png-data")

    result = await do_screenshot(page, full_page=True)
    assert result.data == b"png-data"
    assert result.full_page is True


@pytest.mark.asyncio
async def test_do_screenshot_with_selector() -> None:
    page = MagicMock()
    element = MagicMock()
    element.screenshot = AsyncMock(return_value=b"element-data")
    page.locator.return_value = element

    result = await do_screenshot(page, selector="#header")
    assert result.data == b"element-data"


@pytest.mark.asyncio
async def test_do_act_success() -> None:
    page = MagicMock()
    page.act = AsyncMock()
    result = await do_act(page, "enter the password")
    assert result.prompt == "enter the password"
    assert result.completed is True


@pytest.mark.asyncio
async def test_do_extract_rejects_bad_schema() -> None:
    with pytest.raises(GuardError, match="Invalid JSON schema"):
        await do_extract(MagicMock(), "get data", schema="not-json")


@pytest.mark.asyncio
async def test_do_extract_success() -> None:
    page = MagicMock()
    page.extract = AsyncMock(return_value={"title": "Example"})

    result = await do_extract(page, "get the title")
    assert result.extracted == {"title": "Example"}


def test_parse_extract_schema_accepts_preparsed_dict() -> None:
    schema = {"type": "object", "properties": {"title": {"type": "string"}}}
    parsed = parse_extract_schema(schema)
    assert parsed is schema


@pytest.mark.asyncio
async def test_do_extract_accepts_preparsed_dict() -> None:
    page = MagicMock()
    page.extract = AsyncMock(return_value={"title": "Example"})
    schema = {"type": "object", "properties": {"title": {"type": "string"}}}

    result = await do_extract(page, "get the title", schema=schema)
    assert result.extracted == {"title": "Example"}
    page.extract.assert_awaited_once_with(prompt="get the title", schema=schema, skip_refresh=False)


# ---------------------------------------------------------------------------
# session_ops.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_session_create_local() -> None:
    skyvern = MagicMock()
    skyvern.launch_local_browser = AsyncMock(return_value=MagicMock())

    browser, result = await do_session_create(skyvern, local=True, headless=True)
    assert result.local is True
    assert result.session_id is None


@pytest.mark.asyncio
async def test_do_session_create_local_rejects_browser_profile_options() -> None:
    skyvern = MagicMock()
    skyvern.launch_local_browser = AsyncMock(return_value=MagicMock())

    with pytest.raises(ValueError, match="only supported for cloud sessions"):
        await do_session_create(skyvern, local=True, browser_profile_id="bp_123")
    with pytest.raises(ValueError, match="only supported for cloud sessions"):
        await do_session_create(skyvern, local=True, generate_browser_profile=True)

    skyvern.launch_local_browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_do_session_create_cloud() -> None:
    skyvern = MagicMock()
    browser_mock = MagicMock()
    browser_mock.browser_session_id = "pbs_123"
    skyvern.launch_cloud_browser = AsyncMock(return_value=browser_mock)

    browser, result = await do_session_create(skyvern, timeout=30)
    assert result.session_id == "pbs_123"
    assert result.timeout_minutes == 30


@pytest.mark.asyncio
async def test_do_session_create_cloud_forwards_extensions() -> None:
    skyvern = MagicMock()
    browser_mock = MagicMock()
    browser_mock.browser_session_id = "pbs_ext"
    skyvern.launch_cloud_browser = AsyncMock(return_value=browser_mock)

    browser, result = await do_session_create(
        skyvern,
        timeout=30,
        extensions=[CAPTCHA_SOLVER_EXTENSION],
    )

    assert browser is browser_mock
    assert result.session_id == "pbs_ext"
    skyvern.launch_cloud_browser.assert_awaited_once_with(
        timeout=30,
        proxy_location=None,
        extensions=[CAPTCHA_SOLVER_EXTENSION],
    )


@pytest.mark.asyncio
async def test_do_session_create_cloud_forwards_browser_profile_id() -> None:
    skyvern = MagicMock()
    browser_mock = MagicMock()
    browser_mock.browser_session_id = "pbs_profile"
    skyvern.launch_cloud_browser = AsyncMock(return_value=browser_mock)

    browser, result = await do_session_create(
        skyvern,
        timeout=30,
        browser_profile_id="bp_123",
    )

    assert browser is browser_mock
    assert result.session_id == "pbs_profile"
    skyvern.launch_cloud_browser.assert_awaited_once_with(
        timeout=30,
        proxy_location=None,
        browser_profile_id="bp_123",
    )


@pytest.mark.asyncio
async def test_do_session_create_cloud_enables_browser_profile_export() -> None:
    skyvern = MagicMock()
    browser_mock = MagicMock()
    browser_mock.browser_session_id = "pbs_profile"
    skyvern.launch_cloud_browser = AsyncMock(return_value=browser_mock)
    skyvern._client_wrapper.httpx_client.request = AsyncMock(
        return_value=MagicMock(status_code=200, text="", json=lambda: {})
    )

    browser, result = await do_session_create(
        skyvern,
        timeout=30,
        generate_browser_profile=True,
    )

    assert browser is browser_mock
    assert result.session_id == "pbs_profile"
    skyvern._client_wrapper.httpx_client.request.assert_awaited_once_with(
        "v1/browser_sessions/pbs_profile",
        method="PATCH",
        json={"generate_browser_profile": True},
        headers={"content-type": "application/json"},
    )


@pytest.mark.asyncio
async def test_do_session_create_cloud_rolls_back_when_browser_profile_export_patch_fails() -> None:
    skyvern = MagicMock()
    browser_mock = MagicMock()
    browser_mock.browser_session_id = "pbs_profile"
    browser_mock.close = AsyncMock()
    skyvern.launch_cloud_browser = AsyncMock(return_value=browser_mock)
    skyvern.close_browser_session = AsyncMock()
    skyvern._client_wrapper.httpx_client.request = AsyncMock(
        return_value=MagicMock(status_code=500, text="patch failed", json=lambda: {"detail": "patch failed"})
    )

    with pytest.raises(RuntimeError, match="Failed to update browser session pbs_profile"):
        await do_session_create(
            skyvern,
            timeout=30,
            generate_browser_profile=True,
        )

    skyvern.close_browser_session.assert_awaited_once_with("pbs_profile")
    browser_mock.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_do_session_create_preserves_patch_failure_when_rollback_fails() -> None:
    skyvern = MagicMock()
    browser_mock = MagicMock()
    browser_mock.browser_session_id = "pbs_profile"
    browser_mock.close = AsyncMock()
    skyvern.launch_cloud_browser = AsyncMock(return_value=browser_mock)
    skyvern.close_browser_session = AsyncMock(side_effect=RuntimeError("close failed"))
    skyvern._client_wrapper.httpx_client.request = AsyncMock(
        return_value=MagicMock(status_code=500, text="patch failed", json=lambda: {"detail": "patch failed"})
    )

    with pytest.raises(RuntimeError) as exc_info:
        await do_session_create(
            skyvern,
            timeout=30,
            generate_browser_profile=True,
        )

    assert "Failed to update browser session pbs_profile" in str(exc_info.value)
    assert "close failed" in str(exc_info.value)
    skyvern.close_browser_session.assert_awaited_once_with("pbs_profile")


@pytest.mark.asyncio
async def test_do_session_close() -> None:
    skyvern = MagicMock()
    skyvern.close_browser_session = AsyncMock()

    result = await do_session_close(skyvern, "pbs_123")
    assert result.session_id == "pbs_123"
    assert result.closed is True


@pytest.mark.asyncio
async def test_do_session_list() -> None:
    session = MagicMock()
    session.browser_session_id = "pbs_1"
    session.status = "active"
    session.started_at = None
    session.timeout = 60
    session.runnable_id = None
    session.browser_address = "ws://localhost:1234"

    skyvern = MagicMock()
    skyvern.get_browser_sessions = AsyncMock(return_value=[session])

    result = await do_session_list(skyvern)
    assert len(result) == 1
    assert result[0].session_id == "pbs_1"
    assert result[0].available is True
