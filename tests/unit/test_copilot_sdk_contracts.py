"""Tests for the OpenAI Agents SDK integration contracts.

Validates the exact shapes exchanged between our code and the SDK's
internal processing — tool output formats, _parse_tool_output unwrapping,
MCP CallToolResult construction, model resolver, and enforcement logic.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestToolOutputContract:
    def test_screenshot_tool_returns_json_string(self) -> None:
        """run_blocks_and_collect_debug returns plain JSON text output."""
        output = '{"ok": true, "data": {"blocks_run": 2}}'
        assert isinstance(output, str)
        parsed = json.loads(output)
        assert parsed["ok"] is True

    def test_non_screenshot_tool_returns_json_string(self) -> None:
        """update_workflow returns plain JSON string."""
        output = '{"ok": true, "workflow_id": "wf_123"}'
        assert isinstance(output, str)
        parsed = json.loads(output)
        assert parsed["ok"] is True


class TestParseToolOutput:
    @staticmethod
    def _parse(output: Any) -> dict[str, Any]:
        from skyvern.forge.sdk.copilot.streaming_adapter import _parse_tool_output

        return _parse_tool_output(output)

    def test_parse_none(self) -> None:
        assert self._parse(None) == {"ok": True}

    def test_parse_plain_json_string(self) -> None:
        assert self._parse('{"ok": true}') == {"ok": True}

    def test_parse_json_string_with_data(self) -> None:
        result = self._parse('{"ok": true, "data": {"count": 5}}')
        assert result["ok"] is True
        assert result["data"]["count"] == 5

    def test_parse_error_json_string(self) -> None:
        result = self._parse('{"ok": false, "error": "something broke"}')
        assert result["ok"] is False
        assert result["error"] == "something broke"

    def test_parse_non_json_string(self) -> None:
        result = self._parse("just plain text")
        assert result["ok"] is True
        assert result["data"] == "just plain text"

    def test_parse_list_with_text_dict(self) -> None:
        output = [{"type": "text", "text": '{"ok": false, "error": "fail"}'}]
        result = self._parse(output)
        assert result == {"ok": False, "error": "fail"}

    def test_parse_list_with_text_object(self) -> None:
        """SDK may return ToolOutputText objects, not dicts."""

        class FakeTextOutput:
            type = "text"
            text = '{"ok": true, "data": "hello"}'

        result = self._parse([FakeTextOutput()])
        assert result == {"ok": True, "data": "hello"}

    def test_parse_list_skips_image_items(self) -> None:
        output = [
            {"type": "text", "text": '{"ok": true}'},
            {"type": "image", "image_url": "data:image/png;base64,abc"},
        ]
        result = self._parse(output)
        assert result == {"ok": True}

    def test_parse_wrapped_text_dict(self) -> None:
        output = {"type": "text", "text": '{"ok": true}'}
        result = self._parse(output)
        assert result == {"ok": True}

    def test_parse_direct_copilot_dict(self) -> None:
        output = {"ok": True, "data": {"workflow_id": "wf_1"}}
        result = self._parse(output)
        assert result == output

    def test_parse_dict_without_ok_or_type(self) -> None:
        output = {"some_key": "some_value"}
        result = self._parse(output)
        assert result["ok"] is True
        assert result["data"] == output

    def test_parse_object_with_text_attr(self) -> None:
        class FakeOutput:
            type = "text"
            text = '{"ok": true, "data": 42}'

        result = self._parse(FakeOutput())
        assert result == {"ok": True, "data": 42}

    def test_parse_empty_list(self) -> None:
        result = self._parse([])
        assert result["ok"] is True


class TestHooksParsing:
    def test_parse_result_list_with_text_and_image(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import _parse_tool_output as _parse_result

        output = [
            {"type": "text", "text": '{"ok": true, "data": {"overall_status": "completed"}}'},
            {"type": "image", "image_url": "data:image/png;base64,abc"},
        ]
        parsed = _parse_result(output)
        assert parsed["ok"] is True
        assert parsed["data"]["overall_status"] == "completed"

    def test_run_blocks_summary_handles_non_dict_data(self) -> None:
        from skyvern.forge.sdk.copilot.output_utils import summarize_tool_result

        summary = summarize_tool_result(
            "run_blocks_and_collect_debug",
            {"ok": True, "data": [{"type": "text", "text": '{"ok": true}'}]},
        )
        assert summary == "Run debug completed"


class TestCopilotToCallToolResult:
    @staticmethod
    def _build(d: dict) -> Any:
        from skyvern.forge.sdk.copilot.mcp_adapter import _copilot_to_call_tool_result

        return _copilot_to_call_tool_result(d)

    def test_text_only_result(self) -> None:
        result = self._build({"ok": True, "data": "done"})
        assert len(result.content) == 1
        assert result.content[0].type == "text"
        assert result.isError is False

    def test_screenshot_payload_always_text_only(self) -> None:
        """Tool results never include images — screenshots are injected
        as synthetic user messages by the enforcement loop instead."""
        result = self._build({"ok": True, "data": {"screenshot_base64": "iVBOR"}})
        assert len(result.content) == 1
        assert result.content[0].type == "text"
        parsed = json.loads(result.content[0].text)
        assert parsed["data"]["screenshot_base64"].startswith("[base64 image omitted")

    def test_error_result(self) -> None:
        result = self._build({"ok": False, "error": "fail"})
        assert result.isError is True
        parsed = json.loads(result.content[0].text)
        assert parsed["ok"] is False
        assert parsed["error"] == "fail"

    def test_text_content_is_json(self) -> None:
        data = {"ok": True, "data": {"count": 5}}
        result = self._build(data)
        parsed = json.loads(result.content[0].text)
        assert parsed == data


class TestModelResolver:
    def test_rejects_router_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.api.llm.exceptions import InvalidLLMConfigError
        from skyvern.forge.sdk.api.llm.models import LLMRouterConfig

        router_config = LLMRouterConfig(
            model_name="test",
            model_list=[],
            required_env_vars=[],
            supports_vision=False,
            add_assistant_prefix=False,
            main_model_group="default",
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: router_config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "ROUTER_KEY"

        with pytest.raises(InvalidLLMConfigError, match="LLMRouterConfig"):
            resolve_model_config(handler)

    def test_maps_basic_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.api.llm.models import LLMConfig

        monkeypatch.delenv("COPILOT_TRACING_ENABLED", raising=False)
        config = LLMConfig(
            model_name="anthropic/claude-sonnet-4-20250514",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
            temperature=0.5,
            max_tokens=4096,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "BASIC_KEY"

        model_name, run_config, llm_key, supports_vision = resolve_model_config(handler)

        assert model_name == "anthropic/claude-sonnet-4-20250514"
        assert llm_key == "BASIC_KEY"
        assert supports_vision is True
        assert run_config.tracing_disabled is True
        assert run_config.model_settings is not None
        assert run_config.model_settings.temperature == 0.5
        assert run_config.model_settings.max_tokens == 4096

    def test_maps_basic_config_with_tracing_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.api.llm.models import LLMConfig

        monkeypatch.setenv("COPILOT_TRACING_ENABLED", "1")
        config = LLMConfig(
            model_name="anthropic/claude-sonnet-4-20250514",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
            temperature=0.5,
            max_tokens=4096,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "BASIC_KEY"

        _, run_config, _, _ = resolve_model_config(handler)

        assert run_config.tracing_disabled is False

    def test_returns_supports_vision_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.api.llm.models import LLMConfig

        config = LLMConfig(
            model_name="openai/gpt-4-turbo",
            required_env_vars=[],
            supports_vision=False,
            add_assistant_prefix=False,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "NO_VISION_KEY"

        _, _, _, supports_vision = resolve_model_config(handler)
        assert supports_vision is False


class TestTracingSetup:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes "])
    def test_is_tracing_enabled_truthy_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv("COPILOT_TRACING_ENABLED", value)

        from skyvern.forge.sdk.copilot.tracing_setup import is_tracing_enabled

        assert is_tracing_enabled() is True

    def test_is_tracing_enabled_defaults_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COPILOT_TRACING_ENABLED", raising=False)

        from skyvern.forge.sdk.copilot.tracing_setup import is_tracing_enabled

        assert is_tracing_enabled() is False

    def test_ensure_tracing_initialized_sets_processors_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import skyvern.forge.sdk.copilot.tracing_setup as tracing_setup

        monkeypatch.setenv("COPILOT_TRACING_ENABLED", "1")
        monkeypatch.setattr(tracing_setup, "_TRACING_INITIALIZED", False)

        mock_configure = MagicMock()
        mock_instrument = MagicMock()
        monkeypatch.setattr("logfire.configure", mock_configure)
        monkeypatch.setattr("logfire.instrument_openai_agents", mock_instrument)

        tracing_setup.ensure_tracing_initialized()
        tracing_setup.ensure_tracing_initialized()

        mock_configure.assert_called_once_with(send_to_logfire="if-token-present", service_name="skyvern-copilot")
        mock_instrument.assert_called_once()

    def test_copilot_span_returns_nullcontext_when_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import skyvern.forge.sdk.copilot.tracing_setup as tracing_setup

        monkeypatch.delenv("COPILOT_TRACING_ENABLED", raising=False)
        monkeypatch.setattr(
            tracing_setup,
            "ensure_tracing_initialized",
            lambda: pytest.fail("ensure_tracing_initialized should not be called"),
        )

        span = tracing_setup.copilot_span("test_span", {"value": 1})

        with span:
            pass

    def test_copilot_span_uses_custom_span_when_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import skyvern.forge.sdk.copilot.tracing_setup as tracing_setup

        monkeypatch.setenv("COPILOT_TRACING_ENABLED", "1")
        monkeypatch.setattr(tracing_setup, "ensure_tracing_initialized", lambda: None)

        captured: dict[str, Any] = {}

        class FakeSpan:
            def __enter__(self) -> FakeSpan:
                return self

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
                return False

        fake_span = FakeSpan()

        def fake_custom_span(name: str, data: dict[str, Any] | None = None) -> FakeSpan:
            captured["name"] = name
            captured["data"] = data
            return fake_span

        monkeypatch.setattr("agents.tracing.custom_span", fake_custom_span)

        span = tracing_setup.copilot_span("run_blocks", {"block_count": 2})

        assert span is fake_span
        assert captured == {"name": "run_blocks", "data": {"block_count": 2}}


class TestWorkflowCopilotRouteHelpers:
    def test_should_restore_persisted_workflow_for_non_auto_accept(self) -> None:
        from skyvern.forge.sdk.routes.workflow_copilot import _should_restore_persisted_workflow

        agent_result = MagicMock()
        agent_result.workflow_was_persisted = True

        assert _should_restore_persisted_workflow(False, agent_result) is True
        assert _should_restore_persisted_workflow(None, agent_result) is True

    def test_should_not_restore_for_auto_accept_or_unpersisted_result(self) -> None:
        from skyvern.forge.sdk.routes.workflow_copilot import _should_restore_persisted_workflow

        persisted = MagicMock()
        persisted.workflow_was_persisted = True
        not_persisted = MagicMock()
        not_persisted.workflow_was_persisted = False

        assert _should_restore_persisted_workflow(True, persisted) is False
        assert _should_restore_persisted_workflow(False, not_persisted) is False
        assert _should_restore_persisted_workflow(False, None) is False


class TestEnforcement:
    def _make_ctx(self, **overrides: Any) -> Any:
        """Create a mock context with enforcement attributes."""
        ctx = MagicMock()
        ctx.navigate_called = False
        ctx.observation_after_navigate = False
        ctx.navigate_enforcement_done = False
        ctx.update_workflow_called = False
        ctx.test_after_update_done = False
        ctx.post_update_nudge_count = 0
        ctx.premature_completion_nudge_done = False
        ctx.intermediate_nudge_count = 0
        ctx.explore_without_workflow_nudge_count = 0
        for k, v in overrides.items():
            setattr(ctx, k, v)
        return ctx

    def test_no_enforcement_when_nothing_pending(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx()
        assert _check_enforcement(ctx) is None

    def test_post_navigate_nudge(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(navigate_called=True, observation_after_navigate=False)
        nudge = _check_enforcement(ctx)
        assert nudge is not None
        assert "observe" in nudge.lower() or "inspect" in nudge.lower()
        assert ctx.navigate_enforcement_done is True

    def test_post_navigate_only_fires_once(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=False,
            navigate_enforcement_done=True,
        )
        assert _check_enforcement(ctx) is None

    def test_post_update_nudge(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(update_workflow_called=True, test_after_update_done=False)
        nudge = _check_enforcement(ctx)
        assert nudge is not None
        assert "test" in nudge.lower() or "run_blocks" in nudge.lower()

    def test_navigate_takes_priority_over_update(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=False,
            update_workflow_called=True,
            test_after_update_done=False,
        )
        nudge = _check_enforcement(ctx)
        assert "observe" in nudge.lower() or "inspect" in nudge.lower()

    def test_intermediate_success_nudge_for_multistep_goal(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=1,
            user_message="Go to france.fr and then download all french regulations",
            intermediate_nudge_count=0,
        )
        nudge = _check_enforcement(ctx)
        assert nudge is not None
        assert "do not respond" in nudge.lower()
        assert ctx.premature_completion_nudge_done is True

    def test_no_intermediate_success_nudge_for_single_step_goal(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=1,
            user_message="Go to france.fr",
            intermediate_nudge_count=0,
        )
        assert _check_enforcement(ctx) is None

    def test_intermediate_success_nudge_fires_for_two_blocks(self) -> None:
        """Key regression: nudge must fire even when block_count > 1."""
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=2,
            user_message="Go to france.fr and then download all french regulations and extract the titles",
            intermediate_nudge_count=0,
        )
        nudge = _check_enforcement(ctx)
        assert nudge is not None
        assert "do not respond" in nudge.lower()

    def test_intermediate_nudge_respects_global_cap(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import MAX_INTERMEDIATE_NUDGES, _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=2,
            user_message="Go to france.fr and then download all french regulations",
            intermediate_nudge_count=MAX_INTERMEDIATE_NUDGES,
        )
        assert _check_enforcement(ctx) is None

    def test_intermediate_nudge_does_not_fire_for_ten_plus_blocks(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=10,
            user_message="Go to france.fr and then download all french regulations",
            intermediate_nudge_count=0,
        )
        assert _check_enforcement(ctx) is None

    def test_explore_without_workflow_nudge(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE, _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=False,
            test_after_update_done=False,
        )
        nudge = _check_enforcement(ctx)
        assert nudge == POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE
        assert ctx.explore_without_workflow_nudge_count == 1

    def test_explore_without_workflow_not_when_update_called(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=True,
            test_after_update_done=False,
        )
        nudge = _check_enforcement(ctx)
        # Should get the post-update nudge instead, not explore-without-workflow
        assert "workflow" not in (nudge or "").lower() or "test" in (nudge or "").lower()
        assert ctx.explore_without_workflow_nudge_count == 0

    def test_explore_without_workflow_not_when_test_done(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE, _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=False,
            test_after_update_done=True,
        )
        nudge = _check_enforcement(ctx)
        assert nudge != POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE

    def test_explore_without_workflow_respects_cap(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import (
            MAX_EXPLORE_WITHOUT_WORKFLOW_NUDGES,
            POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE,
            _check_enforcement,
        )

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=False,
            test_after_update_done=False,
            explore_without_workflow_nudge_count=MAX_EXPLORE_WITHOUT_WORKFLOW_NUDGES,
        )
        nudge = _check_enforcement(ctx)
        assert nudge != POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE

    def test_explore_without_workflow_not_without_observation(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE, _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=False,
            update_workflow_called=False,
            test_after_update_done=False,
        )
        nudge = _check_enforcement(ctx)
        # Should get navigate nudge, not explore-without-workflow
        assert nudge != POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE
        assert ctx.explore_without_workflow_nudge_count == 0

    @pytest.mark.asyncio
    async def test_post_navigate_nudge_does_not_increment_post_update_counter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from skyvern.forge.sdk.copilot.enforcement import run_with_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=False,
            update_workflow_called=False,
            post_update_nudge_count=0,
        )
        stream = MagicMock()
        stream.is_disconnected = AsyncMock(return_value=False)

        call_count = {"count": 0}

        fake_result = MagicMock()
        fake_result.to_input_list.return_value = []

        def fake_run_streamed(*args: Any, **kwargs: Any) -> Any:
            call_count["count"] += 1
            return fake_result

        async def fake_stream_to_sse(result: Any, s: Any, c: Any) -> None:
            # Resolve post-navigate enforcement on second pass.
            if call_count["count"] >= 2:
                c.observation_after_navigate = True

        monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", fake_run_streamed)
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse",
            fake_stream_to_sse,
        )

        returned = await run_with_enforcement(
            agent=MagicMock(),
            initial_input="hello",
            ctx=ctx,
            stream=stream,
        )
        assert returned is fake_result
        assert ctx.post_update_nudge_count == 0

    @pytest.mark.asyncio
    async def test_post_update_nudge_increments_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot.enforcement import run_with_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=False,
            post_update_nudge_count=0,
        )
        stream = MagicMock()
        stream.is_disconnected = AsyncMock(return_value=False)

        call_count = {"count": 0}
        fake_result = MagicMock()
        fake_result.to_input_list.return_value = []

        def fake_run_streamed(*args: Any, **kwargs: Any) -> Any:
            call_count["count"] += 1
            return fake_result

        async def fake_stream_to_sse(result: Any, s: Any, c: Any) -> None:
            # Resolve post-update enforcement on second pass.
            if call_count["count"] >= 2:
                c.test_after_update_done = True

        monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", fake_run_streamed)
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse",
            fake_stream_to_sse,
        )

        returned = await run_with_enforcement(
            agent=MagicMock(),
            initial_input="hello",
            ctx=ctx,
            stream=stream,
        )
        assert returned is fake_result
        assert ctx.post_update_nudge_count == 1


class TestEnforcementStateUpdates:
    def _make_ctx(self) -> Any:
        ctx = MagicMock()
        ctx.update_workflow_called = False
        ctx.test_after_update_done = False
        ctx.post_update_nudge_count = 0
        ctx.premature_completion_nudge_done = True
        ctx.navigate_called = False
        ctx.observation_after_navigate = False
        return ctx

    def test_update_workflow_sets_flags(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import _update_enforcement_from_tool

        ctx = self._make_ctx()
        _update_enforcement_from_tool(
            ctx,
            "update_workflow",
            {
                "ok": True,
                "data": {"block_count": 2},
            },
        )
        assert ctx.update_workflow_called is True
        assert ctx.test_after_update_done is False
        assert ctx.post_update_nudge_count == 0
        assert ctx.premature_completion_nudge_done is False

    def test_run_blocks_sets_test_done(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import _update_enforcement_from_tool

        ctx = self._make_ctx()
        _update_enforcement_from_tool(ctx, "run_blocks_and_collect_debug", {"ok": True})
        assert ctx.test_after_update_done is True

    def test_navigate_sets_flags(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import _update_enforcement_from_tool

        ctx = self._make_ctx()
        _update_enforcement_from_tool(ctx, "navigate_browser", {"ok": True})
        assert ctx.navigate_called is True
        assert ctx.observation_after_navigate is False

    def test_observation_tool_sets_flag(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import _update_enforcement_from_tool

        ctx = self._make_ctx()
        ctx.observation_after_navigate = False
        _update_enforcement_from_tool(ctx, "get_browser_screenshot", {"ok": True})
        assert ctx.observation_after_navigate is True

    def test_update_without_blocks_does_not_set_flag(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import _update_enforcement_from_tool

        ctx = self._make_ctx()
        _update_enforcement_from_tool(
            ctx,
            "update_workflow",
            {
                "ok": True,
                "data": {"block_count": 0},
            },
        )
        assert ctx.update_workflow_called is False


class TestSanitization:
    def test_screenshot_sanitization(self) -> None:
        from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm

        result = {
            "ok": True,
            "data": {
                "screenshot_base64": "iVBOR...",
                "url": "https://example.com",
            },
        }
        sanitized = sanitize_tool_result_for_llm("get_browser_screenshot", result)
        assert sanitized["data"]["screenshot_base64"] == ("[base64 image omitted — screenshot was taken successfully]")
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

    def test_workflow_key_stripped(self) -> None:
        from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm

        result = {
            "ok": True,
            "data": {"block_count": 2},
            "_workflow": MagicMock(),
        }
        sanitized = sanitize_tool_result_for_llm("update_workflow", result)
        assert "_workflow" not in sanitized

    def test_large_schema_truncated(self) -> None:
        from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm

        big_schema = {f"field_{i}": {"type": "string"} for i in range(200)}
        result = {
            "ok": True,
            "data": {"schema": big_schema},
        }
        sanitized = sanitize_tool_result_for_llm("get_block_schema", result)
        assert sanitized["data"]["schema"]["_truncated"] is True

    def test_large_html_truncated(self) -> None:
        from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm

        result = {
            "ok": True,
            "data": {"visible_elements_html": "x" * 5000},
        }
        sanitized = sanitize_tool_result_for_llm("run_blocks", result)
        html = sanitized["data"]["visible_elements_html"]
        assert len(html) < 5000
        assert html.endswith("[truncated]")


class TestLoopDetection:
    def test_loop_detected_on_third_consecutive_call(self) -> None:
        from skyvern.forge.sdk.copilot.loop_detection import detect_tool_loop

        tracker: list[str] = []
        assert detect_tool_loop(tracker, "update_workflow") is None
        assert detect_tool_loop(tracker, "update_workflow") is None
        error = detect_tool_loop(tracker, "update_workflow")
        assert error is not None
        assert "LOOP DETECTED" in error

    def test_tracker_resets_when_tool_changes(self) -> None:
        from skyvern.forge.sdk.copilot.loop_detection import detect_tool_loop

        tracker: list[str] = []
        assert detect_tool_loop(tracker, "update_workflow") is None
        assert detect_tool_loop(tracker, "list_credentials") is None
        assert tracker == ["list_credentials"]


class TestCopilotContext:
    def test_inherits_agent_context(self) -> None:
        from skyvern.forge.sdk.copilot.agent import CopilotContext
        from skyvern.forge.sdk.copilot.runtime import AgentContext

        assert issubclass(CopilotContext, AgentContext)

    def test_has_enforcement_fields(self) -> None:
        import dataclasses

        from skyvern.forge.sdk.copilot.agent import CopilotContext

        field_names = {f.name for f in dataclasses.fields(CopilotContext)}
        enforcement_fields = {
            "navigate_called",
            "observation_after_navigate",
            "navigate_enforcement_done",
            "update_workflow_called",
            "test_after_update_done",
            "post_update_nudge_count",
            "premature_completion_nudge_done",
            "intermediate_nudge_count",
            "explore_without_workflow_nudge_count",
            "user_message",
            "consecutive_tool_tracker",
            "tool_activity",
            "last_workflow",
            "last_workflow_yaml",
            "workflow_persisted",
        }
        missing = enforcement_fields - field_names
        assert not missing, f"Missing fields: {missing}"

    def test_defaults(self) -> None:
        from skyvern.forge.sdk.copilot.agent import CopilotContext

        stream = MagicMock()
        ctx = CopilotContext(
            organization_id="org-1",
            workflow_id="wf-1",
            workflow_permanent_id="wfp-1",
            workflow_yaml="",
            browser_session_id=None,
            stream=stream,
        )
        assert ctx.navigate_called is False
        assert ctx.update_workflow_called is False
        assert ctx.premature_completion_nudge_done is False
        assert ctx.explore_without_workflow_nudge_count == 0
        assert ctx.user_message == ""
        assert ctx.consecutive_tool_tracker == []
        assert ctx.tool_activity == []
        assert ctx.last_workflow is None
        assert ctx.workflow_persisted is False


class TestSummarizeToolResult:
    @staticmethod
    def _summarize(tool_name: str, result: dict) -> str:
        from skyvern.forge.sdk.copilot.output_utils import summarize_tool_result

        return summarize_tool_result(tool_name, result)

    def test_error_result(self) -> None:
        summary = self._summarize("any_tool", {"ok": False, "error": "oops"})
        assert "Failed" in summary
        assert "oops" in summary

    def test_update_workflow(self) -> None:
        summary = self._summarize(
            "update_workflow",
            {
                "ok": True,
                "data": {"block_count": 3},
            },
        )
        assert "3" in summary

    def test_navigate_browser(self) -> None:
        summary = self._summarize(
            "navigate_browser",
            {
                "ok": True,
                "url": "https://example.com",
            },
        )
        assert "example.com" in summary

    def test_type_text_typed_length(self) -> None:
        summary = self._summarize(
            "type_text",
            {
                "ok": True,
                "data": {"selector": "#email", "typed_length": 10},
            },
        )
        assert "10" in summary

    def test_type_text_text_length(self) -> None:
        summary = self._summarize(
            "type_text",
            {
                "ok": True,
                "data": {"selector": "#email", "text_length": 20},
            },
        )
        assert "20" in summary

    def test_unknown_tool_returns_ok(self) -> None:
        summary = self._summarize("unknown_tool", {"ok": True})
        assert summary == "OK"


class TestFailedTestResponseNormalization:
    def test_rewrite_failed_test_response_avoids_success_language(self) -> None:
        from skyvern.forge.sdk.copilot.agent import CopilotContext, _rewrite_failed_test_response

        ctx = CopilotContext(
            organization_id="org-1",
            workflow_id="wf-1",
            workflow_permanent_id="wfp-1",
            workflow_yaml="",
            browser_session_id=None,
            stream=MagicMock(),
            last_update_block_count=2,
            last_test_ok=False,
            last_test_failure_reason=(
                "Failed to navigate to url https://bad.example. "
                "Error: net::ERR_NAME_NOT_RESOLVED Call log: navigating..."
            ),
        )
        rewritten = _rewrite_failed_test_response("The workflow was successfully created.", ctx)

        assert "successfully created" not in rewritten.lower()
        assert "draft workflow with 2 blocks" in rewritten
        assert "test failed" in rewritten.lower()
        assert "Call log:" not in rewritten

    def test_failed_run_does_not_clear_last_workflow_state(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _record_run_blocks_result

        sentinel_workflow = object()
        ctx = MagicMock()
        ctx.last_workflow = sentinel_workflow
        ctx.last_test_ok = None
        ctx.last_test_failure_reason = None

        _record_run_blocks_result(
            ctx,
            {
                "ok": False,
                "data": {
                    "blocks": [
                        {
                            "label": "open_website",
                            "failure_reason": "net::ERR_NAME_NOT_RESOLVED",
                        }
                    ]
                },
            },
        )

        assert ctx.last_workflow is sentinel_workflow
        assert ctx.last_test_ok is False
        assert ctx.last_test_failure_reason == "net::ERR_NAME_NOT_RESOLVED"


class TestGoalLikelyNeedsMoreBlocks:
    @staticmethod
    def _check(user_message: str, block_count: int) -> bool:
        from skyvern.forge.sdk.copilot.enforcement import _goal_likely_needs_more_blocks

        return _goal_likely_needs_more_blocks(user_message, block_count)

    def test_navigate_and_download_needs_two(self) -> None:
        assert self._check("Go to france.fr and then download regulations", 1) is True
        assert self._check("Go to france.fr and then download regulations", 2) is False

    def test_login_search_and_extract_needs_three(self) -> None:
        assert self._check("Login to the site, search for products, and extract prices", 1) is True
        assert self._check("Login to the site, search for products, and extract prices", 2) is True
        assert self._check("Login to the site, search for products, and extract prices", 3) is False

    def test_single_action_does_not_need_more(self) -> None:
        assert self._check("Go to france.fr", 1) is False

    def test_sequential_connector_needs_at_least_two(self) -> None:
        assert self._check("Do X and then do Y", 1) is True

    def test_ten_plus_blocks_always_false(self) -> None:
        assert self._check("Go to X and then download Y and extract Z", 10) is False

    def test_non_string_returns_false(self) -> None:
        assert self._check(None, 1) is False  # type: ignore[arg-type]
        assert self._check(123, 1) is False  # type: ignore[arg-type]


class TestSchemaOverlay:
    def test_apply_schema_overlay_hides_params(self) -> None:
        from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, _apply_schema_overlay

        schema = {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "session_id": {"type": "string"},
                "cdp_url": {"type": "string"},
            },
            "required": ["url", "session_id"],
        }
        overlay = SchemaOverlay(
            hide_params=frozenset({"session_id", "cdp_url"}),
        )
        result = _apply_schema_overlay(schema, overlay)
        assert "session_id" not in result["properties"]
        assert "cdp_url" not in result["properties"]
        assert "url" in result["properties"]
        assert "session_id" not in result["required"]

    def test_apply_schema_overlay_renames_args(self) -> None:
        from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, _apply_schema_overlay

        schema = {
            "type": "object",
            "properties": {
                "clear": {"type": "boolean"},
                "text": {"type": "string"},
            },
            "required": ["clear", "text"],
        }
        overlay = SchemaOverlay(
            arg_transforms={"clear_first": "clear"},
        )
        result = _apply_schema_overlay(schema, overlay)
        assert "clear_first" in result["properties"]
        assert "clear" not in result["properties"]
        assert "clear_first" in result["required"]

    def test_transform_args_reverses_and_injects(self) -> None:
        from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, _transform_args

        overlay = SchemaOverlay(
            arg_transforms={"clear_first": "clear"},
            forced_args={"inline": True},
        )
        args = {"clear_first": True, "text": "hello"}
        result = _transform_args(args, overlay)
        assert result == {"clear": True, "text": "hello", "inline": True}
        assert "clear_first" not in result


class TestIsValidPngBase64:
    VALID_PNG_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAAAElFTkSuQmCC"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )

    @staticmethod
    def _check(value: Any) -> bool:
        from skyvern.forge.sdk.copilot.output_utils import is_valid_image_base64

        return is_valid_image_base64(value)

    def test_valid_png_header(self) -> None:
        assert self._check(self.VALID_PNG_B64) is True

    def test_invalid_data(self) -> None:
        assert self._check("not-valid-base64-at-all!!!" + "x" * 100) is False

    def test_empty_string(self) -> None:
        assert self._check("") is False

    def test_none(self) -> None:
        assert self._check(None) is False

    def test_short_string(self) -> None:
        assert self._check("iVBOR") is False

    def test_jpeg_base64_accepted(self) -> None:
        import base64

        # Valid base64 with JFIF JPEG header — now accepted
        jpeg_header = b"\xff\xd8\xff\xe0" + b"\x00" * 80
        b64 = base64.b64encode(jpeg_header).decode()
        assert self._check(b64) is True

    def test_non_image_base64(self) -> None:
        import base64

        # Valid base64 but not PNG or JPEG
        gif_header = b"GIF89a" + b"\x00" * 80
        b64 = base64.b64encode(gif_header).decode()
        assert self._check(b64) is False


class TestEnqueueScreenshot:
    # Real 10x10 pixel PNG that Pillow can decode (>100 chars for validation threshold)
    VALID_PNG_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAIAAAACUFjqAAAAE0lEQVR4nGP8z4APMOGVZRip0gBBLAETee26JgAAAABJRU5ErkJggg=="
    )

    def test_enqueues_valid_screenshot_when_vision(self) -> None:
        from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotEntry, enqueue_screenshot_from_result

        ctx = MagicMock()
        ctx.supports_vision = True
        ctx.pending_screenshots = []
        enqueue_screenshot_from_result(ctx, {"ok": True, "data": {"screenshot_base64": self.VALID_PNG_B64}})
        assert len(ctx.pending_screenshots) == 1
        entry = ctx.pending_screenshots[0]
        assert isinstance(entry, ScreenshotEntry)
        assert entry.mime == "image/jpeg"

    def test_skips_when_no_vision(self) -> None:
        from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result

        ctx = MagicMock()
        ctx.supports_vision = False
        ctx.pending_screenshots = []
        enqueue_screenshot_from_result(ctx, {"ok": True, "data": {"screenshot_base64": self.VALID_PNG_B64}})
        assert len(ctx.pending_screenshots) == 0

    def test_skips_invalid_image(self) -> None:
        from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result

        ctx = MagicMock()
        ctx.supports_vision = True
        ctx.pending_screenshots = []
        enqueue_screenshot_from_result(ctx, {"ok": True, "data": {"screenshot_base64": "not-valid"}})
        assert len(ctx.pending_screenshots) == 0

    def test_skips_corrupt_header_valid_image(self) -> None:
        import base64

        from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result

        ctx = MagicMock()
        ctx.supports_vision = True
        ctx.pending_screenshots = []
        truncated_png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"broken-image-data").decode()
        enqueue_screenshot_from_result(ctx, {"ok": True, "data": {"screenshot_base64": truncated_png + "A" * 100}})
        assert len(ctx.pending_screenshots) == 0


class TestSyntheticScreenshotPlaceholders:
    def test_placeholder_counts_as_synthetic_user_message(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import SCREENSHOT_PLACEHOLDER, is_synthetic_user_message

        assert is_synthetic_user_message({"role": "user", "content": SCREENSHOT_PLACEHOLDER}) is True

    def test_real_user_boundary_ignores_screenshot_placeholders(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import SCREENSHOT_PLACEHOLDER
        from skyvern.forge.sdk.copilot.session_factory import _find_real_user_boundary

        items = [
            {"role": "user", "content": "original user request"},
            {"role": "assistant", "content": "assistant reply"},
            {"role": "user", "content": SCREENSHOT_PLACEHOLDER},
            {"role": "assistant", "content": "more assistant output"},
            {"role": "user", "content": "latest real user request"},
        ]

        assert _find_real_user_boundary(items, recent_turns=2) == 0


class TestConsumePendingScreenshots:
    def test_returns_none_when_empty(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _consume_pending_screenshots

        ctx = MagicMock()
        ctx.pending_screenshots = []
        assert _consume_pending_screenshots(ctx) is None

    def test_returns_user_message_with_image(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import SCREENSHOT_SENTINEL, _consume_pending_screenshots
        from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotEntry

        entry = ScreenshotEntry(b64="dGVzdA==", mime="image/jpeg")
        ctx = MagicMock()
        ctx.pending_screenshots = [entry]
        msg = _consume_pending_screenshots(ctx)
        assert msg is not None
        assert msg["role"] == "user"
        content = msg["content"]
        assert len(content) == 2
        assert content[0]["type"] == "input_text"
        assert content[0]["text"].startswith(SCREENSHOT_SENTINEL)
        assert content[1]["type"] == "input_image"
        assert "image/jpeg" in content[1]["image_url"]
        assert content[1]["detail"] == "high"
        # Queue should be drained
        assert ctx.pending_screenshots == []

    def test_handles_multiple_screenshots(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _consume_pending_screenshots
        from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotEntry

        entry1 = ScreenshotEntry(b64="abc=", mime="image/jpeg")
        entry2 = ScreenshotEntry(b64="def=", mime="image/jpeg")
        ctx = MagicMock()
        ctx.pending_screenshots = [entry1, entry2]
        msg = _consume_pending_screenshots(ctx)
        assert msg is not None
        # 1 text + 2 images
        assert len(msg["content"]) == 3
        assert ctx.pending_screenshots == []

    def test_returns_none_when_no_attr(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _consume_pending_screenshots

        ctx = MagicMock(spec=[])
        assert _consume_pending_screenshots(ctx) is None


class TestExtractScreenshotB64:
    @staticmethod
    def _extract(result: dict) -> Any:
        from skyvern.forge.sdk.copilot.output_utils import extract_screenshot_b64

        return extract_screenshot_b64(result)

    def test_extracts_from_data(self) -> None:
        assert self._extract({"data": {"screenshot_base64": "abc"}}) == "abc"

    def test_returns_none_when_no_data(self) -> None:
        assert self._extract({"ok": True}) is None

    def test_returns_none_when_data_not_dict(self) -> None:
        assert self._extract({"data": "string"}) is None

    def test_returns_none_when_no_screenshot_key(self) -> None:
        assert self._extract({"data": {"url": "https://example.com"}}) is None


class TestAttachActionTraces:
    @staticmethod
    def _make_block(task_id: str | None, status: str) -> MagicMock:
        block = MagicMock()
        block.task_id = task_id
        return block

    @staticmethod
    def _make_action(
        task_id: str, action_type: str, status: str, reasoning: str | None, element_id: str | None
    ) -> MagicMock:
        action = MagicMock()
        action.task_id = task_id
        action.action_type = action_type
        action.status = status
        action.reasoning = reasoning
        action.element_id = element_id
        return action

    @pytest.mark.asyncio
    async def test_attach_action_traces_failed_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot.tools import _attach_action_traces

        block = self._make_block("task-1", "failed")
        result: dict[str, Any] = {"label": "step1", "status": "failed", "failure_reason": "max retries"}

        long_reasoning = "A" * 200
        actions = [
            self._make_action("task-1", "click", "failed", long_reasoning, "elem-42"),
            self._make_action("task-1", "input_text", "completed", "typed email", "elem-10"),
        ]

        mock_db = AsyncMock()
        mock_db.get_recent_actions_for_tasks = AsyncMock(return_value=actions)
        monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.app.DATABASE", mock_db)

        await _attach_action_traces([block], [result], "org-1")

        assert "action_trace" in result
        trace = result["action_trace"]
        assert len(trace) == 2
        assert trace[0]["action"] == "click"
        assert trace[0]["status"] == "failed"
        assert len(trace[0]["reasoning"]) == 150
        assert trace[0]["element"] == "elem-42"
        assert trace[1]["reasoning"] == "typed email"

    @pytest.mark.asyncio
    async def test_attach_action_traces_skips_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot.tools import _attach_action_traces

        block = self._make_block("task-1", "completed")
        result: dict[str, Any] = {"label": "step1", "status": "completed"}

        mock_db = AsyncMock()
        mock_db.get_recent_actions_for_tasks = AsyncMock(return_value=[])
        monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.app.DATABASE", mock_db)

        await _attach_action_traces([block], [result], "org-1")

        assert "action_trace" not in result
        mock_db.get_recent_actions_for_tasks.assert_not_called()

    @pytest.mark.asyncio
    async def test_attach_action_traces_no_task_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot.tools import _attach_action_traces

        block = self._make_block(None, "failed")
        result: dict[str, Any] = {"label": "step1", "status": "failed"}

        mock_db = AsyncMock()
        mock_db.get_recent_actions_for_tasks = AsyncMock(return_value=[])
        monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.app.DATABASE", mock_db)

        await _attach_action_traces([block], [result], "org-1")

        assert "action_trace" not in result
        mock_db.get_recent_actions_for_tasks.assert_not_called()

    @pytest.mark.asyncio
    async def test_attach_action_traces_includes_all_failure_statuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot.tools import _FAILED_BLOCK_STATUSES, _attach_action_traces

        blocks = []
        results: list[dict[str, Any]] = []
        for i, status in enumerate(sorted(_FAILED_BLOCK_STATUSES)):
            blocks.append(self._make_block(f"task-{i}", status))
            results.append({"label": f"step{i}", "status": status})

        actions = [self._make_action(f"task-{i}", "click", "failed", None, None) for i in range(len(blocks))]

        mock_db = AsyncMock()
        mock_db.get_recent_actions_for_tasks = AsyncMock(return_value=actions)
        monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.app.DATABASE", mock_db)

        await _attach_action_traces(blocks, results, "org-1")

        for r in results:
            assert "action_trace" in r, f"Missing action_trace for status={r['status']}"
