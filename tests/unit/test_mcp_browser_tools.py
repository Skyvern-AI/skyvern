"""Tests for MCP browser tool preflight validation behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import browser as mcp_browser


@pytest.mark.asyncio
async def test_skyvern_extract_invalid_schema_preflight_before_session(monkeypatch: pytest.MonkeyPatch) -> None:
    get_page = AsyncMock(side_effect=AssertionError("get_page should not be called for invalid schema"))
    monkeypatch.setattr(mcp_browser, "get_page", get_page)

    result = await mcp_browser.skyvern_extract(prompt="extract data", schema="{invalid")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    assert "Invalid JSON schema" in result["error"]["message"]
    get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_skyvern_extract_preparsed_schema_passed_to_core(monkeypatch: pytest.MonkeyPatch) -> None:
    page = object()
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))

    do_extract = AsyncMock(return_value=SimpleNamespace(extracted={"ok": True}))
    monkeypatch.setattr(mcp_browser, "do_extract", do_extract)

    result = await mcp_browser.skyvern_extract(prompt="extract data", schema='{"type":"object"}')

    assert result["ok"] is True
    await_args = do_extract.await_args
    assert await_args is not None
    assert isinstance(await_args.kwargs["schema"], dict)


@pytest.mark.asyncio
async def test_skyvern_navigate_invalid_wait_until_preflight_before_session(monkeypatch: pytest.MonkeyPatch) -> None:
    get_page = AsyncMock(side_effect=AssertionError("get_page should not be called for invalid wait_until"))
    monkeypatch.setattr(mcp_browser, "get_page", get_page)

    result = await mcp_browser.skyvern_navigate(url="https://example.com", wait_until="not-a-real-wait-until")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_skyvern_act_password_prompt_preflight_before_session(monkeypatch: pytest.MonkeyPatch) -> None:
    get_page = AsyncMock(side_effect=AssertionError("get_page should not be called for password prompt"))
    monkeypatch.setattr(mcp_browser, "get_page", get_page)

    result = await mcp_browser.skyvern_act(prompt="enter the password and submit")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    get_page.assert_not_awaited()


def _login_page_mock(login_side_effect: object) -> object:
    agent = SimpleNamespace(login=AsyncMock(side_effect=login_side_effect))
    return SimpleNamespace(agent=agent)


@pytest.mark.asyncio
async def test_skyvern_login_timeout_returns_timeout_code(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _login_page_mock(asyncio.TimeoutError())
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_browser.skyvern_login(
        credential_type="skyvern",
        credential_id="cred_123",
        timeout_seconds=30,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.TIMEOUT
    assert result["error"]["message"]
    assert "30" in result["error"]["message"]
    assert "dashboard" in result["error"]["hint"].lower() or "run" in result["error"]["hint"].lower()


@pytest.mark.asyncio
async def test_skyvern_login_empty_exception_message_falls_back_to_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Silent(Exception):
        pass

    page = _login_page_mock(_Silent())
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_browser.skyvern_login(
        credential_type="skyvern",
        credential_id="cred_123",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.SDK_ERROR
    assert result["error"]["message"] == "_Silent"


@pytest.mark.asyncio
async def test_skyvern_login_success_returns_run_data(monkeypatch: pytest.MonkeyPatch) -> None:
    response = SimpleNamespace(
        run_id="wr_123",
        status="completed",
        output={"ok": True},
        failure_reason=None,
        recording_url="https://example.com/rec",
        app_url="https://app.example.com/wr_123",
    )
    page = _login_page_mock(None)
    page.agent.login = AsyncMock(return_value=response)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_browser.skyvern_login(
        credential_type="skyvern",
        credential_id="cred_123",
    )

    assert result["ok"] is True
    assert result["data"]["run_id"] == "wr_123"
    assert result["data"]["status"] == "completed"


@pytest.mark.asyncio
async def test_skyvern_run_task_timeout_returns_timeout_code(monkeypatch: pytest.MonkeyPatch) -> None:
    page = SimpleNamespace(agent=SimpleNamespace(run_task=AsyncMock(side_effect=asyncio.TimeoutError())))
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_browser.skyvern_run_task(prompt="navigate and click", timeout_seconds=45)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.TIMEOUT
    assert "45" in result["error"]["message"]
