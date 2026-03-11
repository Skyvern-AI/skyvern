"""Tests for copilot session injection and output contract adapters."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core import session_manager
from skyvern.cli.core.result import BrowserContext as MCPBrowserContext
from skyvern.cli.core.session_manager import SessionState, scoped_session
from skyvern.forge.sdk.copilot.runtime import AgentContext, mcp_to_copilot


@pytest.fixture(autouse=True)
def _reset_session_state() -> None:
    session_manager._current_session.set(None)
    session_manager._global_session = None
    session_manager.set_stateless_http_mode(False)


def _make_stream() -> MagicMock:
    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    return stream


def _make_ctx(**overrides) -> AgentContext:
    defaults = dict(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id="pbs_test_123",
        stream=_make_stream(),
        api_key="sk-test-key",
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


@pytest.mark.asyncio
async def test_scoped_session_pushes_and_restores() -> None:
    """scoped_session sets the ContextVar within scope and restores on exit."""
    # ContextVar starts as None (from fixture reset)
    assert session_manager._current_session.get() is None

    injected = SessionState(
        browser=MagicMock(),
        context=MCPBrowserContext(mode="cloud_session", session_id="pbs_injected"),
    )

    async with scoped_session(injected):
        inside = session_manager.get_current_session()
        assert inside is injected
        assert inside.context.session_id == "pbs_injected"

    after = session_manager._current_session.get()
    # Should be restored to None (the value before scoped_session set it)
    assert after is None


@pytest.mark.asyncio
async def test_scoped_session_does_not_touch_global() -> None:
    """scoped_session must NOT mutate _global_session."""
    session_manager._global_session = None

    injected = SessionState(
        browser=MagicMock(),
        context=MCPBrowserContext(mode="cloud_session", session_id="pbs_test"),
    )

    async with scoped_session(injected):
        pass

    assert session_manager._global_session is None


@pytest.mark.asyncio
async def test_scoped_session_concurrent_isolation() -> None:
    """Two concurrent scoped_session calls don't interfere with each other."""
    results: dict[str, str | None] = {}

    async def worker(session_id: str, delay: float) -> None:
        state = SessionState(
            browser=MagicMock(),
            context=MCPBrowserContext(mode="cloud_session", session_id=session_id),
        )
        async with scoped_session(state):
            await asyncio.sleep(delay)
            current = session_manager.get_current_session()
            results[session_id] = current.context.session_id if current.context else None

    await asyncio.gather(
        worker("pbs_a", 0.05),
        worker("pbs_b", 0.01),
    )

    assert results["pbs_a"] == "pbs_a"
    assert results["pbs_b"] == "pbs_b"
    assert session_manager._global_session is None


def test_mcp_to_copilot_success() -> None:
    mcp_result = {
        "ok": True,
        "action": "skyvern_navigate",
        "browser_context": {"mode": "cloud_session", "session_id": "pbs_1"},
        "data": {"url": "https://example.com", "title": "Example"},
        "timing_ms": {"total": 500},
        "artifacts": [],
    }
    result = mcp_to_copilot(mcp_result)
    assert result["ok"] is True
    assert result["data"]["url"] == "https://example.com"
    assert "action" not in result
    assert "browser_context" not in result
    assert "timing_ms" not in result
    assert "artifacts" not in result


def test_mcp_to_copilot_error() -> None:
    mcp_result = {
        "ok": False,
        "error": {"code": "NO_ACTIVE_BROWSER", "message": "No browser", "hint": "Create one"},
    }
    result = mcp_to_copilot(mcp_result)
    assert result["ok"] is False
    assert "No browser" in result["error"]
    assert "Create one" in result["error"]


class TestScreenshotAdapter:
    @pytest.mark.asyncio
    async def test_screenshot_adapts_inline_result(self) -> None:
        mcp_result = {
            "ok": True,
            "action": "skyvern_screenshot",
            "browser_context": {"mode": "cloud_session", "session_id": "pbs_1"},
            "data": {
                "inline": True,
                "data": "iVBOR...",
                "mime": "image/png",
                "bytes": 1234,
                "sdk_equivalent": "await page.screenshot()",
            },
            "timing_ms": {"total": 100},
        }
        result = mcp_to_copilot(mcp_result)
        data = result["data"]
        assert "data" in data or "inline" in data


class TestNavigateAdapter:
    def test_navigate_copilot_contract(self) -> None:
        mcp_result = {
            "ok": True,
            "action": "skyvern_navigate",
            "data": {"url": "https://example.com", "title": "Example", "sdk_equivalent": "..."},
            "browser_context": {"mode": "cloud_session"},
        }
        result = mcp_to_copilot(mcp_result)
        if result.get("ok"):
            data = result.pop("data", {})
            result["url"] = data.get("url", "")
            result["next_step"] = "Page loaded."

        assert result["ok"] is True
        assert result["url"] == "https://example.com"
        assert "next_step" in result
        assert "data" not in result


class TestClickAdapter:
    def test_click_copilot_contract(self) -> None:
        mcp_result = {
            "ok": True,
            "action": "skyvern_click",
            "data": {
                "selector": "#btn",
                "intent": None,
                "ai_mode": False,
                "sdk_equivalent": "...",
            },
            "browser_context": {"mode": "cloud_session", "url": "https://ex.com", "title": "Page"},
        }
        result = mcp_to_copilot(mcp_result)
        if result.get("ok") and result.get("data"):
            data = result["data"]
            adapted = {"selector": data.get("selector", "")}
            browser_ctx = mcp_result.get("browser_context", {})
            adapted["url"] = browser_ctx.get("url", "")
            adapted["title"] = browser_ctx.get("title", "")
            result["data"] = adapted

        assert result["data"]["selector"] == "#btn"
        assert result["data"]["url"] == "https://ex.com"
        assert result["data"]["title"] == "Page"


class TestTypeTextAdapter:
    def test_type_text_copilot_contract(self) -> None:
        mcp_result = {
            "ok": True,
            "action": "skyvern_type",
            "data": {
                "selector": "#email",
                "intent": None,
                "ai_mode": False,
                "text_length": 15,
                "sdk_equivalent": "...",
            },
            "browser_context": {"mode": "cloud_session", "url": "https://ex.com"},
        }
        result = mcp_to_copilot(mcp_result)
        if result.get("ok") and result.get("data"):
            data = result["data"]
            adapted = {
                "selector": data.get("selector", ""),
                "typed_length": data.get("text_length", 0),
            }
            browser_ctx = mcp_result.get("browser_context", {})
            adapted["url"] = browser_ctx.get("url", "")
            result["data"] = adapted

        assert result["data"]["selector"] == "#email"
        assert result["data"]["typed_length"] == 15
        assert result["data"]["url"] == "https://ex.com"


class TestEvaluateAdapter:
    def test_evaluate_copilot_contract(self) -> None:
        mcp_result = {
            "ok": True,
            "action": "skyvern_evaluate",
            "data": {
                "result": {"title": "Test"},
                "sdk_equivalent": "await page.evaluate(...)",
            },
            "browser_context": {"mode": "cloud_session"},
        }
        result = mcp_to_copilot(mcp_result)
        assert result["data"]["result"] == {"title": "Test"}


class TestSanitization:
    def test_screenshot_sanitization(self) -> None:
        from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm

        result = {
            "ok": True,
            "data": {
                "screenshot_base64": "iVBOR...",
                "url": "https://example.com",
                "title": "Example",
            },
        }
        sanitized = sanitize_tool_result_for_llm("get_browser_screenshot", result)
        assert sanitized["data"]["screenshot_base64"] == "[base64 image omitted — screenshot was taken successfully]"
        assert sanitized["data"]["url"] == "https://example.com"

    def test_mcp_fields_stripped(self) -> None:
        from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm

        result = {
            "ok": True,
            "action": "skyvern_navigate",
            "browser_context": {"mode": "cloud_session"},
            "timing_ms": {"total": 500},
            "artifacts": [],
            "data": {
                "url": "https://example.com",
                "sdk_equivalent": "await page.goto(...)",
            },
        }
        sanitized = sanitize_tool_result_for_llm("navigate_browser", result)
        assert "action" not in sanitized
        assert "browser_context" not in sanitized
        assert "timing_ms" not in sanitized
        assert "artifacts" not in sanitized
        assert "sdk_equivalent" not in sanitized.get("data", {})

    def test_summarize_type_text_accepts_both_field_names(self) -> None:
        from skyvern.forge.sdk.copilot.output_utils import summarize_tool_result

        # Copilot field name
        result1 = {"ok": True, "data": {"selector": "#email", "typed_length": 10}}
        assert "10" in summarize_tool_result("type_text", result1)

        # MCP field name
        result2 = {"ok": True, "data": {"selector": "#email", "text_length": 20}}
        assert "20" in summarize_tool_result("type_text", result2)

    def test_summarize_navigate_reads_top_level_url(self) -> None:
        from skyvern.forge.sdk.copilot.output_utils import summarize_tool_result

        # Copilot puts url at top level, not in data
        result = {"ok": True, "url": "https://example.com", "data": {}}
        summary = summarize_tool_result("navigate_browser", result)
        assert "example.com" in summary


class TestUpdateWorkflowDirect:
    @pytest.mark.asyncio
    async def test_calls_internal_with_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """update_workflow uses direct path even when api_key is set."""
        from skyvern.forge.sdk.copilot.tools import _update_workflow

        ctx = _make_ctx(api_key="sk-test-key", workflow_permanent_id="wpid_abc123")

        mock_workflow = MagicMock()
        mock_workflow.title = "Test"
        mock_workflow.description = ""
        mock_workflow.workflow_definition = MagicMock()
        mock_workflow.workflow_definition.blocks = [MagicMock(), MagicMock()]

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.process_workflow_yaml",
            lambda **kwargs: mock_workflow,
        )

        mock_wf_service = MagicMock()
        mock_wf_service.update_workflow_definition = AsyncMock()
        monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.app.WORKFLOW_SERVICE", mock_wf_service)

        yaml_str = "title: Test\nworkflow_definition:\n  blocks: []"
        result = await _update_workflow({"workflow_yaml": yaml_str}, ctx)

        assert result["ok"] is True
        assert result["data"]["block_count"] == 2
        assert result["_workflow"] is mock_workflow
        assert ctx.workflow_yaml == yaml_str
        mock_wf_service.update_workflow_definition.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_calls_internal_without_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """update_workflow uses direct path when api_key is None."""
        from skyvern.forge.sdk.copilot.tools import _update_workflow

        ctx = _make_ctx(api_key=None)

        mock_workflow = MagicMock()
        mock_workflow.title = "Test"
        mock_workflow.description = ""
        mock_workflow.workflow_definition = MagicMock()
        mock_workflow.workflow_definition.blocks = []

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.process_workflow_yaml",
            lambda **kwargs: mock_workflow,
        )

        mock_wf_service = MagicMock()
        mock_wf_service.update_workflow_definition = AsyncMock()
        monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.app.WORKFLOW_SERVICE", mock_wf_service)

        result = await _update_workflow({"workflow_yaml": "title: Test"}, ctx)

        assert result["ok"] is True
        mock_wf_service.update_workflow_definition.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_yaml_parse_error_returns_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """YAML parse errors return ok: false."""
        import yaml as _yaml

        from skyvern.forge.sdk.copilot.tools import _update_workflow

        ctx = _make_ctx(api_key="sk-test-key")

        def raise_yaml_error(**kwargs: Any) -> None:
            raise _yaml.YAMLError("bad yaml")

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.process_workflow_yaml",
            raise_yaml_error,
        )

        result = await _update_workflow({"workflow_yaml": "bad: yaml: {"}, ctx)

        assert result["ok"] is False
        assert "Workflow validation failed" in result["error"]

    @pytest.mark.asyncio
    async def test_ctx_workflow_yaml_not_updated_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ctx.workflow_yaml should NOT be updated when processing fails."""
        from pydantic import ValidationError as _ValidationError

        from skyvern.forge.sdk.copilot.tools import _update_workflow

        ctx = _make_ctx(
            api_key="sk-test-key",
            workflow_yaml="original yaml",
        )

        def raise_validation_error(**kwargs: Any) -> None:
            raise _ValidationError.from_exception_data(
                title="WorkflowCreateYAMLRequest",
                line_errors=[],
            )

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.process_workflow_yaml",
            raise_validation_error,
        )

        await _update_workflow({"workflow_yaml": "new broken yaml"}, ctx)

        assert ctx.workflow_yaml == "original yaml"


class TestWorkflowUpdatePersistence:
    def test_record_marks_persisted_even_when_workflow_has_zero_blocks(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _record_workflow_update_result

        ctx = MagicMock()
        ctx.workflow_yaml = "title: Empty workflow"
        ctx.workflow_persisted = False

        workflow = MagicMock()
        workflow.workflow_definition = MagicMock()
        workflow.workflow_definition.blocks = []

        _record_workflow_update_result(
            ctx,
            {
                "ok": True,
                "_workflow": workflow,
                "data": {"block_count": 0},
            },
        )

        assert ctx.last_workflow is workflow
        assert ctx.last_workflow_yaml == "title: Empty workflow"
        assert ctx.workflow_persisted is True
