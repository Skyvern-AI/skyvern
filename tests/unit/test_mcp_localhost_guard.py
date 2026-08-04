"""Tests for localhost URL detection and cloud browser guard."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import browser as mcp_browser
from skyvern.cli.mcp_tools._localhost import is_localhost_url

LOCALHOST_RECOVERY_HINT = (
    "Run `pip install skyvern && skyvern browser serve --tunnel` to bridge "
    "your local dev server to a cloud browser via ngrok. "
    "Or use `local=true` in skyvern_browser_session_create for a local browser."
)

# ---------------------------------------------------------------------------
# is_localhost_url unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:3000",
        "http://localhost:5173/some/path",
        "https://localhost:8080",
        "http://localhost",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:8000/api/v1/tasks",
        "https://127.0.0.1",
        "http://0.0.0.0:3000",
        "http://[::1]:3000",
    ],
)
def test_is_localhost_url_detects_localhost(url: str) -> None:
    assert is_localhost_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "https://app.skyvern.com",
        "http://my-localhost-app.com",
        "https://api.skyvern.com/mcp/",
        "http://192.168.1.1:3000",
        "https://10.0.0.1:8080",
    ],
)
def test_is_localhost_url_allows_non_localhost(url: str) -> None:
    assert is_localhost_url(url) is False


def test_is_localhost_url_handles_garbage_input() -> None:
    assert is_localhost_url("") is False
    assert is_localhost_url("not a url") is False


# ---------------------------------------------------------------------------
# skyvern_navigate cloud + localhost guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "kwargs"),
    [
        ("skyvern_navigate_and_screenshot", {}),
        ("skyvern_navigate_extract_and_screenshot", {"prompt": "read"}),
    ],
)
async def test_navigate_rejects_localhost_on_cloud_session(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, kwargs: dict[str, str]
) -> None:
    page = object()
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test", can_access_localhost=False)
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

    result = await getattr(mcp_browser, tool_name)(url="http://localhost:3000", **kwargs)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    assert "localhost" in result["error"]["message"].lower()
    assert result["error"]["hint"] == LOCALHOST_RECOVERY_HINT


@pytest.mark.asyncio
async def test_navigate_rejects_127_0_0_1_on_cloud_session(monkeypatch: pytest.MonkeyPatch) -> None:
    page = object()
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test", can_access_localhost=False)
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

    result = await mcp_browser.skyvern_navigate(url="http://127.0.0.1:5173/dashboard")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    assert "127.0.0.1" in result["error"]["message"]
    assert result["error"]["hint"] == LOCALHOST_RECOVERY_HINT


@pytest.mark.asyncio
async def test_navigate_allows_localhost_on_local_session(monkeypatch: pytest.MonkeyPatch) -> None:
    page = AsyncMock()
    ctx = BrowserContext(mode="local", can_access_localhost=True)
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
    monkeypatch.setattr(
        mcp_browser,
        "do_navigate",
        AsyncMock(return_value=AsyncMock(url="http://localhost:3000", title="App")),
    )

    result = await mcp_browser.skyvern_navigate(url="http://localhost:3000")

    assert result["ok"] is True


@pytest.mark.asyncio
async def test_navigate_rejects_localhost_when_context_permission_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = AsyncMock()
    ctx = BrowserContext(mode="cdp", cdp_url="ws://localhost:9222")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
    do_navigate = AsyncMock()
    monkeypatch.setattr(mcp_browser, "do_navigate", do_navigate)

    result = await mcp_browser.skyvern_navigate(url="http://localhost:3000")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    do_navigate.assert_not_awaited()


@pytest.mark.asyncio
async def test_navigate_attempts_localhost_when_cloud_session_can_access_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = AsyncMock()
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test", can_access_localhost=True)
    do_navigate = AsyncMock(return_value=AsyncMock(url="http://localhost:3000", title="App"))
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
    monkeypatch.setattr(mcp_browser, "do_navigate", do_navigate)

    result = await mcp_browser.skyvern_navigate(url="http://localhost:3000")

    assert result["ok"] is True
    do_navigate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://localhost:3000/", "http://127.0.0.1:8000/"])
async def test_navigate_allows_local_url_when_context_permits(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    page = AsyncMock()
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test", can_access_localhost=True)
    do_navigate = AsyncMock(return_value=AsyncMock(url=url, title="App"))
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
    monkeypatch.setattr(mcp_browser, "do_navigate", do_navigate)

    result = await mcp_browser.skyvern_navigate(url=url)

    assert result["ok"] is True
    do_navigate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "can_access_localhost"),
    [
        pytest.param("http://169.254.169.254/", False, id="metadata"),
        pytest.param("http://10.20.30.40/", False, id="private"),
        pytest.param("http://127.0.0.2/", False, id="alternate-loopback"),
        pytest.param("http://2130706433/", False, id="integer-loopback"),
        pytest.param("http://169.254.169.254/", True, id="metadata-local-context"),
        pytest.param("http://10.20.30.40/", True, id="private-local-context"),
    ],
)
async def test_navigate_rejects_unsafe_url_before_delegate(
    monkeypatch: pytest.MonkeyPatch, url: str, can_access_localhost: bool
) -> None:
    page = AsyncMock()
    ctx = BrowserContext(
        mode="local" if can_access_localhost else "cloud_session",
        session_id=None if can_access_localhost else "pbs_test",
        can_access_localhost=can_access_localhost,
    )
    do_navigate = AsyncMock()
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
    monkeypatch.setattr(mcp_browser, "do_navigate", do_navigate)

    result = await mcp_browser.skyvern_navigate(url=url)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    do_navigate.assert_not_awaited()


@pytest.mark.asyncio
async def test_navigate_allows_public_url_on_cloud_session(monkeypatch: pytest.MonkeyPatch) -> None:
    page = AsyncMock()
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test", can_access_localhost=False)
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
    monkeypatch.setattr(mcp_browser, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(
        mcp_browser,
        "do_navigate",
        AsyncMock(return_value=AsyncMock(url="https://example.com", title="Example")),
    )

    result = await mcp_browser.skyvern_navigate(url="https://example.com")

    assert result["ok"] is True


# ---------------------------------------------------------------------------
# skyvern_run_task cloud + localhost guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_task_rejects_localhost_on_cloud_session(monkeypatch: pytest.MonkeyPatch) -> None:
    page = object()
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test", can_access_localhost=False)
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

    result = await mcp_browser.skyvern_run_task(
        prompt="Extract the page title",
        url="http://localhost:5173",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    assert "localhost" in result["error"]["message"].lower()
    assert result["error"]["hint"] == LOCALHOST_RECOVERY_HINT


@pytest.mark.asyncio
async def test_run_task_attempts_localhost_when_cloud_session_can_access_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = AsyncMock()
    page.agent = AsyncMock()
    page.agent.run_task = AsyncMock(
        return_value=AsyncMock(
            run_id="r_1",
            status="completed",
            output=None,
            failure_reason=None,
            recording_url=None,
            app_url=None,
        )
    )
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test", can_access_localhost=True)
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

    result = await mcp_browser.skyvern_run_task(prompt="Extract the page title", url="http://localhost:5173")

    assert result["ok"] is True
    page.agent.run_task.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_task_allows_no_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_task with url=None should not trigger the localhost guard."""
    page = AsyncMock()
    page.agent = AsyncMock()
    page.agent.run_task = AsyncMock(
        return_value=AsyncMock(
            run_id="r_1",
            status="completed",
            output=None,
            failure_reason=None,
            recording_url=None,
            app_url=None,
        )
    )
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

    result = await mcp_browser.skyvern_run_task(prompt="Do something on current page")

    assert result["ok"] is True
