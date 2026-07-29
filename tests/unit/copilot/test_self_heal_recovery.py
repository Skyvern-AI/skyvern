from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.self_heal_recovery import run_self_heal_recovery
from skyvern.forge.sdk.copilot.tools import _authority_tool_error
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentAuthority, TurnIntentMode
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin


class _FakeCodeBlock:
    def _compose_heal_goal(self, *, workflow_run_context: object, failing_line: int | None) -> str:
        del workflow_run_context, failing_line
        return "Recover the page state"


def _fake_context() -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id="wf_1",
        workflow_permanent_id="wpid_1",
        parameters={},
    )


def _patch_recovery_common(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verified: bool = True,
) -> None:
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.request_policy.build_request_policy",
        AsyncMock(
            return_value=SimpleNamespace(
                completion_criteria=["criterion"],
                graded_completion_criteria=lambda: ["criterion"],
            )
        ),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.outcome_fully_verified",
        lambda _ctx: verified,
    )


@pytest.mark.asyncio
async def test_seed_completion_criteria_false_when_no_gradeable_criteria(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.copilot.self_heal_recovery import _seed_completion_criteria

    # A conservative generator can return a policy with zero gradeable criteria; seeding must
    # report that as unseeded (verification can't pass) rather than a successful seed.
    async def _seed(criteria: list) -> bool:
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.request_policy.build_request_policy",
            AsyncMock(return_value=SimpleNamespace(graded_completion_criteria=lambda: criteria)),
        )
        return await _seed_completion_criteria(
            SimpleNamespace(request_policy=None),
            composed_goal="recover the page",
            organization_id="o_1",
            llm_handler=None,
            copilot_config=None,
            workflow_run_id="wr_1",
            workflow_run_block_id="wrb_1",
        )

    assert await _seed([]) is False
    assert await _seed(["c0"]) is True


@pytest.mark.asyncio
async def test_recovery_uses_browser_only_surface_and_no_native_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _patch_recovery_common(monkeypatch, verified=True)

    async def _fake_loop(**kwargs: object) -> object:
        captured.update(kwargs)
        ctx = kwargs["ctx"]
        assert hasattr(ctx, "scout_trajectory")
        ctx.scout_trajectory.append({"tool_name": "click", "selector": "#ok"})
        return object()

    monkeypatch.setattr("skyvern.forge.sdk.copilot.agent._run_agent_loop_with_surface", _fake_loop)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.output_utils.extract_final_text",
        lambda _result: '{"type":"REPLY"}',
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.llm_config.resolve_main_copilot_handler",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.model_resolver.resolve_model_config",
        lambda *args, **kwargs: ("gpt-test", object(), "llm_key", True),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.self_heal_recovery.app", SimpleNamespace(AGENT_FUNCTION=MagicMock()))
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.app.AGENT_FUNCTION.get_copilot_config",
        lambda: CopilotConfig(),
    )

    result = await run_self_heal_recovery(
        block=_FakeCodeBlock(),
        workflow_run_context=_fake_context(),
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        organization_id="org_1",
        browser_state=object(),
        failing_line=8,
        api_key="sk-test",
        max_actions=15,
        wall_clock_budget_seconds=10,
    )

    assert result.success is True
    assert result.verified is True
    assert captured["native_tools"] == []
    alias_map = cast(dict[str, str], captured["alias_map"])
    assert set(alias_map) == {
        "navigate_browser",
        "get_browser_screenshot",
        "evaluate",
        "click",
        "type_text",
        "scroll",
        "console_messages",
        "select_option",
        "press_key",
    }
    assert "get_block_schema" not in alias_map


@pytest.mark.asyncio
async def test_recovery_passes_non_empty_output_guardrails(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _patch_recovery_common(monkeypatch, verified=True)

    async def _fake_loop(**kwargs: object) -> object:
        captured.update(kwargs)
        ctx = kwargs["ctx"]
        assert hasattr(ctx, "scout_trajectory")
        ctx.scout_trajectory.append({"tool_name": "click", "selector": "#ok"})
        return object()

    monkeypatch.setattr("skyvern.forge.sdk.copilot.agent._run_agent_loop_with_surface", _fake_loop)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.output_utils.extract_final_text",
        lambda _result: '{"type":"REPLY"}',
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.llm_config.resolve_main_copilot_handler",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.model_resolver.resolve_model_config",
        lambda *args, **kwargs: ("gpt-test", object(), "llm_key", True),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.self_heal_recovery.app", SimpleNamespace(AGENT_FUNCTION=MagicMock()))
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.app.AGENT_FUNCTION.get_copilot_config",
        lambda: CopilotConfig(),
    )

    result = await run_self_heal_recovery(
        block=_FakeCodeBlock(),
        workflow_run_context=_fake_context(),
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        organization_id="org_1",
        browser_state=object(),
        failing_line=8,
        api_key="sk-test",
        max_actions=15,
        wall_clock_budget_seconds=10,
    )

    assert result.success is True
    assert result.verified is True
    output_guardrails = captured["output_guardrails"]
    assert isinstance(output_guardrails, list)
    assert len(output_guardrails) == 1
    assert output_guardrails[0].name == "self_heal_output_guardrail"


@pytest.mark.asyncio
async def test_recovery_fails_closed_on_ask_question(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_recovery_common(monkeypatch, verified=True)

    async def _fake_loop(**kwargs: object) -> object:
        ctx = kwargs["ctx"]
        assert hasattr(ctx, "scout_trajectory")
        ctx.scout_trajectory.append({"tool_name": "click", "selector": "#ok"})
        return object()

    monkeypatch.setattr("skyvern.forge.sdk.copilot.agent._run_agent_loop_with_surface", _fake_loop)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.output_utils.extract_final_text",
        lambda _result: '{"type":"ASK_QUESTION","user_response":"What should I do?"}',
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.llm_config.resolve_main_copilot_handler",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.model_resolver.resolve_model_config",
        lambda *args, **kwargs: ("gpt-test", object(), "llm_key", True),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.self_heal_recovery.app", SimpleNamespace(AGENT_FUNCTION=MagicMock()))
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.app.AGENT_FUNCTION.get_copilot_config",
        lambda: CopilotConfig(),
    )

    result = await run_self_heal_recovery(
        block=_FakeCodeBlock(),
        workflow_run_context=_fake_context(),
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        organization_id="org_1",
        browser_state=object(),
        failing_line=8,
        api_key="sk-test",
        max_actions=15,
        wall_clock_budget_seconds=10,
    )

    assert result.success is False
    assert result.failure_note == "asked_user_question"


@pytest.mark.asyncio
async def test_recovery_replace_workflow_terminal_has_distinct_failure_note(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_recovery_common(monkeypatch, verified=True)

    async def _fake_loop(**kwargs: object) -> object:
        ctx = kwargs["ctx"]
        ctx.scout_trajectory.append({"tool_name": "click", "selector": "#ok"})
        return object()

    monkeypatch.setattr("skyvern.forge.sdk.copilot.agent._run_agent_loop_with_surface", _fake_loop)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.output_utils.extract_final_text",
        lambda _result: '{"type":"REPLACE_WORKFLOW","workflow_yaml":"blocks: []"}',
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.llm_config.resolve_main_copilot_handler",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.model_resolver.resolve_model_config",
        lambda *args, **kwargs: ("gpt-test", object(), "llm_key", True),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.self_heal_recovery.app", SimpleNamespace(AGENT_FUNCTION=MagicMock()))
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.app.AGENT_FUNCTION.get_copilot_config",
        lambda: CopilotConfig(),
    )

    result = await run_self_heal_recovery(
        block=_FakeCodeBlock(),
        workflow_run_context=_fake_context(),
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        organization_id="org_1",
        browser_state=object(),
        failing_line=8,
        api_key="sk-test",
        max_actions=15,
        wall_clock_budget_seconds=10,
    )

    assert result.success is False
    assert result.failure_note == "proposed_workflow_mutation"


@pytest.mark.asyncio
async def test_recovery_unparseable_terminal_has_distinct_failure_note(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_recovery_common(monkeypatch, verified=True)

    async def _fake_loop(**kwargs: object) -> object:
        ctx = kwargs["ctx"]
        ctx.scout_trajectory.append({"tool_name": "click", "selector": "#ok"})
        return object()

    monkeypatch.setattr("skyvern.forge.sdk.copilot.agent._run_agent_loop_with_surface", _fake_loop)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.output_utils.extract_final_text", lambda _result: "nonsense")
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.llm_config.resolve_main_copilot_handler",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.model_resolver.resolve_model_config",
        lambda *args, **kwargs: ("gpt-test", object(), "llm_key", True),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.self_heal_recovery.app", SimpleNamespace(AGENT_FUNCTION=MagicMock()))
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.app.AGENT_FUNCTION.get_copilot_config",
        lambda: CopilotConfig(),
    )

    result = await run_self_heal_recovery(
        block=_FakeCodeBlock(),
        workflow_run_context=_fake_context(),
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        organization_id="org_1",
        browser_state=object(),
        failing_line=8,
        api_key="sk-test",
        max_actions=15,
        wall_clock_budget_seconds=10,
    )

    assert result.success is False
    assert result.failure_note == "unparseable_terminal"


@pytest.mark.asyncio
async def test_recovery_marks_terminal_reply_unverified_when_judge_not_satisfied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_recovery_common(monkeypatch, verified=False)

    async def _fake_loop(**kwargs: object) -> object:
        ctx = kwargs["ctx"]
        ctx.scout_trajectory.append({"tool_name": "click", "selector": "#ok"})
        return object()

    monkeypatch.setattr("skyvern.forge.sdk.copilot.agent._run_agent_loop_with_surface", _fake_loop)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.output_utils.extract_final_text",
        lambda _result: '{"type":"REPLY","user_response":"done"}',
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.llm_config.resolve_main_copilot_handler",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.model_resolver.resolve_model_config",
        lambda *args, **kwargs: ("gpt-test", object(), "llm_key", True),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.self_heal_recovery.app", SimpleNamespace(AGENT_FUNCTION=MagicMock()))
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.app.AGENT_FUNCTION.get_copilot_config",
        lambda: CopilotConfig(),
    )

    result = await run_self_heal_recovery(
        block=_FakeCodeBlock(),
        workflow_run_context=_fake_context(),
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        organization_id="org_1",
        browser_state=object(),
        failing_line=8,
        api_key="sk-test",
        max_actions=15,
        wall_clock_budget_seconds=10,
    )

    assert result.success is True
    assert result.verified is False
    assert result.failure_note == "goal_unverified"


@pytest.mark.asyncio
async def test_recovery_navigation_only_counts_as_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_recovery_common(monkeypatch, verified=True)

    async def _fake_loop(**kwargs: object) -> object:
        ctx = kwargs["ctx"]
        ctx.tool_activity.append({"tool": "navigate_browser", "summary": "Navigated to http://example.test"})
        return object()

    monkeypatch.setattr("skyvern.forge.sdk.copilot.agent._run_agent_loop_with_surface", _fake_loop)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.output_utils.extract_final_text",
        lambda _result: '{"type":"REPLY","user_response":"done"}',
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.llm_config.resolve_main_copilot_handler",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.model_resolver.resolve_model_config",
        lambda *args, **kwargs: ("gpt-test", object(), "llm_key", True),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.self_heal_recovery.app", SimpleNamespace(AGENT_FUNCTION=MagicMock()))
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.app.AGENT_FUNCTION.get_copilot_config",
        lambda: CopilotConfig(),
    )

    result = await run_self_heal_recovery(
        block=_FakeCodeBlock(),
        workflow_run_context=_fake_context(),
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        organization_id="org_1",
        browser_state=object(),
        failing_line=8,
        api_key="sk-test",
        max_actions=15,
        wall_clock_budget_seconds=10,
    )

    assert result.success is True
    assert result.verified is True
    assert result.action_count == 1


def test_evaluate_tool_counts_as_self_heal_mutation() -> None:
    from skyvern.forge.sdk.copilot.self_heal_recovery import _performed_mutation_during_self_heal

    # evaluate runs arbitrary JS, so a successful evaluate must count as a mutation for the
    # fail-closed floor-suppression guard; read-only tools and failed calls must not.
    assert _performed_mutation_during_self_heal(
        SimpleNamespace(tool_activity=[{"tool": "evaluate", "summary": "Ran document.forms[0].submit()"}])
    )
    assert not _performed_mutation_during_self_heal(
        SimpleNamespace(tool_activity=[{"tool": "get_browser_screenshot", "summary": "Screenshot taken"}])
    )
    assert not _performed_mutation_during_self_heal(
        SimpleNamespace(tool_activity=[{"tool": "evaluate", "summary": "Failed: page.evaluate timed out"}])
    )


@pytest.mark.asyncio
async def test_recovery_runs_post_loop_verification_from_browser_state_without_evaluate_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_recovery_common(monkeypatch, verified=True)
    completion_verify_mock = AsyncMock(return_value=None)
    record_observation_mock = MagicMock(return_value=7)

    async def _fake_loop(**kwargs: object) -> object:
        ctx = kwargs["ctx"]
        ctx.tool_activity.extend(
            [
                {"tool": "click", "summary": "Clicked #next"},
                {"tool": "type_text", "summary": "Typed into #email"},
            ]
        )
        return object()

    page = SimpleNamespace(
        url="https://example.test/dashboard",
        evaluate=AsyncMock(return_value="<main>done</main>"),
        title=AsyncMock(return_value="Dashboard"),
    )
    browser_state = SimpleNamespace(get_or_create_page=AsyncMock(return_value=page))

    monkeypatch.setattr("skyvern.forge.sdk.copilot.agent._run_agent_loop_with_surface", _fake_loop)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.output_utils.extract_final_text",
        lambda _result: '{"type":"REPLY","user_response":"done"}',
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.llm_config.resolve_main_copilot_handler",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.model_resolver.resolve_model_config",
        lambda *args, **kwargs: ("gpt-test", object(), "llm_key", True),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.self_heal_recovery.app", SimpleNamespace(AGENT_FUNCTION=MagicMock()))
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.app.AGENT_FUNCTION.get_copilot_config",
        lambda: CopilotConfig(),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.page_observation._record_composition_page_observation",
        record_observation_mock,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.completion._maybe_run_completion_verification_from_page_observation",
        completion_verify_mock,
    )

    result = await run_self_heal_recovery(
        block=_FakeCodeBlock(),
        workflow_run_context=_fake_context(),
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        organization_id="org_1",
        browser_state=browser_state,
        failing_line=8,
        api_key="sk-test",
        max_actions=15,
        wall_clock_budget_seconds=10,
    )

    assert result.success is True
    assert result.verified is True
    assert result.performed_mutation is True
    record_observation_mock.assert_called_once()
    completion_verify_mock.assert_awaited_once()
    assert completion_verify_mock.await_args.kwargs["observed_data"] == {
        "html": "<main>done</main>",
        "url": "https://example.test/dashboard",
        "title": "Dashboard",
    }


@pytest.mark.asyncio
async def test_recovery_fails_closed_on_wall_clock_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_recovery_common(monkeypatch, verified=True)

    async def _slow_loop(**kwargs: object) -> object:
        await asyncio.sleep(0.05)
        return object()

    monkeypatch.setattr("skyvern.forge.sdk.copilot.agent._run_agent_loop_with_surface", _slow_loop)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.llm_config.resolve_main_copilot_handler",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.model_resolver.resolve_model_config",
        lambda *args, **kwargs: ("gpt-test", object(), "llm_key", True),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.self_heal_recovery.app", SimpleNamespace(AGENT_FUNCTION=MagicMock()))
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.self_heal_recovery.app.AGENT_FUNCTION.get_copilot_config",
        lambda: CopilotConfig(),
    )

    result = await run_self_heal_recovery(
        block=_FakeCodeBlock(),
        workflow_run_context=_fake_context(),
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        organization_id="org_1",
        browser_state=object(),
        failing_line=8,
        api_key="sk-test",
        max_actions=15,
        wall_clock_budget_seconds=0,
    )

    assert result.success is False
    assert result.verified is False
    assert result.failure_note == "wall_clock_budget_exhausted"


def test_runtime_self_heal_guardrail_rejects_native_tool_call() -> None:
    ctx = CopilotContext(
        organization_id="org_1",
        workflow_id="wf_1",
        workflow_permanent_id="wpid_1",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        turn_origin=TurnOrigin.runtime_self_heal,
        turn_intent=TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=False, may_run_blocks=False),
        ),
    )

    payload = _authority_tool_error(ctx, "update_workflow")

    assert payload is not None
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "runtime_self_heal_native_tool_blocked"


@pytest.mark.asyncio
async def test_runtime_self_heal_inline_replace_is_downgraded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_module,
        "extract_final_text",
        lambda _result: json.dumps({"type": "REPLACE_WORKFLOW", "user_response": "replace", "workflow_yaml": "x"}),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._process_workflow_yaml",
        AsyncMock(side_effect=AssertionError("inline replace must not process workflow in runtime self-heal")),
    )
    ctx = CopilotContext(
        organization_id="org_1",
        workflow_id="wf_1",
        workflow_permanent_id="wpid_1",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        turn_origin=TurnOrigin.runtime_self_heal,
    )
    chat_request = SimpleNamespace(workflow_id="wf_1", workflow_permanent_id="wpid_1", workflow_yaml="")

    agent_result = await agent_module._translate_to_agent_result(
        object(),
        ctx,
        global_llm_context=None,
        chat_request=chat_request,
        organization_id="org_1",
    )

    assert agent_result.response_type == "REPLY"
    assert "runtime self-heal" in agent_result.user_response.lower()


def test_build_copilot_output_guardrails_returns_real_output_guardrail_instances() -> None:
    from agents import GuardrailFunctionOutput, OutputGuardrail

    guardrails = agent_module._build_copilot_output_guardrails(OutputGuardrail, GuardrailFunctionOutput)

    assert isinstance(guardrails, list)
    assert len(guardrails) > 0
    assert all(isinstance(guardrail, OutputGuardrail) for guardrail in guardrails)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("final_text", "expected_tripwire"),
    [
        pytest.param('{"type":"REPLY","user_response":"done"}', False, id="reply_passes"),
        pytest.param(
            '{"type":"REPLACE_WORKFLOW","workflow_yaml":"blocks: []","user_response":"updated"}',
            True,
            id="replace_workflow_trips",
        ),
        pytest.param('{"type":"ASK_QUESTION","user_response":"Need input?"}', True, id="ask_question_trips"),
        pytest.param("garbage output with no marker", False, id="garbage_without_replace_marker_passes"),
        pytest.param("garbage REPLACE_WORKFLOW marker only", True, id="garbage_with_replace_marker_trips"),
    ],
)
async def test_self_heal_output_guardrail_runtime_verdicts(final_text: str, expected_tripwire: bool) -> None:
    from agents import GuardrailFunctionOutput, OutputGuardrail
    from agents.run_context import RunContextWrapper

    guardrails = agent_module._build_self_heal_output_guardrails(OutputGuardrail, GuardrailFunctionOutput)
    fake_result = SimpleNamespace(final_output=final_text, new_items=[])
    result = await guardrails[0].run(
        RunContextWrapper(context=SimpleNamespace()),
        SimpleNamespace(),
        fake_result,
    )

    assert result.output.tripwire_triggered is expected_tripwire
