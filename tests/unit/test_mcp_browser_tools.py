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


def _click_page(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    click = AsyncMock(return_value="#resolved")
    page = SimpleNamespace(click=click)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    return click


# Default (resilient) keeps the shared MCP surface unchanged for external callers: a selector
# tries first, then dismisses overlays and AI-falls-back. selector_mode="direct" (which the
# copilot binds via forced_args) opts into deterministic, no-AI behavior.
@pytest.mark.asyncio
async def test_skyvern_click_selector_is_resilient_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    click = _click_page(monkeypatch)

    result = await mcp_browser.skyvern_click(selector="#submit")

    assert result["ok"] is True
    assert click.await_args.kwargs.get("mode") != "direct"


@pytest.mark.asyncio
async def test_skyvern_click_selector_mode_direct_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    click = _click_page(monkeypatch)

    result = await mcp_browser.skyvern_click(selector="#submit", selector_mode="direct")

    assert result["ok"] is True
    assert click.await_args.kwargs["mode"] == "direct"
    assert "prompt" not in click.await_args.kwargs and "ai" not in click.await_args.kwargs


@pytest.mark.asyncio
async def test_skyvern_click_selector_plus_intent_falls_back_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    click = _click_page(monkeypatch)

    result = await mcp_browser.skyvern_click(selector="#submit", intent="the blue submit button")

    assert result["ok"] is True
    assert click.await_args.kwargs.get("ai") == "fallback"
    assert click.await_args.kwargs.get("mode") != "direct"


@pytest.mark.asyncio
async def test_skyvern_click_selector_mode_direct_ignores_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    click = _click_page(monkeypatch)

    result = await mcp_browser.skyvern_click(
        selector="#submit", intent="the blue submit button", selector_mode="direct"
    )

    assert result["ok"] is True
    assert click.await_args.kwargs["mode"] == "direct"
    assert "prompt" not in click.await_args.kwargs


@pytest.mark.asyncio
async def test_skyvern_click_intent_only_uses_proactive_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    click = _click_page(monkeypatch)

    result = await mcp_browser.skyvern_click(intent="the blue submit button")

    assert result["ok"] is True
    assert click.await_args.kwargs["ai"] == "proactive"
    assert click.await_args.kwargs.get("mode") != "direct"


def _action_page(monkeypatch: pytest.MonkeyPatch, **methods: AsyncMock) -> None:
    page = SimpleNamespace(**methods)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))


@pytest.mark.asyncio
async def test_skyvern_type_selector_is_resilient_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    fill = AsyncMock(return_value="Noor")
    _action_page(monkeypatch, fill=fill)

    result = await mcp_browser.skyvern_type(selector="#first_name", text="Noor")

    assert result["ok"] is True
    assert fill.await_args.kwargs.get("mode") != "direct"


@pytest.mark.asyncio
async def test_skyvern_type_selector_mode_direct_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    fill = AsyncMock(return_value="Noor")
    _action_page(monkeypatch, fill=fill)

    result = await mcp_browser.skyvern_type(selector="#first_name", text="Noor", selector_mode="direct")

    assert result["ok"] is True
    assert fill.await_args.kwargs["mode"] == "direct"
    assert "prompt" not in fill.await_args.kwargs and "ai" not in fill.await_args.kwargs


@pytest.mark.asyncio
async def test_skyvern_type_selector_plus_intent_falls_back_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    fill = AsyncMock(return_value="Noor")
    _action_page(monkeypatch, fill=fill)

    result = await mcp_browser.skyvern_type(selector="#first_name", text="Noor", intent="the first name field")

    assert result["ok"] is True
    assert fill.await_args.kwargs.get("ai") == "fallback"
    assert fill.await_args.kwargs.get("mode") != "direct"


@pytest.mark.asyncio
async def test_skyvern_type_intent_only_uses_proactive_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    fill = AsyncMock(return_value="Noor")
    _action_page(monkeypatch, fill=fill)

    result = await mcp_browser.skyvern_type(intent="the first name field", text="Noor")

    assert result["ok"] is True
    assert fill.await_args.kwargs["ai"] == "proactive"
    assert fill.await_args.kwargs.get("mode") != "direct"


@pytest.mark.asyncio
async def test_skyvern_select_option_selector_is_resilient_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    select = AsyncMock(return_value="us")
    _action_page(monkeypatch, select_option=select)

    result = await mcp_browser.skyvern_select_option(selector="#country", value="us")

    assert result["ok"] is True
    assert "ai" not in select.await_args.kwargs


@pytest.mark.asyncio
async def test_skyvern_select_option_selector_mode_direct_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    select = AsyncMock(return_value="us")
    _action_page(monkeypatch, select_option=select)

    result = await mcp_browser.skyvern_select_option(selector="#country", value="us", selector_mode="direct")

    assert result["ok"] is True
    assert select.await_args.kwargs["ai"] is None


@pytest.mark.asyncio
async def test_skyvern_select_option_intent_only_uses_proactive_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    select = AsyncMock(return_value="us")
    _action_page(monkeypatch, select_option=select)

    result = await mcp_browser.skyvern_select_option(intent="the country dropdown", value="United States")

    assert result["ok"] is True
    assert select.await_args.kwargs["ai"] == "proactive"


# A blank/whitespace selector (how MCP clients serialize an omitted optional arg) must be
# treated as "no selector" so it uses the intent's AI path, not a deterministic action on "".
@pytest.mark.asyncio
async def test_skyvern_click_blank_selector_with_intent_uses_proactive_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    click = _click_page(monkeypatch)

    result = await mcp_browser.skyvern_click(selector="   ", intent="the blue submit button")

    assert result["ok"] is True
    assert click.await_args.kwargs["ai"] == "proactive"
    assert click.await_args.kwargs.get("mode") != "direct"


@pytest.mark.asyncio
async def test_skyvern_type_blank_selector_with_intent_uses_proactive_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    fill = AsyncMock(return_value="Noor")
    _action_page(monkeypatch, fill=fill)

    result = await mcp_browser.skyvern_type(selector="", text="Noor", intent="the first name field")

    assert result["ok"] is True
    assert fill.await_args.kwargs["ai"] == "proactive"
    assert fill.await_args.kwargs.get("mode") != "direct"


@pytest.mark.asyncio
async def test_skyvern_select_option_blank_selector_with_intent_uses_proactive_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    select = AsyncMock(return_value="us")
    _action_page(monkeypatch, select_option=select)

    result = await mcp_browser.skyvern_select_option(selector="", intent="the country dropdown", value="US")

    assert result["ok"] is True
    assert select.await_args.kwargs["ai"] == "proactive"
