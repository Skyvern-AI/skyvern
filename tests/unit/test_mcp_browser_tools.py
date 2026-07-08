"""Tests for MCP browser tool preflight validation behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import browser as mcp_browser
from skyvern.cli.mcp_tools import mcp
from skyvern.client.errors import InternalServerError, UnprocessableEntityError


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


@pytest.mark.asyncio
async def test_browser_tool_targeting_schema_prefers_direct_params() -> None:
    tools_by_name = {tool.name: tool for tool in await mcp.list_tools()}
    expectations = {
        "skyvern_click": (("selector",), ("intent",)),
        "skyvern_drag": (("source_selector", "target_selector"), ("source_intent", "target_intent")),
        "skyvern_file_upload": (("selector",), ("intent",)),
        "skyvern_hover": (("selector",), ("intent",)),
        "skyvern_type": (("selector",), ("intent",)),
        "skyvern_screenshot": (("selector",), ()),
        "skyvern_scroll": (("selector",), ("intent",)),
        "skyvern_select_option": (("selector",), ("intent",)),
        "skyvern_press_key": (("selector",), ("intent",)),
        "skyvern_wait": (("selector",), ("intent",)),
        "skyvern_observe": (("selector",), ()),
        "skyvern_frame_switch": (("selector",), ()),
        "skyvern_execute": (("steps",), ()),
        "skyvern_get_html": (("selector",), ()),
        "skyvern_get_value": (("selector",), ()),
        "skyvern_get_styles": (("selector",), ()),
    }

    for tool_name, (direct_params, intent_params) in expectations.items():
        tool = tools_by_name[tool_name]
        properties = tool.parameters["properties"]
        ordered_property_names = list(properties)

        for direct_param in direct_params:
            assert mcp_browser.DIRECT_TARGET_DESCRIPTION in properties[direct_param]["description"]
            assert ordered_property_names.index(direct_param) < ordered_property_names.index("session_id")
            for intent_param in intent_params:
                assert ordered_property_names.index(direct_param) < ordered_property_names.index(intent_param)

        for intent_param in intent_params:
            assert properties[intent_param]["description"] == mcp_browser.AI_FALLBACK_DESCRIPTION


@pytest.mark.asyncio
async def test_run_task_schema_description_demotes_autonomous_trial() -> None:
    tools_by_name = {tool.name: tool for tool in await mcp.list_tools()}
    description = tools_by_name["skyvern_run_task"].description or ""

    assert "one-off autonomous trial" in description
    assert "highest-cost AI path" in description
    assert "Not for production or reusable automations" in description
    assert "Prefer direct tools" in description
    assert "selector/ref" in description
    assert "skyvern_observe + skyvern_execute" in description


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
async def test_skyvern_click_selector_plus_intent_runs_native_option_precheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, select_option = _native_option_page(monkeypatch)

    result = await mcp_browser.skyvern_click(selector="#region > option:nth-of-type(4)", intent="pick the region")

    assert result["ok"] is True
    page.click.assert_not_awaited()
    select_option.assert_awaited_once_with(value="east", timeout=30000)
    assert result["data"]["selected_option"]["select_selector"] == "#region"


@pytest.mark.asyncio
async def test_skyvern_click_selector_plus_intent_probe_miss_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    click = _click_page(monkeypatch)
    probe = AsyncMock(return_value=None)
    monkeypatch.setattr(mcp_browser, "select_native_option_if_targeted", probe)

    result = await mcp_browser.skyvern_click(selector="#possibly-stale", intent="the blue submit button")

    assert result["ok"] is True
    probe.assert_awaited_once()
    assert click.await_args.kwargs.get("ai") == "fallback"


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


def _native_option_page(
    monkeypatch: pytest.MonkeyPatch,
    *,
    option_selector: str = "#region > option:nth-of-type(4)",
    option_info: dict[str, str] | None = None,
    select_side_effect: object | None = None,
) -> tuple[SimpleNamespace, AsyncMock]:
    option_info = option_info or {
        "select_selector": "#region",
        "value": "east",
        "label": "East",
    }
    option_locator = SimpleNamespace(evaluate=AsyncMock(return_value=option_info))
    option_locator.first = option_locator
    select_option = AsyncMock(side_effect=select_side_effect)
    select_locator = SimpleNamespace(select_option=select_option)
    raw_page = SimpleNamespace(
        locator=MagicMock(
            side_effect=lambda selector: option_locator if selector == option_selector else select_locator
        )
    )
    page = SimpleNamespace(page=raw_page, click=AsyncMock(return_value="#unexpected-click"))
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    return page, select_option


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
async def test_skyvern_click_native_option_selector_uses_parent_select(monkeypatch: pytest.MonkeyPatch) -> None:
    page, select_option = _native_option_page(monkeypatch)

    result = await mcp_browser.skyvern_click(selector="#region > option:nth-of-type(4)")

    assert result["ok"] is True
    page.click.assert_not_awaited()
    select_option.assert_awaited_once_with(value="east", timeout=30000)
    assert result["data"]["resolved_selector"] == "#region"
    assert result["data"]["selected_option"] == {
        "select_selector": "#region",
        "selected_by": "value",
        "value": "east",
        "label": "East",
    }
    assert result["data"]["sdk_equivalent"] == 'await page.select_option("#region", value="east")'


@pytest.mark.asyncio
async def test_skyvern_click_native_option_selector_can_fall_back_to_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, select_option = _native_option_page(monkeypatch, select_side_effect=[Exception("bad value"), None])

    result = await mcp_browser.skyvern_click(selector="#region > option:nth-of-type(4)", timeout=5000)

    assert result["ok"] is True
    page.click.assert_not_awaited()
    assert select_option.await_args_list[0].kwargs == {"value": "east", "timeout": 5000}
    assert select_option.await_args_list[1].kwargs == {"label": "East", "timeout": 5000}
    assert result["data"]["selected_option"]["selected_by"] == "label"
    assert result["data"]["sdk_equivalent"] == 'await page.select_option("#region", label="East")'


@pytest.mark.asyncio
async def test_skyvern_click_native_option_selector_selects_by_index(monkeypatch: pytest.MonkeyPatch) -> None:
    # The ref identifies ONE specific <option>; a duplicate/empty value must not resolve to the
    # wrong option. Selection must use the probe's index, not the value.
    page, select_option = _native_option_page(
        monkeypatch,
        option_info={"select_selector": "#region", "index": 3, "value": "", "label": "East"},
    )

    result = await mcp_browser.skyvern_click(selector="#region > option:nth-of-type(4)")

    assert result["ok"] is True
    page.click.assert_not_awaited()
    select_option.assert_awaited_once_with(index=3, timeout=30000)
    assert result["data"]["selected_option"]["selected_by"] == "index"
    assert result["data"]["selected_option"]["index"] == 3
    assert result["data"]["sdk_equivalent"] == 'await page.select_option("#region", index=3)'


@pytest.mark.asyncio
async def test_skyvern_type_ai_error_surfaces_http_status_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # 4xx bodies are the API's intended client-facing feedback — surfaced for self-correction.
    fill = AsyncMock(
        side_effect=UnprocessableEntityError(
            body={"detail": "Element locator did not match any element"},
            headers={"authorization": "redacted"},
        )
    )
    _action_page(monkeypatch, fill=fill)

    result = await mcp_browser.skyvern_type(intent="the first name field", text="Noor")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.AI_FALLBACK_FAILED
    assert result["error"]["message"] == "HTTP 422: Element locator did not match any element"
    assert "headers" not in result["error"]["message"].lower()
    assert "authorization" not in result["error"]["message"].lower()
    assert result["error"]["details"] == {
        "exception_type": "UnprocessableEntityError",
        "status_code": 422,
    }


@pytest.mark.asyncio
async def test_skyvern_type_ai_error_5xx_body_text_is_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    # 5xx bodies carry the backend's wrapped internal exception text
    # ("Unexpected error: {exception}"); only status + exception type may surface.
    fill = AsyncMock(side_effect=InternalServerError(body={"error": "Unexpected error: KeyError('internal-host')"}))
    _action_page(monkeypatch, fill=fill)

    result = await mcp_browser.skyvern_type(intent="the first name field", text="Noor")

    assert result["ok"] is False
    assert result["error"]["message"] == "HTTP 500: InternalServerError"
    assert "internal-host" not in result["error"]["message"]
    assert "Unexpected error" not in result["error"]["message"]


@pytest.mark.asyncio
async def test_skyvern_type_ai_error_empty_body_does_not_leak_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    # An ApiError whose body carries no recognized message key must not fall back to str(exc),
    # which renders headers + raw body via the SDK ApiError.__str__. Surface only status + type.
    fill = AsyncMock(
        side_effect=InternalServerError(
            body=None,
            headers={"authorization": "Bearer sk-SECRET", "set-cookie": "sess=abc"},
        )
    )
    _action_page(monkeypatch, fill=fill)

    result = await mcp_browser.skyvern_type(intent="the first name field", text="Noor")

    assert result["ok"] is False
    message = result["error"]["message"]
    assert message == "HTTP 500: InternalServerError"
    for leaked in ("authorization", "bearer", "sk-secret", "set-cookie", "sess=abc", "headers"):
        assert leaked not in message.lower()


@pytest.mark.asyncio
async def test_skyvern_type_ai_error_string_body_does_not_leak_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # A raw string body (not a recognized dict shape) can carry tokens/secrets; it must never
    # be surfaced verbatim — only status + exception type.
    fill = AsyncMock(side_effect=InternalServerError(body="upstream said: token=sk-LEAKED-1234 is invalid"))
    _action_page(monkeypatch, fill=fill)

    result = await mcp_browser.skyvern_type(intent="the first name field", text="Noor")

    assert result["ok"] is False
    message = result["error"]["message"]
    assert message == "HTTP 500: InternalServerError"
    assert "sk-LEAKED-1234" not in message
    assert "token=" not in message


@pytest.mark.asyncio
async def test_skyvern_act_sdk_error_does_not_leak_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    # The SDK/agent handlers (act/extract/validate/run_task/login) must also route ApiError
    # through _exception_message, not str(e) — otherwise ApiError.__str__ leaks headers/body.
    _action_page(monkeypatch)
    monkeypatch.setattr(
        mcp_browser,
        "do_act",
        AsyncMock(side_effect=InternalServerError(body=None, headers={"authorization": "Bearer sk-SECRET"})),
    )

    result = await mcp_browser.skyvern_act(prompt="close the banner")

    assert result["ok"] is False
    message = result["error"]["message"]
    assert message == "HTTP 500: InternalServerError"
    for leaked in ("authorization", "bearer", "sk-secret", "headers"):
        assert leaked not in message.lower()


@pytest.mark.asyncio
async def test_skyvern_type_ai_error_uses_exception_type_for_empty_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SilentFailure(Exception):
        def __str__(self) -> str:
            return ""

    fill = AsyncMock(side_effect=SilentFailure())
    _action_page(monkeypatch, fill=fill)

    result = await mcp_browser.skyvern_type(intent="the first name field", text="Noor")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.AI_FALLBACK_FAILED
    assert result["error"]["message"] == "SilentFailure"
    assert result["error"]["details"] == {"exception_type": "SilentFailure"}


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
