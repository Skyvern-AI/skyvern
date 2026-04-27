"""Tests for copilot runtime helpers.

Covers `mcp_to_copilot`, the pure dict adapter that normalizes MCP results
into the copilot `{ok, data, error}` envelope, plus the error-sanitization
contract on `ensure_browser_session`. Full coverage of the async context
managers lives in `tests/unit/test_copilot_session_injection.py`
alongside the tools and enforcement helpers they exercise end-to-end.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.copilot.runtime import AgentContext, ensure_browser_session, mcp_browser_context, mcp_to_copilot


def test_mcp_to_copilot_ok_passthrough() -> None:
    result = mcp_to_copilot({"ok": True, "data": {"count": 3}})
    assert result == {"ok": True, "data": {"count": 3}}


def test_mcp_to_copilot_defaults_ok_true_when_missing() -> None:
    result = mcp_to_copilot({"data": "x"})
    assert result["ok"] is True
    assert result["data"] == "x"


def test_mcp_to_copilot_defaults_ok_false_when_error_present_without_ok() -> None:
    # Upstream MCP tool returning an error-shaped dict without an explicit
    # ok field must not produce {"ok": True, "error": "..."}.
    result = mcp_to_copilot({"error": "tool exploded"})
    assert result == {"ok": False, "error": "tool exploded"}


def test_mcp_to_copilot_error_with_hint_joins_message_and_hint() -> None:
    result = mcp_to_copilot({"ok": False, "error": {"code": "E1", "message": "boom", "hint": "retry later"}})
    assert result == {"ok": False, "error": "boom. retry later"}


def test_mcp_to_copilot_error_without_hint_uses_message_only() -> None:
    result = mcp_to_copilot({"ok": False, "error": {"code": "E1", "message": "boom"}})
    assert result == {"ok": False, "error": "boom"}


def test_mcp_to_copilot_error_with_empty_hint_uses_message_only() -> None:
    result = mcp_to_copilot({"ok": False, "error": {"message": "boom", "hint": ""}})
    assert result == {"ok": False, "error": "boom"}


def test_mcp_to_copilot_error_dict_without_message_uses_default() -> None:
    result = mcp_to_copilot({"ok": False, "error": {"code": "E1"}})
    assert result == {"ok": False, "error": "Unknown error"}


def test_mcp_to_copilot_non_dict_error_coerced_with_str() -> None:
    result = mcp_to_copilot({"ok": False, "error": ValueError("boom")})
    assert result == {"ok": False, "error": "boom"}


def test_mcp_to_copilot_string_error_passthrough() -> None:
    result = mcp_to_copilot({"ok": False, "error": "boom"})
    assert result == {"ok": False, "error": "boom"}


def test_mcp_to_copilot_data_none_omitted() -> None:
    result = mcp_to_copilot({"ok": True, "data": None})
    assert result == {"ok": True}


def test_mcp_to_copilot_warnings_passthrough() -> None:
    result = mcp_to_copilot({"ok": True, "warnings": ["slow response"]})
    assert result == {"ok": True, "warnings": ["slow response"]}


def test_mcp_to_copilot_empty_warnings_omitted() -> None:
    result = mcp_to_copilot({"ok": True, "warnings": []})
    assert "warnings" not in result


def _make_ctx(*, api_key: str | None = "test-api-key") -> AgentContext:
    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    return AgentContext(
        organization_id="org_1",
        workflow_id="wf_1",
        workflow_permanent_id="wpid_1",
        workflow_yaml="",
        browser_session_id=None,
        stream=stream,
        api_key=api_key,
    )


@pytest.mark.asyncio
async def test_ensure_browser_session_error_dict_omits_raw_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    # The returned error envelope flows back through the tool/agent path and
    # can surface in LLM-visible or user-visible output. The raw exception
    # may carry internal URLs, file paths, or backend identifiers -- it must
    # stay in the logs, not the return value.
    import skyvern.forge.sdk.copilot.runtime as runtime

    mock_manager = MagicMock()
    mock_manager.create_session = AsyncMock(
        side_effect=RuntimeError("internal: http://persistent-sessions.internal.svc:8080/ failed"),
    )
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    result = await ensure_browser_session(ctx)
    assert result is not None
    assert isinstance(result, dict)
    assert result["ok"] is False
    error_text: Any = result["error"]
    assert isinstance(error_text, str)
    assert "persistent-sessions.internal.svc" not in error_text
    assert "http://" not in error_text
    assert "internal:" not in error_text


@pytest.mark.asyncio
async def test_ensure_browser_session_waits_for_browser_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # DefaultPersistentSessionsManager.create_session returns before chromium
    # has finished booting; ensure_browser_session must poll until
    # browser_context is set so the next mcp_browser_context lookup succeeds.
    import skyvern.forge.sdk.copilot.runtime as runtime

    session = MagicMock()
    session.persistent_browser_session_id = "bs_1"

    pending_state = MagicMock()
    pending_state.browser_context = None
    ready_state = MagicMock()
    ready_state.browser_context = MagicMock()

    mock_manager = MagicMock()
    mock_manager.create_session = AsyncMock(return_value=session)
    mock_manager.get_browser_state = AsyncMock(side_effect=[None, pending_state, ready_state])
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.0)

    ctx = _make_ctx()
    result = await ensure_browser_session(ctx)
    assert result is None
    assert ctx.browser_session_id == "bs_1"
    assert mock_manager.get_browser_state.await_count == 3


@pytest.mark.asyncio
async def test_ensure_browser_session_times_out_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    # If chromium never boots within _BROWSER_BOOT_WAIT_SECONDS, fall into the
    # cleanup branch so the agent does not keep building on a phantom session.
    import skyvern.forge.sdk.copilot.runtime as runtime

    session = MagicMock()
    session.persistent_browser_session_id = "bs_2"

    mock_manager = MagicMock()
    mock_manager.create_session = AsyncMock(return_value=session)
    mock_manager.get_browser_state = AsyncMock(return_value=None)
    mock_manager.close_session = AsyncMock()
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.0)

    ctx = _make_ctx()
    result = await ensure_browser_session(ctx)
    assert result == {"ok": False, "error": "Failed to create browser session"}
    assert ctx.browser_session_id is None
    mock_manager.close_session.assert_awaited_once_with(
        organization_id="org_1",
        browser_session_id="bs_2",
    )


@pytest.mark.asyncio
async def test_mcp_browser_context_rejects_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silently skipping set_api_key_override when ctx.api_key is None would
    let get_active_api_key() fall back to settings.SKYVERN_API_KEY — the
    exact coarse-grained-auth hole the override exists to close. The CM
    must refuse to enter without an api_key, before touching any backend."""
    import skyvern.forge.sdk.copilot.runtime as runtime

    # If the guard is in the right place (pre-backend), we should never see
    # PERSISTENT_SESSIONS_MANAGER touched. Install a tripwire.
    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(
        side_effect=AssertionError("backend accessed before api_key guard"),
    )
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx(api_key=None)
    ctx.browser_session_id = "bs_1"

    with pytest.raises(RuntimeError, match="missing api_key"):
        async with mcp_browser_context(ctx):
            pass

    # Tripwire must not have fired: the backend call should not have happened.
    mock_manager.get_browser_state.assert_not_awaited()
