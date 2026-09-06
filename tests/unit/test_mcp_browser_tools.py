"""Tests for MCP browser tool preflight validation behavior."""

from __future__ import annotations

import ast
import asyncio
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, Mock, call

import pytest

from skyvern.browser_extension.runtime import BrowserExtensionRuntime
from skyvern.cli.core import browser_ops
from skyvern.cli.core import result as result_mod
from skyvern.cli.core import session_manager
from skyvern.cli.core.browser_ops import (
    CustomSelectClassifyError,
    CustomSelectMatchError,
    CustomSelectOpenError,
    CustomSelectPasswordError,
    NavigateResult,
    do_select_option,
)
from skyvern.cli.core.result import Artifact, BrowserContext, set_concise_responses
from skyvern.cli.mcp_tools import _element_state
from skyvern.cli.mcp_tools import browser as mcp_browser
from skyvern.cli.mcp_tools import cdp_input as mcp_cdp_input
from skyvern.cli.mcp_tools import mcp
from skyvern.client.errors import InternalServerError, UnprocessableEntityError
from skyvern.config import settings
from skyvern.constants import TEXT_PRESS_MAX_LENGTH
from skyvern.exceptions import StaleFrameSelectionError
from skyvern.forge.sdk.forge_log import codeblock_parameter_log_redaction
from tests.unit._mcp_browser_fakes import (
    StaleScopePage,
    make_mock_page,
    make_probe_locator,
    make_real_wait_for_timeout,
    make_select_like_page,
    make_select_option_page,
    make_skyvern_page,
    patch_get_page,
)


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
async def test_skyvern_login_null_recording_url_stays_stripped_in_concise_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """skyvern_browser_session_get/close preserve a null recording_url in concise mode via an
    action-scoped allowlist — unrelated tools like skyvern_login must keep stripping it."""
    response = SimpleNamespace(
        run_id="wr_124",
        status="completed",
        output={"ok": True},
        failure_reason=None,
        recording_url=None,
        app_url="https://app.example.com/wr_124",
    )
    page = _login_page_mock(None)
    page.agent.login = AsyncMock(return_value=response)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))

    set_concise_responses(True)
    try:
        result = await mcp_browser.skyvern_login(
            credential_type="skyvern",
            credential_id="cred_123",
        )
    finally:
        set_concise_responses(False)

    assert result["ok"] is True
    assert "recording_url" not in result["data"]


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
        "skyvern_click": (("selector", "x", "y"), ("intent",)),
        "skyvern_drag": (("source_selector", "target_selector"), ("source_intent", "target_intent")),
        "skyvern_file_upload": (("selector",), ("intent",)),
        "skyvern_hover": (("selector",), ("intent",)),
        "skyvern_type": (("selector", "x", "y"), ("intent",)),
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
    assert "via selector" in description
    assert "skyvern_observe + skyvern_execute" in description


def _click_page(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    click = AsyncMock(return_value="#resolved")
    page = make_skyvern_page(MagicMock())
    page.click = click
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    return click


def _direct_click_page(
    monkeypatch: pytest.MonkeyPatch,
    *,
    count: int = 1,
    visible: bool = True,
    enabled: bool = True,
    probe_side_effect: Exception | None = None,
    click_error: Exception | None = None,
) -> tuple[AsyncMock, MagicMock]:
    locator = make_probe_locator(count=count, visible=visible, enabled=enabled, side_effect=probe_side_effect)
    raw_page = MagicMock()
    raw_page.locator = MagicMock(return_value=locator)
    click = AsyncMock(side_effect=click_error, return_value="#target")
    page = SimpleNamespace(page=raw_page, locator_scope=raw_page, click=click)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    return click, locator


def _direct_type_page(
    monkeypatch: pytest.MonkeyPatch,
    *,
    count: int = 1,
    visible: bool = True,
    enabled: bool = True,
    fill_error: Exception | None = None,
) -> tuple[AsyncMock, MagicMock]:
    locator = make_probe_locator(count=count, visible=visible, enabled=enabled)
    raw_page = MagicMock()
    raw_page.locator = MagicMock(return_value=locator)
    fill = AsyncMock(side_effect=fill_error, return_value="typed")
    page = SimpleNamespace(page=raw_page, locator_scope=raw_page, evaluate=AsyncMock(return_value=False), fill=fill)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    return fill, locator


@pytest.mark.asyncio
async def test_skyvern_click_coordinates_dispatches_raw_mouse_click(monkeypatch: pytest.MonkeyPatch) -> None:
    page = make_mock_page()
    page.mouse.click = AsyncMock()
    context = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_browser, page, context)
    resolve_ai_mode = Mock(side_effect=AssertionError("coordinate clicks must not resolve AI mode"))
    select_native_option = AsyncMock(side_effect=AssertionError("coordinate clicks must not resolve selectors"))
    monkeypatch.setattr(mcp_browser, "_resolve_ai_mode", resolve_ai_mode)
    monkeypatch.setattr(mcp_browser, "select_native_option_if_targeted", select_native_option)

    result = await mcp_browser.skyvern_click(x=12.5, y=24.0, button="right", click_count=2)

    assert result["ok"] is True, result
    page.mouse.click.assert_awaited_once_with(12.5, 24.0, button="right", click_count=2)
    assert result["data"]["resolved_target"] == "coordinates (12.5, 24.0)"
    assert result["data"]["sdk_equivalent"] == ("await page.mouse.click(12.5, 24.0, button='right', click_count=2)")
    resolve_ai_mode.assert_not_called()
    select_native_option.assert_not_awaited()


@pytest.mark.asyncio
async def test_skyvern_type_coordinates_focuses_then_types(monkeypatch: pytest.MonkeyPatch) -> None:
    page = make_mock_page()
    page.mouse.click = AsyncMock()
    page.keyboard.type = AsyncMock()
    page.keyboard.press = AsyncMock()
    page.evaluate = AsyncMock(return_value=False)
    context = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_browser, page, context)

    result = await mcp_browser.skyvern_type(text="Noor", x=18.0, y=36.5, clear=False)

    assert result["ok"] is True, result
    page.mouse.click.assert_awaited_once_with(18.0, 36.5)
    page.keyboard.type.assert_awaited_once_with("Noor")
    page.keyboard.press.assert_not_awaited()
    assert result["data"]["resolved_target"] == "coordinates (18.0, 36.5)"


@pytest.mark.asyncio
async def test_skyvern_type_coordinates_refuses_password_input(monkeypatch: pytest.MonkeyPatch) -> None:
    page = make_mock_page()
    page.mouse.click = AsyncMock()
    page.keyboard.type = AsyncMock()
    page.keyboard.press = AsyncMock()
    page.evaluate = AsyncMock(return_value=True)
    context = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_browser, page, context)

    result = await mcp_browser.skyvern_type(text="not-a-secret", x=18.0, y=36.5, clear=False)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    assert "password" in result["error"]["message"].lower()
    page.keyboard.type.assert_not_awaited()


@pytest.mark.asyncio
async def test_skyvern_type_coordinates_refuses_unverifiable_target(monkeypatch: pytest.MonkeyPatch) -> None:
    page = make_mock_page()
    page.mouse.click = AsyncMock()
    page.keyboard.type = AsyncMock()
    page.keyboard.press = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    context = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_browser, page, context)

    result = await mcp_browser.skyvern_type(text="Noor", x=18.0, y=36.5, clear=False)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    assert result["error"]["message"] == browser_ops.COORDINATE_TYPE_TARGET_REFUSAL_MESSAGE
    page.keyboard.type.assert_not_awaited()


@pytest.mark.asyncio
async def test_skyvern_click_requires_both_coordinates_before_session(monkeypatch: pytest.MonkeyPatch) -> None:
    get_page = AsyncMock(side_effect=AssertionError("get_page should not be called for invalid coordinates"))
    monkeypatch.setattr(mcp_browser, "get_page", get_page)

    result = await mcp_browser.skyvern_click(x=12.5)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    assert "x" in result["error"]["message"] and "y" in result["error"]["message"]
    get_page.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("x", "y"),
    [
        pytest.param(-1.0, 0.0, id="negative"),
        pytest.param(float("inf"), 0.0, id="infinite"),
        pytest.param(float("nan"), 0.0, id="nan"),
    ],
)
async def test_skyvern_click_rejects_invalid_coordinates_before_session(
    monkeypatch: pytest.MonkeyPatch,
    x: float,
    y: float,
) -> None:
    get_page = AsyncMock(side_effect=AssertionError("get_page should not be called for invalid coordinates"))
    monkeypatch.setattr(mcp_browser, "get_page", get_page)

    result = await mcp_browser.skyvern_click(x=x, y=y)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_skyvern_type_rejects_selector_with_coordinates_before_session(monkeypatch: pytest.MonkeyPatch) -> None:
    get_page = AsyncMock(side_effect=AssertionError("get_page should not be called for conflicting targets"))
    monkeypatch.setattr(mcp_browser, "get_page", get_page)

    result = await mcp_browser.skyvern_type(text="Noor", selector="#first_name", x=18.0, y=36.5)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    assert "selector" in result["error"]["message"].lower()
    assert "coordinates" in result["error"]["message"].lower()
    get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_skyvern_execute_click_coordinates_flow_through_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    page = make_mock_page()
    page.mouse.click = AsyncMock()
    context = BrowserContext(mode="local")
    get_page = patch_get_page(monkeypatch, mcp_browser, page, context)
    get_page.return_value[0]._working_frame = None

    result = await mcp_browser.skyvern_execute(
        steps=[{"tool": "click", "params": {"x": 12.5, "y": 24.0, "button": "middle"}}]
    )

    assert result["ok"] is True, result
    assert result["data"]["steps_completed"] == 1
    page.mouse.click.assert_awaited_once_with(12.5, 24.0, button="middle", click_count=1)


@pytest.mark.asyncio
async def test_skyvern_execute_type_coordinates_flow_through_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    page = make_mock_page()
    page.mouse.click = AsyncMock()
    page.keyboard.type = AsyncMock()
    page.keyboard.press = AsyncMock()
    page.evaluate = AsyncMock(return_value=False)
    context = BrowserContext(mode="local")
    get_page = patch_get_page(monkeypatch, mcp_browser, page, context)
    get_page.return_value[0]._working_frame = None

    result = await mcp_browser.skyvern_execute(
        steps=[
            {
                "tool": "type",
                "params": {
                    "text": "Noor",
                    "x": 18.0,
                    "y": 36.5,
                    "clear_first": False,
                    "press_enter": True,
                },
            }
        ]
    )

    assert result["ok"] is True, result
    page.mouse.click.assert_awaited_once_with(18.0, 36.5)
    page.keyboard.type.assert_awaited_once_with("Noor")
    page.keyboard.press.assert_awaited_once_with("Enter")


@pytest.mark.asyncio
async def test_coordinate_actions_record_explicit_trajectory_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    page = make_mock_page()
    page.mouse.click = AsyncMock()
    page.keyboard.type = AsyncMock()
    page.keyboard.press = AsyncMock()
    page.evaluate = AsyncMock(return_value=False)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    patch_get_page(monkeypatch, mcp_browser, page, context)
    append_trajectory = Mock()
    monkeypatch.setattr(mcp_browser, "append_trajectory_entry", append_trajectory)
    monkeypatch.setattr(mcp_browser, "current_api_key_hash", lambda: None)

    click_result = await mcp_browser.skyvern_click(x=12.5, y=24.0)
    type_result = await mcp_browser.skyvern_type(text="Noor", x=18.0, y=36.5)

    assert click_result["ok"] is True, click_result
    assert type_result["ok"] is True, type_result
    click_entry, type_entry = (entry.kwargs["entry"] for entry in append_trajectory.call_args_list)
    assert "selector" not in click_entry
    assert (click_entry["x"], click_entry["y"]) == (12.5, 24.0)
    assert click_entry["sdk_equivalent"] == click_result["data"]["sdk_equivalent"]
    assert "locator" not in click_entry["sdk_equivalent"]
    assert "selector" not in type_entry
    assert (type_entry["x"], type_entry["y"]) == (18.0, 36.5)
    assert type_entry["sdk_equivalent"] == type_result["data"]["sdk_equivalent"]
    assert "locator" not in type_entry["sdk_equivalent"]
    assert "coordinates (" not in repr([click_entry, type_entry])


# Default (resilient) keeps the shared MCP surface unchanged for external callers: a selector
# tries first, then dismisses overlays and AI-falls-back. selector_mode="direct" (which the
# copilot binds via forced_args) opts into deterministic, no-AI behavior.
@pytest.mark.asyncio
async def test_skyvern_click_selector_is_resilient_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    click = _click_page(monkeypatch)

    result = await mcp_browser.skyvern_click(selector="#submit")

    assert result["ok"] is True
    assert click.await_args.kwargs.get("mode") != "direct"
    assert click.await_args.kwargs["_skip_element_prep"] is True


@pytest.mark.asyncio
async def test_skyvern_click_page_adapter_never_receives_private_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    click = AsyncMock(return_value="#resolved")
    raw_page = MagicMock()
    page = SimpleNamespace(page=raw_page, locator_scope=raw_page, click=click)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_browser.skyvern_click(selector="#submit")

    assert result["ok"] is True
    assert "_skip_element_prep" not in click.await_args.kwargs


@pytest.mark.asyncio
async def test_skyvern_click_selector_mode_direct_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    click = _click_page(monkeypatch)

    result = await mcp_browser.skyvern_click(selector="#submit", selector_mode="direct")

    assert result["ok"] is True
    assert click.await_args.kwargs["mode"] == "direct"
    assert "prompt" not in click.await_args.kwargs and "ai" not in click.await_args.kwargs
    assert "_skip_element_prep" not in click.await_args.kwargs


@pytest.mark.asyncio
async def test_skyvern_click_selector_only_uses_fast_direct_default_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    click = _click_page(monkeypatch)

    result = await mcp_browser.skyvern_click(selector="#submit")

    assert result["ok"] is True
    assert click.await_args.kwargs["timeout"] == 5000


@pytest.mark.asyncio
async def test_skyvern_click_selector_mode_direct_uses_fast_default_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    click = _click_page(monkeypatch)

    result = await mcp_browser.skyvern_click(selector="#submit", selector_mode="direct")

    assert result["ok"] is True
    assert click.await_args.kwargs["timeout"] == 5000


@pytest.mark.asyncio
async def test_skyvern_click_explicit_timeout_is_honored_on_direct_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    click = _click_page(monkeypatch)

    result = await mcp_browser.skyvern_click(selector="#submit", timeout=1234)

    assert result["ok"] is True
    assert click.await_args.kwargs["timeout"] == 1234


@pytest.mark.asyncio
async def test_skyvern_click_selector_plus_intent_keeps_30s_default_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    click = _click_page(monkeypatch)

    result = await mcp_browser.skyvern_click(selector="#submit", intent="the blue submit button")

    assert result["ok"] is True
    assert click.await_args.kwargs["timeout"] == 30000


@pytest.mark.asyncio
async def test_skyvern_click_intent_only_keeps_30s_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    click = _click_page(monkeypatch)

    result = await mcp_browser.skyvern_click(intent="the blue submit button")

    assert result["ok"] is True
    assert click.await_args.kwargs["timeout"] == 30000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env_value", "expected_timeout"),
    [
        ("2500", 2500),
        ("42", 1000),
        ("90000", 60000),
        ("not-an-int", 5000),
    ],
)
async def test_skyvern_click_direct_timeout_env_override_is_respected_and_clamped(
    monkeypatch: pytest.MonkeyPatch,
    env_value: str,
    expected_timeout: int,
) -> None:
    monkeypatch.setenv("SKYVERN_MCP_DIRECT_TIMEOUT_MS", env_value)
    click = _click_page(monkeypatch)

    result = await mcp_browser.skyvern_click(selector="#submit")

    assert result["ok"] is True
    assert click.await_args.kwargs["timeout"] == expected_timeout


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("element_state", "count", "visible", "enabled", "message", "expected_code"),
    [
        ("not_found", 0, False, False, "Timeout 5000ms exceeded.", mcp_browser.ErrorCode.SELECTOR_NOT_FOUND),
        ("hidden", 1, False, True, "Timeout 5000ms exceeded.", mcp_browser.ErrorCode.ACTION_FAILED),
        ("disabled", 1, True, False, "Timeout 5000ms exceeded.", mcp_browser.ErrorCode.ACTION_FAILED),
        (
            "occluded",
            1,
            True,
            True,
            "<div class='overlay'></div> intercepts pointer events",
            mcp_browser.ErrorCode.ACTION_FAILED,
        ),
    ],
)
async def test_skyvern_click_direct_failure_reports_element_state(
    monkeypatch: pytest.MonkeyPatch,
    element_state: str,
    count: int,
    visible: bool,
    enabled: bool,
    message: str,
    expected_code: str,
) -> None:
    click_error = mcp_browser.PlaywrightTimeoutError(message)
    click, locator = _direct_click_page(
        monkeypatch,
        count=count,
        visible=visible,
        enabled=enabled,
        click_error=click_error,
    )

    result = await mcp_browser.skyvern_click(selector="#target", selector_mode="direct")

    assert result["ok"] is False
    assert result["error"]["code"] == expected_code
    assert result["error"]["details"]["element_state"] == element_state
    assert result["error"]["hint"]
    click.assert_awaited_once()
    locator.count.assert_awaited_once()


@pytest.mark.parametrize(
    "bad_str",
    [
        pytest.param(lambda self: (_ for _ in ()).throw(RuntimeError("boom")), id="raising"),
        pytest.param(lambda self: 7, id="non_str"),
    ],
)
def test_a_hostile_dunder_str_cannot_raise_out_of_the_element_state_detail(bad_str) -> None:  # noqa: ANN001
    """Reading the message must not turn a handled actionability failure into an unhandled one."""

    class Hostile(Exception):
        __str__ = bad_str

    assert _element_state._redacted_exception_detail(Hostile()) == ""


@pytest.mark.asyncio
async def test_skyvern_click_direct_failure_returns_the_underlying_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = (
        "Locator.click: Timeout 5000ms exceeded.\n"
        'Call log:\n  - <div class="privacy-notice-veil">Accept</div> intercepts pointer events'
    )
    click, _ = _direct_click_page(
        monkeypatch,
        count=1,
        visible=True,
        enabled=True,
        click_error=mcp_browser.PlaywrightTimeoutError(message),
    )

    result = await mcp_browser.skyvern_click(selector="#target", selector_mode="direct")

    details = result["error"]["details"]
    assert "privacy-notice-veil" in details["exception_detail"]
    assert "intercepts pointer events" in details["exception_detail"]
    assert details["exception_detail"] != details["exception_type"]


@pytest.mark.asyncio
async def test_skyvern_click_direct_failure_redacts_a_secret_bearing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    click, _ = _direct_click_page(
        monkeypatch,
        count=1,
        visible=True,
        enabled=True,
        click_error=mcp_browser.PlaywrightTimeoutError(
            "Locator.click: navigating to https://example.com/callback?access_token=abcd1234efgh5678 failed"
        ),
    )

    result = await mcp_browser.skyvern_click(selector="#target", selector_mode="direct")

    assert "abcd1234efgh5678" not in result["error"]["details"]["exception_detail"]


@pytest.mark.asyncio
async def test_skyvern_click_direct_failure_masks_a_parameter_straddling_the_detail_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The codeblock redactor replaces parameter values by exact match, so bounding the text before
    # it runs would leave a cut value behind as an unmatchable fragment.
    parameter_value = "vault-secret-" + "q" * 40
    prefix = "Locator.click: Timeout 5000ms exceeded. Call log: navigating to "
    message = prefix.ljust(_element_state.ELEMENT_STATE_ERROR_DETAIL_MAX_CHARS - 30, "-") + parameter_value + " failed"
    cut = message[: _element_state.ELEMENT_STATE_ERROR_DETAIL_MAX_CHARS]
    assert parameter_value[:20] in cut and parameter_value not in cut

    click, _ = _direct_click_page(
        monkeypatch,
        count=1,
        visible=True,
        enabled=True,
        click_error=mcp_browser.PlaywrightTimeoutError(message),
    )

    with codeblock_parameter_log_redaction(lambda value: value.replace(parameter_value, "[REDACTED]")):
        result = await mcp_browser.skyvern_click(selector="#target", selector_mode="direct")

    detail = result["error"]["details"]["exception_detail"]
    assert len(detail) <= _element_state.ELEMENT_STATE_ERROR_DETAIL_MAX_CHARS
    assert "vault-secret" not in detail


@pytest.mark.asyncio
async def test_skyvern_click_direct_failure_probes_working_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    frame_locator = make_probe_locator(visible=False)
    frame = MagicMock()
    frame.locator = MagicMock(return_value=frame_locator)
    main_locator = make_probe_locator(count=0)
    raw_page = MagicMock()
    raw_page.locator = MagicMock(return_value=main_locator)
    click = AsyncMock(side_effect=mcp_browser.PlaywrightTimeoutError("Timeout 5000ms exceeded."))
    page = SimpleNamespace(page=raw_page, click=click, locator_scope=frame)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_browser.skyvern_click(selector="#in-iframe", selector_mode="direct")

    assert result["ok"] is False
    # Main frame has count=0: a main-frame probe would say not_found; hidden proves the frame was probed.
    assert result["error"]["details"]["element_state"] == "hidden"
    frame.locator.assert_called_with("#in-iframe")


@pytest.mark.asyncio
async def test_skyvern_click_direct_failure_reports_unknown_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    click, locator = _direct_click_page(
        monkeypatch,
        probe_side_effect=asyncio.TimeoutError(),
        click_error=mcp_browser.PlaywrightTimeoutError("Timeout 5000ms exceeded."),
    )

    result = await mcp_browser.skyvern_click(selector="#target", selector_mode="direct")

    assert result["ok"] is False
    assert result["error"]["details"]["element_state"] == "unknown"
    assert result["error"]["hint"]
    click.assert_awaited_once()
    locator.count.assert_awaited_once()


@pytest.mark.asyncio
async def test_skyvern_click_selector_only_failure_uses_direct_element_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _click, _locator = _direct_click_page(
        monkeypatch,
        count=0,
        visible=False,
        enabled=False,
        click_error=mcp_browser.PlaywrightTimeoutError("Timeout 5000ms exceeded."),
    )

    result = await mcp_browser.skyvern_click(selector="#target")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.SELECTOR_NOT_FOUND
    assert result["error"]["details"]["element_state"] == "not_found"
    assert result["error"]["details"]["actionability_timeout_ms"] == 5000


@pytest.mark.asyncio
async def test_skyvern_press_key_selector_only_uses_fast_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    press = AsyncMock()
    locator = MagicMock()
    locator.press = press
    page = SimpleNamespace(locator=MagicMock(return_value=locator))
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_browser.skyvern_press_key(key="Enter", selector="#field")

    assert result["ok"] is True
    press.assert_awaited_once_with("Enter", timeout=5000)


@pytest.mark.asyncio
async def test_skyvern_press_key_intent_explicit_timeout_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    press = AsyncMock()
    locator = MagicMock()
    locator.press = press
    page = SimpleNamespace(locator=MagicMock(return_value=locator))
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_browser.skyvern_press_key(key="Enter", intent="the search box", timeout=1234)

    assert result["ok"] is True
    press.assert_awaited_once_with("Enter", timeout=1234)


@pytest.mark.asyncio
async def test_skyvern_press_key_intent_keeps_30s_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    press = AsyncMock()
    locator = MagicMock()
    locator.press = press
    page = SimpleNamespace(locator=MagicMock(return_value=locator))
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_browser.skyvern_press_key(key="Enter", intent="the search box")

    assert result["ok"] is True
    press.assert_awaited_once_with("Enter", timeout=30000)


@pytest.mark.asyncio
async def test_skyvern_press_key_intent_error_does_not_leak_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    press = AsyncMock(
        side_effect=InternalServerError(
            body={"error": "Unexpected error: sk-BODY-SECRET"},
            headers={"authorization": "Bearer sk-SECRET"},
        )
    )
    _action_page(monkeypatch, locator=MagicMock(return_value=SimpleNamespace(press=press)))

    result = await mcp_browser.skyvern_press_key(key="Enter", intent="the search box")

    assert result["ok"] is False
    message = result["error"]["message"]
    assert message == "HTTP 500: InternalServerError"
    assert result["error"]["details"] == {"exception_type": "InternalServerError", "status_code": 500}
    for leaked in ("authorization", "bearer", "sk-secret", "sk-body-secret", "headers", "body"):
        assert leaked not in message.lower()


@pytest.mark.asyncio
async def test_skyvern_press_key_direct_failure_reports_element_state(monkeypatch: pytest.MonkeyPatch) -> None:
    probe_locator = make_probe_locator(count=1, visible=False)
    raw_page = MagicMock()
    raw_page.locator = MagicMock(return_value=probe_locator)
    press_locator = MagicMock()
    press_locator.press = AsyncMock(side_effect=mcp_browser.PlaywrightTimeoutError("Timeout 5000ms exceeded."))
    page = SimpleNamespace(page=raw_page, locator_scope=raw_page, locator=MagicMock(return_value=press_locator))
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_browser.skyvern_press_key(key="Enter", selector="#field")

    assert result["ok"] is False
    assert result["error"]["details"]["element_state"] == "hidden"
    assert result["error"]["details"]["selector"] == "#field"


@pytest.mark.asyncio
async def test_skyvern_click_selector_plus_intent_falls_back_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    click = _click_page(monkeypatch)

    result = await mcp_browser.skyvern_click(selector="#submit", intent="the blue submit button")

    assert result["ok"] is True
    assert click.await_args.kwargs.get("ai") == "fallback"
    assert click.await_args.kwargs.get("mode") != "direct"
    assert "_skip_element_prep" not in click.await_args.kwargs


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


def _action_page(monkeypatch: pytest.MonkeyPatch, *, skyvern_page: bool = False, **methods: AsyncMock) -> object:
    raw_page = MagicMock()
    page = (
        make_skyvern_page(raw_page)
        if skyvern_page
        else SimpleNamespace(page=raw_page, locator_scope=raw_page, url="https://example.test/two-factor")
    )
    if skyvern_page:
        page.evaluate = AsyncMock(return_value=False)
    for name, method in methods.items():
        setattr(page, name, method)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    return page


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
    page = SimpleNamespace(page=raw_page, locator_scope=raw_page, click=AsyncMock(return_value="#unexpected-click"))
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    return page, select_option


_SDK_LITERAL_VALUES = (
    pytest.param('contains "double quotes"', id="double-quote"),
    pytest.param("contains a single quote's mark", id="single-quote"),
    pytest.param('contains "both" quote\'s marks', id="both-quotes"),
    pytest.param("first line\nsecond line", id="newline"),
    pytest.param(r"path\with\backslashes", id="backslash"),
    pytest.param("unicode café 🐍", id="unicode"),
    pytest.param('xpath=//div[@aria-label="Continue"]', id="xpath"),
)


def _sdk_equivalent_page(monkeypatch: pytest.MonkeyPatch) -> None:
    locator = SimpleNamespace(
        hover=AsyncMock(),
        press=AsyncMock(),
        scroll_into_view_if_needed=AsyncMock(),
    )
    raw_page = SimpleNamespace(
        locator=MagicMock(return_value=locator),
        url="https://example.test",
        evaluate=AsyncMock(return_value=False),
    )
    run_task_result = SimpleNamespace(
        run_id="wr_test",
        status="completed",
        output=None,
        failure_reason=None,
        recording_url=None,
        app_url=None,
    )
    page = SimpleNamespace(
        page=raw_page,
        _working_frame=SimpleNamespace(url="https://example.test/frame"),
        locator_scope=raw_page,
        url="https://example.test",
        agent=SimpleNamespace(run_task=AsyncMock(return_value=run_task_result)),
        click=AsyncMock(side_effect=lambda **kwargs: kwargs.get("selector")),
        evaluate=AsyncMock(return_value=False),
        fill=AsyncMock(),
        keyboard=SimpleNamespace(press=AsyncMock()),
        locator=MagicMock(return_value=locator),
        select_option=AsyncMock(),
        validate=AsyncMock(return_value=True),
        wait_for_selector=AsyncMock(),
    )
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setenv("SKYVERN_DISABLE_CUSTOM_SELECT", "1")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    monkeypatch.setattr(mcp_browser, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(mcp_browser, "get_current_session", lambda: SimpleNamespace(_working_frame=None))
    monkeypatch.setattr(mcp_browser, "clear_session_ref_map", Mock())
    monkeypatch.setattr(
        mcp_browser,
        "do_navigate",
        AsyncMock(return_value=NavigateResult(url="https://example.test", title="Example")),
    )
    monkeypatch.setattr(mcp_browser, "do_extract", AsyncMock(return_value=SimpleNamespace(extracted={})))
    monkeypatch.setattr(mcp_browser, "do_act", AsyncMock(return_value=SimpleNamespace(prompt="done", completed=True)))
    monkeypatch.setattr(
        mcp_browser,
        "do_frame_switch",
        AsyncMock(return_value=SimpleNamespace(name="frame", url="https://example.test/frame")),
    )
    monkeypatch.setattr(mcp_browser, "select_native_option_if_targeted", AsyncMock(return_value=None))


def _assert_sdk_equivalent_parses(result: dict[str, object]) -> ast.Module:
    assert result["ok"] is True, result
    data = result["data"]
    assert isinstance(data, dict)
    sdk_equivalent = data["sdk_equivalent"]
    assert isinstance(sdk_equivalent, str)
    return ast.parse(f"async def _f():\n    {sdk_equivalent}\n")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "field", "base_kwargs"),
    [
        pytest.param("skyvern_navigate", "url", {}, id="navigate-url"),
        pytest.param("skyvern_click", "intent", {"selector": "#target"}, id="click-intent"),
        pytest.param("skyvern_hover", "intent", {"selector": "#target"}, id="hover-intent"),
        pytest.param("skyvern_type", "text", {"selector": "#target", "intent": "field"}, id="type-text"),
        pytest.param("skyvern_type", "intent", {"selector": "#target", "text": "value"}, id="type-intent"),
        pytest.param("skyvern_scroll", "intent", {"direction": "down", "selector": "#target"}, id="scroll-intent"),
        pytest.param("skyvern_select_option", "value", {"intent": "dropdown"}, id="select-value"),
        pytest.param("skyvern_select_option", "intent", {"value": "choice"}, id="select-intent"),
        pytest.param("skyvern_press_key", "key", {"selector": "#target", "intent": "field"}, id="press-key"),
        pytest.param("skyvern_press_key", "intent", {"selector": "#target", "key": "Enter"}, id="press-intent"),
        pytest.param("skyvern_press_key", "key", {}, id="press-key-bare"),
        pytest.param("skyvern_wait", "intent", {}, id="wait-intent"),
        pytest.param("skyvern_evaluate", "expression", {}, id="evaluate-expression"),
        pytest.param("skyvern_extract", "prompt", {}, id="extract-prompt"),
        pytest.param("skyvern_validate", "prompt", {}, id="validate-prompt"),
        pytest.param("skyvern_act", "prompt", {}, id="act-prompt"),
        pytest.param("skyvern_run_task", "prompt", {}, id="run-task-prompt"),
        pytest.param("skyvern_frame_switch", "name", {}, id="frame-name"),
        pytest.param("skyvern_click", "selector", {}, id="click-selector"),
        pytest.param("skyvern_hover", "selector", {}, id="hover-selector"),
        pytest.param("skyvern_type", "selector", {"text": "value"}, id="type-selector"),
        pytest.param("skyvern_scroll", "selector", {"direction": "down", "intent": "target"}, id="scroll-selector"),
        pytest.param("skyvern_select_option", "selector", {"value": "choice"}, id="select-option-selector"),
        pytest.param("skyvern_press_key", "selector", {"key": "Enter"}, id="press-key-selector"),
        pytest.param("skyvern_wait", "selector", {}, id="wait-selector"),
        pytest.param("skyvern_frame_switch", "selector", {}, id="frame-switch-selector"),
    ],
)
@pytest.mark.parametrize("value", _SDK_LITERAL_VALUES)
async def test_sdk_equivalent_quotes_caller_strings(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    field: str,
    base_kwargs: dict[str, object],
    value: str,
) -> None:
    _sdk_equivalent_page(monkeypatch)

    result = await getattr(mcp_browser, tool_name)(**{**base_kwargs, field: value})

    _assert_sdk_equivalent_parses(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("selected_by", ["value", "label"])
@pytest.mark.parametrize("value", _SDK_LITERAL_VALUES)
async def test_click_native_option_sdk_equivalent_quotes_observed_strings(
    monkeypatch: pytest.MonkeyPatch,
    selected_by: Literal["value", "label"],
    value: str,
) -> None:
    _sdk_equivalent_page(monkeypatch)
    selection = browser_ops.NativeOptionSelection(
        select_selector='xpath=//select[@aria-label="Region"]',
        value=value if selected_by == "value" else None,
        label=value if selected_by == "label" else None,
        selected_by=selected_by,
    )
    monkeypatch.setattr(mcp_browser, "select_native_option_if_targeted", AsyncMock(return_value=selection))

    result = await mcp_browser.skyvern_click(selector="#region > option")

    _assert_sdk_equivalent_parses(result)


@pytest.mark.asyncio
async def test_evaluate_uses_direct_extension_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    get_page = AsyncMock(side_effect=AssertionError("extension evaluation must bypass Playwright"))
    evaluate = AsyncMock(return_value={"answer": 42})
    runtime = SimpleNamespace(evaluate=evaluate)
    monkeypatch.setattr(mcp_browser, "get_page", get_page)
    monkeypatch.setattr(
        mcp_browser,
        "get_current_session",
        lambda: SimpleNamespace(context=BrowserContext(mode="extension")),
    )
    monkeypatch.setattr(
        BrowserExtensionRuntime,
        "instance",
        classmethod(lambda _cls: runtime),
    )

    result = await mcp_browser.skyvern_evaluate(expression="({ answer: 6 * 7 })")

    assert result["ok"] is True
    assert result["data"]["result"] == {"answer": 42}
    evaluate.assert_awaited_once_with("({ answer: 6 * 7 })")
    get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_evaluate_does_not_reclaim_tab_without_active_extension_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_page = AsyncMock(side_effect=session_manager.BrowserNotAvailableError("No active browser session"))
    evaluate = AsyncMock(side_effect=AssertionError("closed extension session must not evaluate"))
    runtime = SimpleNamespace(evaluate=evaluate)
    monkeypatch.setattr(mcp_browser, "get_page", get_page)
    monkeypatch.setattr(
        mcp_browser,
        "get_current_session",
        lambda: SimpleNamespace(context=None),
    )
    monkeypatch.setattr(
        BrowserExtensionRuntime,
        "instance",
        classmethod(lambda _cls: runtime),
    )

    result = await mcp_browser.skyvern_evaluate(expression="document.title")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.NO_ACTIVE_BROWSER
    evaluate.assert_not_awaited()
    get_page.assert_awaited_once_with(session_id=None, cdp_url=None)


@pytest.mark.asyncio
async def test_evaluate_sdk_equivalent_truncates_before_quoting(monkeypatch: pytest.MonkeyPatch) -> None:
    _sdk_equivalent_page(monkeypatch)
    expression = "x" * 79 + '"truncated'

    result = await mcp_browser.skyvern_evaluate(expression=expression)

    tree = _assert_sdk_equivalent_parses(result)
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call))
    assert isinstance(call.args[0], ast.Constant)
    assert call.args[0].value == expression[:80]


@pytest.mark.asyncio
async def test_skyvern_type_selector_is_resilient_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    fill = AsyncMock(return_value="Noor")
    _action_page(monkeypatch, skyvern_page=True, fill=fill)

    result = await mcp_browser.skyvern_type(selector="#first_name", text="Noor")

    assert result["ok"] is True, result
    assert fill.await_args.kwargs.get("mode") != "direct"
    assert fill.await_args.kwargs["_skip_element_prep"] is True


@pytest.mark.asyncio
async def test_skyvern_type_direct_uses_event_strategy_deterministically(monkeypatch: pytest.MonkeyPatch) -> None:
    locator = MagicMock()
    locator.first = locator
    locator.fill = AsyncMock()
    locator.dispatch_event = AsyncMock()
    raw_page = MagicMock()
    locator.page = raw_page
    raw_page.locator = MagicMock(return_value=locator)
    raw_page.url = "https://example.test/form"
    raw_page.evaluate = AsyncMock(return_value=False)
    page = make_skyvern_page(raw_page)
    page.locator_scope = raw_page
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    clear_field = AsyncMock()
    type_text = AsyncMock()
    monkeypatch.setattr("skyvern.webeye.actions.handler_utils.EventStrategyFactory.clear_field", clear_field)
    monkeypatch.setattr("skyvern.webeye.actions.handler_utils.EventStrategyFactory.type_text", type_text)

    result = await mcp_browser.skyvern_type(
        selector="#first_name",
        text="Noor",
        selector_mode="direct",
    )

    assert result["ok"] is True, result.get("error")
    assert raw_page.locator.call_args_list == [call("#first_name"), call("#first_name")]
    clear_field.assert_awaited_once_with(
        raw_page,
        locator,
        char_count=0,
        timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
    )
    type_text.assert_awaited_once_with(
        raw_page,
        locator,
        "Noor",
        timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
        allow_batched_playwright=True,
    )
    assert [call.args[0] for call in locator.dispatch_event.await_args_list] == ["change", "blur"]
    locator.fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_skyvern_type_direct_event_strategy_handles_aggregate_playwright_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator = MagicMock()
    locator.first = locator
    locator.fill = AsyncMock()
    raw_page = MagicMock()
    locator.page = raw_page
    raw_page.locator = MagicMock(return_value=locator)
    raw_page.url = "https://example.test/form"
    raw_page.evaluate = AsyncMock(return_value=False)
    page = make_skyvern_page(raw_page)
    page.locator_scope = raw_page
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    monkeypatch.setattr(
        "skyvern.webeye.actions.handler_utils.EventStrategyFactory.clear_field",
        AsyncMock(side_effect=TimeoutError),
    )

    result = await mcp_browser.skyvern_type(
        selector="#first_name",
        text="Noor",
        selector_mode="direct",
        timeout=1234,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.ACTION_FAILED
    assert result["error"]["details"]["actionability_timeout_ms"] == 1234


@pytest.mark.asyncio
async def test_skyvern_type_missing_selector_preflight_uses_aggregate_direct_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight_cancelled = asyncio.Event()

    async def wait_for_missing_selector(*_args: object, **_kwargs: object) -> bool:
        try:
            await asyncio.Future()
        finally:
            preflight_cancelled.set()
        return False

    raw_page = MagicMock()
    raw_page.url = "https://example.test/form"
    active_frame = MagicMock()
    matches = MagicMock()
    matches.count = AsyncMock(return_value=0)
    first_match = MagicMock()
    first_match.evaluate = AsyncMock(side_effect=wait_for_missing_selector)
    matches.first = first_match
    active_frame.locator.return_value = matches
    page = SimpleNamespace(page=raw_page, locator_scope=active_frame)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    strategy_aware_input = AsyncMock()
    monkeypatch.setattr(mcp_browser, "strategy_aware_input", strategy_aware_input)

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    result = await asyncio.wait_for(
        mcp_browser.skyvern_type(
            selector="#missing",
            text="Noor",
            selector_mode="direct",
            timeout=1000,
        ),
        timeout=1.5,
    )

    assert loop.time() - started_at < 1.5
    assert preflight_cancelled.is_set()
    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.SELECTOR_NOT_FOUND
    assert result["error"]["details"]["actionability_timeout_ms"] == 1000
    strategy_aware_input.assert_not_awaited()


@pytest.mark.asyncio
async def test_skyvern_type_direct_append_uses_strategy_without_replacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator = MagicMock()
    locator.first = locator
    locator.fill = AsyncMock()
    raw_page = MagicMock()
    locator.page = raw_page
    raw_page.locator = MagicMock(return_value=locator)
    raw_page.url = "https://example.test/form"
    raw_page.evaluate = AsyncMock(return_value=False)
    page = make_skyvern_page(raw_page)
    page.locator_scope = raw_page
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    clear_field = AsyncMock()
    type_text = AsyncMock()
    monkeypatch.setattr("skyvern.webeye.actions.handler_utils.EventStrategyFactory.clear_field", clear_field)
    monkeypatch.setattr("skyvern.webeye.actions.handler_utils.EventStrategyFactory.type_text", type_text)

    result = await mcp_browser.skyvern_type(
        selector="#notes",
        text="more",
        selector_mode="direct",
        clear=False,
        delay=17,
    )

    assert result["ok"] is True, result
    clear_field.assert_not_awaited()
    locator.fill.assert_not_awaited()
    type_text.assert_awaited_once_with(
        raw_page,
        locator,
        "more",
        timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
        delay=17,
        no_wait_after=None,
        allow_batched_playwright=True,
    )


@pytest.mark.asyncio
async def test_skyvern_type_direct_long_replacement_keeps_atomic_prefix_and_strategy_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "x" * (TEXT_PRESS_MAX_LENGTH + 4)
    locator = MagicMock()
    locator.first = locator
    locator.fill = AsyncMock()
    raw_page = MagicMock()
    locator.page = raw_page
    raw_page.locator = MagicMock(return_value=locator)
    raw_page.url = "https://example.test/form"
    raw_page.evaluate = AsyncMock(return_value=False)
    page = make_skyvern_page(raw_page)
    page.locator_scope = raw_page
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    clear_field = AsyncMock()
    type_text = AsyncMock()
    monkeypatch.setattr("skyvern.webeye.actions.handler_utils.EventStrategyFactory.clear_field", clear_field)
    monkeypatch.setattr("skyvern.webeye.actions.handler_utils.EventStrategyFactory.type_text", type_text)

    result = await mcp_browser.skyvern_type(
        selector="#notes",
        text=text,
        selector_mode="direct",
    )

    assert result["ok"] is True, result
    clear_field.assert_not_awaited()
    locator.fill.assert_awaited_once_with(text[:-TEXT_PRESS_MAX_LENGTH], timeout=5000)
    type_text.assert_awaited_once_with(
        raw_page,
        locator,
        text[-TEXT_PRESS_MAX_LENGTH:],
        timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
        allow_batched_playwright=True,
    )


@pytest.mark.asyncio
async def test_skyvern_type_direct_event_strategy_uses_active_iframe_and_first_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_page = MagicMock()
    raw_page.url = "https://example.test/form"
    raw_page.evaluate = AsyncMock(return_value=False)
    active_frame = MagicMock()
    matches = MagicMock()
    first_match = MagicMock()
    first_match.page = raw_page
    matches.first = first_match
    active_frame.locator.return_value = matches
    page = make_skyvern_page(raw_page)
    page.locator_scope = active_frame
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    clear_field = AsyncMock()
    type_text = AsyncMock()
    monkeypatch.setattr("skyvern.webeye.actions.handler_utils.EventStrategyFactory.clear_field", clear_field)
    monkeypatch.setattr("skyvern.webeye.actions.handler_utils.EventStrategyFactory.type_text", type_text)

    result = await mcp_browser.skyvern_type(
        selector=".shared-name",
        text="Noor",
        selector_mode="direct",
    )

    assert result["ok"] is True, result
    active_frame.locator.assert_called_with(".shared-name")
    raw_page.locator.assert_not_called()
    clear_field.assert_awaited_once_with(
        raw_page,
        first_match,
        char_count=0,
        timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
    )
    type_text.assert_awaited_once_with(
        raw_page,
        first_match,
        "Noor",
        timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
        allow_batched_playwright=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("clear", [pytest.param(True, id="replace"), pytest.param(False, id="append")])
async def test_skyvern_type_direct_event_strategy_refuses_active_iframe_password_first_match(
    monkeypatch: pytest.MonkeyPatch,
    clear: bool,
) -> None:
    raw_page = MagicMock()
    raw_page.url = "https://example.test/form"
    raw_page.evaluate = AsyncMock()
    active_frame = MagicMock()
    matches = MagicMock()
    first_match = MagicMock()
    first_match.evaluate = AsyncMock(return_value=True)
    matches.first = first_match
    active_frame.locator.return_value = matches
    page = make_skyvern_page(raw_page)
    page.locator_scope = active_frame
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    strategy_aware_input = AsyncMock()
    monkeypatch.setattr(mcp_browser, "strategy_aware_input", strategy_aware_input)

    result = await mcp_browser.skyvern_type(
        selector=".shared-field",
        text="opaque-value",
        selector_mode="direct",
        clear=clear,
    )

    assert result["ok"] is False
    assert "password" in result["error"]["message"].lower()
    active_frame.locator.assert_called_once_with(".shared-field")
    first_match.evaluate.assert_awaited_once()
    raw_page.evaluate.assert_not_awaited()
    strategy_aware_input.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("clear", [True, False], ids=["fill", "append"])
async def test_skyvern_type_with_intent_refuses_top_document_password_match(
    monkeypatch: pytest.MonkeyPatch,
    clear: bool,
) -> None:
    top_match = MagicMock()
    top_match.first = top_match
    top_match.evaluate = AsyncMock(return_value=True)
    top_match.type = AsyncMock()
    raw_page = MagicMock()
    raw_page.url = "https://example.test/form"
    raw_page.locator = MagicMock(return_value=top_match)
    frame_match = MagicMock()
    frame_match.first = frame_match
    frame_match.evaluate = AsyncMock(return_value=False)
    active_frame = MagicMock()
    active_frame.locator = MagicMock(return_value=frame_match)
    page = make_skyvern_page(raw_page)
    page.locator_scope = active_frame
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_browser.skyvern_type(
        selector=".shared-field",
        intent="the promo code box",
        text="opaque-value",
        clear=clear,
    )

    assert result["ok"] is False
    assert "password" in result["error"]["message"].lower()
    top_match.type.assert_not_awaited()
    page.fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_skyvern_type_append_with_intent_builds_the_ai_locator_on_the_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = MagicMock()
    probe.first = probe
    probe.evaluate = AsyncMock(return_value=False)
    raw_page = MagicMock()
    raw_page.url = "https://example.test/form"
    raw_page.locator = MagicMock(return_value=probe)
    active_frame = MagicMock()
    active_frame.locator = MagicMock(return_value=probe)
    ai_locator = MagicMock()
    ai_locator.type = AsyncMock()
    page = make_skyvern_page(raw_page)
    page.locator_scope = active_frame
    page.locator = MagicMock(return_value=ai_locator)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_browser.skyvern_type(
        selector=".promo-field",
        intent="the promo code box",
        text="opaque-value",
        clear=False,
    )

    assert result["ok"] is True, result.get("error")
    page.locator.assert_called_once_with(selector=".promo-field", prompt="the promo code box", ai="fallback")
    ai_locator.type.assert_awaited_once()


@pytest.mark.asyncio
async def test_skyvern_type_selector_only_append_skips_skyvern_page_prep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    type_text = AsyncMock(return_value="Noor")
    _action_page(monkeypatch, skyvern_page=True, type=type_text)

    result = await mcp_browser.skyvern_type(selector="#first_name", text="Noor", clear=False)

    assert result["ok"] is True, result
    assert type_text.await_args.kwargs["_skip_element_prep"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("clear", [True, False], ids=["fill", "type"])
@pytest.mark.parametrize("page_kind", ["adapter", "raw"])
async def test_skyvern_type_page_adapter_never_receives_private_kwarg(
    monkeypatch: pytest.MonkeyPatch,
    clear: bool,
    page_kind: str,
) -> None:
    action = AsyncMock(return_value="Noor")
    method = "fill" if clear else "type"
    if page_kind == "adapter":
        _action_page(monkeypatch, **{method: action})
    else:
        page = MagicMock()
        page.page = page
        page.evaluate = AsyncMock(return_value=False)
        setattr(page, method, action)
        context = BrowserContext(mode="cloud_session", session_id="pbs_test")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_browser.skyvern_type(selector="#first_name", text="Noor", clear=clear)

    assert result["ok"] is True
    assert "_skip_element_prep" not in action.await_args.kwargs


@pytest.mark.asyncio
async def test_skyvern_type_selector_plus_intent_falls_back_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    fill = AsyncMock(return_value="Noor")
    _action_page(monkeypatch, fill=fill)

    result = await mcp_browser.skyvern_type(selector="#first_name", text="Noor", intent="the first name field")

    assert result["ok"] is True
    assert fill.await_args.kwargs.get("ai") == "fallback"
    assert fill.await_args.kwargs.get("mode") != "direct"
    assert "_skip_element_prep" not in fill.await_args.kwargs


@pytest.mark.asyncio
async def test_skyvern_type_intent_only_uses_proactive_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    fill = AsyncMock(return_value="Noor")
    _action_page(monkeypatch, fill=fill)

    result = await mcp_browser.skyvern_type(intent="the first name field", text="Noor")

    assert result["ok"] is True
    assert fill.await_args.kwargs["ai"] == "proactive"
    assert fill.await_args.kwargs.get("mode") != "direct"
    assert "_skip_element_prep" not in fill.await_args.kwargs


@pytest.mark.asyncio
async def test_skyvern_type_selector_only_failure_uses_direct_element_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fill, _locator = _direct_type_page(
        monkeypatch,
        count=1,
        visible=False,
        enabled=True,
        fill_error=mcp_browser.PlaywrightTimeoutError("Timeout 5000ms exceeded."),
    )

    result = await mcp_browser.skyvern_type(selector="#target", text="Noor")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.ACTION_FAILED
    assert result["error"]["details"]["element_state"] == "hidden"
    assert result["error"]["details"]["actionability_timeout_ms"] == 5000


@pytest.mark.asyncio
async def test_skyvern_click_native_option_selector_uses_parent_select(monkeypatch: pytest.MonkeyPatch) -> None:
    page, select_option = _native_option_page(monkeypatch)

    result = await mcp_browser.skyvern_click(selector="#region > option:nth-of-type(4)")

    assert result["ok"] is True
    page.click.assert_not_awaited()
    select_option.assert_awaited_once_with(value="east", timeout=5000)
    assert result["data"]["resolved_selector"] == "#region"
    assert result["data"]["selected_option"] == {
        "select_selector": "#region",
        "selected_by": "value",
        "value": "east",
        "label": "East",
    }
    assert result["data"]["sdk_equivalent"] == "await page.select_option('#region', value='east')"


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
    assert result["data"]["sdk_equivalent"] == "await page.select_option('#region', label='East')"


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
    select_option.assert_awaited_once_with(index=3, timeout=5000)
    assert result["data"]["selected_option"]["selected_by"] == "index"
    assert result["data"]["selected_option"]["index"] == 3
    assert result["data"]["sdk_equivalent"] == "await page.select_option('#region', index=3)"


def test_exception_message_suppresses_body_outside_4xx() -> None:
    class NonErrorStatusException(Exception):
        status_code = 200
        body = {"detail": "should not be surfaced"}

    message = mcp_browser._exception_message(NonErrorStatusException())

    assert "should not be surfaced" not in message


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
async def test_skyvern_drag_ai_error_does_not_leak_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    _action_page(monkeypatch)
    monkeypatch.setattr(
        mcp_browser,
        "do_act",
        AsyncMock(
            side_effect=InternalServerError(
                body={"error": "Unexpected error: sk-BODY-SECRET"},
                headers={"authorization": "Bearer sk-SECRET"},
            )
        ),
    )

    result = await mcp_browser.skyvern_drag(source_intent="the card", target_intent="the trash bin")

    assert result["ok"] is False
    message = result["error"]["message"]
    assert message == "HTTP 500: InternalServerError"
    assert result["error"]["details"] == {"exception_type": "InternalServerError", "status_code": 500}
    for leaked in ("authorization", "bearer", "sk-secret", "sk-body-secret", "headers", "body"):
        assert leaked not in message.lower()


@pytest.mark.asyncio
async def test_skyvern_wait_intent_error_does_not_leak_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    _action_page(
        monkeypatch,
        validate=AsyncMock(
            side_effect=InternalServerError(
                body={"error": "Unexpected error: sk-BODY-SECRET"},
                headers={"authorization": "Bearer sk-SECRET"},
            )
        ),
        wait_for_timeout=make_real_wait_for_timeout(),
    )

    result = await mcp_browser.skyvern_wait(
        intent="the spinner disappears",
        timeout=1000,
        poll_interval_ms=500,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.SDK_ERROR
    message = result["error"]["message"]
    assert message == "HTTP 500: InternalServerError"
    assert result["error"]["details"] == {"exception_type": "InternalServerError", "status_code": 500}
    for leaked in ("authorization", "bearer", "sk-secret", "sk-body-secret", "headers", "body"):
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
async def test_select_option_reports_the_browser_attach_when_the_custom_path_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _action_page(monkeypatch, select_option=AsyncMock(return_value="us"))
    result_mod._pending_attach.set(700)
    try:
        result = await mcp_browser.skyvern_select_option(selector="#country", value="us")
    finally:
        result_mod._pending_attach.set(None)

    assert result["ok"] is True
    assert result["timing_ms"].get("attach") == 700


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
async def test_skyvern_select_option_routes_custom_widget_through_core_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, native_select_option = make_select_option_page()
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    do_select_option = AsyncMock(return_value="Music")
    monkeypatch.setattr(mcp_browser, "do_select_option", do_select_option, raising=False)

    result = await mcp_browser.skyvern_select_option(
        selector="#category",
        value="music",
        selector_mode="direct",
        timeout=4321,
    )

    assert result["ok"] is True
    assert result["data"]["sdk_equivalent"] == (
        "# No single SDK method -- open/filter '#category', then click exact observed option 'Music'"
    )
    do_select_option.assert_awaited_once_with(
        page.page,
        "#category",
        "music",
        by_label=False,
        timeout=4321,
        restore_value_on_failure=False,
        fail_closed_on_unknown=False,
    )
    native_select_option.assert_not_awaited()


@pytest.mark.asyncio
async def test_skyvern_select_option_routes_custom_widget_through_working_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = MagicMock()
    page, native_select_option = make_select_option_page(locator_scope=frame)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    custom_select = AsyncMock(return_value="Music")
    monkeypatch.setattr(mcp_browser, "do_select_option", custom_select)

    result = await mcp_browser.skyvern_select_option(selector="#category", value="music", timeout=4321)

    assert result["ok"] is True
    custom_select.assert_awaited_once_with(
        frame,
        "#category",
        "music",
        by_label=False,
        timeout=4321,
        restore_value_on_failure=False,
        fail_closed_on_unknown=False,
    )
    native_select_option.assert_not_awaited()


@pytest.mark.asyncio
async def test_do_select_option_keeps_native_select_on_existing_path() -> None:
    page, control = make_select_like_page({"tag": "select", "role": "combobox", "haspopup": False, "editable": False})

    result = await do_select_option(page, "#region", "east", timeout=1)

    assert result is None
    control.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_do_select_option_defers_password_input_without_mutating_it() -> None:
    page, control = make_select_like_page(
        {
            "tag": "input",
            "type": "password",
            "isPassword": True,
            "role": "textbox",
            "haspopup": False,
            # Defense in depth must reject even a bad classifier payload.
            "editable": True,
        }
    )

    with pytest.raises(CustomSelectPasswordError):
        await do_select_option(page, "#secret", "hunter2", timeout=100)

    # Only the classification probe ran — no fill/click, value untouched.
    assert control.evaluate.await_count == 1
    control.fill.assert_not_awaited()
    control.fill.assert_not_awaited()
    control.click.assert_not_awaited()
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        {"tag": "div", "role": "combobox", "haspopup": False, "editable": False},
        {"tag": "div", "role": "listbox", "haspopup": False, "editable": False},
        {"tag": "button", "role": "", "haspopup": True, "editable": False},
    ],
)
async def test_do_select_option_recognizes_aria_custom_selects(
    monkeypatch: pytest.MonkeyPatch,
    target: dict[str, object],
) -> None:
    page, control = make_select_like_page(target)
    dom_options = [{"selector": "#music", "role": "option", "name": "Music"}]
    monkeypatch.setattr(browser_ops, "_get_dom_observe_elements", AsyncMock(return_value=dom_options))
    # do_select_option's deadline is real wall clock (started_at + timeout/1000), so a
    # loaded runner can blow the 100ms budget before the option loop ticks. Freeze the
    # clock: this asserts the success path, not the deadline.
    monkeypatch.setattr(browser_ops, "time", SimpleNamespace(monotonic=Mock(return_value=0)))
    control.evaluate.side_effect = [
        target,
        [{"selector": "#music", "label": "Music", "value": "music"}],
        {"text": "Select", "value": "", "dataValues": [""], "expanded": "true", "optionSelected": False},
        {"text": "Music", "value": "", "dataValues": ["music"], "expanded": "false", "optionSelected": False},
    ]

    result = await do_select_option(page, "#category", "music", timeout=100)

    assert result == "Music"
    assert control.click.await_count == 2


@pytest.mark.asyncio
async def test_do_select_option_scan_observed_control_uses_widened_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {
        "tag": "button",
        "role": "",
        "haspopup": False,
        "editable": False,
        "selectlike": False,
        "optionish": True,
        "related": False,
    }
    page, control = make_select_like_page(target)
    dom_options = [
        {"selector": "#category", "role": "button", "name": "Category"},
        {"selector": "#music", "role": "option", "name": "Music"},
    ]
    monkeypatch.setattr(browser_ops, "_get_dom_observe_elements", AsyncMock(return_value=dom_options))
    control.evaluate.side_effect = [
        target,
        True,
        [{"selector": "#music", "label": "Music", "value": "music"}],
        {"text": "Select", "value": "", "dataValues": [""], "expanded": "true", "optionSelected": False},
        {"text": "Music", "value": "", "dataValues": ["music"], "expanded": "false", "optionSelected": False},
    ]
    # SKY-12634: freeze the wall clock so a loaded CI runner cannot blow the 100ms budget.
    monkeypatch.setattr(browser_ops, "time", SimpleNamespace(monotonic=Mock(return_value=0)))

    result = await do_select_option(page, "#category", "music", timeout=100)

    assert result == "Music"


@pytest.mark.asyncio
async def test_do_select_option_matches_observed_accessible_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {"tag": "button", "role": "combobox", "haspopup": False, "editable": False}
    page, control = make_select_like_page(target)
    dom_options = [{"selector": "#us", "role": "option", "name": "United States"}]
    monkeypatch.setattr(browser_ops, "_get_dom_observe_elements", AsyncMock(return_value=dom_options))
    control.evaluate.side_effect = [
        target,
        [{"selector": "#us", "label": "US", "value": "us"}],
        {"text": "Choose", "value": "", "dataValues": [""], "expanded": "true", "optionSelected": False},
        {"text": "US", "value": "", "dataValues": ["us"], "expanded": "false", "optionSelected": False},
    ]

    result = await do_select_option(page, "#country", "United States", by_label=True, timeout=100)

    assert result == "United States"


@pytest.mark.asyncio
async def test_do_select_option_rejects_substring_value_false_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {"tag": "button", "role": "combobox", "haspopup": False, "editable": False}
    page, control = make_select_like_page(target)
    dom_options = [{"selector": "#oregon", "role": "option", "name": "OR"}]
    monkeypatch.setattr(browser_ops, "_get_dom_observe_elements", AsyncMock(return_value=dom_options))
    before = {
        "text": "Choose OR state",
        "value": "",
        "dataValues": ["north"],
        "expanded": "true",
        "optionSelected": False,
    }
    control.evaluate.side_effect = [
        target,
        [{"selector": "#oregon", "label": "OR", "value": "OR"}],
        before,
        {**before, "expanded": "false"},
    ]
    monkeypatch.setattr(browser_ops, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0, 1])))
    monkeypatch.setattr(browser_ops, "asyncio", SimpleNamespace(sleep=AsyncMock()))

    with pytest.raises(RuntimeError, match="did not commit 'OR'"):
        await do_select_option(page, "#state", "or", timeout=100)


@pytest.mark.asyncio
async def test_do_select_option_accepts_idempotent_exact_committed_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {"tag": "button", "role": "combobox", "haspopup": False, "editable": False}
    page, control = make_select_like_page(target)
    monkeypatch.setattr(
        browser_ops,
        "_get_dom_observe_elements",
        AsyncMock(return_value=[{"selector": "#oregon", "role": "option", "name": "OR"}]),
    )
    committed = {
        "text": "Oregon",
        "value": "OR",
        "dataValues": [],
        "expanded": "false",
        "optionVisible": False,
        "optionSelected": False,
    }
    control.evaluate.side_effect = [
        target,
        [{"selector": "#oregon", "label": "OR", "value": "OR"}],
        committed,
        committed,
    ]

    assert await do_select_option(page, "#state", "or", timeout=100) == "OR"
    assert control.click.await_count == 2
    assert control.evaluate.await_count == 4


@pytest.mark.asyncio
async def test_do_select_option_still_scans_and_probes_once_when_opening_ate_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stalled event loop between the open click and the first scan (a loaded CI shard) must not
    # turn the budget into "no option looked at": the deadline bounds waiting, not the first tick.
    target = {"tag": "button", "role": "combobox", "haspopup": False, "editable": False}
    page, control = make_select_like_page(target)
    monkeypatch.setattr(
        browser_ops,
        "_get_dom_observe_elements",
        AsyncMock(return_value=[{"selector": "#oregon", "role": "option", "name": "OR"}]),
    )
    committed = {
        "text": "Oregon",
        "value": "OR",
        "dataValues": [],
        "expanded": "false",
        "optionVisible": False,
        "optionSelected": False,
    }
    control.evaluate.side_effect = [
        target,
        [{"selector": "#oregon", "label": "OR", "value": "OR"}],
        committed,
        committed,
    ]

    async def stalled_click(**_: object) -> None:
        await asyncio.sleep(0.02)

    control.click = AsyncMock(side_effect=stalled_click)

    assert await do_select_option(page, "#state", "or", timeout=1) == "OR"


@pytest.mark.asyncio
async def test_do_select_option_rejects_toggle_deselect_of_requested_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {"tag": "button", "role": "combobox", "haspopup": False, "editable": False}
    page, control = make_select_like_page(target)
    monkeypatch.setattr(
        browser_ops,
        "_get_dom_observe_elements",
        AsyncMock(return_value=[{"selector": "#oregon", "role": "option", "name": "OR"}]),
    )
    # Coincidental static data-value="OR" that never changes, while the matched option stays
    # present and its aria-selected flips true->false — the deselection veto must reject.
    before = {
        "text": "OR",
        "value": "",
        "dataValue": "OR",
        "expanded": "true",
        "optionVisible": True,
        "optionPresent": True,
        "optionSelected": True,
    }
    control.evaluate.side_effect = [
        target,
        [{"selector": "#oregon", "label": "OR", "value": "OR"}],
        before,
        {**before, "optionVisible": False, "optionPresent": True, "optionSelected": False},
    ]
    monkeypatch.setattr(browser_ops, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0, 1])))
    monkeypatch.setattr(browser_ops, "asyncio", SimpleNamespace(sleep=AsyncMock()))

    with pytest.raises(RuntimeError, match="did not commit 'OR'"):
        await do_select_option(page, "#state", "or", timeout=100)


@pytest.mark.asyncio
async def test_do_select_option_ignores_unrelated_stable_data_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {"tag": "button", "role": "combobox", "haspopup": False, "editable": False}
    page, control = make_select_like_page(target)
    monkeypatch.setattr(
        browser_ops,
        "_get_dom_observe_elements",
        AsyncMock(return_value=[{"selector": "#oregon", "role": "option", "name": "OR"}]),
    )
    before = {
        "text": "Choose state",
        "value": "",
        "dataValue": "",
        "dataValues": ["OR"],
        "expanded": "true",
        "optionVisible": True,
        "optionSelected": False,
    }
    control.evaluate.side_effect = [
        target,
        [{"selector": "#oregon", "label": "OR", "value": "OR"}],
        before,
        {**before, "optionVisible": False},
    ]
    monkeypatch.setattr(browser_ops, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0, 1])))
    monkeypatch.setattr(browser_ops, "asyncio", SimpleNamespace(sleep=AsyncMock()))

    with pytest.raises(RuntimeError, match="did not commit 'OR'"):
        await do_select_option(page, "#state", "or", timeout=100)


@pytest.mark.asyncio
@pytest.mark.parametrize("editable", [False, True])
async def test_do_select_option_uses_writable_target_action(
    monkeypatch: pytest.MonkeyPatch,
    editable: bool,
) -> None:
    target = {"tag": "input", "role": "combobox", "haspopup": True, "editable": editable}
    page, control = make_select_like_page(target)
    monkeypatch.setattr(
        browser_ops,
        "_get_dom_observe_elements",
        AsyncMock(return_value=[{"selector": "#west", "role": "option", "name": "West"}]),
    )
    control.evaluate.side_effect = [
        target,
        [{"selector": "#west", "label": "West", "value": "west"}],
        {"text": "", "value": "", "dataValues": [], "expanded": "true", "optionSelected": False},
        {"text": "West", "value": "west", "dataValues": [], "expanded": "false", "optionSelected": False},
    ]
    # do_select_option's deadline is real wall clock (started_at + timeout/1000), so a
    # loaded runner can blow the 100ms budget before the commit is observed. Freeze the
    # clock: this asserts the success path, not the deadline.
    monkeypatch.setattr(browser_ops, "time", SimpleNamespace(monotonic=Mock(return_value=0)))

    assert await do_select_option(page, "#state", "west", timeout=100) == "West"
    assert control.fill.await_count == int(editable)


@pytest.mark.asyncio
async def test_do_select_option_scan_observed_bare_input_uses_typeahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {"tag": "input", "role": "textbox", "haspopup": False, "editable": True, "selectlike": False}
    page, control = make_select_like_page(target)
    dom_elements = [{"selector": "#town", "role": "textbox", "name": "Town"}]
    monkeypatch.setattr(browser_ops, "_get_dom_observe_elements", AsyncMock(return_value=dom_elements))
    monkeypatch.setattr(
        browser_ops,
        "_scoped_custom_options",
        AsyncMock(return_value=[{"selector": "#fairview", "role": "option", "name": "Fairview", "label": "Fairview"}]),
    )
    control.evaluate.side_effect = [
        target,
        True,
        {"text": "", "value": "Fairview", "dataValues": [], "expanded": None, "optionSelected": False},
        {"text": "", "value": "Fairview", "dataValues": [], "expanded": None, "optionSelected": True},
    ]
    # SKY-12634: freeze the wall clock so a loaded CI runner cannot blow the 100ms budget.
    monkeypatch.setattr(browser_ops, "time", SimpleNamespace(monotonic=Mock(return_value=0)))

    assert await do_select_option(page, "#town", "Fairview", timeout=100) == "Fairview"
    control.fill.assert_awaited_once_with("Fairview", timeout=100)


@pytest.mark.asyncio
async def test_do_select_option_bare_input_rejects_preexisting_neighbor_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {
        "tag": "input",
        "role": "textbox",
        "haspopup": False,
        "editable": True,
        "selectlike": False,
        "related": False,
    }
    page, control = make_select_like_page(target)
    dom_elements = [
        {"selector": "#town", "role": "textbox", "name": "Town"},
        {"selector": "#neighbor-fairview", "role": "option", "name": "Fairview"},
    ]
    monkeypatch.setattr(browser_ops, "_get_dom_observe_elements", AsyncMock(return_value=dom_elements))
    scoped_options = AsyncMock(return_value=[])
    monkeypatch.setattr(browser_ops, "_scoped_custom_options", scoped_options)
    control.evaluate.side_effect = [target, True]
    monkeypatch.setattr(browser_ops, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 1])))
    monkeypatch.setattr(browser_ops, "asyncio", SimpleNamespace(sleep=AsyncMock()))

    with pytest.raises(CustomSelectMatchError):
        await do_select_option(page, "#town", "Fairview", timeout=100)

    assert scoped_options.await_args.kwargs["before_option_selectors"] == {"#neighbor-fairview"}
    assert scoped_options.await_args.kwargs["bare_input"] is True
    control.fill.assert_awaited_once_with("Fairview", timeout=100)
    assert control.click.await_count == 0


@pytest.mark.asyncio
async def test_do_select_option_bare_input_fill_value_does_not_verify_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {
        "tag": "input",
        "role": "textbox",
        "haspopup": False,
        "editable": True,
        "selectlike": False,
        "related": False,
    }
    page, control = make_select_like_page(target)
    dom_elements = [{"selector": "#town", "role": "textbox", "name": "Town"}]
    monkeypatch.setattr(browser_ops, "_get_dom_observe_elements", AsyncMock(return_value=dom_elements))
    monkeypatch.setattr(
        browser_ops,
        "_scoped_custom_options",
        AsyncMock(return_value=[{"selector": "#fairview", "role": "option", "name": "Fairview"}]),
    )
    before = {
        "text": "Searching",
        "value": "Fairview",
        "dataValues": [],
        "expanded": None,
        "optionVisible": True,
        "optionSelected": False,
    }
    control.evaluate.side_effect = [target, True, before, {**before, "text": "Idle"}]
    monkeypatch.setattr(browser_ops, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0, 1])))
    monkeypatch.setattr(browser_ops, "asyncio", SimpleNamespace(sleep=AsyncMock()))

    with pytest.raises(RuntimeError, match="did not commit 'Fairview'"):
        await do_select_option(page, "#town", "Fairview", timeout=100)


@pytest.mark.asyncio
async def test_do_select_option_editable_list_close_requires_absent_container_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {"tag": "input", "role": "combobox", "haspopup": True, "editable": True}
    page, control = make_select_like_page(target)
    monkeypatch.setattr(
        browser_ops,
        "_get_dom_observe_elements",
        AsyncMock(return_value=[{"selector": "#lakewood", "role": "option", "name": "Lakewood"}]),
    )
    before = {
        "text": "",
        "value": "Lakewood",
        "dataValue": "",
        "dataValues": [],
        "containerChannels": [{"key": "#city-value:value", "value": ""}],
        "expanded": "true",
        "optionVisible": True,
        "optionSelected": False,
    }
    control.evaluate.side_effect = [
        target,
        [{"selector": "#lakewood", "label": "Lakewood", "value": "Lakewood"}],
        before,
        {**before, "expanded": "false", "optionVisible": False},
    ]
    monkeypatch.setattr(browser_ops, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0, 1])))
    monkeypatch.setattr(browser_ops, "asyncio", SimpleNamespace(sleep=AsyncMock()))

    with pytest.raises(RuntimeError, match="did not commit 'Lakewood'"):
        await do_select_option(page, "#city", "Lakewood", timeout=100)


@pytest.mark.asyncio
@pytest.mark.parametrize("committed_value", ["Lakewood", "city-123"], ids=["requested", "matched-option-value"])
async def test_do_select_option_accepts_editable_container_channel_transition(
    monkeypatch: pytest.MonkeyPatch,
    committed_value: str,
) -> None:
    target = {"tag": "input", "role": "combobox", "haspopup": True, "editable": True}
    page, control = make_select_like_page(target)
    monkeypatch.setattr(
        browser_ops,
        "_get_dom_observe_elements",
        AsyncMock(return_value=[{"selector": "#lakewood", "role": "option", "name": "Lakewood"}]),
    )
    before = {
        "text": "",
        "value": "Lakewood",
        "dataValue": "",
        "dataValues": [],
        "containerChannels": [{"key": "#city-value:value", "value": ""}],
        "expanded": "true",
        "optionVisible": True,
        "optionSelected": False,
    }
    control.evaluate.side_effect = [
        target,
        [{"selector": "#lakewood", "label": "Lakewood", "value": committed_value}],
        before,
        {
            **before,
            "containerChannels": [{"key": "#city-value:value", "value": committed_value}],
        },
    ]
    # do_select_option's deadline is real wall clock (started_at + timeout/1000), so a
    # loaded runner can blow the 100ms budget before the commit is observed. Freeze the
    # clock: this asserts the success path, not the deadline.
    monkeypatch.setattr(browser_ops, "time", SimpleNamespace(monotonic=Mock(return_value=0)))

    assert await do_select_option(page, "#city", "Lakewood", by_label=True, timeout=100) == "Lakewood"


@pytest.mark.asyncio
async def test_do_select_option_rejects_new_unrelated_container_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {"tag": "input", "role": "combobox", "haspopup": True, "editable": True}
    page, control = make_select_like_page(target)
    monkeypatch.setattr(
        browser_ops,
        "_get_dom_observe_elements",
        AsyncMock(return_value=[{"selector": "#lakewood", "role": "option", "name": "Lakewood"}]),
    )
    before = {
        "text": "",
        "value": "Lakewood",
        "dataValue": "",
        "dataValues": [],
        "containerChannels": [{"key": "input:hidden:city:value", "value": ""}],
        "expanded": "true",
        "optionVisible": True,
        "optionSelected": False,
    }
    control.evaluate.side_effect = [
        target,
        [{"selector": "#lakewood", "label": "Lakewood", "value": "Lakewood"}],
        before,
        {
            **before,
            "containerChannels": [
                {"key": "input:hidden:city:value", "value": ""},
                {"key": "#city-widget:data-loading", "value": "true"},
            ],
            "expanded": "false",
            "optionVisible": False,
        },
    ]
    monkeypatch.setattr(browser_ops, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0, 1])))
    monkeypatch.setattr(browser_ops, "asyncio", SimpleNamespace(sleep=AsyncMock()))

    with pytest.raises(RuntimeError, match="did not commit 'Lakewood'"):
        await do_select_option(page, "#city", "Lakewood", timeout=100)


@pytest.mark.asyncio
async def test_do_select_option_keeps_editable_list_close_as_channel_free_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {"tag": "input", "role": "combobox", "haspopup": True, "editable": True}
    page, control = make_select_like_page(target)
    monkeypatch.setattr(
        browser_ops,
        "_get_dom_observe_elements",
        AsyncMock(return_value=[{"selector": "#lakewood", "role": "option", "name": "Lakewood"}]),
    )
    before = {
        "text": "",
        "value": "Lakewood",
        "dataValue": "",
        "dataValues": [],
        "containerChannels": [],
        "expanded": "true",
        "optionVisible": True,
        "optionSelected": False,
    }
    control.evaluate.side_effect = [
        target,
        [{"selector": "#lakewood", "label": "Lakewood", "value": "Lakewood"}],
        before,
        {**before, "expanded": "false", "optionVisible": False},
    ]
    # do_select_option's deadline is real wall clock (started_at + timeout/1000), so a
    # loaded runner can blow the 100ms budget before the commit is observed. Freeze the
    # clock: this asserts the success path, not the deadline.
    monkeypatch.setattr(browser_ops, "time", SimpleNamespace(monotonic=Mock(return_value=0)))

    assert await do_select_option(page, "#city", "Lakewood", timeout=100) == "Lakewood"


@pytest.mark.asyncio
@pytest.mark.parametrize("bare_input", [False, True], ids=["aria", "bare"])
async def test_do_select_option_retries_editable_with_real_key_events_when_fill_has_no_options(
    monkeypatch: pytest.MonkeyPatch,
    bare_input: bool,
) -> None:
    target = {
        "tag": "input",
        "role": "textbox" if bare_input else "combobox",
        "haspopup": not bare_input,
        "editable": True,
        "selectlike": False,
        "related": not bare_input,
    }
    page, control = make_select_like_page(target)
    control.press_sequentially = AsyncMock()
    dom_elements = [{"selector": "#city", "role": target["role"], "name": "City"}]
    observe = AsyncMock(return_value=dom_elements)
    monkeypatch.setattr(browser_ops, "_get_dom_observe_elements", observe)
    option = {"selector": "#lakewood", "role": "option", "name": "Lakewood", "label": "Lakewood"}
    scoped_options = AsyncMock(side_effect=[[], [], [], [option]])
    monkeypatch.setattr(browser_ops, "_scoped_custom_options", scoped_options)
    before = {
        "text": "",
        "value": "Lakewood",
        "dataValue": "",
        "dataValues": [],
        "containerChannels": [],
        "expanded": "true",
        "optionVisible": True,
        "optionSelected": False,
    }
    evaluate_results: list[object] = [target]
    if bare_input:
        evaluate_results.append(True)
    evaluate_results.extend([before, {**before, "optionSelected": True}])
    control.evaluate.side_effect = evaluate_results
    monkeypatch.setattr(
        browser_ops,
        "time",
        SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0.5, 1.01, 1.02, 1.03])),
    )
    monkeypatch.setattr(browser_ops, "asyncio", SimpleNamespace(sleep=AsyncMock()))

    assert await do_select_option(page, "#city", "Lakewood", timeout=2000) == "Lakewood"
    assert control.fill.await_args_list == [call("Lakewood", timeout=2000), call("", timeout=2000)]
    control.press_sequentially.assert_awaited_once_with("Lakewood", timeout=2000)
    assert observe.await_count == (2 if bare_input else 1)


@pytest.mark.asyncio
async def test_do_select_option_retry_failure_restores_original_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {
        "tag": "input",
        "role": "combobox",
        "haspopup": True,
        "editable": True,
        "related": True,
    }
    page, control = make_select_like_page(target)
    control.press_sequentially = AsyncMock(side_effect=RuntimeError("keydown failed"))
    control.evaluate.side_effect = [target, "Original city", "Original city"]
    monkeypatch.setattr(browser_ops, "_scoped_custom_options", AsyncMock(side_effect=[[], []]))
    monkeypatch.setattr(browser_ops, "_get_dom_observe_elements", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        browser_ops,
        "time",
        SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 1.01])),
    )
    monkeypatch.setattr(browser_ops, "asyncio", SimpleNamespace(sleep=AsyncMock()))

    with pytest.raises(CustomSelectOpenError, match="keydown failed"):
        await do_select_option(
            page,
            "#city",
            "Lakewood",
            timeout=2000,
            restore_value_on_failure=True,
        )

    assert control.fill.await_args_list == [
        call("Lakewood", timeout=2000),
        call("", timeout=2000),
        call("Original city", timeout=1000),
    ]


@pytest.mark.asyncio
async def test_do_select_option_retry_restore_failure_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {
        "tag": "input",
        "role": "combobox",
        "haspopup": True,
        "editable": True,
        "related": True,
    }
    page, control = make_select_like_page(target)
    control.press_sequentially = AsyncMock(side_effect=RuntimeError("keydown failed"))
    control.fill.side_effect = [None, None, RuntimeError("restore blocked")]
    control.evaluate.side_effect = [target, "Original city"]
    monkeypatch.setattr(browser_ops, "_scoped_custom_options", AsyncMock(side_effect=[[], []]))
    monkeypatch.setattr(browser_ops, "_get_dom_observe_elements", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        browser_ops,
        "time",
        SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 1.01])),
    )
    monkeypatch.setattr(browser_ops, "asyncio", SimpleNamespace(sleep=AsyncMock()))

    with pytest.raises(browser_ops.CustomSelectRestoreError, match="restore original value"):
        await do_select_option(
            page,
            "#city",
            "Lakewood",
            timeout=2000,
            restore_value_on_failure=True,
        )


@pytest.mark.asyncio
async def test_scoped_custom_options_scans_all_owned_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    page = MagicMock()
    control = MagicMock()
    control.evaluate = AsyncMock(return_value=[{"selector": "#second-option", "label": "Second", "value": "second"}])
    observe = AsyncMock(
        side_effect=[
            [],
            [{"selector": "#second-option", "role": "option", "name": "Second"}],
        ]
    )
    monkeypatch.setattr(browser_ops, "_get_dom_observe_elements", observe)

    options = await browser_ops._scoped_custom_options(
        page,
        control,
        scan_selectors=["#first-root", "#second-root"],
    )

    assert options == [
        {"selector": "#second-option", "role": "option", "name": "Second", "label": "Second", "value": "second"}
    ]
    assert [item.args[1] for item in observe.await_args_list] == ["#first-root", "#second-root"]


@pytest.mark.asyncio
async def test_do_select_option_surfaces_option_click_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    target = {"tag": "button", "role": "combobox", "haspopup": True, "editable": False}
    page, control = make_select_like_page(target)
    monkeypatch.setattr(
        browser_ops,
        "_get_dom_observe_elements",
        AsyncMock(return_value=[{"selector": "#west", "role": "option", "name": "West"}]),
    )
    control.evaluate.side_effect = [
        target,
        [{"selector": "#west", "label": "West", "value": "west"}],
        {"text": "Choose", "value": "", "dataValues": [], "expanded": "true", "optionSelected": False},
    ]
    control.click.side_effect = [None, RuntimeError("click intercepted by overlay")]
    monkeypatch.setattr(browser_ops, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 1])))
    monkeypatch.setattr(browser_ops, "asyncio", SimpleNamespace(sleep=AsyncMock()))

    with pytest.raises(RuntimeError, match="click intercepted by overlay"):
        await do_select_option(page, "#state", "west", timeout=100)


@pytest.mark.asyncio
async def test_do_select_option_scan_observed_control_without_options_uses_bounded_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {
        "tag": "button",
        "role": "",
        "haspopup": False,
        "editable": False,
        "selectlike": True,
        "optionish": False,
        "related": True,
    }
    page, control = make_select_like_page(target)
    dom_elements = [{"selector": "#empty", "role": "button", "name": "Empty"}]
    monkeypatch.setattr(browser_ops, "_get_dom_observe_elements", AsyncMock(return_value=dom_elements))
    scoped_options = AsyncMock(return_value=[])
    monkeypatch.setattr(browser_ops, "_scoped_custom_options", scoped_options)
    control.evaluate.side_effect = [target, True]
    monkeypatch.setattr(browser_ops, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 4])))
    monkeypatch.setattr(browser_ops, "asyncio", SimpleNamespace(sleep=AsyncMock()))

    result = await do_select_option(page, "#empty", "anything", timeout=30000)

    assert result is None
    scoped_options.assert_awaited_once()


@pytest.mark.asyncio
async def test_skyvern_select_option_no_match_preserves_structured_option_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, native_select_option = make_select_option_page()
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    observed_options = ["Books", "Music", "Games"]
    monkeypatch.setattr(
        mcp_browser,
        "do_select_option",
        AsyncMock(side_effect=CustomSelectMatchError("#category", "podcasts", observed_options)),
        raising=False,
    )

    result = await mcp_browser.skyvern_select_option(
        selector="#category",
        value="podcasts",
        selector_mode="direct",
        timeout=4321,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.ACTION_FAILED
    assert result["error"]["details"] == {
        "element_state": "no_unambiguous_match",
        "selector": "#category",
        "requested_option": "podcasts",
        "observed_options": observed_options,
    }
    assert result["error"]["hint"]
    native_select_option.assert_not_awaited()


@pytest.mark.asyncio
async def test_skyvern_select_option_custom_failure_preserves_hybrid_ai_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, native_select_option = make_select_option_page()
    monkeypatch.setattr(
        mcp_browser,
        "get_page",
        AsyncMock(return_value=(page, BrowserContext(mode="cloud_session", session_id="pbs_test"))),
    )
    monkeypatch.setattr(
        mcp_browser,
        "do_select_option",
        AsyncMock(side_effect=CustomSelectMatchError("#category", "podcasts", ["Music"])),
    )

    result = await mcp_browser.skyvern_select_option(
        selector="#category", intent="the category dropdown", value="podcasts"
    )

    assert result["ok"] is True
    native_select_option.assert_awaited_once_with(
        selector="#category", value="podcasts", prompt="the category dropdown", ai="fallback", timeout=30000
    )


@pytest.mark.asyncio
async def test_skyvern_select_option_kill_switch_skips_custom_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, native_select_option = make_select_option_page()
    monkeypatch.setattr(
        mcp_browser,
        "get_page",
        AsyncMock(return_value=(page, BrowserContext(mode="cloud_session", session_id="pbs_test"))),
    )
    do_select = AsyncMock(side_effect=AssertionError("custom path must not run when disabled"))
    monkeypatch.setattr(mcp_browser, "do_select_option", do_select)
    monkeypatch.setenv("SKYVERN_DISABLE_CUSTOM_SELECT", "1")

    result = await mcp_browser.skyvern_select_option(
        selector="#category", intent="the category dropdown", value="music"
    )

    assert result["ok"] is True
    do_select.assert_not_awaited()
    native_select_option.assert_awaited_once()


@pytest.mark.asyncio
async def test_skyvern_select_option_open_failure_preserves_hybrid_ai_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, native_select_option = make_select_option_page()
    monkeypatch.setattr(
        mcp_browser,
        "get_page",
        AsyncMock(return_value=(page, BrowserContext(mode="cloud_session", session_id="pbs_test"))),
    )
    monkeypatch.setattr(
        mcp_browser,
        "do_select_option",
        AsyncMock(side_effect=CustomSelectOpenError("click intercepted")),
    )

    result = await mcp_browser.skyvern_select_option(
        selector="#category", intent="the category dropdown", value="music"
    )

    assert result["ok"] is True
    native_select_option.assert_awaited_once()


@pytest.mark.asyncio
async def test_skyvern_select_option_open_failure_terminal_for_direct_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, native_select_option = make_select_option_page()
    monkeypatch.setattr(
        mcp_browser,
        "get_page",
        AsyncMock(return_value=(page, BrowserContext(mode="cloud_session", session_id="pbs_test"))),
    )
    monkeypatch.setattr(
        mcp_browser,
        "do_select_option",
        AsyncMock(side_effect=CustomSelectOpenError("click intercepted")),
    )

    result = await mcp_browser.skyvern_select_option(selector="#category", value="music", selector_mode="direct")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.ACTION_FAILED
    assert result["error"]["message"] == "click intercepted"
    native_select_option.assert_not_awaited()


@pytest.mark.asyncio
async def test_do_select_option_open_click_failure_raises_open_error() -> None:
    page, control = make_select_like_page({"tag": "div", "role": "combobox", "haspopup": True, "editable": False})
    control.click = AsyncMock(side_effect=RuntimeError("intercepted"))

    with pytest.raises(CustomSelectOpenError):
        await do_select_option(page, "#x", "music", timeout=500)


@pytest.mark.asyncio
async def test_skyvern_select_option_credential_intent_rejected_before_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_page = AsyncMock(side_effect=AssertionError("get_page must not run for a credential intent"))
    monkeypatch.setattr(mcp_browser, "get_page", get_page)

    result = await mcp_browser.skyvern_select_option(intent="select the account password", value="s3cr3t")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_skyvern_select_option_classify_error_fails_closed_for_hybrid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, native_select_option = make_select_option_page()
    monkeypatch.setattr(
        mcp_browser,
        "get_page",
        AsyncMock(return_value=(page, BrowserContext(mode="cloud_session", session_id="pbs_test"))),
    )
    monkeypatch.setattr(mcp_browser, "do_select_option", AsyncMock(side_effect=CustomSelectClassifyError("#x")))

    # Hybrid selector+intent: a mid-probe detach must not forward the value to the AI fallback.
    hybrid = await mcp_browser.skyvern_select_option(selector="#x", intent="the dropdown", value="leak-me")
    assert hybrid["ok"] is False
    assert hybrid["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    native_select_option.assert_not_awaited()

    # Direct call defers to the native SDK (which cannot forward the value to an LLM).
    monkeypatch.setattr(mcp_browser, "do_select_option", AsyncMock(side_effect=CustomSelectClassifyError("#x")))
    direct = await mcp_browser.skyvern_select_option(selector="#x", value="east", selector_mode="direct")
    assert direct["ok"] is True
    native_select_option.assert_awaited()


@pytest.mark.asyncio
async def test_skyvern_select_option_password_target_never_reaches_native_or_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, native_select_option = make_select_option_page()
    monkeypatch.setattr(
        mcp_browser,
        "get_page",
        AsyncMock(return_value=(page, BrowserContext(mode="cloud_session", session_id="pbs_test"))),
    )
    monkeypatch.setattr(
        mcp_browser,
        "do_select_option",
        AsyncMock(side_effect=CustomSelectPasswordError("#pw")),
    )

    # Non-credential intent so this exercises the do_select_option CustomSelectPasswordError
    # wrapper (a password TARGET), not the earlier credential-intent preflight.
    result = await mcp_browser.skyvern_select_option(selector="#pw", intent="the flavor dropdown", value="s3cr3t")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
    native_select_option.assert_not_awaited()


@pytest.mark.asyncio
async def test_skyvern_select_option_post_option_failure_skips_hybrid_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, native_select_option = make_select_option_page()
    monkeypatch.setattr(
        mcp_browser,
        "get_page",
        AsyncMock(return_value=(page, BrowserContext(mode="cloud_session", session_id="pbs_test"))),
    )
    do_select = AsyncMock(side_effect=RuntimeError("Custom select did not commit 'podcasts'"))
    monkeypatch.setattr(mcp_browser, "do_select_option", do_select)

    result = await mcp_browser.skyvern_select_option(
        selector="#category", intent="the category dropdown", value="podcasts"
    )

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.ACTION_FAILED
    assert result["error"]["message"] == "Custom select did not commit 'podcasts'"
    native_select_option.assert_not_awaited()
    assert do_select.await_args.kwargs["restore_value_on_failure"] is True


@pytest.mark.asyncio
async def test_skyvern_select_option_restore_failure_skips_hybrid_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, native_select_option = make_select_option_page()
    monkeypatch.setattr(
        mcp_browser,
        "get_page",
        AsyncMock(return_value=(page, BrowserContext(mode="cloud_session", session_id="pbs_test"))),
    )
    monkeypatch.setattr(
        mcp_browser,
        "do_select_option",
        AsyncMock(side_effect=browser_ops.CustomSelectRestoreError("Could not restore original value")),
    )

    result = await mcp_browser.skyvern_select_option(
        selector="#category", intent="the category dropdown", value="podcasts"
    )

    assert result["ok"] is False
    assert result["error"]["message"] == "Could not restore original value"
    native_select_option.assert_not_awaited()


@pytest.mark.asyncio
async def test_skyvern_select_option_direct_call_does_not_request_value_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, _ = make_select_option_page()
    monkeypatch.setattr(
        mcp_browser,
        "get_page",
        AsyncMock(return_value=(page, BrowserContext(mode="cloud_session", session_id="pbs_test"))),
    )
    do_select = AsyncMock(return_value="Music")
    monkeypatch.setattr(mcp_browser, "do_select_option", do_select)

    result = await mcp_browser.skyvern_select_option(selector="#category", value="music", selector_mode="direct")

    assert result["ok"] is True
    assert do_select.await_args.kwargs["restore_value_on_failure"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("disable_custom", [False, True], ids=["classifier-none", "kill-switch"])
async def test_skyvern_select_option_pure_native_timing_matches_origin(
    monkeypatch: pytest.MonkeyPatch,
    disable_custom: bool,
) -> None:
    class FakeTimer:
        clock_ms = 0

        def __init__(self) -> None:
            self.started_ms = 0
            self.marks: dict[str, int] = {}

        def __enter__(self) -> FakeTimer:
            self.started_ms = self.clock_ms
            return self

        def __exit__(self, *_args: object) -> None:
            self.marks["total"] = self.clock_ms - self.started_ms

        def mark(self, name: str) -> None:
            self.marks[name] = self.clock_ms - self.started_ms

        @property
        def timing_ms(self) -> dict[str, int]:
            return self.marks.copy()

    async def classify_native(*_args: object, **_kwargs: object) -> None:
        FakeTimer.clock_ms += 100
        return None

    async def select_native(*_args: object, **_kwargs: object) -> str:
        FakeTimer.clock_ms += 200
        return "east"

    page, _ = make_select_option_page()
    page.select_option = AsyncMock(side_effect=select_native)
    monkeypatch.setattr(
        mcp_browser,
        "get_page",
        AsyncMock(return_value=(page, BrowserContext(mode="cloud_session", session_id="pbs_test"))),
    )
    custom_select = AsyncMock(side_effect=classify_native)
    monkeypatch.setattr(mcp_browser, "do_select_option", custom_select)
    monkeypatch.setattr(mcp_browser, "Timer", FakeTimer)
    if disable_custom:
        monkeypatch.setenv("SKYVERN_DISABLE_CUSTOM_SELECT", "1")
    else:
        monkeypatch.delenv("SKYVERN_DISABLE_CUSTOM_SELECT", raising=False)

    result = await mcp_browser.skyvern_select_option(
        selector="#region",
        value="east",
        selector_mode="direct",
    )

    assert result["ok"] is True
    assert result["timing_ms"] == {"sdk": 200, "total": 200}

    async def reject_native(*_args: object, **_kwargs: object) -> None:
        FakeTimer.clock_ms += 200
        raise RuntimeError("invalid native option")

    FakeTimer.clock_ms = 0
    page.select_option = AsyncMock(side_effect=reject_native)
    failed = await mcp_browser.skyvern_select_option(
        selector="#region",
        value="missing",
        selector_mode="direct",
    )

    assert failed["ok"] is False
    assert failed["timing_ms"] == {}
    if disable_custom:
        custom_select.assert_not_awaited()
    else:
        assert custom_select.await_count == 2


@pytest.mark.asyncio
async def test_skyvern_select_option_hybrid_timing_includes_custom_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTimer:
        clock_ms = 0

        def __init__(self) -> None:
            self.started_ms = 0
            self.marks: dict[str, int] = {}

        def __enter__(self) -> FakeTimer:
            self.started_ms = self.clock_ms
            return self

        def __exit__(self, *_args: object) -> None:
            self.marks["total"] = self.clock_ms - self.started_ms

        def mark(self, name: str) -> None:
            self.marks[name] = self.clock_ms - self.started_ms

        @property
        def timing_ms(self) -> dict[str, int]:
            return self.marks.copy()

    async def fail_custom(*_args: object, **_kwargs: object) -> None:
        FakeTimer.clock_ms += 100
        raise CustomSelectMatchError("#category", "podcasts", ["Music"])

    async def select_fallback(*_args: object, **_kwargs: object) -> str:
        FakeTimer.clock_ms += 200
        return "podcasts"

    page, _ = make_select_option_page()
    page.select_option = AsyncMock(side_effect=select_fallback)
    monkeypatch.setattr(
        mcp_browser,
        "get_page",
        AsyncMock(return_value=(page, BrowserContext(mode="cloud_session", session_id="pbs_test"))),
    )
    monkeypatch.setattr(mcp_browser, "do_select_option", AsyncMock(side_effect=fail_custom))
    monkeypatch.setattr(mcp_browser, "Timer", FakeTimer)

    result = await mcp_browser.skyvern_select_option(
        selector="#category", intent="the category dropdown", value="podcasts"
    )

    assert result["ok"] is True
    assert result["timing_ms"]["total"] == 300

    async def fail_fallback(*_args: object, **_kwargs: object) -> None:
        FakeTimer.clock_ms += 200
        raise RuntimeError("fallback failed")

    FakeTimer.clock_ms = 0
    page.select_option = AsyncMock(side_effect=fail_fallback)
    failed = await mcp_browser.skyvern_select_option(
        selector="#category", intent="the category dropdown", value="podcasts"
    )

    assert failed["ok"] is False
    assert failed["timing_ms"]["total"] == 300


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


def _write_grid_page(*, focused_is_password: bool) -> tuple[SimpleNamespace, SimpleNamespace]:
    cdp = SimpleNamespace(send=AsyncMock(), detach=AsyncMock())
    raw_page = SimpleNamespace(context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=cdp)))
    page = SimpleNamespace(
        page=raw_page,
        click=AsyncMock(),
        evaluate=AsyncMock(return_value=focused_is_password),
    )
    page.locator_scope = raw_page
    return page, cdp


@pytest.mark.asyncio
async def test_write_grid_password_focus_selector_preflight_before_session(monkeypatch: pytest.MonkeyPatch) -> None:
    get_page = AsyncMock(side_effect=AssertionError("get_page should not be called for a credential target"))
    monkeypatch.setattr(mcp_cdp_input, "get_page", get_page)

    result = await mcp_cdp_input.skyvern_write_grid(rows=[["a"]], focus_selector="#login-password")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_cdp_input.ErrorCode.INVALID_INPUT
    get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_grid_blocks_when_focus_lands_on_a_password_input(monkeypatch: pytest.MonkeyPatch) -> None:
    # skyvern_type checks the DOM type of its target; write_grid types into whatever holds focus,
    # so the equivalent guard is on the focused element rather than on a named selector.
    page, cdp = _write_grid_page(focused_is_password=True)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_cdp_input, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_cdp_input.skyvern_write_grid(rows=[["s3cr3t"]], focus_selector="#A1", screenshot=False)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_cdp_input.ErrorCode.INVALID_INPUT
    assert not any(c.args[0] == "Input.insertText" for c in cdp.send.await_args_list)
    cdp.detach.assert_awaited()


@pytest.mark.asyncio
async def test_write_grid_writes_when_focus_is_not_a_password_input(monkeypatch: pytest.MonkeyPatch) -> None:
    page, cdp = _write_grid_page(focused_is_password=False)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_cdp_input, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_cdp_input.skyvern_write_grid(rows=[["a", "b"]], focus_selector="#A1", screenshot=False)

    assert result["ok"] is True, result
    assert result["data"]["cells"] == 2
    inserted = [c.args[1]["text"] for c in cdp.send.await_args_list if c.args[0] == "Input.insertText"]
    assert inserted == ["a", "b"]


@pytest.mark.asyncio
async def test_write_grid_blocks_password_field_reached_after_first_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    page, cdp = _write_grid_page(focused_is_password=False)
    page.evaluate = AsyncMock(side_effect=[False, True])
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_cdp_input, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_cdp_input.skyvern_write_grid(rows=[["allowed", "blocked"]], screenshot=False)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_cdp_input.ErrorCode.INVALID_INPUT
    inserted = [call.args[1]["text"] for call in cdp.send.await_args_list if call.args[0] == "Input.insertText"]
    assert inserted == ["allowed"]


@pytest.mark.asyncio
async def test_write_grid_password_check_exception_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    page, cdp = _write_grid_page(focused_is_password=False)
    page.evaluate = AsyncMock(side_effect=RuntimeError("evaluation failed"))
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_cdp_input, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_cdp_input.skyvern_write_grid(rows=[["blocked"]], screenshot=False)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_cdp_input.ErrorCode.INVALID_INPUT
    assert not any(call.args[0] == "Input.insertText" for call in cdp.send.await_args_list)


@pytest.mark.asyncio
async def test_write_grid_rejects_oversize_row_before_session(monkeypatch: pytest.MonkeyPatch) -> None:
    get_page = AsyncMock(side_effect=AssertionError("get_page must not run"))
    monkeypatch.setattr(mcp_cdp_input, "get_page", get_page)

    result = await mcp_cdp_input.skyvern_write_grid(rows=[["cell"] * 51], screenshot=False)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_cdp_input.ErrorCode.INVALID_INPUT
    get_page.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "extra_kwargs"),
    [
        pytest.param("skyvern_navigate_and_screenshot", {}, id="navigate-and-screenshot"),
        pytest.param(
            "skyvern_navigate_extract_and_screenshot", {"prompt": "read the page"}, id="navigate-extract-screenshot"
        ),
    ],
)
async def test_navigating_paired_tools_invalidate_refs_around_navigation(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, extra_kwargs: dict[str, object]
) -> None:
    """Parity with skyvern_navigate: prior refs cannot survive navigation.

    Invalidate before AND after, because a failed goto can partially replace the
    document and a concurrent observe can publish while navigation is in flight.
    """
    _sdk_equivalent_page(monkeypatch)
    events: list[str] = []

    async def navigate(*_args: object, **_kwargs: object) -> NavigateResult:
        events.append("navigate")
        return NavigateResult(url="https://example.test", title="Example", load_state="load")

    monkeypatch.setattr(mcp_browser, "do_navigate", AsyncMock(side_effect=navigate))
    monkeypatch.setattr(mcp_browser, "do_screenshot", AsyncMock(return_value=SimpleNamespace(data=b"png")))
    monkeypatch.setattr(mcp_browser, "do_extract", AsyncMock(return_value=SimpleNamespace(extracted={"value": 1})))
    monkeypatch.setattr(
        mcp_browser,
        "save_artifact",
        Mock(return_value=Artifact(kind="screenshot", path="/tmp/shot.png", mime="image/png", bytes=3)),
    )
    invalidate = Mock(side_effect=lambda **_kwargs: events.append("invalidate"))
    monkeypatch.setattr(mcp_browser, "invalidate_session_ref_map", invalidate)

    result = await getattr(mcp_browser, tool_name)(url="https://example.test", **extra_kwargs)

    assert result["ok"] is True, result
    assert events == ["invalidate", "navigate", "invalidate"]


@pytest.mark.asyncio
async def test_navigate_reports_ok_with_a_warning_when_the_page_settles_below_the_requested_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page that never fires `load` is navigated, not failed — say so instead of ACTION_FAILED."""
    _sdk_equivalent_page(monkeypatch)
    monkeypatch.setattr(
        mcp_browser,
        "do_navigate",
        AsyncMock(return_value=NavigateResult(url="https://example.test", title="Example", load_state="commit")),
    )

    result = await mcp_browser.skyvern_navigate(url="https://example.test")

    assert result["ok"] is True, result
    assert result["data"]["load_state"] == "commit"
    assert result["data"]["url"] == "https://example.test"
    assert any("never reached 'load'" in warning for warning in result["warnings"]), result


@pytest.mark.asyncio
async def test_navigate_does_not_warn_when_the_requested_state_is_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sdk_equivalent_page(monkeypatch)
    monkeypatch.setattr(
        mcp_browser,
        "do_navigate",
        AsyncMock(
            return_value=NavigateResult(url="https://example.test", title="Example", load_state="domcontentloaded")
        ),
    )

    result = await mcp_browser.skyvern_navigate(url="https://example.test", wait_until="domcontentloaded")

    assert result["ok"] is True, result
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_navigating_paired_tools_surface_the_degraded_load_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sdk_equivalent_page(monkeypatch)
    monkeypatch.setattr(
        mcp_browser,
        "do_navigate",
        AsyncMock(return_value=NavigateResult(url="https://example.test", title="Example", load_state="commit")),
    )
    monkeypatch.setattr(mcp_browser, "do_screenshot", AsyncMock(return_value=SimpleNamespace(data=b"png")))
    monkeypatch.setattr(
        mcp_browser,
        "save_artifact",
        Mock(return_value=Artifact(kind="screenshot", path="/tmp/shot.png", mime="image/png", bytes=3)),
    )

    result = await mcp_browser.skyvern_navigate_and_screenshot(url="https://example.test")

    assert result["ok"] is True, result
    assert result["data"]["load_state"] == "commit"
    assert any("never reached 'load'" in warning for warning in result["warnings"]), result


@pytest.mark.asyncio
async def test_navigating_paired_tools_invalidate_refs_even_when_navigation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sdk_equivalent_page(monkeypatch)
    events: list[str] = []

    async def fail_navigation(*_args: object, **_kwargs: object) -> NavigateResult:
        events.append("navigate")
        raise RuntimeError("net::ERR_ABORTED")

    monkeypatch.setattr(mcp_browser, "do_navigate", AsyncMock(side_effect=fail_navigation))
    invalidate = Mock(side_effect=lambda **_kwargs: events.append("invalidate"))
    monkeypatch.setattr(mcp_browser, "invalidate_session_ref_map", invalidate)

    result = await mcp_browser.skyvern_navigate_and_screenshot(url="https://example.test")

    assert result["ok"] is False
    assert events == ["invalidate", "navigate", "invalidate"]


@pytest.mark.asyncio
async def test_navigate_and_screenshot_returns_session_expired_result_for_cdp_4408(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # session_manager.get_page() (SKY-14282/14283/14307/14279 fix) now always wraps a
    # connect/session-lifecycle failure in BrowserSessionConnectionError before it reaches a
    # caller -- mock at that same shape rather than a bare RuntimeError so this test still
    # reflects what get_page() can actually raise.
    expired_error = session_manager.BrowserSessionConnectionError(
        "BrowserType.connect_over_cdp: Target page, context or browser has been closed\n"
        "Browser logs: session expired\n"
        "Call log: <ws disconnected> code=4408 reason=session expired"
    )
    get_page = AsyncMock(side_effect=expired_error)
    monkeypatch.setattr(mcp_browser, "get_page", get_page)

    result = await mcp_browser.skyvern_navigate_and_screenshot(url="https://example.test")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.SESSION_EXPIRED
    assert result["error"]["message"] == "Browser session expired or closed."
    assert "new browser session" in result["error"]["hint"]
    get_page.assert_awaited_once_with(session_id=None, cdp_url=None)


@pytest.mark.asyncio
async def test_navigate_and_screenshot_returns_sdk_error_result_for_unclassified_connect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Before the fix, a connection failure that didn't match the narrow 4408 signature
    # bubbled out of _run_paired_capture as a raw exception -- past every MCP tool's
    # `except BrowserNotAvailableError` guard and out to the client as an unsanitized
    # fastmcp ToolError (SKY-14283/SKY-14279), including the raw session-router hostname and
    # nginx response body for 14283. It must now degrade to a structured envelope, never raise.
    # This shape (no target-closed marker) isn't a dead session -- SESSION_EXPIRED would be
    # misleading, and so would the generic "no browser" hint to pass session_id, since one was
    # already given -- so it gets a distinct SDK_ERROR with a fully sanitized detail.
    other_close = session_manager.BrowserSessionConnectionError(
        "BrowserType.connect_over_cdp: WebSocket error: wss://sessions.skyvern.com/pbs_123/tok "
        "401 Unauthorized <html><body>nginx</body></html> "
        "Call log: <ws disconnected> code=4401 reason=unauthorized"
    )
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(side_effect=other_close))

    result = await mcp_browser.skyvern_navigate_and_screenshot(url="https://example.test")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.SDK_ERROR
    assert "sessions.skyvern.com" not in result["error"]["message"]
    assert "wss://" not in result["error"]["message"]
    assert "nginx" not in result["error"]["message"]
    assert "code=4401" not in result["error"]["message"]
    assert "transient" in result["error"]["hint"]


@pytest.mark.asyncio
async def test_navigate_and_screenshot_returns_sdk_error_with_type_name_for_empty_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SKY-14279: the underlying failure can be an exception with an empty str() (e.g. an
    # httpx.ReadError on the loopback call that fetches the session row, before any CDP connect
    # is even attempted). The tool must still report something identifiable, not a blank message.
    class ReadError(Exception):
        pass

    blank_error = session_manager._wrap_browser_connection_failure(ReadError())
    assert blank_error.raw_detail == "ReadError"
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(side_effect=blank_error))

    result = await mcp_browser.skyvern_navigate_and_screenshot(url="https://example.test")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.SDK_ERROR
    assert "ReadError" in result["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "call_kwargs", "raw_detail", "expected_code"),
    [
        # SKY-14307: skyvern_navigate, driver connection severed -- target-closed family.
        pytest.param(
            "skyvern_navigate",
            {"url": "https://example.test"},
            "BrowserType.connect_over_cdp: Connection closed while reading from the driver",
            "SESSION_EXPIRED",
            id="sky-14307-navigate-driver-closed",
        ),
        # SKY-14283: skyvern_screenshot, session-router rejected a stale cdp_url with a raw
        # nginx 401 body -- not a target-closed marker, so it's the sanitized SDK_ERROR bucket.
        pytest.param(
            "skyvern_screenshot",
            {},
            (
                "BrowserType.connect_over_cdp: WebSocket error: "
                "wss://sessions.skyvern.com/pbs_563919350820380452/MTAuMC4zNy4xNjg6cGJz.659ded3d7b0cfbb0/"
                "devtools/browser/04d5140b-cf5d-4b7e-864b-3b3d9b6cb015 401 Unauthorized "
                "<html><head><title>401 Authorization Required</title></head>"
                "<body><center><h1>401 Authorization Required</h1></center>"
                "<hr><center>nginx</center></body></html>"
            ),
            "SDK_ERROR",
            id="sky-14283-screenshot-nginx-401",
        ),
        # SKY-14279: skyvern_click, the underlying failure had no message at all.
        pytest.param(
            "skyvern_click",
            {"selector": "#submit"},
            "",
            "SDK_ERROR",
            id="sky-14279-click-empty-message",
        ),
        # SKY-14282: skyvern_navigate_extract_and_screenshot, the composite/paired-capture path.
        pytest.param(
            "skyvern_navigate_extract_and_screenshot",
            {"url": "https://example.test", "prompt": "get the title"},
            "BrowserContext.new_page: Target page, context or browser has been closed",
            "SESSION_EXPIRED",
            id="sky-14282-navigate-extract-and-screenshot-target-closed",
        ),
    ],
)
async def test_atomic_browser_tools_never_leak_raw_connect_failures(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    call_kwargs: dict[str, object],
    raw_detail: str,
    expected_code: str,
) -> None:
    # Reproduces SKY-14307/14283/14279 end to end on the exact atomic tools they were filed
    # against: before the fix, get_page() let the raw driver/CDP exception escape uncaught past
    # this tool's `except BrowserNotAvailableError` guard, and fastmcp surfaced it to the MCP
    # client as an unsanitized ToolError carrying the raw text (including, for 14283, the
    # session-router hostname and a raw nginx response body).
    error = (
        session_manager.BrowserSessionConnectionError(raw_detail)
        if raw_detail
        else (session_manager._wrap_browser_connection_failure(Exception()))
    )
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(side_effect=error))

    tool = getattr(mcp_browser, tool_name)
    result = await tool(**call_kwargs)

    assert result["ok"] is False
    assert result["error"]["code"] == getattr(mcp_browser.ErrorCode, expected_code)
    assert "sessions.skyvern.com" not in result["error"]["message"]
    assert "wss://" not in result["error"]["message"]
    assert "nginx" not in result["error"]["message"]
    assert "401 Authorization Required" not in result["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "call_kwargs", "expected_steps"),
    [
        pytest.param(
            "skyvern_navigate_and_screenshot",
            {
                "url": "https://example.test",
                "timeout": 45000,
                "wait_until": "domcontentloaded",
                "full_page": True,
                "inline": True,
            },
            [
                {
                    "tool": "navigate",
                    "params": {
                        "url": "https://example.test",
                        "timeout": 45000,
                        "wait_until": "domcontentloaded",
                    },
                },
                {"tool": "screenshot", "params": {"full_page": True, "inline": True}},
            ],
            id="navigate",
        ),
        pytest.param(
            "skyvern_evaluate_and_screenshot",
            {"expression": "document.title", "full_page": True, "inline": True},
            [
                {"tool": "evaluate", "params": {"expression": "document.title"}},
                {"tool": "screenshot", "params": {"full_page": True, "inline": True}},
            ],
            id="evaluate",
        ),
        pytest.param(
            "skyvern_extract_and_screenshot",
            {"prompt": "read the page"},
            [
                {"tool": "extract", "params": {"prompt": "read the page", "schema": None}},
                {"tool": "screenshot", "params": {"full_page": False, "inline": False}},
            ],
            id="extract",
        ),
        pytest.param(
            "skyvern_navigate_extract_and_screenshot",
            {"url": "https://example.test", "prompt": "read the page"},
            [
                {"tool": "navigate", "params": {"url": "https://example.test", "timeout": 30000, "wait_until": None}},
                {"tool": "extract", "params": {"prompt": "read the page", "schema": None}},
                {"tool": "screenshot", "params": {"full_page": False, "inline": False}},
            ],
            id="navigate-extract",
        ),
    ],
)
async def test_paired_tools_delegate_to_existing_primitives_and_log_the_paired_action(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    call_kwargs: dict[str, object],
    expected_steps: list[dict[str, object]],
) -> None:
    _sdk_equivalent_page(monkeypatch)
    expected_operations = tuple(f"skyvern_{step['tool']}" for step in expected_steps)
    operation_mocks = {
        name: AsyncMock(return_value={"ok": True, "data": {"sdk_equivalent": f"{name}()"}})
        for name in expected_operations
    }
    operation_mocks["skyvern_screenshot"].return_value = {
        "data": {"path": "/tmp/shot.png", "sdk_equivalent": "skyvern_screenshot()"},
        "artifacts": [{"kind": "screenshot", "path": "/tmp/shot.png"}],
    }
    for name, operation in operation_mocks.items():
        monkeypatch.setattr(mcp_browser, name, operation)
    enqueue = Mock()
    monkeypatch.setattr(mcp_browser, "enqueue_action_event", enqueue)

    result = await getattr(mcp_browser, tool_name)(
        **call_kwargs,
        session_id="pbs_test",
        cdp_url="ws://browser.test",
    )

    assert result["ok"] is True
    assert result["action"] == tool_name
    assert result["data"]["screenshot"]["path"] == "/tmp/shot.png"
    assert result["data"]["sdk_equivalent"] == "; ".join(f"{name}()" for name in expected_operations)
    assert result["artifacts"][0]["path"] == "/tmp/shot.png"
    for name, step in zip(expected_operations, expected_steps, strict=True):
        operation_mocks[name].assert_awaited_once_with(
            **step.get("params", {}), session_id="pbs_test", cdp_url="ws://browser.test"
        )
    assert any(call.kwargs["tool"] == tool_name for call in enqueue.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "call_kwargs", "failed_operation", "error_code"),
    [
        ("skyvern_extract_and_screenshot", {"prompt": "read"}, "skyvern_extract", "SDK_ERROR"),
        ("skyvern_evaluate_and_screenshot", {"expression": "throw 1"}, "skyvern_evaluate", "ACTION_FAILED"),
    ],
)
async def test_paired_read_failure_keeps_screenshot_and_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    call_kwargs: dict[str, object],
    failed_operation: str,
    error_code: str,
) -> None:
    _sdk_equivalent_page(monkeypatch)
    screenshot = AsyncMock(
        return_value={
            "ok": True,
            "data": {"path": "/tmp/shot.png"},
            "artifacts": [{"kind": "screenshot", "path": "/tmp/shot.png"}],
        }
    )
    monkeypatch.setattr(
        mcp_browser, failed_operation, AsyncMock(return_value={"ok": False, "error": {"code": error_code}})
    )
    monkeypatch.setattr(mcp_browser, "skyvern_screenshot", screenshot)

    result = await getattr(mcp_browser, tool_name)(**call_kwargs)

    assert result["ok"] is False
    assert result["error"]["code"] == error_code
    assert result["data"]["extracted" if failed_operation == "skyvern_extract" else "result"] is None
    assert result["data"]["screenshot"]["path"] == "/tmp/shot.png"
    assert result["artifacts"][0]["path"] == "/tmp/shot.png"


@pytest.mark.asyncio
async def test_inline_screenshot_is_also_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    _sdk_equivalent_page(monkeypatch)
    monkeypatch.setattr(mcp_browser, "do_screenshot", AsyncMock(return_value=SimpleNamespace(data=b"png")))
    artifact = Artifact(kind="screenshot", path="/tmp/shot.png", mime="image/png", bytes=3)
    monkeypatch.setattr(mcp_browser, "save_artifact", Mock(return_value=artifact))

    result = await mcp_browser.skyvern_screenshot(inline=True)

    assert result["data"]["path"] == artifact.path
    assert result["artifacts"] == [artifact.to_dict()]


def _either_wait_page(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: dict[str, float | Exception],
) -> list[asyncio.Task]:
    """Fake a page whose per-selector waits resolve after a delay, raise, or time out: an outcome of
    None means the selector never appears, so that waiter times out the way Playwright's does. Returns
    the waiter tasks so a caller can assert the tool drained them.
    """
    waiters: list[asyncio.Task] = []

    async def wait_for_selector(selector: str, timeout: float = 30000, **_: object) -> SimpleNamespace:
        task = asyncio.current_task()
        if task is not None:
            waiters.append(task)
        outcome = outcomes.get(selector)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            await asyncio.sleep(timeout / 1000)
            raise mcp_browser.PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded waiting for {selector}")
        await asyncio.sleep(outcome)
        return SimpleNamespace(selector=selector)

    _action_page(monkeypatch, wait_for_selector=AsyncMock(side_effect=wait_for_selector))
    return waiters


@pytest.mark.asyncio
async def test_wait_timeout_on_one_selector_points_at_the_two_state_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """The timeout is the moment the caller learns its guess was wrong — the error is where MCP puts
    feedback a model can act on, so it names the form that would not have cost the ceiling."""
    _either_wait_page(monkeypatch, {"#login": None})

    result = await mcp_browser.skyvern_wait(selector="#login", timeout=1000)

    assert result["ok"] is False
    assert "skyvern_wait_for_either_state" in result["error"]["hint"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("appears", "expected"), [("#login", "selector_a"), ("#home", "selector_b")])
async def test_wait_for_either_state_names_the_side_that_matched(
    monkeypatch: pytest.MonkeyPatch,
    appears: str,
    expected: str,
) -> None:
    """One tool, one operation: both states are required, so the answer is always which one it is."""
    absent = "#home" if appears == "#login" else "#login"
    _either_wait_page(monkeypatch, {appears: 0.01, absent: None})

    result = await mcp_browser.skyvern_wait_for_either_state(selector_a="#login", selector_b="#home", timeout=120000)

    assert result["ok"] is True
    assert result["data"]["matched_selector"] == appears
    assert result["data"]["matched"] == expected
    assert isinstance(result["data"]["observed_wait_ms"], int)
    assert result["data"]["source_url"] == "https://example.test/two-factor"
    assert result["data"]["result_url"] == "https://example.test/two-factor"


@pytest.mark.asyncio
async def test_wait_for_either_state_reports_the_observed_failed_wait_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _either_wait_page(monkeypatch, {"#login": None, "#home": None})

    result = await mcp_browser.skyvern_wait_for_either_state(
        selector_a="#login",
        selector_b="#home",
        timeout=1000,
    )

    assert result["ok"] is False
    assert result["data"]["observed_wait_ms"] >= 1000
    assert result["data"]["source_url"] == "https://example.test/two-factor"
    assert result["data"]["result_url"] == "https://example.test/two-factor"


@pytest.mark.asyncio
async def test_wait_for_either_state_rejects_states_where_absence_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _either_wait_page(monkeypatch, {"#login": 0.01, "#home": 0.01})

    result = await mcp_browser.skyvern_wait_for_either_state(selector_a="#a", selector_b="#b", state="hidden")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("concise", [True, False])
def test_either_state_output_schema_accepts_both_envelope_modes(concise: bool) -> None:
    """Declared schemas are enforced at runtime, and the two envelope modes disagree on absent fields."""
    import jsonschema

    from skyvern.cli.core.result import set_concise_responses

    set_concise_responses(concise)
    try:
        for envelope in (
            mcp_browser.make_result(
                "skyvern_wait_for_either_state",
                browser_context=BrowserContext(mode="local"),
                data={"matched_selector": "#home", "matched": "selector_b"},
                timing_ms={"total": 5},
            ),
            mcp_browser.make_result(
                "skyvern_wait_for_either_state",
                ok=False,
                browser_context=BrowserContext(mode="local"),
                error=mcp_browser.make_error(mcp_browser.ErrorCode.TIMEOUT, "nope", "hint"),
            ),
        ):
            jsonschema.validate(envelope, mcp_browser.EITHER_STATE_OUTPUT_SCHEMA)
    finally:
        set_concise_responses(False)


@pytest.mark.asyncio
async def test_password_probe_waits_as_long_as_the_write_it_guards() -> None:
    """A probe bounded tighter than the write answers 'not a password' for a field the write
    then waits for and types into, so the bound must be the write's own timeout."""
    probe = make_probe_locator(is_password=True)
    scope = MagicMock()
    scope.locator = MagicMock(return_value=probe)

    assert await browser_ops.selector_targets_password([scope], "#pw", timeout=30_000) is True
    assert probe.evaluate.await_args.kwargs["timeout"] == 30_000


@pytest.mark.asyncio
async def test_native_option_probe_refuses_a_frame_owned_by_another_page() -> None:
    """Pins the ownership read inside the probe itself.

    skyvern_click refuses a stale selection further down as well, so deleting this probe's read
    leaves every tool-level row green; only a direct call proves the probe still carries it.
    """
    raw = make_mock_page()
    page = StaleScopePage(raw)

    with pytest.raises(StaleFrameSelectionError):
        await browser_ops.select_native_option_if_targeted(page, "#ship option[value='ground']", timeout=1000)

    raw.locator.assert_not_called()


@pytest.mark.asyncio
async def test_write_grid_refuses_a_frame_owned_by_another_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """focus_xy dispatches page-space CDP input, so it must refuse an unowned selection too."""
    raw, _cdp = _write_grid_page(focused_is_password=False)
    page = StaleScopePage(raw)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_cdp_input, "get_page", AsyncMock(return_value=(page, context)))

    result = await mcp_cdp_input.skyvern_write_grid(rows=[["hello"]], focus_xy=[10.0, 20.0], screenshot=False)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.STALE_FRAME_SELECTION
    raw.page.context.new_cdp_session.assert_not_called()
