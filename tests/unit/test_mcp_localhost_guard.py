"""Tests for localhost URL detection and cloud browser guard."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import browser as mcp_browser
from skyvern.cli.mcp_tools._localhost import is_localhost_url

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
async def test_navigate_rejects_localhost_on_cloud_session(monkeypatch: pytest.MonkeyPatch) -> None:
    page = object()
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

    result = await mcp_browser.skyvern_navigate(url="http://localhost:3000")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    assert "localhost" in result["error"]["message"].lower()
    assert "skyvern browser serve --tunnel" in result["error"]["hint"]


@pytest.mark.asyncio
async def test_navigate_rejects_127_0_0_1_on_cloud_session(monkeypatch: pytest.MonkeyPatch) -> None:
    page = object()
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

    result = await mcp_browser.skyvern_navigate(url="http://127.0.0.1:5173/dashboard")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    assert "localhost" in result["error"]["message"].lower()


@pytest.mark.asyncio
async def test_navigate_allows_localhost_on_local_session(monkeypatch: pytest.MonkeyPatch) -> None:
    page = AsyncMock()
    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
    monkeypatch.setattr(
        mcp_browser,
        "do_navigate",
        AsyncMock(return_value=AsyncMock(url="http://localhost:3000", title="App")),
    )

    result = await mcp_browser.skyvern_navigate(url="http://localhost:3000")

    assert result["ok"] is True


@pytest.mark.asyncio
async def test_navigate_allows_localhost_on_cdp_session(monkeypatch: pytest.MonkeyPatch) -> None:
    page = AsyncMock()
    ctx = BrowserContext(mode="cdp", cdp_url="ws://localhost:9222")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
    monkeypatch.setattr(
        mcp_browser,
        "do_navigate",
        AsyncMock(return_value=AsyncMock(url="http://localhost:3000", title="App")),
    )

    result = await mcp_browser.skyvern_navigate(url="http://localhost:3000")

    assert result["ok"] is True


@pytest.mark.asyncio
async def test_navigate_allows_public_url_on_cloud_session(monkeypatch: pytest.MonkeyPatch) -> None:
    page = AsyncMock()
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
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
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

    result = await mcp_browser.skyvern_run_task(
        prompt="Extract the page title",
        url="http://localhost:5173",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    assert "localhost" in result["error"]["message"].lower()
    assert "skyvern browser serve --tunnel" in result["error"]["hint"]


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
